"""
Sistema de movimentação de estoque - backend em tempo real
------------------------------------------------------------------------
- Login por sessão (Flask-Login), senha com hash (werkzeug).
- POST /api/movimento grava direto: log de auditoria + saldo por posição,
  dentro de uma única transação, com lock de linha no saldo.
- Idempotência: se o id_movimento (UUID gerado no coletor) já existe no
  log, a requisição é aceita como "já processado" e não duplica nada -
  isso cobre retry de rede do navegador.
"""

import pymssql
from datetime import datetime
from flask import Flask, request, jsonify, session, render_template, send_from_directory
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import check_password_hash

import config

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY

login_manager = LoginManager(app)
login_manager.login_view = None  # API pura, sem redirect de página

limiter = Limiter(get_remote_address, app=app, default_limits=[])


def get_conn():
    return pymssql.connect(
        server=config.SQL_SERVER,
        port=config.SQL_PORT,
        user=config.SQL_USER,
        password=config.SQL_PASSWORD,
        database=config.SQL_DATABASE,
        as_dict=False,
        tds_version="7.4",   # Azure SQL exige TDS 7.4+; sem isso o FreeTDS do
                             # Linux (Render) falha o handshake TLS e derruba
                             # a conexao com "Adaptive Server connection failed"
        login_timeout=10,
        timeout=30,
    )


# --------------------------------------------------------------------------
# AUTENTICAÇÃO
# --------------------------------------------------------------------------
class Operador(UserMixin):
    def __init__(self, id, nome_completo, login, perfil):
        self.id = id
        self.nome_completo = nome_completo
        self.login = login
        self.perfil = perfil


@login_manager.user_loader
def carregar_operador(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome_completo, login, perfil FROM tb_operadores WHERE id = %s AND ativo = 1",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    if row:
        return Operador(row[0], row[1], row[2], row[3])
    return None


@app.route("/api/login", methods=["POST"])
@limiter.limit("5 per minute")  # trava força bruta de senha
def api_login():
    dados = request.get_json(force=True)
    login = (dados.get("login") or "").strip()
    senha = dados.get("senha") or ""

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome_completo, login, senha_hash, perfil FROM tb_operadores "
        "WHERE login = %s AND ativo = 1",
        (login,),
    )
    row = cur.fetchone()
    conn.close()

    if not row or not check_password_hash(row[3], senha):
        return jsonify({"erro": "Usuário ou senha inválidos."}), 401

    operador = Operador(row[0], row[1], row[2], row[4])
    login_user(operador)
    return jsonify({"nome_completo": operador.nome_completo, "perfil": operador.perfil})


@app.route("/api/logout", methods=["POST"])
@login_required
def api_logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/api/quem-sou-eu", methods=["GET"])
def api_quem_sou_eu():
    if current_user.is_authenticated:
        return jsonify({"logado": True, "nome_completo": current_user.nome_completo})
    return jsonify({"logado": False})


# --------------------------------------------------------------------------
# PRODUTOS
# --------------------------------------------------------------------------
@app.route("/api/produtos/<sku>", methods=["GET"])
@login_required
def buscar_produto(sku):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT sku, descricao FROM tb_produtos WHERE sku = %s", (sku,))
    row = cur.fetchone()
    conn.close()
    if row:
        return jsonify({"sku": row[0], "descricao": row[1]})
    return jsonify({"erro": "não encontrado"}), 404


@app.route("/api/produtos", methods=["POST"])
@login_required
def cadastrar_produto():
    dados = request.get_json(force=True)
    sku = (dados.get("sku") or "").strip()
    descricao = (dados.get("descricao") or "").strip()
    if not sku or not descricao:
        return jsonify({"erro": "sku e descricao são obrigatórios"}), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO tb_produtos (sku, descricao, pendente_validacao) VALUES (%s, %s, 1)",
            (sku, descricao),
        )
        conn.commit()
    except pymssql.IntegrityError:
        # já existe (corrida entre dois coletores bipando o mesmo SKU novo ao mesmo tempo)
        conn.rollback()
    conn.close()
    return jsonify({"sku": sku, "descricao": descricao})


# --------------------------------------------------------------------------
# MOVIMENTO (o coração do sistema)
# --------------------------------------------------------------------------
def aplicar_saldo(cur, sku, posicao, delta, lote=""):
    """Soma/subtrai na tb_saldo_posicao com lock de linha, evitando
    concorrência entre dois movimentos batendo no mesmo saldo ao mesmo tempo."""
    cur.execute(
        "UPDATE tb_saldo_posicao WITH (UPDLOCK, HOLDLOCK) "
        "SET quantidade = quantidade + %s, atualizado_em = SYSDATETIME() "
        "WHERE sku = %s AND posicao = %s AND lote = %s",
        (delta, sku, posicao, lote),
    )
    if cur.rowcount == 0:
        cur.execute(
            "INSERT INTO tb_saldo_posicao (sku, posicao, lote, quantidade, atualizado_em) "
            "VALUES (%s, %s, %s, %s, SYSDATETIME())",
            (sku, posicao, lote, delta),
        )


def extrair_numero_nf(chave_nfe):
    """A chave de acesso da NF-e tem sempre 44 dígitos. O número da nota
    ocupa as posições 26 a 34 (índice 25:34 em base 0)."""
    chave = (chave_nfe or "").strip()
    if len(chave) != 44 or not chave.isdigit():
        return None
    numero = chave[25:34]
    return numero.lstrip("0") or "0"  # remove zeros à esquerda, mas mantém "0" se for só zeros


def garantir_posicao_cadastrada(cur, codigo):
    """Cadastra a posição na tb_posicoes se ainda não existir - sem
    bloquear o movimento, já que o cadastro fechado de posições ainda
    não está pronto."""
    if not codigo:
        return
    cur.execute("SELECT 1 FROM tb_posicoes WHERE codigo = %s", (codigo,))
    if not cur.fetchone():
        try:
            cur.execute("INSERT INTO tb_posicoes (codigo) VALUES (%s)", (codigo,))
        except pymssql.IntegrityError:
            pass  # corrida entre dois coletores bipando a mesma posição nova


@app.route("/api/leitura-camera", methods=["POST"])
@login_required
def registrar_leitura_camera():
    """Registra uma ENTRADA a partir da leitura sequencial de dois QR Codes
    (posição + produto) feita pela câmera no navegador. Não exige NF-e,
    ao contrário de /api/movimento - esse fluxo é guarda física simples,
    sem nota vinculada. Cadastra o produto automaticamente se o SKU lido
    ainda não existir (com a descrição informada na hora pelo operador)."""
    dados = request.get_json(force=True)

    posicao = (dados.get("posicao") or "").strip()
    sku = (dados.get("sku") or "").strip()
    descricao_produto = (dados.get("descricao_produto") or "").strip()
    quantidade = dados.get("quantidade", 1)
    id_movimento = (dados.get("id_movimento") or "").strip()

    if not posicao or not sku or not id_movimento:
        return jsonify({"erro": "posicao, sku e id_movimento são obrigatórios"}), 400

    try:
        quantidade = float(quantidade)
        if quantidade <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"erro": "quantidade inválida"}), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        # ---- idempotência: já processamos essa leitura antes? ----
        cur.execute(
            "SELECT 1 FROM tb_log_auditoria WHERE id_movimento = %s",
            (id_movimento,),
        )
        if cur.fetchone():
            conn.close()
            return jsonify({"ok": True, "duplicado": True})

        garantir_posicao_cadastrada(cur, posicao)

        # ---- produto novo? cadastra na hora (fica pendente_validacao) ----
        cur.execute("SELECT descricao FROM tb_produtos WHERE sku = %s", (sku,))
        row = cur.fetchone()
        if row:
            descricao_produto = row[0]
        else:
            if not descricao_produto:
                descricao_produto = f"Produto {sku} - cadastrado via leitor de câmera"
            try:
                cur.execute(
                    "INSERT INTO tb_produtos (sku, descricao, pendente_validacao) VALUES (%s, %s, 1)",
                    (sku, descricao_produto),
                )
            except pymssql.IntegrityError:
                pass  # corrida entre duas leituras do mesmo SKU novo

        # ---- log de auditoria (chave_nfe vazia = sem nota vinculada) ----
        cur.execute(
            "INSERT INTO tb_log_auditoria "
            "(id_movimento, data_hora_brasilia, tipo_operacao, operador_id, chave_nfe, numero_nf, "
            " sku, descricao_produto, quantidade, posicao_origem, posicao_destino) "
            "VALUES (%s, SYSDATETIME(), 'ENTRADA', %s, '', NULL, %s, %s, %s, NULL, %s)",
            (id_movimento, current_user.id, sku, descricao_produto, quantidade, posicao),
        )

        aplicar_saldo(cur, sku, posicao, quantidade)

        conn.commit()
        return jsonify({
            "ok": True, "duplicado": False,
            "posicao": posicao, "sku": sku,
            "descricao_produto": descricao_produto, "quantidade": quantidade,
        })

    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao registrar leitura de câmera")
        return jsonify({"erro": "falha ao gravar leitura", "detalhe": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/produtos/pendentes", methods=["GET"])
@login_required
def produtos_pendentes():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT sku, descricao FROM tb_produtos WHERE pendente_validacao = 1 ORDER BY sku")
    dados = [{"sku": r[0], "descricao": r[1]} for r in cur.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route("/api/produtos/<sku>/aprovar", methods=["POST"])
@login_required
def aprovar_produto(sku):
    dados = request.get_json(force=True) or {}
    nova_descricao = (dados.get("descricao") or "").strip()

    conn = get_conn()
    cur = conn.cursor()
    try:
        if nova_descricao:
            cur.execute(
                "UPDATE tb_produtos SET descricao = %s, pendente_validacao = 0 WHERE sku = %s",
                (nova_descricao, sku),
            )
        else:
            cur.execute(
                "UPDATE tb_produtos SET pendente_validacao = 0 WHERE sku = %s",
                (sku,),
            )
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"erro": "produto não encontrado"}), 404
        conn.commit()
        return jsonify({"ok": True, "sku": sku})
    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao aprovar produto")
        return jsonify({"erro": "falha ao aprovar", "detalhe": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/produtos/<sku>/rejeitar", methods=["POST"])
@login_required
def rejeitar_produto(sku):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT ISNULL(SUM(quantidade), 0) FROM tb_saldo_posicao WHERE sku = %s",
            (sku,),
        )
        saldo_total = float(cur.fetchone()[0])
        if saldo_total != 0:
            conn.close()
            return jsonify({
                "erro": "esse SKU já tem saldo lançado em alguma posição - "
                        "não pode ser removido, só corrigido (use aprovar com nova descrição)"
            }), 400

        cur.execute("DELETE FROM tb_produtos WHERE sku = %s", (sku,))
        if cur.rowcount == 0:
            conn.close()
            return jsonify({"erro": "produto não encontrado"}), 404
        conn.commit()
        return jsonify({"ok": True, "sku": sku})
    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao rejeitar produto")
        return jsonify({"erro": "falha ao rejeitar", "detalhe": str(e)}), 500
    finally:
        conn.close()


# --------------------------------------------------------------------------
# CONFERÊNCIA (mapa de posições bipadas)
# --------------------------------------------------------------------------
@app.route("/api/conferencia/bipar", methods=["POST"])
@login_required
def conferencia_bipar():
    """Registra que uma posição foi fisicamente conferida hoje - com ou
    sem produto encontrado. Usado tanto pelo modo de conferência (mapa
    que vai ficando verde) quanto pelo leitor de câmera quando nenhuma
    etiqueta de produto é lida a tempo (posição vazia)."""
    dados = request.get_json(force=True)
    posicao = (dados.get("posicao") or "").strip()
    vazio = bool(dados.get("vazio"))
    if not posicao:
        return jsonify({"erro": "posicao é obrigatória"}), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        garantir_posicao_cadastrada(cur, posicao)
        import uuid
        tipo = "CONFERENCIA_VAZIA" if vazio else "CONFERENCIA"
        cur.execute(
            "INSERT INTO tb_log_auditoria "
            "(id_movimento, data_hora_brasilia, tipo_operacao, operador_id, chave_nfe, numero_nf, "
            " sku, descricao_produto, quantidade, posicao_origem, posicao_destino) "
            "VALUES (%s, SYSDATETIME(), %s, %s, '', NULL, NULL, NULL, 0, NULL, %s)",
            (str(uuid.uuid4()), tipo, current_user.id, posicao),
        )
        conn.commit()
        return jsonify({"ok": True, "posicao": posicao, "vazio": vazio})
    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao registrar conferência")
        return jsonify({"erro": "falha ao registrar conferência", "detalhe": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/conferencia/mapa", methods=["GET"])
@login_required
def conferencia_mapa():
    """Lista todas as posições com lado/coluna/nível já separados e se
    foram conferidas hoje - usado para montar o mapa em grade (colunas x
    níveis) que vai ficando verde em tempo real conforme se bipa."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            p.codigo,
            CASE WHEN p.codigo LIKE 'JMK1-BLC-%%' THEN 'BLC'
                 ELSE PARSENAME(REPLACE(p.codigo, '-', '.'), 3) END AS lado,
            CASE WHEN p.codigo LIKE 'JMK1-BLC-%%' THEN NULL
                 ELSE PARSENAME(REPLACE(p.codigo, '-', '.'), 2) END AS coluna,
            CASE WHEN p.codigo LIKE 'JMK1-BLC-%%' THEN NULL
                 ELSE PARSENAME(REPLACE(p.codigo, '-', '.'), 1) END AS nivel,
            CASE WHEN EXISTS (
                SELECT 1 FROM tb_log_auditoria l
                WHERE l.posicao_destino = p.codigo
                  AND l.tipo_operacao IN ('CONFERENCIA', 'CONFERENCIA_VAZIA')
                  AND CAST(l.data_hora_brasilia AS DATE) = CAST(SYSDATETIME() AS DATE)
            ) THEN 1 ELSE 0 END AS conferida
        FROM tb_posicoes p
        ORDER BY p.codigo
    """)
    dados = [
        {"codigo": r[0], "lado": r[1], "coluna": r[2], "nivel": r[3], "conferida": bool(r[4])}
        for r in cur.fetchall()
    ]
    conn.close()
    return jsonify(dados)


@app.route("/aprovacoes")
def pagina_aprovacoes():
    return send_from_directory("templates", "aprovacoes.html")


@app.route("/conferencia")
def pagina_conferencia():
    return send_from_directory("templates", "conferencia.html")


@app.route("/api/movimento", methods=["POST"])
@login_required
def registrar_movimento():
    dados = request.get_json(force=True)

    campos_obrigatorios = ["id_movimento", "tipo_operacao", "sku", "quantidade", "chave_nfe"]
    faltando = [c for c in campos_obrigatorios if not dados.get(c)]
    if faltando:
        return jsonify({"erro": f"campos faltando: {', '.join(faltando)}"}), 400

    tipo = dados["tipo_operacao"]
    if tipo not in ("ENTRADA", "SAIDA", "TRANSFERENCIA"):
        return jsonify({"erro": "tipo_operacao inválido"}), 400

    chave_nfe = dados["chave_nfe"].strip()
    if len(chave_nfe) != 44 or not chave_nfe.isdigit():
        return jsonify({"erro": "chave_nfe inválida: precisa ter 44 dígitos numéricos"}), 400
    numero_nf = extrair_numero_nf(chave_nfe)

    conn = get_conn()
    cur = conn.cursor()
    try:
        # ---- idempotência: já processamos esse id_movimento antes? ----
        cur.execute(
            "SELECT 1 FROM tb_log_auditoria WHERE id_movimento = %s",
            (dados["id_movimento"],),
        )
        if cur.fetchone():
            conn.close()
            return jsonify({"ok": True, "duplicado": True})

        garantir_posicao_cadastrada(cur, dados.get("posicao_origem"))
        garantir_posicao_cadastrada(cur, dados.get("posicao_destino"))

        # ---- log de auditoria (append-only) ----
        cur.execute(
            "INSERT INTO tb_log_auditoria "
            "(id_movimento, data_hora_brasilia, tipo_operacao, operador_id, chave_nfe, numero_nf, "
            " sku, descricao_produto, quantidade, posicao_origem, posicao_destino) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                dados["id_movimento"],
                dados.get("data_hora_brasilia") or datetime.utcnow().isoformat(),
                tipo,
                current_user.id,
                chave_nfe,
                numero_nf,
                dados["sku"],
                dados.get("descricao_produto"),
                dados["quantidade"],
                dados.get("posicao_origem"),
                dados.get("posicao_destino"),
            ),
        )

        # ---- saldo por posição ----
        qtd = float(dados["quantidade"])
        if tipo == "ENTRADA":
            aplicar_saldo(cur, dados["sku"], dados["posicao_destino"], qtd)
        elif tipo == "SAIDA":
            aplicar_saldo(cur, dados["sku"], dados["posicao_origem"], -qtd)
        elif tipo == "TRANSFERENCIA":
            aplicar_saldo(cur, dados["sku"], dados["posicao_origem"], -qtd)
            aplicar_saldo(cur, dados["sku"], dados["posicao_destino"], qtd)

        conn.commit()
        return jsonify({"ok": True, "duplicado": False})

    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao registrar movimento")
        return jsonify({"erro": "falha ao gravar movimento", "detalhe": str(e)}), 500
    finally:
        conn.close()


# --------------------------------------------------------------------------
# PAINEL GERENCIAL
# --------------------------------------------------------------------------
def parseia_codigo_sql():
    """Fragmento reutilizado nas queries: separa 'JMK1-A1-01-3' em
    rua (A), lado (A1) e coluna (01), usando PARSENAME já que o código
    tem exatamente 4 segmentos separados por hífen."""
    return "REPLACE(codigo, '-', '.')"


@app.route("/api/dashboard/resumo", methods=["GET"])
@login_required
def dashboard_resumo():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM tb_posicoes")
    total_posicoes = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(DISTINCT posicao) FROM tb_saldo_posicao WHERE quantidade > 0"
    )
    posicoes_ocupadas = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM tb_produtos")
    total_produtos = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM tb_produtos WHERE pendente_validacao = 1"
    )
    produtos_pendentes = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM tb_log_auditoria "
        "WHERE CAST(data_hora_brasilia AS DATE) = CAST(SYSDATETIME() AS DATE)"
    )
    movimentos_hoje = cur.fetchone()[0]

    cur.execute(
        "SELECT tipo_operacao, COUNT(*) FROM tb_log_auditoria "
        "WHERE CAST(data_hora_brasilia AS DATE) = CAST(SYSDATETIME() AS DATE) "
        "GROUP BY tipo_operacao"
    )
    por_tipo = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute("SELECT COUNT(*) FROM tb_operadores WHERE ativo = 1")
    operadores_ativos = cur.fetchone()[0]

    conn.close()

    pct_ocupacao = round((posicoes_ocupadas / total_posicoes) * 100, 1) if total_posicoes else 0

    return jsonify({
        "total_posicoes": total_posicoes,
        "posicoes_ocupadas": posicoes_ocupadas,
        "posicoes_livres": total_posicoes - posicoes_ocupadas,
        "pct_ocupacao": pct_ocupacao,
        "total_produtos": total_produtos,
        "produtos_pendentes": produtos_pendentes,
        "movimentos_hoje": movimentos_hoje,
        "entradas_hoje": por_tipo.get("ENTRADA", 0),
        "saidas_hoje": por_tipo.get("SAIDA", 0),
        "transferencias_hoje": por_tipo.get("TRANSFERENCIA", 0),
        "operadores_ativos": operadores_ativos,
    })


@app.route("/api/dashboard/movimentos-por-rua", methods=["GET"])
@login_required
def dashboard_movimentos_por_rua():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            CASE WHEN posicao LIKE 'JMK1-BLC-%%' THEN 'BLC'
                 ELSE LEFT(PARSENAME({parseia_codigo_sql()}, 3), 1) END AS rua,
            COUNT(*) AS total
        FROM (
            SELECT posicao_origem AS posicao FROM tb_log_auditoria
            WHERE posicao_origem IS NOT NULL
              AND CAST(data_hora_brasilia AS DATE) = CAST(SYSDATETIME() AS DATE)
            UNION ALL
            SELECT posicao_destino FROM tb_log_auditoria
            WHERE posicao_destino IS NOT NULL
              AND CAST(data_hora_brasilia AS DATE) = CAST(SYSDATETIME() AS DATE)
        ) t
        GROUP BY CASE WHEN posicao LIKE 'JMK1-BLC-%%' THEN 'BLC'
                      ELSE LEFT(PARSENAME({parseia_codigo_sql()}, 3), 1) END
        ORDER BY rua
    """)
    dados = [{"rua": row[0], "total": row[1]} for row in cur.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route("/api/dashboard/timeline", methods=["GET"])
@login_required
def dashboard_timeline():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT CAST(data_hora_brasilia AS DATE) AS dia, COUNT(*) AS total
        FROM tb_log_auditoria
        WHERE data_hora_brasilia >= DATEADD(day, -7, SYSDATETIME())
        GROUP BY CAST(data_hora_brasilia AS DATE)
        ORDER BY dia
    """)
    dados = [{"dia": row[0].isoformat(), "total": row[1]} for row in cur.fetchall()]
    conn.close()
    return jsonify(dados)


@app.route("/api/dashboard/ocupacao", methods=["GET"])
@login_required
def dashboard_ocupacao():
    """Ocupação agregada por rua+lado+coluna (soma dos 7 níveis),
    usada pra desenhar o mapa de calor do armazém."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3) AS lado,
            CAST(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2) AS INT) AS coluna,
            COUNT(DISTINCT CASE WHEN s.quantidade > 0 THEN p.codigo END) AS niveis_ocupados,
            COUNT(DISTINCT p.codigo) AS niveis_totais
        FROM tb_posicoes p
        LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo
        WHERE p.codigo NOT LIKE 'JMK1-BLC-%%'
        GROUP BY PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3),
                 PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2)
    """)
    dados = [
        {
            "lado": row[0], "coluna": row[1],
            "niveis_ocupados": row[2], "niveis_totais": row[3],
        }
        for row in cur.fetchall()
    ]
    conn.close()
    return jsonify(dados)


@app.route("/api/dashboard/sugestao-armazenagem", methods=["GET"])
@login_required
def dashboard_sugestao_armazenagem():
    """Sugere posições livres, priorizando níveis mais baixos
    (mais fáceis de acessar) e distribuindo entre ruas menos usadas hoje."""
    limite = request.args.get("limite", 10, type=int)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT TOP (%s) p.codigo
        FROM tb_posicoes p
        LEFT JOIN tb_saldo_posicao s
            ON s.posicao = p.codigo AND s.quantidade > 0
        WHERE s.id IS NULL AND p.codigo NOT LIKE 'JMK1-BLC-%%'
        ORDER BY CAST(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 1) AS INT) ASC,
                 NEWID()
    """, (limite,))
    sugestoes = [row[0] for row in cur.fetchall()]
    conn.close()
    return jsonify(sugestoes)


@app.route("/api/dashboard/movimentos-recentes", methods=["GET"])
@login_required
def dashboard_movimentos_recentes():
    limite = request.args.get("limite", 20, type=int)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP (%s) l.data_hora_brasilia, l.tipo_operacao, l.sku, l.descricao_produto,
               l.quantidade, l.posicao_origem, l.posicao_destino, l.numero_nf, o.nome_completo
        FROM tb_log_auditoria l
        JOIN tb_operadores o ON o.id = l.operador_id
        ORDER BY l.id DESC
    """, (limite,))
    dados = [
        {
            "data_hora": row[0], "tipo_operacao": row[1], "sku": row[2],
            "descricao_produto": row[3], "quantidade": float(row[4]),
            "posicao_origem": row[5], "posicao_destino": row[6],
            "numero_nf": row[7], "operador": row[8],
        }
        for row in cur.fetchall()
    ]
    conn.close()
    return jsonify(dados)


@app.route("/api/dashboard/avisos", methods=["GET"])
@login_required
def dashboard_avisos():
    conn = get_conn()
    cur = conn.cursor()
    avisos = []

    cur.execute("SELECT COUNT(*) FROM tb_produtos WHERE pendente_validacao = 1")
    pendentes = cur.fetchone()[0]
    if pendentes > 0:
        avisos.append({
            "tipo": "warning",
            "titulo": f"{pendentes} produto(s) pendente(s) de validação",
            "descricao": "Cadastrados na hora pelo coletor - confira se a descrição está correta.",
        })

    # divergências de inventário aguardando decisão do admin
    try:
        cur.execute("SELECT COUNT(*) FROM tb_inventario WHERE status = 'PENDENTE'")
        div_pend = cur.fetchone()[0]
        if div_pend > 0:
            avisos.append({
                "tipo": "danger",
                "titulo": f"{div_pend} divergência(s) de inventário aguardando aprovação",
                "descricao": "Contagens com diferença entre o físico e o sistema - revise no painel.",
            })
    except Exception:
        pass  # tabela de inventário ainda não criada

    # posições com estoque parado há mais de 30 dias (sem nenhuma movimentação)
    cur.execute("""
        SELECT COUNT(*) FROM tb_saldo_posicao s
        WHERE s.quantidade > 0
          AND NOT EXISTS (
            SELECT 1 FROM tb_log_auditoria l
            WHERE (l.posicao_origem = s.posicao OR l.posicao_destino = s.posicao)
              AND l.data_hora_brasilia >= DATEADD(day, -30, SYSDATETIME())
          )
    """)
    paradas = cur.fetchone()[0]
    if paradas > 0:
        avisos.append({
            "tipo": "warning",
            "titulo": f"{paradas} posição(ões) com estoque parado há 30+ dias",
            "descricao": "Sem nenhuma movimentação no período - candidatas a inventário rotativo.",
        })

    cur.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3) AS lado,
                   PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2) AS coluna,
                   COUNT(*) AS totais,
                   SUM(CASE WHEN s.quantidade > 0 THEN 1 ELSE 0 END) AS ocupados
            FROM tb_posicoes p
            LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo
            WHERE p.codigo NOT LIKE 'JMK1-BLC-%%'
            GROUP BY PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3),
                     PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2)
            HAVING COUNT(*) = SUM(CASE WHEN s.quantidade > 0 THEN 1 ELSE 0 END)
        ) t
    """)
    colunas_cheias = cur.fetchone()[0]
    if colunas_cheias > 0:
        avisos.append({
            "tipo": "danger",
            "titulo": f"{colunas_cheias} coluna(s) 100% lotada(s)",
            "descricao": "Todos os 7 níveis ocupados - considere redistribuir estoque.",
        })

    cur.execute("SELECT COUNT(*) FROM tb_posicoes")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT posicao) FROM tb_saldo_posicao WHERE quantidade > 0")
    ocupadas = cur.fetchone()[0]
    pct = (ocupadas / total * 100) if total else 0
    if pct >= 85:
        avisos.append({
            "tipo": "danger",
            "titulo": f"Armazém com {pct:.1f}% de ocupação",
            "descricao": "Capacidade crítica - avalie giro de estoque ou expansão.",
        })
    elif pct >= 70:
        avisos.append({
            "tipo": "warning",
            "titulo": f"Armazém com {pct:.1f}% de ocupação",
            "descricao": "Aproximando da capacidade máxima.",
        })

    if not avisos:
        avisos.append({
            "tipo": "success",
            "titulo": "Tudo certo por aqui",
            "descricao": "Nenhum alerta no momento.",
        })

    conn.close()
    return jsonify(avisos)


@app.route("/api/dashboard/funil", methods=["GET"])
@login_required
def dashboard_funil():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            COUNT(*) AS colunas_totais,
            SUM(CASE WHEN ocupados > 0 THEN 1 ELSE 0 END) AS colunas_com_estoque,
            SUM(CASE WHEN ocupados * 1.0 / totais >= 0.9 THEN 1 ELSE 0 END) AS colunas_quase_cheias,
            SUM(CASE WHEN ocupados = totais THEN 1 ELSE 0 END) AS colunas_cheias
        FROM (
            SELECT PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3) AS lado,
                   PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2) AS coluna,
                   COUNT(*) AS totais,
                   SUM(CASE WHEN s.quantidade > 0 THEN 1 ELSE 0 END) AS ocupados
            FROM tb_posicoes p
            LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo
            WHERE p.codigo NOT LIKE 'JMK1-BLC-%%'
            GROUP BY PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3),
                     PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2)
        ) t
    """)
    row = cur.fetchone()
    conn.close()
    return jsonify({
        "colunas_totais": row[0] or 0,
        "colunas_com_estoque": row[1] or 0,
        "colunas_quase_cheias": row[2] or 0,
        "colunas_cheias": row[3] or 0,
    })


@app.route("/api/simulador-alocacao", methods=["GET"])
@login_required
def simulador_alocacao():
    sku = request.args.get("sku", "").strip()
    quantidade = request.args.get("quantidade", type=float)
    if not sku:
        return jsonify({"erro": "informe o código do produto"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT descricao FROM tb_produtos WHERE sku = %s", (sku,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"erro": "produto não encontrado"}), 404
    descricao = row[0]

    cur.execute(
        "SELECT posicao, quantidade FROM tb_saldo_posicao "
        "WHERE sku = %s AND quantidade > 0 ORDER BY quantidade ASC",
        (sku,),
    )
    consolidar = [{"codigo": r[0], "quantidade_atual": float(r[1])} for r in cur.fetchall()]

    cur.execute(f"""
        SELECT TOP 5 p.codigo
        FROM tb_posicoes p
        LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo AND s.quantidade > 0
        WHERE s.id IS NULL AND p.codigo NOT LIKE 'JMK1-BLC-%%'
        ORDER BY CAST(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 1) AS INT) ASC,
                 NEWID()
    """)
    livres = [r[0] for r in cur.fetchall()]
    conn.close()

    return jsonify({
        "sku": sku, "descricao": descricao, "quantidade_informada": quantidade,
        "consolidar": consolidar[:5], "sugestoes_livres": livres,
    })


@app.route("/api/dashboard/movimentos-filtro", methods=["GET"])
@login_required
def dashboard_movimentos_filtro():
    numero_nf = request.args.get("numero_nf", "").strip()
    sku = request.args.get("sku", "").strip()
    rua = request.args.get("rua", "").strip().upper()
    posicao = request.args.get("posicao", "").strip().upper()
    tipo = request.args.get("tipo", "").strip().upper()

    condicoes = []
    params = []
    if numero_nf:
        condicoes.append("l.numero_nf LIKE %s")
        params.append(f"%{numero_nf}%")
    if sku:
        condicoes.append("(l.sku LIKE %s OR l.descricao_produto LIKE %s)")
        params += [f"%{sku}%", f"%{sku}%"]
    if rua:
        condicoes.append(
            "LEFT(PARSENAME(REPLACE(ISNULL(l.posicao_destino, l.posicao_origem), '-', '.'), 3), 1) = %s"
        )
        params.append(rua)
    if posicao:
        condicoes.append("(l.posicao_origem LIKE %s OR l.posicao_destino LIKE %s)")
        params += [f"%{posicao}%", f"%{posicao}%"]
    if tipo:
        condicoes.append("l.tipo_operacao = %s")
        params.append(tipo)

    where = ("WHERE " + " AND ".join(condicoes)) if condicoes else ""
    sql = f"""
        SELECT TOP 100 l.data_hora_brasilia, l.tipo_operacao, l.sku, l.descricao_produto,
               l.quantidade, l.posicao_origem, l.posicao_destino, l.numero_nf, o.nome_completo
        FROM tb_log_auditoria l
        JOIN tb_operadores o ON o.id = l.operador_id
        {where}
        ORDER BY l.id DESC
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    dados = [
        {
            "data_hora": row[0], "tipo_operacao": row[1], "sku": row[2],
            "descricao_produto": row[3], "quantidade": float(row[4]),
            "posicao_origem": row[5], "posicao_destino": row[6],
            "numero_nf": row[7], "operador": row[8],
        }
        for row in cur.fetchall()
    ]
    conn.close()
    return jsonify(dados)



@login_required
def dashboard_ruas_resumo():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            CASE WHEN p.codigo LIKE 'JMK1-BLC-%%' THEN 'BLC'
                 ELSE LEFT(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3), 1) END AS rua,
            COUNT(DISTINCT p.codigo) AS total,
            COUNT(DISTINCT CASE WHEN s.quantidade > 0 THEN p.codigo END) AS ocupadas
        FROM tb_posicoes p
        LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo
        GROUP BY CASE WHEN p.codigo LIKE 'JMK1-BLC-%%' THEN 'BLC'
                      ELSE LEFT(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3), 1) END
        ORDER BY rua
    """)
    dados = []
    for row in cur.fetchall():
        total, ocupadas = row[1], row[2]
        dados.append({
            "rua": row[0], "total": total, "ocupadas": ocupadas,
            "livres": total - ocupadas,
            "pct": round((ocupadas / total) * 100, 1) if total else 0,
        })
    conn.close()
    return jsonify(dados)


# --------------------------------------------------------------------------
# INVENTÁRIO ROTATIVO
# --------------------------------------------------------------------------
@app.route("/api/inventario/contagem", methods=["POST"])
@login_required
def inventario_contagem():
    dados = request.get_json(force=True)
    obrigatorios = ["id_contagem", "posicao", "sku", "qtd_contada"]
    faltando = [c for c in obrigatorios if dados.get(c) in (None, "")]
    if faltando:
        return jsonify({"erro": f"campos faltando: {', '.join(faltando)}"}), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM tb_inventario WHERE id_contagem = %s", (dados["id_contagem"],))
        if cur.fetchone():
            conn.close()
            return jsonify({"ok": True, "duplicado": True})

        garantir_posicao_cadastrada(cur, dados["posicao"])

        cur.execute(
            "SELECT ISNULL(SUM(quantidade), 0) FROM tb_saldo_posicao WHERE posicao = %s AND sku = %s",
            (dados["posicao"], dados["sku"]),
        )
        qtd_sistema = float(cur.fetchone()[0])
        qtd_contada = float(dados["qtd_contada"])
        divergencia = qtd_contada - qtd_sistema
        status = "SEM_DIVERGENCIA" if divergencia == 0 else "PENDENTE"

        cur.execute(
            "INSERT INTO tb_inventario (id_contagem, data_hora, posicao, sku, qtd_contada, "
            "qtd_sistema, divergencia, operador_id, status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                dados["id_contagem"],
                dados.get("data_hora") or datetime.utcnow().isoformat(),
                dados["posicao"], dados["sku"],
                qtd_contada, qtd_sistema, divergencia,
                current_user.id, status,
            ),
        )
        conn.commit()
        return jsonify({
            "ok": True, "duplicado": False,
            "qtd_sistema": qtd_sistema, "divergencia": divergencia, "status": status,
        })
    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao registrar contagem")
        return jsonify({"erro": "falha ao registrar contagem", "detalhe": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/inventario/divergencias", methods=["GET"])
@login_required
def inventario_divergencias():
    status = request.args.get("status", "PENDENTE").upper()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT TOP 100 i.id, i.data_hora, i.posicao, i.sku, p.descricao,
               i.qtd_contada, i.qtd_sistema, i.divergencia, o.nome_completo, i.status
        FROM tb_inventario i
        JOIN tb_operadores o ON o.id = i.operador_id
        LEFT JOIN tb_produtos p ON p.sku = i.sku
        WHERE i.status = %s
        ORDER BY i.id DESC
    """, (status,))
    dados = [
        {
            "id": r[0], "data_hora": str(r[1]), "posicao": r[2], "sku": r[3],
            "descricao": r[4], "qtd_contada": float(r[5]), "qtd_sistema": float(r[6]),
            "divergencia": float(r[7]), "operador": r[8], "status": r[9],
        }
        for r in cur.fetchall()
    ]
    conn.close()
    return jsonify(dados)


@app.route("/api/inventario/<int:inv_id>/decidir", methods=["POST"])
@login_required
def inventario_decidir(inv_id):
    """Aprova (gera AJUSTE_INVENTARIO no log e corrige o saldo) ou rejeita."""
    if current_user.perfil != "admin":
        return jsonify({"erro": "apenas administradores podem aprovar ajustes"}), 403

    dados = request.get_json(force=True)
    decisao = (dados.get("decisao") or "").upper()
    if decisao not in ("APROVAR", "REJEITAR"):
        return jsonify({"erro": "decisao deve ser APROVAR ou REJEITAR"}), 400

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT posicao, sku, divergencia, status FROM tb_inventario WHERE id = %s",
            (inv_id,),
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return jsonify({"erro": "contagem não encontrada"}), 404
        posicao, sku, divergencia, status = row[0], row[1], float(row[2]), row[3]
        if status != "PENDENTE":
            conn.close()
            return jsonify({"erro": f"contagem já está com status {status}"}), 400

        if decisao == "APROVAR":
            import uuid
            cur.execute(
                "INSERT INTO tb_log_auditoria (id_movimento, data_hora_brasilia, tipo_operacao, "
                "operador_id, chave_nfe, numero_nf, sku, quantidade, posicao_origem, posicao_destino) "
                "VALUES (%s, SYSDATETIME(), 'AJUSTE_INVENTARIO', %s, '', NULL, %s, %s, %s, %s)",
                (
                    str(uuid.uuid4()), current_user.id, sku, abs(divergencia),
                    posicao if divergencia < 0 else None,
                    posicao if divergencia > 0 else None,
                ),
            )
            aplicar_saldo(cur, sku, posicao, divergencia)
            novo_status = "APROVADO"
        else:
            novo_status = "REJEITADO"

        cur.execute(
            "UPDATE tb_inventario SET status = %s, aprovado_por = %s, aprovado_em = SYSDATETIME() WHERE id = %s",
            (novo_status, current_user.id, inv_id),
        )
        conn.commit()
        return jsonify({"ok": True, "status": novo_status})
    except Exception as e:
        conn.rollback()
        app.logger.exception("Erro ao decidir contagem")
        return jsonify({"erro": "falha ao processar decisão", "detalhe": str(e)}), 500
    finally:
        conn.close()


# --------------------------------------------------------------------------
# RELATÓRIOS (CSV para abrir no Excel)
# --------------------------------------------------------------------------
import csv as csv_mod
import io
from flask import Response


def gerar_csv(colunas, linhas, nome_arquivo):
    buf = io.StringIO()
    escritor = csv_mod.writer(buf, delimiter=";")
    escritor.writerow(colunas)
    escritor.writerows(linhas)
    conteudo = "\ufeff" + buf.getvalue()  # BOM p/ acentuação no Excel
    return Response(
        conteudo, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={nome_arquivo}"},
    )


@app.route("/api/relatorio/movimentos.csv")
@login_required
def relatorio_movimentos():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT l.data_hora_brasilia, l.tipo_operacao, l.sku, l.descricao_produto,
               l.quantidade, l.posicao_origem, l.posicao_destino, l.numero_nf, o.nome_completo
        FROM tb_log_auditoria l JOIN tb_operadores o ON o.id = l.operador_id
        ORDER BY l.id DESC
    """)
    linhas = [[str(c) if c is not None else "" for c in r] for r in cur.fetchall()]
    conn.close()
    return gerar_csv(
        ["Data/Hora", "Tipo", "SKU", "Produto", "Qtd", "Origem", "Destino", "NF", "Operador"],
        linhas, "movimentos.csv",
    )


@app.route("/api/relatorio/saldo.csv")
@login_required
def relatorio_saldo():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT s.posicao, s.sku, p.descricao, s.quantidade, s.atualizado_em
        FROM tb_saldo_posicao s LEFT JOIN tb_produtos p ON p.sku = s.sku
        WHERE s.quantidade > 0 ORDER BY s.posicao
    """)
    linhas = [[str(c) if c is not None else "" for c in r] for r in cur.fetchall()]
    conn.close()
    return gerar_csv(["Posição", "SKU", "Produto", "Qtd", "Atualizado em"], linhas, "saldo_por_posicao.csv")


@app.route("/api/relatorio/inventario.csv")
@login_required
def relatorio_inventario():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT i.data_hora, i.posicao, i.sku, p.descricao, i.qtd_contada,
               i.qtd_sistema, i.divergencia, i.status, o.nome_completo
        FROM tb_inventario i
        JOIN tb_operadores o ON o.id = i.operador_id
        LEFT JOIN tb_produtos p ON p.sku = i.sku
        ORDER BY i.id DESC
    """)
    linhas = [[str(c) if c is not None else "" for c in r] for r in cur.fetchall()]
    conn.close()
    return gerar_csv(
        ["Data/Hora", "Posição", "SKU", "Produto", "Contado", "Sistema", "Divergência", "Status", "Operador"],
        linhas, "inventario.csv",
    )


@app.route("/api/dashboard/ranking-ruas", methods=["GET"])
@login_required
def dashboard_ranking_ruas():
    dias = request.args.get("dias", 7, type=int)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            CASE WHEN posicao LIKE 'JMK1-BLC-%%' THEN 'BLC'
                 ELSE LEFT(PARSENAME(REPLACE(posicao, '-', '.'), 3), 1) END AS rua,
            COUNT(*) AS total
        FROM (
            SELECT posicao_origem AS posicao, data_hora_brasilia FROM tb_log_auditoria WHERE posicao_origem IS NOT NULL
            UNION ALL
            SELECT posicao_destino, data_hora_brasilia FROM tb_log_auditoria WHERE posicao_destino IS NOT NULL
        ) t
        WHERE data_hora_brasilia >= DATEADD(day, -%s, SYSDATETIME())
        GROUP BY CASE WHEN posicao LIKE 'JMK1-BLC-%%' THEN 'BLC'
                      ELSE LEFT(PARSENAME(REPLACE(posicao, '-', '.'), 3), 1) END
        ORDER BY total DESC
    """, (dias,))
    linhas = cur.fetchall()
    total_geral = sum(r[1] for r in linhas) or 1
    dados = [{"rua": r[0], "total": r[1], "pct": round(r[1] / total_geral * 100, 1)} for r in linhas]
    conn.close()
    return jsonify(dados)


@app.route("/api/dashboard/rua/<rua>/detalhe", methods=["GET"])
@login_required
def dashboard_rua_detalhe(rua):
    conn = get_conn()
    cur = conn.cursor()

    if rua == "BLC":
        cur.execute("""
            SELECT p.codigo, s.quantidade, s.sku, pr.descricao
            FROM tb_posicoes p
            LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo AND s.quantidade > 0
            LEFT JOIN tb_produtos pr ON pr.sku = s.sku
            WHERE p.codigo LIKE 'JMK1-BLC-%%'
            ORDER BY p.codigo
        """)
        posicoes = [
            {
                "codigo": row[0], "lado": "BLC", "coluna": None, "nivel": None,
                "ocupado": row[1] is not None,
                "quantidade": float(row[1]) if row[1] else 0,
                "sku": row[2], "descricao": row[3],
            }
            for row in cur.fetchall()
        ]
        conn.close()
        return jsonify({"rua": "BLC", "posicoes": posicoes})

    if len(rua) != 1 or not rua.isalpha():
        conn.close()
        return jsonify({"erro": "rua inválida"}), 400

    cur.execute(f"""
        SELECT p.codigo,
               PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 3) AS lado,
               CAST(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 2) AS INT) AS coluna,
               CAST(PARSENAME({parseia_codigo_sql().replace('codigo', 'p.codigo')}, 1) AS INT) AS nivel,
               s.quantidade, s.sku, pr.descricao
        FROM tb_posicoes p
        LEFT JOIN tb_saldo_posicao s ON s.posicao = p.codigo AND s.quantidade > 0
        LEFT JOIN tb_produtos pr ON pr.sku = s.sku
        WHERE p.codigo LIKE %s AND p.codigo NOT LIKE 'JMK1-BLC-%%'
        ORDER BY lado, coluna, nivel
    """, (f"JMK1-{rua.upper()}%",))

    posicoes = [
        {
            "codigo": row[0], "lado": row[1], "coluna": row[2], "nivel": row[3],
            "ocupado": row[4] is not None,
            "quantidade": float(row[4]) if row[4] else 0,
            "sku": row[5], "descricao": row[6],
        }
        for row in cur.fetchall()
    ]
    conn.close()
    return jsonify({"rua": rua.upper(), "posicoes": posicoes})


@app.route("/estoque-visual")
def pagina_estoque_visual():
    return send_from_directory("templates", "estoque_visual.html")


@app.route("/dashboard")
def pagina_dashboard():
    return send_from_directory("templates", "dashboard.html")


@app.route("/leitor")
def pagina_leitor_camera():
    return send_from_directory("templates", "leitor_camera.html")


# --------------------------------------------------------------------------
# SERVE O COLETOR (arquivo estático)
# --------------------------------------------------------------------------
@app.route("/coletor")
def pagina_coletor():
    return send_from_directory("templates", "coletor.html")


if __name__ == "__main__":
    # host 0.0.0.0 para ser acessível pelo coletor Android na rede local
    app.run(host="0.0.0.0", port=5004, debug=False)