#!/usr/bin/env bash
# Exit on error
set -o errexit

# Instalando as dependências do sistema necessárias para o Microsoft ODBC Driver no Linux
apt-get update && apt-get install -y curl apt-transport-https debconf-utils

# Baixando e instalando a chave e o repositório oficial da Microsoft para o ODBC Driver 17
curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /etc/apt/trusted.gpg.d/microsoft.asc.gpg
curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list

apt-get update
ACCEPT_EULA=Y apt-get install -y msodbcsql17 unixodbc-dev

# Instalando as bibliotecas do Python
pip install -r requirements.txt