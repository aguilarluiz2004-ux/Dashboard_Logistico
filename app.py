import pandas as pd
from datetime import datetime, timedelta
import sqlite3
import shutil
import os
import secrets
import threading
import time
from flask import Flask, render_template, jsonify, request, session, redirect, url_for
from werkzeug.security import check_password_hash

app = Flask(__name__)

CAMINHO_PLANILHA = 'base_operacao.xlsx'


#=============================
# Configuração de segurança / login
#=============================
def carregar_secret_key():
    """A SECRET_KEY assina o cookie de sessão. Se alguém descobrir essa chave,
    consegue forjar um login. Por isso ela NÃO fica escrita no código:
    1) usa a variável de ambiente SECRET_KEY, se existir;
    2) senão, usa/gera o arquivo secret_key.txt (coloque no .gitignore!)."""
    chave = os.environ.get('SECRET_KEY')
    if chave:
        return chave

    if os.path.exists('secret_key.txt'):
        with open('secret_key.txt') as f:
            return f.read().strip()

    chave = secrets.token_hex(32)
    with open('secret_key.txt', 'w') as f:
        f.write(chave)
    return chave


app.secret_key = carregar_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,     # JavaScript não consegue ler o cookie
    SESSION_COOKIE_SAMESITE='Lax',    # dificulta ataques vindos de outros sites
    # Só envia o cookie por HTTPS. Ative com a variável HTTPS=1 quando publicar.
    SESSION_COOKIE_SECURE=os.environ.get('HTTPS') == '1',
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),  # login expira em 8h
)

# Bloqueio simples contra tentativa de adivinhar senha (por IP)
MAX_FALHAS = 5
BLOQUEIO_SEGUNDOS = 300
tentativas = {}  # ip -> (falhas, bloqueado_ate)


def get_conn_usuarios():
    """Usuários ficam em um banco SEPARADO (usuarios.db), pra nunca serem
    afetados pela reimportação da planilha no operacao.db."""
    conn = sqlite3.connect('usuarios.db')
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario TEXT PRIMARY KEY,
            senha_hash TEXT NOT NULL,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    return conn


@app.before_request
def exigir_login():
    """Roda ANTES de toda requisição. Protege páginas e APIs de uma vez,
    então nenhuma rota nova fica aberta por esquecimento."""
    if request.endpoint in ('login', 'static'):
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


#=============================
# Importação para o banco
#=============================
def validar_datas(df, coluna, identificador, nome_tabela):
    """Avisa no terminal quais linhas têm um valor não-vazio na coluna de
    data que não é uma data válida (ex: alguém digitou '8' em vez de uma
    data). Não impede a importação — só chama atenção pro problema."""
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
    """Recria a tabela com id automático e insere TODAS as linhas da
    planilha, sem perder nenhuma (mesma nota pode aparecer mais de uma vez)."""
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


def rodar_importacao():
    """Faz backup (copiando o arquivo) e depois lê/importa a PARTIR DA CÓPIA,
    nunca do arquivo original - assim não briga com o Excel pelo acesso."""

    #===================================
    # Backup (feito ANTES da leitura, de propósito)
    #==================================
    pasta_backup = 'backups'
    os.makedirs(pasta_backup, exist_ok=True)

    hoje = datetime.now().strftime('%Y-%m-%d')
    nome_backup = f'base_operacao_{hoje}.xlsx'
    caminho_backup = os.path.join(pasta_backup, nome_backup)

    shutil.copy2(CAMINHO_PLANILHA, caminho_backup)

    #=============================
    # Lendo da cópia, não do arquivo original
    #=============================
    df_recebimento = pd.read_excel(caminho_backup, sheet_name='RECEBIMENTO')
    df_expedicao = pd.read_excel(caminho_backup, sheet_name='EXPEDICAO')
    df_etiquetas = pd.read_excel(caminho_backup, sheet_name='ETIQUETAS')

    #=============================
    # Formatando o recebimento
    #=============================
    df_recebimento = df_recebimento[['NOTA FISCAL', 'QTD.PALLET', 'DATA', 'CLIENTE', 'NUM.PEDIDO', 'PLACA', 'NUM.CTE', 'TIPO DO VEICULO']]

    df_recebimento = df_recebimento.rename(columns={
        'NOTA FISCAL': 'nota_fiscal',
        'QTD.PALLET': 'pallets',
        'DATA': 'data',
        'CLIENTE': 'cliente',
        'NUM.PEDIDO': 'pedido',
        'PLACA': 'placa',
        'NUM.CTE': 'cte',
        'TIPO DO VEICULO': 'veiculo'
    })

    validar_datas(df_recebimento, coluna='data', identificador='nota_fiscal', nome_tabela='recebimento')

    #=============================
    # Formatando o expedicao
    #==============================
    df_expedicao = df_expedicao[['NOTA FISCAL', 'QTD.PALLET', 'PLACA', 'DATA', 'CLIENTE', 'QUANTIDADE DE CAIXAS', 'TIPO DO VEICULO', 'ROMANEIO']]

    df_expedicao = df_expedicao.rename(columns={
        'NOTA FISCAL': 'nota_fiscal',
        'QTD.PALLET': 'pallets',
        'PLACA': 'placa',
        'DATA': 'data',
        'CLIENTE': 'cliente',
        'QUANTIDADE DE CAIXAS': 'caixas',
        'TIPO DO VEICULO': 'veiculo',
        'ROMANEIO': 'romaneio'
    })

    validar_datas(df_expedicao, coluna='data', identificador='nota_fiscal', nome_tabela='expedicao')

    #=============================
    # Formatando o etiqueta
    #==============================
    df_etiquetas = df_etiquetas[[
        'NÚMERO DO PEDIDO', 'QUANTIDADE DE PALLETS', 'QUANTIDADE DE CAIXAS', 'CLIENTE',
        'TAMANHO DA ETIQUETA', 'DATA DE INICIO', 'OPERADOR', 'DATA DE CONCLUSÃO', 'CAIXAS ETIQUETADAS'
    ]]
    df_etiquetas = df_etiquetas.rename(columns={
        'NÚMERO DO PEDIDO': 'pedido',
        'QUANTIDADE DE PALLETS': 'pallets',
        'QUANTIDADE DE CAIXAS': 'caixas',
        'CLIENTE': 'cliente',
        'TAMANHO DA ETIQUETA': 'etiqueta',
        'DATA DE INICIO': 'dt_inicio',
        'OPERADOR': 'operador',
        'DATA DE CONCLUSÃO': 'dt_conclusao',
        'CAIXAS ETIQUETADAS': 'cx_etiquetadas'
    })

    validar_datas(df_etiquetas, coluna='dt_inicio', identificador='pedido', nome_tabela='etiquetas')
    validar_datas(df_etiquetas, coluna='dt_conclusao', identificador='pedido', nome_tabela='etiquetas')

    #=============================
    # Gravando no banco
    #=============================
    conn = sqlite3.connect('operacao.db')
    cursor = conn.cursor()

    importar_tabela_completa(
        df=df_recebimento,
        nome_tabela='recebimento',
        colunas_sql='nota_fiscal TEXT, pallets INTEGER, data TEXT, cliente TEXT, pedido TEXT, placa TEXT, cte TEXT, veiculo TEXT',
        conn=conn
    )

    importar_tabela_completa(
        df=df_expedicao,
        nome_tabela='expedicao',
        colunas_sql='nota_fiscal TEXT, pallets INTEGER, placa TEXT, data TEXT, cliente TEXT, caixas INTEGER, veiculo TEXT, romaneio TEXT',
        conn=conn
    )

    importar_tabela(
        df=df_etiquetas,
        nome_tabela='etiquetas',
        colunas_sql='pedido TEXT, pallets INTEGER, caixas INTEGER, cliente TEXT, etiqueta TEXT, dt_inicio TEXT, operador TEXT, dt_conclusao TEXT, cx_etiquetadas INTEGER',
        chave_primaria='pedido',
        conn=conn,
        cursor=cursor
    )

    conn.close()
    print(f'[{datetime.now().strftime("%H:%M:%S")}] Planilha importada (backup: {caminho_backup})')


#=============================
# Vigia da planilha
#=============================
def rodar_importacao_com_retentativas(tentativas=3, espera=2):
    """Tenta importar a planilha algumas vezes antes de desistir - útil
    pro caso do arquivo estar temporariamente travado (Excel salvando,
    OneDrive sincronizando) bem no momento em que o servidor está subindo."""
    for tentativa in range(1, tentativas + 1):
        try:
            rodar_importacao()
            return True
        except PermissionError as erro:
            print(f'Tentativa {tentativa}/{tentativas}: arquivo ocupado ({erro}). Tentando de novo em {espera}s...')
            time.sleep(espera)
        except FileNotFoundError as erro:
            print(f'Planilha não encontrada: {erro}')
            return False

    print('Não foi possível importar a planilha depois de várias tentativas.')
    print('Feche o Excel (ou aguarde o OneDrive sincronizar) e reinicie o servidor,')
    print('ou aguarde - o monitoramento automático vai tentar de novo quando o arquivo mudar.')
    return False


def monitorar_planilha(intervalo=5):
    """Roda em segundo plano: a cada `intervalo` segundos, confere se o
    arquivo mudou (pela data de modificação) e reimporta se tiver mudado."""
    ultima_modificacao = os.path.getmtime(CAMINHO_PLANILHA)

    while True:
        time.sleep(intervalo)
        try:
            modificacao_atual = os.path.getmtime(CAMINHO_PLANILHA)
        except FileNotFoundError:
            continue

        if modificacao_atual != ultima_modificacao:
            print('Planilha foi alterada, reimportando...')
            try:
                rodar_importacao()
                ultima_modificacao = modificacao_atual
            except Exception as erro:
                print(f'Falha ao reimportar (vai tentar de novo no próximo ciclo): {erro}')


#===============================
# Operacoes Recebimento
#===============================
def resumo_recebimento():
    conn = sqlite3.connect('operacao.db')
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*), SUM(pallets) FROM recebimento')
    total_notas, total_pallets = cursor.fetchone()

    conn.close()
    return {
        'total_notas': total_notas or 0,
        'total_pallets': total_pallets or 0
    }


def ranking_clientes_recebimento():
    conn = sqlite3.connect('operacao.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT cliente, COUNT(*) as qtd_notas, SUM(pallets) as total_pallets
        FROM recebimento
        GROUP BY cliente
        ORDER BY total_pallets DESC
    ''')
    dados = cursor.fetchall()

    conn.close()
    return dados


#===============================
# Operacoes Expedicao
#===============================
def resumo_expedicao():
    conn = sqlite3.connect('operacao.db')
    cursor = conn.cursor()

    cursor.execute('SELECT COUNT(*), SUM(pallets), SUM(caixas) FROM expedicao')
    total_notas, total_pallets, total_caixas = cursor.fetchone()

    conn.close()
    return {
        'total_notas': total_notas or 0,
        'total_pallets': total_pallets or 0,
        'total_caixas': total_caixas or 0
    }


def ranking_veiculos_expedicao():
    conn = sqlite3.connect('operacao.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('''
        SELECT veiculo, COUNT(*) as qtd_viagens, SUM(pallets) as total_pallets
        FROM expedicao
        GROUP BY veiculo
        ORDER BY qtd_viagens DESC
    ''')
    dados = cursor.fetchall()

    conn.close()
    return dados


#===============================
# Operacoes Etiquetas
#===============================
# Caixas etiquetadas "efetivas": se o pedido tem data de conclusão,
# conta como 100% concluído (usa 'caixas'); senão, usa o que foi
# digitado em cx_etiquetadas (ou 0 se estiver vazio).
CX_ETIQ = """
    CASE
        WHEN dt_conclusao IS NOT NULL AND TRIM(dt_conclusao) != ''
            THEN caixas
        ELSE COALESCE(cx_etiquetadas, 0)
    END
"""


def resumo_etiquetas():
    conn = sqlite3.connect('operacao.db')
    cursor = conn.cursor()

    cursor.execute(f'SELECT COUNT(*), SUM({CX_ETIQ}), SUM(caixas) FROM etiquetas')
    total_pedidos, cx_etiquetadas, cx_previstas = cursor.fetchone()

    conn.close()
    return {
        'total_pedidos': total_pedidos or 0,
        'cx_etiquetadas': cx_etiquetadas or 0,
        'percentual_concluido': round((cx_etiquetadas / cx_previstas) * 100, 1) if cx_previstas else 0
    }


def produtividade_operadores():
    conn = sqlite3.connect('operacao.db')
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(f'''
        SELECT operador, COUNT(*) as qtd_pedidos, SUM({CX_ETIQ}) as total_caixas
        FROM etiquetas
        GROUP BY operador
        ORDER BY total_caixas DESC
    ''')
    dados = cursor.fetchall()

    conn.close()
    return dados


def tempo_medio_etiquetagem():
    conn = sqlite3.connect('operacao.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT AVG(julianday(dt_conclusao) - julianday(dt_inicio)) * 24
        FROM etiquetas
        WHERE dt_inicio IS NOT NULL AND dt_conclusao IS NOT NULL
    ''')
    media_horas = cursor.fetchone()[0]

    conn.close()
    return round(media_horas, 1) if media_horas else 0


#===============================
# Comparacao
#===============================
def comparativo_recebido_expedido():
    recebido = resumo_recebimento()
    expedido = resumo_expedicao()

    return {
        'pallets_recebidos': recebido['total_pallets'],
        'pallets_expedidos': expedido['total_pallets'],
        'saldo': recebido['total_pallets'] - expedido['total_pallets']
    }


def get_conn():
    conn = sqlite3.connect('operacao.db')
    conn.row_factory = sqlite3.Row
    return conn


def filtro_data(mes, ano, quinzena=None, coluna='data'):
    """Monta o WHERE e os parâmetros pra filtrar por mês/ano/quinzena.

    coluna: nome da coluna de data a filtrar. Recebimento/expedição usam
    'data'; etiquetas usa 'dt_inicio' (não existe coluna 'data' lá).
    """
    condicoes = [f"strftime('%m', {coluna}) = ?", f"strftime('%Y', {coluna}) = ?"]
    parametros = [f'{int(mes):02d}', str(ano)]

    if quinzena == '1':
        condicoes.append(f"CAST(strftime('%d', {coluna}) AS INTEGER) <= 15")
    elif quinzena == '2':
        condicoes.append(f"CAST(strftime('%d', {coluna}) AS INTEGER) > 15")

    return ' AND '.join(condicoes), parametros


#===============================
# Páginas
#===============================
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


#===============================
# API - Recebimento
#===============================
@app.route('/api/recebimento/resumo')
def api_recebimento_resumo():
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
    conn = get_conn()
    cursor = conn.cursor()
    # Veículos recebidos = CTEs distintos (o mesmo CTE se repete em várias linhas)
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
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


#===============================
# API - Expedição
#===============================
@app.route('/api/expedicao/resumo')
def api_expedicao_resumo():
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
    conn = get_conn()
    cursor = conn.cursor()
    # Veículos expedidos = romaneios distintos (o mesmo romaneio se repete em várias linhas)
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
    conn = get_conn()
    cursor = conn.cursor()
    cursor.execute(f'''
        SELECT veiculo as tipo, SUM(pallets) as paletes
        FROM expedicao
        WHERE {where}
        GROUP BY veiculo
        ORDER BY paletes DESC
    ''', params)
    dados = [dict(linha) for linha in cursor.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route('/api/expedicao/por-cliente')
def api_expedicao_por_cliente():
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'))
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


#===============================
# API - Etiquetas
#===============================
@app.route('/api/etiquetas/resumo')
def api_etiquetas_resumo():
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'), coluna='dt_inicio')
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'), coluna='dt_inicio')
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'), coluna='dt_inicio')
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
    where, params = filtro_data(request.args.get('mes'), request.args.get('ano'), request.args.get('quinzena'), coluna='dt_inicio')
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


if __name__ == '__main__':
    get_conn_usuarios().close()  # garante que usuarios.db e a tabela existem
    rodar_importacao_com_retentativas()  # importa (com retentativas) ao subir o servidor

    thread_monitor = threading.Thread(target=monitorar_planilha, daemon=True)
    thread_monitor.start()

    from waitress import serve
    print('Servidor no ar!')
    print('Neste computador, acesse:  http://127.0.0.1:5000')
    print('De outros computadores da rede, use o IP desta máquina (rode "ipconfig" pra descobrir), ex: http://192.168.0.8:5000')
    serve(app, host='0.0.0.0', port=5000)