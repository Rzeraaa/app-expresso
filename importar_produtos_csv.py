"""
Importa produtos de um CSV (exportado do SSMS a partir do senior_wms)
para a tabela tb_produtos do estoque_jmk.

Formato esperado (sem cabeçalho, separado por ; - exatamente como o SSMS
exporta pelo "Salvar Resultados Como..."):
codproduto;produto
10;BEIJINHO 6X2 KG
100;CERVEGELA PATAGONIA 600ML (UNITÁRIO)

Uso: python importar_produtos_csv.py produtos_origem.csv
"""
import sys
import csv
import pyodbc

import config

if len(sys.argv) != 2:
    print("Uso: python importar_produtos_csv.py caminho_do_arquivo.csv")
    sys.exit(1)

caminho_csv = sys.argv[1]

conn = pyodbc.connect(config.CONNECTION_STRING)
cur = conn.cursor()

# carrega os SKUs que já existem, pra não tentar inserir de novo
cur.execute("SELECT sku FROM tb_produtos")
existentes = {row[0] for row in cur.fetchall()}
print(f"Já existem {len(existentes)} produtos cadastrados.")

novos = []
ignorados_vazios = 0

with open(caminho_csv, encoding="utf-8-sig") as f:
    leitor = csv.reader(f, delimiter=";")

    for linha in leitor:
        if len(linha) < 2:
            ignorados_vazios += 1
            continue

        sku = linha[0].strip()
        descricao = linha[1].strip()

        if not sku or not descricao:
            ignorados_vazios += 1
            continue
        if sku in existentes:
            continue

        novos.append((sku, descricao))
        existentes.add(sku)  # evita duplicar se o CSV tiver linha repetida

print(f"{len(novos)} produtos novos para importar. {ignorados_vazios} linhas ignoradas (sku/descrição vazios).")

if novos:
    cur.fast_executemany = True
    cur.executemany(
        "INSERT INTO tb_produtos (sku, descricao, pendente_validacao) VALUES (?, ?, 0)",
        novos,
    )
    conn.commit()
    print(f"{len(novos)} produtos importados com sucesso.")
else:
    print("Nada para importar.")

conn.close()