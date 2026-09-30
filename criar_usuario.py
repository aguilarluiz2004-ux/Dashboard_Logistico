"""Gerenciador de usuários do dashboard.

Uso (no terminal, na pasta do projeto):
    python criar_usuario.py criar  luiz
    python criar_usuario.py listar
    python criar_usuario.py senha  luiz
    python criar_usuario.py remover luiz
"""
import sqlite3
import sys
import getpass
from werkzeug.security import generate_password_hash

BANCO = 'usuarios.db'
TAMANHO_MINIMO_SENHA = 8


def conectar():
    conn = sqlite3.connect(BANCO)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario TEXT PRIMARY KEY,
            senha_hash TEXT NOT NULL,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    return conn


def pedir_senha():
    """Pede a senha duas vezes sem mostrar o que é digitado."""
    while True:
        senha = getpass.getpass('Senha: ')
        if len(senha) < TAMANHO_MINIMO_SENHA:
            print(f'A senha precisa ter pelo menos {TAMANHO_MINIMO_SENHA} caracteres.')
            continue
        if senha != getpass.getpass('Repita a senha: '):
            print('As senhas não conferem. Tente de novo.')
            continue
        return senha


def criar(usuario):
    conn = conectar()
    existe = conn.execute('SELECT 1 FROM usuarios WHERE usuario = ?', (usuario,)).fetchone()
    if existe:
        print(f'O usuário "{usuario}" já existe. Para trocar a senha use: senha {usuario}')
        conn.close()
        return

    senha = pedir_senha()
    conn.execute(
        'INSERT INTO usuarios (usuario, senha_hash) VALUES (?, ?)',
        (usuario, generate_password_hash(senha))
    )
    conn.commit()
    conn.close()
    print(f'Usuário "{usuario}" criado.')


def listar():
    conn = conectar()
    linhas = conn.execute('SELECT usuario, criado_em FROM usuarios ORDER BY usuario').fetchall()
    conn.close()
    if not linhas:
        print('Nenhum usuário cadastrado.')
    for usuario, criado_em in linhas:
        print(f'{usuario}  (criado em {criado_em})')


def trocar_senha(usuario):
    conn = conectar()
    existe = conn.execute('SELECT 1 FROM usuarios WHERE usuario = ?', (usuario,)).fetchone()
    if not existe:
        print(f'Usuário "{usuario}" não encontrado.')
        conn.close()
        return

    senha = pedir_senha()
    conn.execute(
        'UPDATE usuarios SET senha_hash = ? WHERE usuario = ?',
        (generate_password_hash(senha), usuario)
    )
    conn.commit()
    conn.close()
    print(f'Senha de "{usuario}" atualizada.')


def remover(usuario):
    conn = conectar()
    cursor = conn.execute('DELETE FROM usuarios WHERE usuario = ?', (usuario,))
    conn.commit()
    conn.close()
    if cursor.rowcount:
        print(f'Usuário "{usuario}" removido.')
    else:
        print(f'Usuário "{usuario}" não encontrado.')


if __name__ == '__main__':
    comando = sys.argv[1] if len(sys.argv) > 1 else ''
    nome = sys.argv[2].strip().lower() if len(sys.argv) > 2 else ''

    if comando == 'listar':
        listar()
    elif comando == 'criar' and nome:
        criar(nome)
    elif comando == 'senha' and nome:
        trocar_senha(nome)
    elif comando == 'remover' and nome:
        remover(nome)
    else:
        print(__doc__)