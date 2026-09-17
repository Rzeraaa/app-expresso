"""
Cria (ou atualiza a senha de) um operador direto no banco.
Uso: python criar_operador.py
"""
import getpass
import pymssql
from werkzeug.security import generate_password_hash

import config

nome = input("Nome completo: ").strip()
login = input("Login (usuário): ").strip()
senha = getpass.getpass("Senha: ")
perfil = input("Perfil [operador/admin] (padrão operador): ").strip() or "operador"

senha_hash = generate_password_hash(senha)

conn = pymssql.connect(
    server=config.SQL_SERVER,
    port=config.SQL_PORT,
    user=config.SQL_USER,
    password=config.SQL_PASSWORD,
    database=config.SQL_DATABASE,
    tds_version="7.4",
)
cur = conn.cursor()
cur.execute("SELECT id FROM tb_operadores WHERE login = %s", (login,))
existente = cur.fetchone()

if existente:
    cur.execute(
        "UPDATE tb_operadores SET nome_completo = %s, senha_hash = %s, perfil = %s, ativo = 1 WHERE login = %s",
        (nome, senha_hash, perfil, login),
    )
    print(f"Operador '{login}' atualizado.")
else:
    cur.execute(
        "INSERT INTO tb_operadores (nome_completo, login, senha_hash, perfil) VALUES (%s, %s, %s, %s)",
        (nome, login, senha_hash, perfil),
    )
    print(f"Operador '{login}' criado.")

conn.commit()
conn.close()
