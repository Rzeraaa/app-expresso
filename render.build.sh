#!/usr/bin/env bash
# Exit on error
set -o errexit

# pymssql já traz as bibliotecas de conexão embutidas no pacote Python,
# não precisa instalar driver ODBC nem nada no sistema operacional.
pip install -r requirements.txt