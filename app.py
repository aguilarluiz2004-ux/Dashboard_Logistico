import os
import secrets
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

app = Flask(__name__)

# No PythonAnywhere o site fica atrás de um proxy: isso faz o Flask enxergar
# o IP real do visitante (request.remote_addr) e o HTTPS corretamente.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# =============================
# Caminhos (sempre absolutos!)
# =============================
# No PythonAnywhere o diretório de trabalho NÃO é a pasta do projeto,
# então todo arquivo precisa ser montado a partir da pasta deste app.py.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CAMINHO_PLANILHA = os.path.join(BASE_DIR, 'base', 'base_operacao.xlsx')
DADOS_OPERACAO = os.path.join(BASE_DIR, 'banco_de_dados', 'operacao.db')
DADOS_USUARIOS = os.path.join(BASE_DIR, 'banco_de_dados', 'usuarios.db')
ARQUIVO_SECRET = os.path.join(BASE_DIR, 'secret_key.txt')
ARQUIVO_TOKEN_UPLOAD = os.path.join(BASE_DIR, 'token_upload.txt')
PASTA_BACKUP = os.path.join(BASE_DIR, 'backups')

# Garante que as pastas existem (o SQLite não cria pasta sozinho)
os.makedirs(os.path.dirname(CAMINHO_PLANILHA), exist_ok=True)
os.makedirs(os.path.dirname(DADOS_OPERACAO), exist_ok=True)

lock_importacao = threading.Lock()


# =============================
# Configuração de segurança / login
# =============================
def carregar_secret_key():
    """A SECRET_KEY assina o cookie de sessão. Usa a variável de ambiente
    SECRET_KEY se existir; senão usa/gera o arquivo secret_key.txt
    (coloque no .gitignore!)."""
    chave = os.environ.get('SECRET_KEY')
    if chave:
        return chave

    if os.path.exists(ARQUIVO_SECRET):
        with open(ARQUIVO_SECRET) as f:
            return f.read().strip()

    chave = secrets.token_hex(32)
    with open(ARQUIVO_SECRET, 'w') as f:
        f.write(chave)
    return chave


def carregar_token_upload():
    """Token usado pelo script do seu PC para enviar a planilha sem login.
    Usa a variável UPLOAD_TOKEN ou gera/lê o arquivo token_upload.txt
    (coloque no .gitignore!). Para ver o token: cat ~/Dashboard/token_upload.txt"""
    token = os.environ.get('UPLOAD_TOKEN')
    if token:
        return token

    if os.path.exists(ARQUIVO_TOKEN_UPLOAD):
        with open(ARQUIVO_TOKEN_UPLOAD) as f:
            return f.read().strip()

    token = secrets.token_urlsafe(32)
    with open(ARQUIVO_TOKEN_UPLOAD, 'w') as f:
        f.write(token)
    return token


app.secret_key = carregar_secret_key()
TOKEN_UPLOAD = carregar_token_upload()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # O PythonAnywhere já serve tudo em HTTPS, então o cookie pode ser "secure".
    # Para testar localmente sem HTTPS, rode com a variável LOCAL=1.
    SESSION_COOKIE_SECURE=os.environ.get('LOCAL') != '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # limite de 50 MB no upload da planilha
)

# Bloqueio simples contra tentativa de adivinhar senha (por IP)
MAX_FALHAS = 5
BLOQUEIO_SEGUNDOS = 300
tentativas = {}  # ip -> (falhas, bloqueado_ate)


def get_conn_usuarios():
    """Usuários ficam em um banco SEPARADO (usuarios.db), pra nunca serem
    afetados pela reimportação da planilha."""
    conn = sqlite3.connect(DADOS_USUARIOS)
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario TEXT PRIMARY KEY,
            senha_hash TEXT NOT NULL,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    return conn


def get_conn():
    conn = sqlite3.connect(DADOS_OPERACAO, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@app.before_request
def exigir_login():
    """Roda ANTES de toda requisição, protegendo páginas e APIs."""
    if request.endpoint in ('login', 'static', 'api_upload_planilha'):
        return None
    if 'usuario' in session:
        return None

    if request.path.startswith('/api/'):
        return jsonify({'erro': 'não autenticado'}), 401
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    erro = None

    if request.method == 'POST':
        ip = request.remote_addr
        falhas, bloqueado_ate = tentativas.get(ip, (0, 0))

        if time.time() < bloqueado_ate:
            erro = 'Muitas tentativas. Aguarde alguns minutos.'
        else:
            usuario = request.form.get('usuario', '').strip().lower()
            senha = request.form.get('senha', '')

            conn = get_conn_usuarios()
            linha = conn.execute(
                'SELECT senha_hash FROM usuarios WHERE usuario = ?', (usuario,)
            ).fetchone()
            conn.close()

            if linha and check_password_hash(linha['senha_hash'], senha):
                tentativas.pop(ip, None)
                session.clear()
                session['usuario'] = usuario
                session.permanent = True
                return redirect(url_for('dashboard_recebimento'))

            falhas += 1
            if falhas >= MAX_FALHAS:
                tentativas[ip] = (0, time.time() + BLOQUEIO_SEGUNDOS)
            else:
                tentativas[ip] = (falhas, 0)
            erro = 'Usuário ou senha inválidos.'

    return render_template('login.html', erro=erro)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# =============================
# Importação para o banco
# =============================
def validar_datas(df, coluna, identificador, nome_tabela):
    """Avisa (no log do servidor) quais linhas têm data inválida."""
    datas_convertidas = pd.to_datetime(df[coluna], errors='coerce')
    invalidas = df[datas_convertidas.isna() & df[coluna].notna()]

    if not invalidas.empty:
        print(f'[AVISO] {nome_tabela}: {len(invalidas)} linha(s) com "{coluna}" inválida:')
        for _, linha in invalidas.iterrows():
            print(f'   {identificador}={linha[identificador]!r}  {coluna}={linha[coluna]!r}')


def importar_tabela(df, nome_tabela, colunas_sql, chave_primaria, conn, cursor):
    """Importa com chave primária: se a chave repetir, a linha nova
    sobrescreve a antiga (INSERT OR REPLACE)."""
    cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS {nome_tabela} (
            {colunas_sql},
            PRIMARY KEY ({chave_primaria})
        )
    ''')
    conn.commit()

    tabela_temp = f'temp_{nome_tabela}'
    df.to_sql(tabela_temp, conn, if_exists='replace', index=False)

    colunas_nomes = ', '.join(df.columns)
    cursor.execute(f'''
        INSERT OR REPLACE INTO {nome_tabela} ({colunas_nomes})
        SELECT {colunas_nomes} FROM {tabela_temp}
    ''')
    conn.commit()

    cursor.execute(f'DROP TABLE {tabela_temp}')
    conn.commit()


def importar_tabela_completa(df, nome_tabela, colunas_sql, conn):
    """Recria a tabela com id automático e insere TODAS as linhas."""
    temp = f'{nome_tabela}_temp'
    df.to_sql(temp, conn, if_exists='replace', index=False)

    cursor = conn.cursor()
    cursor.execute(f'DROP TABLE IF EXISTS {nome_tabela}')
    cursor.execute(f'''
        CREATE TABLE {nome_tabela} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {colunas_sql}
        )
    ''')

    colunas = ', '.join(df.columns)
    cursor.execute(f'''
        INSERT INTO {nome_tabela} ({colunas})
        SELECT {colunas} FROM {temp}
    ''')
    cursor.execute(f'DROP TABLE {temp}')
    conn.commit()


def normalizar_colunas(df):
    """Padroniza os nomes das colunas do Excel."""
    df = df.copy()
    df.columns = (
        df.columns
        .astype(str)
        .str.replace("\n", " ", regex=False)
        .str.strip()
        .str.upper()
        .str.replace(r"\\s+", " ", regex=True)
    )
    return df


def preparar_colunas(df, obrigatorias, nome_aba):
    """Normaliza e valida as colunas de uma aba."""
    df = normalizar_colunas(df)

    print("=" * 70)
    print(f"COLUNAS RECEBIDAS - ABA {nome_aba}")
    print(df.columns.tolist())
    print("=" * 70)

    faltando = [c for c in obrigatorias if c not in df.columns]

    if faltando:
        raise ValueError(
            f"Aba {nome_aba}: colunas obrigatórias ausentes: {faltando}. "
            f"Colunas encontradas: {df.columns.tolist()}"
        )

    return df


def rodar_importacao():
    """Faz backup da planilha e importa as três abas para o SQLite."""
    with lock_importacao:
        os.makedirs(PASTA_BACKUP, exist_ok=True)

        hoje = datetime.now().strftime("%Y-%m-%d")
        caminho_backup = os.path.join(
            PASTA_BACKUP,
            f"base_operacao_{hoje}.xlsx"
        )
        shutil.copy2(CAMINHO_PLANILHA, caminho_backup)

        # =====================================================
        # RECEBIMENTO
        # =====================================================
        df_recebimento = pd.read_excel(
            caminho_backup,
            sheet_name="RECEBIMENTO"
        )

        df_recebimento = preparar_colunas(
            df_recebimento,
            [
                "NOTA FISCAL",
                "QTD.PALLET",
                "DATA",
                "CLIENTE",
                "NUM.PEDIDO",
                "PLACA",
                "NUM.CTE",
                "TIPO DO VEICULO"
            ],
            "RECEBIMENTO"
        )

        df_recebimento = df_recebimento[
            [
                "NOTA FISCAL",
                "QTD.PALLET",
                "DATA",
                "CLIENTE",
                "NUM.PEDIDO",
                "PLACA",
                "NUM.CTE",
                "TIPO DO VEICULO"
            ]
        ].rename(columns={
            "NOTA FISCAL": "nota_fiscal",
            "QTD.PALLET": "pallets",
            "DATA": "data",
            "CLIENTE": "cliente",
            "NUM.PEDIDO": "pedido",
            "PLACA": "placa",
            "NUM.CTE": "cte",
            "TIPO DO VEICULO": "veiculo"
        })

        validar_datas(
            df_recebimento,
            "data",
            "nota_fiscal",
            "recebimento"
        )

        # =====================================================
        # EXPEDIÇÃO
        # =====================================================
        df_expedicao = pd.read_excel(
            caminho_backup,
            sheet_name="EXPEDICAO"
        )

        df_expedicao = preparar_colunas(
            df_expedicao,
            [
                "ROMANEIO",
                "MOTORISTA",
                "PLACA",
                "TRANSPORTADORA",
                "NOTA FISCAL",
                "NUM.PEDIDO",
                "QTD.PALLET",
                "TIPO DO VEICULO",
                "QUANTIDADE DE CAIXAS",
                "DATA",
                "HORA",
                "CLIENTE",
                "DESTINO"
            ],
            "EXPEDICAO"
        )

        df_expedicao = df_expedicao[
            [
                "ROMANEIO",
                "MOTORISTA",
                "PLACA",
                "TRANSPORTADORA",
                "NOTA FISCAL",
                "NUM.PEDIDO",
                "QTD.PALLET",
                "TIPO DO VEICULO",
                "QUANTIDADE DE CAIXAS",
                "DATA",
                "HORA",
                "CLIENTE",
                "DESTINO"
            ]
        ].rename(columns={
            "ROMANEIO": "romaneio",
            "MOTORISTA": "motorista",
            "PLACA": "placa",
            "TRANSPORTADORA": "transportadora",
            "NOTA FISCAL": "nota_fiscal",
            "NUM.PEDIDO": "pedido",
            "QTD.PALLET": "pallets",
            "TIPO DO VEICULO": "veiculo",
            "QUANTIDADE DE CAIXAS": "caixas",
            "DATA": "data",
            "HORA": "hora",
            "CLIENTE": "cliente",
            "DESTINO": "destino"
        })

        validar_datas(
            df_expedicao,
            "data",
            "nota_fiscal",
            "expedicao"
        )

        # =====================================================
        # ETIQUETAS
        # =====================================================
        df_etiquetas = pd.read_excel(
            caminho_backup,
            sheet_name="ETIQUETAS"
        )

        df_etiquetas = preparar_colunas(
            df_etiquetas,
            [
                "NÚMERO DO PEDIDO",
                "QUANTIDADE DE PALLETS",
                "QUANTIDADE DE CAIXAS",
                "CLIENTE",
                "TAMANHO DA ETIQUETA",
                "DATA DE INICIO",
                "OPERADOR",
                "DATA DE CONCLUSÃO",
                "CAIXAS ETIQUETADAS"
            ],
            "ETIQUETAS"
        )

        df_etiquetas = df_etiquetas[
            [
                "NÚMERO DO PEDIDO",
                "QUANTIDADE DE PALLETS",
                "QUANTIDADE DE CAIXAS",
                "CLIENTE",
                "TAMANHO DA ETIQUETA",
                "DATA DE INICIO",
                "OPERADOR",
                "DATA DE CONCLUSÃO",
                "CAIXAS ETIQUETADAS"
            ]
        ].rename(columns={
            "NÚMERO DO PEDIDO": "pedido",
            "QUANTIDADE DE PALLETS": "pallets",
            "QUANTIDADE DE CAIXAS": "caixas",
            "CLIENTE": "cliente",
            "TAMANHO DA ETIQUETA": "etiqueta",
            "DATA DE INICIO": "dt_inicio",
            "OPERADOR": "operador",
            "DATA DE CONCLUSÃO": "dt_conclusao",
            "CAIXAS ETIQUETADAS": "cx_etiquetadas"
        })

        validar_datas(
            df_etiquetas,
            "dt_inicio",
            "pedido",
            "etiquetas"
        )
        validar_datas(
            df_etiquetas,
            "dt_conclusao",
            "pedido",
            "etiquetas"
        )

        # =====================================================
        # GRAVAR NO BANCO
        # =====================================================
        conn = sqlite3.connect(DADOS_OPERACAO, timeout=30)
        cursor = conn.cursor()

        importar_tabela_completa(
            df=df_recebimento,
            nome_tabela="recebimento",
            colunas_sql=(
                "nota_fiscal TEXT, "
                "pallets INTEGER, "
                "data TEXT, "
                "cliente TEXT, "
                "pedido TEXT, "
                "placa TEXT, "
                "cte TEXT, "
                "veiculo TEXT"
            ),
            conn=conn
        )

        importar_tabela_completa(
            df=df_expedicao,
            nome_tabela="expedicao",
            colunas_sql=(
                "romaneio TEXT, "
                "motorista TEXT, "
                "placa TEXT, "
                "transportadora TEXT, "
                "nota_fiscal TEXT, "
                "pedido TEXT, "
                "pallets INTEGER, "
                "veiculo TEXT, "
                "caixas INTEGER, "
                "data TEXT, "
                "hora TEXT, "
                "cliente TEXT, "
                "destino TEXT"
            ),
            conn=conn
        )

        importar_tabela(
            df=df_etiquetas,
            nome_tabela="etiquetas",
            colunas_sql=(
                "pedido TEXT, "
                "pallets INTEGER, "
                "caixas INTEGER, "
                "cliente TEXT, "
                "etiqueta TEXT, "
                "dt_inicio TEXT, "
                "operador TEXT, "
                "dt_conclusao TEXT, "
                "cx_etiquetadas INTEGER"
            ),
            chave_primaria="pedido",
            conn=conn,
            cursor=cursor
        )

        conn.close()

        print(
            f'[{datetime.now().strftime("%H:%M:%S")}] '
            f"Planilha importada com sucesso "
            f"(backup: {caminho_backup})"
        )




# =============================
# Atualização da planilha (substitui o "vigia" de arquivo)
# =============================
# No PythonAnywhere não dá pra vigiar o arquivo do seu PC/OneDrive.
# Em vez disso: você envia a planilha nova por esta página e ela é importada.
PAGINA_UPLOAD = '''
<!doctype html>
<html lang="pt-br">
<head><meta charset="utf-8"><title>Atualizar planilha</title></head>
<body style="font-family: sans-serif; max-width: 480px; margin: 40px auto;">
  <h2>Atualizar planilha</h2>
  {msg}
  <form method="post" enctype="multipart/form-data">
    <input type="file" name="planilha" accept=".xlsx" required>
    <button type="submit">Enviar e importar</button>
  </form>
  <p><a href="/">Voltar ao dashboard</a></p>
</body>
</html>
'''


def salvar_e_importar(arquivo):
    """Salva o .xlsx recebido, confere as abas e importa. Levanta erro se algo falhar."""
    temporario = CAMINHO_PLANILHA + '.novo'
    arquivo.save(temporario)
    try:
        abas = pd.ExcelFile(temporario).sheet_names
        faltando = [a for a in ('RECEBIMENTO', 'EXPEDICAO', 'ETIQUETAS') if a not in abas]
        if faltando:
            raise ValueError(f'Abas faltando: {", ".join(faltando)}')

        os.replace(temporario, CAMINHO_PLANILHA)
        rodar_importacao()
    finally:
        if os.path.exists(temporario):
            os.remove(temporario)


@app.route('/atualizar', methods=['GET', 'POST'])
def atualizar_planilha():
    msg = ''
    if request.method == 'POST':
        arquivo = request.files.get('planilha')
        if not arquivo or not arquivo.filename.lower().endswith('.xlsx'):
            msg = '<p style="color:red">Envie um arquivo .xlsx.</p>'
        else:
            try:
                salvar_e_importar(arquivo)
                msg = '<p style="color:green">Planilha importada com sucesso!</p>'
            except Exception as erro:
                msg = f'<p style="color:red">Erro ao importar: {erro}</p>'

    return PAGINA_UPLOAD.format(msg=msg)


@app.route('/api/upload-planilha', methods=['POST'])
def api_upload_planilha():
    """Usada pelo script enviar_planilha.py rodando no seu PC (sem login,
    mas exige o token no cabeçalho X-Token)."""
    token = request.headers.get('X-Token', '')
    if not secrets.compare_digest(token, TOKEN_UPLOAD):
        return jsonify({'erro': 'token inválido'}), 401

    arquivo = request.files.get('planilha')
    if not arquivo:
        return jsonify({'erro': 'arquivo não enviado'}), 400

    try:
        salvar_e_importar(arquivo)
    except Exception as erro:
        import traceback
        print('=' * 70)
        print(f'[ERRO] {type(erro).__name__}: {erro}')
        traceback.print_exc()
        print('=' * 70)

        return jsonify({
            'ok': False,
            'erro': str(erro),
            'tipo': type(erro).__name__
        }), 500

    return jsonify({
        'ok': True,
        'mensagem': 'Planilha enviada e importada com sucesso.'
    })


# Importa ao iniciar, se o banco ainda não existir e a planilha estiver na pasta.
# (No PythonAnywhere isso roda quando o app é carregado/recarregado.)
get_conn_usuarios().close()
if not os.path.exists(DADOS_OPERACAO) and os.path.exists(CAMINHO_PLANILHA):
    try:
        rodar_importacao()
    except Exception as erro:
        print(f'Falha na importação inicial: {erro}')


# =============================
# Filtros / helpers de consulta
# =============================
def filtro_data(mes, ano, quinzena=None, coluna='data'):
    """Monta o WHERE e os parâmetros pra filtrar por mês/ano/quinzena."""
    condicoes = [f"strftime('%m', {coluna}) = ?", f"strftime('%Y', {coluna}) = ?"]
    parametros = [f'{int(mes):02d}', str(ano)]

    if quinzena == '1':
        condicoes.append(f"CAST(strftime('%d', {coluna}) AS INTEGER) <= 15")
    elif quinzena == '2':
        condicoes.append(f"CAST(strftime('%d', {coluna}) AS INTEGER) > 15")

    return ' AND '.join(condicoes), parametros


def args_filtro(coluna='data'):
    return filtro_data(
        request.args.get('mes'),
        request.args.get('ano'),
        request.args.get('quinzena'),
        coluna=coluna
    )


# Caixas etiquetadas "efetivas": com data de conclusão conta 100% (usa 'caixas');
# senão usa o que foi digitado em cx_etiquetadas (ou 0).
CX_ETIQ = """
    CASE
        WHEN dt_conclusao IS NOT NULL AND TRIM(dt_conclusao) != ''
            THEN caixas
        ELSE COALESCE(cx_etiquetadas, 0)
    END
"""


# =============================
# Páginas
# =============================
@app.route('/')
@app.route('/recebimento')
def dashboard_recebimento():
    return render_template('recebimento.html', active='recebimento')


@app.route('/expedicao')
def dashboard_expedicao():
    return render_template('expedicao.html', active='expedicao')


@app.route('/etiquetas')
def dashboard_etiquetas():
    return render_template('etiquetas.html', active='etiquetas')


# =============================
# API - Recebimento
# =============================
@app.route('/api/recebimento/resumo')
def api_recebimento_resumo():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    # Veículos recebidos = CTEs distintos
    cursor.execute(f'''
        SELECT COUNT(*) as total, COUNT(DISTINCT cliente) as clientes, SUM(pallets) as pallets,
               COUNT(DISTINCT cte) as veiculos, COUNT(DISTINCT veiculo) as tipos
        FROM recebimento
        WHERE {where}
    ''', params)
    linha = cursor.fetchone()
    conn.close()
    return jsonify({
        'total_recebimentos': linha['total'],
        'total_clientes': linha['clientes'],
        'total_pallets': linha['pallets'] or 0,
        'total_veiculos': linha['veiculos'],
        'total_tipos_veiculo': linha['tipos']
    })


@app.route('/api/recebimento/por-dia')
def api_recebimento_por_dia():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT strftime('%d/%m', data) as dia, COUNT(*) as total
        FROM recebimento
        WHERE {where}
        GROUP BY dia
        ORDER BY data
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/recebimento/por-cliente')
def api_recebimento_por_cliente():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT cliente, COUNT(*) as total
        FROM recebimento
        WHERE {where}
        GROUP BY cliente
        ORDER BY total DESC
        LIMIT 10
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/recebimento/por-tipo-veiculo')
def api_recebimento_por_tipo_veiculo():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    # Conta veículos distintos (por CTE), não recebimentos
    cursor.execute(f'''
        SELECT veiculo as tipo, COUNT(DISTINCT cte) as total
        FROM recebimento
        WHERE {where}
        GROUP BY veiculo
        ORDER BY total DESC
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


# =============================
# API - Expedição
# =============================
@app.route('/api/expedicao/resumo')
def api_expedicao_resumo():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    # Veículos expedidos = romaneios distintos
    cursor.execute(f'''
        SELECT COUNT(*) as cargas, SUM(pallets) as paletes,
               COUNT(DISTINCT romaneio) as veiculos, COUNT(DISTINCT veiculo) as tipos
        FROM expedicao
        WHERE {where}
    ''', params)
    linha = cursor.fetchone()
    conn.close()
    return jsonify({
        'total_cargas': linha['cargas'],
        'total_paletes': linha['paletes'] or 0,
        'total_veiculos': linha['veiculos'],
        'total_tipos_veiculo': linha['tipos']
    })


@app.route('/api/expedicao/por-dia')
def api_expedicao_por_dia():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT strftime('%d/%m', data) as dia, COUNT(*) as total
        FROM expedicao
        WHERE {where}
        GROUP BY dia
        ORDER BY data
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/expedicao/por-tipo-veiculo')
def api_expedicao_por_tipo_veiculo():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    # Conta veículos distintos (por romaneio), não paletes
    cursor.execute(f'''
        SELECT veiculo as tipo, COUNT(DISTINCT romaneio) as total
        FROM expedicao
        WHERE {where}
        GROUP BY veiculo
        ORDER BY total DESC
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/expedicao/por-cliente')
def api_expedicao_por_cliente():
    where, params = args_filtro()
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT cliente, SUM(pallets) as paletes
        FROM expedicao
        WHERE {where}
        GROUP BY cliente
        ORDER BY paletes DESC
        LIMIT 10
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


# =============================
# API - Expedição detalhada
# =============================
@app.route('/api/expedicao/detalhes')
def api_expedicao_detalhes():
    where, params = args_filtro()

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(f"""
        SELECT
            romaneio, motorista, placa, transportadora,
            nota_fiscal, pedido, pallets, veiculo, caixas,
            data, hora, cliente, destino
        FROM expedicao
        WHERE {where}
        ORDER BY data, hora, romaneio
    """, params)

    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()

    return jsonify(dados)


@app.route('/api/expedicao/por-destino')
def api_expedicao_por_destino():
    where, params = args_filtro()

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(destino), ''), 'Não informado') AS destino,
            COUNT(DISTINCT romaneio) AS total
        FROM expedicao
        WHERE {where}
        GROUP BY destino
        ORDER BY total DESC
    """, params)

    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()

    return jsonify(dados)


@app.route('/api/expedicao/por-motorista')
def api_expedicao_por_motorista():
    where, params = args_filtro()

    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute(f"""
        SELECT
            COALESCE(NULLIF(TRIM(motorista), ''), 'Não informado') AS motorista,
            COUNT(DISTINCT romaneio) AS total
        FROM expedicao
        WHERE {where}
        GROUP BY motorista
        ORDER BY total DESC
    """, params)

    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()

    return jsonify(dados)


# =============================
# API - Etiquetas
# =============================
@app.route('/api/etiquetas/resumo')
def api_etiquetas_resumo():
    where, params = args_filtro(coluna='dt_inicio')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT COUNT(*) as total_pedidos,
               SUM({CX_ETIQ}) as etiquetadas,
               SUM(caixas) as previstas,
               AVG(julianday(dt_conclusao) - julianday(dt_inicio)) * 24 as horas
        FROM etiquetas
        WHERE {where}
    ''', params)
    linha = cursor.fetchone()
    conn.close()

    previstas = linha['previstas'] or 0
    etiquetadas = linha['etiquetadas'] or 0
    percentual = round((etiquetadas / previstas) * 100, 1) if previstas else 0
    tempo_medio = round(linha['horas'], 1) if linha['horas'] else 0

    return jsonify({
        'total_pedidos': linha['total_pedidos'],
        'percentual_concluido': percentual,
        'tempo_medio_horas': tempo_medio
    })


@app.route('/api/etiquetas/por-operador')
def api_etiquetas_por_operador():
    where, params = args_filtro(coluna='dt_inicio')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT operador, SUM({CX_ETIQ}) as caixas
        FROM etiquetas
        WHERE {where}
        GROUP BY operador
        ORDER BY caixas DESC
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/etiquetas/por-cliente')
def api_etiquetas_por_cliente():
    where, params = args_filtro(coluna='dt_inicio')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT cliente, SUM({CX_ETIQ}) as caixas
        FROM etiquetas
        WHERE {where}
        GROUP BY cliente
        ORDER BY caixas DESC
        LIMIT 10
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/etiquetas/por-dia')
def api_etiquetas_por_dia():
    where, params = args_filtro(coluna='dt_inicio')
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT strftime('%d/%m', dt_inicio) as dia, COALESCE(SUM(pallets), 0) as pallets
        FROM etiquetas
        WHERE {where}
        GROUP BY dia
        ORDER BY MIN(dt_inicio)
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


# Não há "if __name__ == '__main__'" nem waitress: no PythonAnywhere quem
# executa o app é o servidor deles, através do arquivo WSGI.