import os
from dotenv import load_dotenv

load_dotenv()  # lê o arquivo .env na mesma pasta

# --------------------------------------------------------------------------
# Nunca coloque valores reais aqui dentro. Tudo vem do .env, que NUNCA
# deve ser versionado no Git (coloque .env no .gitignore).
# --------------------------------------------------------------------------

SQL_SERVER = os.environ["SQL_SERVER"]          # ex: "192.168.1.103,1433"
SQL_DATABASE = os.environ["SQL_DATABASE"]      # ex: "estoque_jmk"
SQL_USER = os.environ["SQL_USER"]              # ex: "app_estoque_jmk"
SQL_PASSWORD = os.environ["SQL_PASSWORD"]

SECRET_KEY = os.environ["FLASK_SECRET_KEY"]    # chave de assinatura da sessão

CONNECTION_STRING = (
    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
    f"SERVER={SQL_SERVER};"
    f"DATABASE={SQL_DATABASE};"
    f"UID={SQL_USER};"
    f"PWD={SQL_PASSWORD};"
)