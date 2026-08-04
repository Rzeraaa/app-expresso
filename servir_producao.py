"""
Sobe o sistema usando o Waitress - um servidor WSGI de produção, ao
contrário do servidor embutido do Flask (que é só para desenvolvimento
e não aguenta uso real, além de não se recuperar sozinho de travamentos).

Uso: python servir_producao.py
"""
from waitress import serve
from app import app

if __name__ == "__main__":
    print("Subindo servidor de produção em http://0.0.0.0:5004 ...")
    serve(app, host="0.0.0.0", port=5004, threads=8)