"""
Pré-cadastra todas as posições físicas do galpão na tb_posicoes:
- Grade: JMK1-{LADO}-{COLUNA}-{NIVEL}
  LADO: A1..A2, B1..B2, ... T1..T2  (20 ruas x 2 lados = 40 combinações)
  COLUNA: 01 a 24
  NIVEL: 1 a 7
  Total: 40 x 24 x 7 = 6.720 posições

- Blocados: JMK1-BLC-01 até JMK1-BLC-45 (45 posições)

Total geral: 6.765 posições

Uso: python gerar_posicoes.py
"""
import string
import pyodbc

import config

PREFIXO = "JMK1"
RUAS = string.ascii_uppercase[:20]  # A até T
LADOS_POR_RUA = [1, 2]
COLUNAS = range(1, 25)   # 1 a 24
NIVEIS = range(1, 8)     # 1 a 7
TOTAL_BLOCADOS = 45

codigos = []

# ---- grade principal ----
for rua in RUAS:
    for lado in LADOS_POR_RUA:
        for coluna in COLUNAS:
            for nivel in NIVEIS:
                codigo = f"{PREFIXO}-{rua}{lado}-{coluna:02d}-{nivel}"
                codigos.append(codigo)

print(f"Grade principal: {len(codigos)} posições geradas.")

# ---- blocados ----
for i in range(1, TOTAL_BLOCADOS + 1):
    codigo = f"{PREFIXO}-BLC-{i:02d}"
    codigos.append(codigo)

print(f"Total geral (grade + blocados): {len(codigos)} posições.")

# ---- gravar no banco ----
conn = pyodbc.connect(config.CONNECTION_STRING)
cur = conn.cursor()

cur.execute("SELECT codigo FROM tb_posicoes")
existentes = {row[0] for row in cur.fetchall()}
print(f"Já existem {len(existentes)} posições cadastradas.")

novas = [(c,) for c in codigos if c not in existentes]
print(f"{len(novas)} posições novas para inserir.")

if novas:
    cur.fast_executemany = True
    cur.executemany(
        "INSERT INTO tb_posicoes (codigo) VALUES (?)",
        novas,
    )
    conn.commit()
    print(f"{len(novas)} posições inseridas com sucesso.")
else:
    print("Nada para inserir - todas já existem.")

conn.close()