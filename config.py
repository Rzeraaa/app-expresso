import os
from dotenv import load_dotenv

load_dotenv()  # lê o arquivo .env na mesma pasta

# --------------------------------------------------------------------------
# Nunca coloque valores reais aqui dentro. Tudo vem do .env, que NUNCA
# deve ser versionado no Git (coloque .env no .gitignore).
# --------------------------------------------------------------------------

_SQL_SERVER_RAW = os.environ["SQL_SERVER"]     # ex: "192.168.1.103,1433" ou "meuservidor.database.windows.net"
SQL_DATABASE = os.environ["SQL_DATABASE"]      # ex: "estoque_jmk"
SQL_USER = os.environ["SQL_USER"]              # ex: "app_estoque_jmk"
SQL_PASSWORD = os.environ["SQL_PASSWORD"]

SECRET_KEY = os.environ["FLASK_SECRET_KEY"]    # chave de assinatura da sessão

# pymssql não aceita "host,porta" (formato ODBC) - ele quer host e porta
# separados. Isso trata os dois formatos, então funciona tanto local
# (SQL Server com porta explícita) quanto no Azure (só o hostname).
if "," in _SQL_SERVER_RAW:
    SQL_SERVER, _porta = _SQL_SERVER_RAW.split(",", 1)
    SQL_PORT = int(_porta)
else:
    SQL_SERVER = _SQL_SERVER_RAW
    SQL_PORT = 1433  # porta padrão do SQL Server / Azure SQL