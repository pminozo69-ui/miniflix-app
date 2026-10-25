import asyncio
import json
import logging
import os
import re
import sys
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
import psycopg2
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# --- CREDENCIAIS E CONFIGURAÇÕES ---
TOKEN = "8659249610:AAH2jGbYgz1ngToEtZeVxs8fzvFMfXb0Ykk"
WEBAPP_URL = "https://pminozo69-ui.github.io/miniflix-app/"
DATABASE_URL = "postgresql://postgres.vmdqbjmyemhxmmcixskf:cC2%254x5V%21gthfD%21@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"
TMDB_API_KEY = "c5fd7ff31feef7e7e9b27247c7aff0a1"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w500"

ITENS_POR_PAGINA = 8
ADMIN_ID = 0

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def normalizar(texto: str) -> str:
    if not texto:
        return ""
    return unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode("utf-8").lower().strip()

def formatar_numero(valor: float) -> str:
    val = float(valor)
    if val.is_integer():
        return f"{int(val):02d}"
    return f"{val:.1f}".rstrip('0').rstrip('.')

def formatar_temporada(valor: float) -> str:
    val = float(valor)
    if val == 0:
        return "⭐ Especiais"
    if val.is_integer():
        return f"Temporada {int(val)}"
    return f"Temporada {val:.1f}".rstrip('0').rstrip('.')

def formatar_valor_json(valor: float):
    val = float(valor)
    return int(val) if val.is_integer() else round(val, 1)

# --- INTEGRAÇÃO COM TMDB ---
def buscar_metadados_tmdb(nome_titulo: str, categoria: str):
    tipo_busca = "movie" if categoria == "filme" else "tv"
    url_busca = f"https://api.themoviedb.org/3/search/{tipo_busca}"
    params = {
        "api_key": TMDB_API_KEY,
        "query": nome_titulo,
        "language": "pt-BR",
        "include_adult": "false"
    }

    try:
        resp = requests.get(url_busca, params=params, timeout=10)
        dados = resp.json()
        resultados = dados.get("results", [])

        if not resultados:
            return None, None, None, None, None, [], None

        item = resultados[0]
        tmdb_id = item.get("id")

        url_detalhes = f"https://api.themoviedb.org/3/{tipo_busca}/{tmdb_id}"
        params_detalhes = {
            "api_key": TMDB_API_KEY,
            "language": "pt-BR",
            "append_to_response": "videos"
        }
        resp_det = requests.get(url_detalhes, params=params_detalhes, timeout=10)
        detalhes = resp_det.json() if resp_det.status_code == 200 else item

        poster_path = detalhes.get("poster_path") or item.get("poster_path")
        poster_url = f"{TMDB_IMG_BASE}{poster_path}" if poster_path else "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=300"
        sinopse = detalhes.get("overview") or item.get("overview") or "Sem sinopse cadastrada."
        
        data_lanc = detalhes.get("release_date") or detalhes.get("first_air_date") or ""
        ano = int(data_lanc.split("-")[0]) if "-" in data_lanc else None

        nota_bruta = detalhes.get("vote_average", 0.0)
        nota = round(float(nota_bruta), 1) if nota_bruta else None

        duracao = ""
        if tipo_busca == "movie":
            runtime = detalhes.get("runtime")
            if runtime:
                horas = runtime // 60
                mins = runtime % 60
                duracao = f"{horas}h {mins:02d}m" if horas > 0 else f"{mins}m"
        else:
            ep_runtimes = detalhes.get("episode_run_time") or []
            if ep_runtimes:
                duracao = f"~{ep_runtimes[0]}m/ep"
            else:
                duracao = "Série"

        generos = [g.get("name") for g in detalhes.get("genres", []) if g.get("name")]

        trailer_key = None
        videos = detalhes.get("videos", {}).get("results", [])
        for v in videos:
            if v.get("site") == "YouTube" and v.get("type") in ["Trailer", "Teaser"]:
                trailer_key = v.get("key")
                break

        return poster_url, sinopse, ano, nota, duracao, generos, trailer_key

    except Exception as e:
        logging.error(f"Erro ao consultar TMDB: {e}")
        return None, None, None, None, None, [], None

# --- BANCO DE DADOS ---
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS titulos (
            id SERIAL PRIMARY KEY,
            categoria TEXT NOT NULL,
            nome TEXT UNIQUE NOT NULL,
            nome_busca TEXT NOT NULL,
            poster_url TEXT,
            sinopse TEXT,
            ano INTEGER,
            nota NUMERIC(3,1),
            duracao TEXT,
            generos TEXT,
            trailer_key TEXT,
            audio TEXT,
            selo TEXT DEFAULT 'NOVO',
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS arquivos (
            id SERIAL PRIMARY KEY,
            titulo_id INTEGER REFERENCES titulos(id) ON DELETE CASCADE,
            temporada NUMERIC(4, 1) NOT NULL,
            episodio NUMERIC(4, 1) NOT NULL,
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            UNIQUE(chat_id, message_id)
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS arquivos_rar (
            id SERIAL PRIMARY KEY,
            titulo_id INTEGER REFERENCES titulos(id) ON DELETE CASCADE,
            temporada NUMERIC(4, 1) DEFAULT 0.0 NOT NULL,
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, message_id)
        );
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            user_id BIGINT PRIMARY KEY,
            primeiro_acesso TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    c.close()
    conn.close()

# --- SERVIDOR WEB NATIVO ---
class ServidorWebHandler(BaseHTTPRequestHandler):
    def _definir_headers_cors(self, status=200, content_type="application/json; charset=utf-8", length=0):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Content-Type", content_type)
        if length > 0:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def do_OPTIONS(self):
        self._definir_headers_cors(200, "text/plain", 0)

    def do_GET(self):
        parsed = urlparse(self.path)
        
        # 1. Rota de disparo de vídeo único
        if parsed.path == "/api/play":
            try:
                params = parse_qs(parsed.query)
                user_id = int(params.get("user_id", [0])[0])
                titulo_id = int(params.get("titulo_id", [0])[0])
                tipo = params.get("tipo", ["filme"])[0]
                temp = float(params.get("temporada", [0.0])[0])
                ep = float(params.get("episodio", [0.0])[0])

                if user_id and titulo_id:
                    conn = get_db_connection()
                    c = conn.cursor()
                    
                    if tipo == "filme" or (temp == 0.0 and ep == 0.0):
                        c.execute("SELECT id, chat_id, message_id FROM arquivos WHERE titulo_id = %s ORDER BY id DESC LIMIT 1;", (titulo_id,))
                    else:
                        c.execute(
                            "SELECT id, chat_id, message_id FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s ORDER BY id DESC LIMIT 1;",
                            (titulo_id, temp, ep)
                        )
                    
                    arq = c.fetchone()
                    if arq:
                        arq_id, chat_origem, msg_origem = arq
                        
                        reply_markup = None
                        if tipo != "filme":
                            c.execute(
                                """SELECT id, episodio FROM arquivos 
                                   WHERE titulo_id = %s AND temporada = %s AND episodio > %s 
                                   ORDER BY episodio ASC LIMIT 1;""",
                                (titulo_id, temp, ep)
                            )
                            prox = c.fetchone()
                            if prox:
                                label_prox = formatar_numero(prox[1])
                                reply_markup = {
                                    "inline_keyboard": [[{
                                        "text": f"▶️ Próximo (Ep {label_prox})",
                                        "callback_data": f"play:{prox[0]}"
                                    }]]
                                }
                        
                        c.close()
                        conn.close()

                        payload = {
                            "chat_id": user_id,
                            "from_chat_id": chat_origem,
                            "message_id": msg_origem
                        }
                        if reply_markup:
                            payload["reply_markup"] = reply_markup

                        requests.post(f"https://api.telegram.org/bot{TOKEN}/copyMessage", json=payload, timeout=5)

                        corpo = json.dumps({"ok": True}).encode("utf-8")
                        self._definir_headers_cors(200, "application/json; charset=utf-8", len(corpo))
                        self.wfile.write(corpo)
                        return
                    
                    c.close()
                    conn.close()
            except Exception as e:
                logging.error(f"Erro na rota /api/play: {e}")

            corpo = json.dumps({"ok": False}).encode("utf-8")
            self._definir_headers_cors(200, "application/json; charset=utf-8", len(corpo))
            self.wfile.write(corpo)

        # 2. Rota de disparo de PACOTE COMPACTADO (.RAR)
        elif parsed.path == "/api/play_rar":
            try:
                params = parse_qs(parsed.query)
                user_id = int(params.get("user_id", [0])[0])
                titulo_id = int(params.get("titulo_id", [0])[0])
                temp = float(params.get("temporada", [0.0])[0])

                if user_id and titulo_id:
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute(
                        "SELECT chat_id, message_id FROM arquivos_rar WHERE titulo_id = %s AND temporada = %s ORDER BY id ASC;",
                        (titulo_id, temp)
                    )
                    partes = c.fetchall()
                    c.close()
                    conn.close()

                    if partes:
                        for chat_origem, msg_origem in partes:
                            payload = {
                                "chat_id": user_id,
                                "from_chat_id": chat_origem,
                                "message_id": msg_origem
                            }
                            requests.post(f"https://api.telegram.org/bot{TOKEN}/copyMessage", json=payload, timeout=5)

                        corpo = json.dumps({"ok": True, "count": len(partes)}).encode("utf-8")
                        self._definir_headers_cors(200, "application/json; charset=utf-8", len(corpo))
                        self.wfile.write(corpo)
                        return
            except Exception as e:
                logging.error(f"Erro na rota /api/play_rar: {e}")

            corpo = json.dumps({"ok": False}).encode("utf-8")
            self._definir_headers_cors(200, "application/json; charset=utf-8", len(corpo))
            self.wfile.write(corpo)

        # 3. Rota do Catálogo com Pacotes .RAR mapeados
        elif parsed.path == "/api/catalogo":
            try:
                conn = get_db_connection()
                c = conn.cursor()
                
                # Buscar Títulos e Episódios Normais
                c.execute("""
                    SELECT t.id, t.categoria, t.nome, t.poster_url, t.sinopse, t.ano,
                           t.nota, t.duracao, t.generos, t.trailer_key, t.audio,
                           a.temporada, a.episodio, COALESCE(t.selo, 'NOVO')
                    FROM titulos t
                    LEFT JOIN arquivos a ON t.id = a.titulo_id
                    ORDER BY COALESCE(t.atualizado_em, t.criado_em) DESC, a.temporada ASC, a.episodio ASC;
                """)
                linhas = c.fetchall()

                # Buscar quais temporadas/filmes têm pacotes RAR
                c.execute("SELECT titulo_id, temporada FROM arquivos_rar GROUP BY titulo_id, temporada;")
                rars = c.fetchall()
                c.close()
                conn.close()

                rar_map = {}
                for t_id_rar, temp_rar in rars:
                    if t_id_rar not in rar_map:
                        rar_map[t_id_rar] = []
                    rar_map[t_id_rar].append(formatar_valor_json(temp_rar))

                titulos_map = {}
                for r in linhas:
                    t_id, cat, nome, poster, sinopse, ano, nota, duracao, generos_raw, trailer_key, audio, temp, ep, selo_val = r
                    
                    if t_id not in titulos_map:
                        generos_list = []
                        if generos_raw:
                            try:
                                generos_list = json.loads(generos_raw) if generos_raw.startswith("[") else [g.strip() for g in generos_raw.split(",") if g.strip()]
                            except Exception:
                                generos_list = []

                        titulos_map[t_id] = {
                            "id": t_id,
                            "tipo": cat,
                            "titulo": nome,
                            "ano": ano or "N/A",
                            "nota": float(nota) if nota else None,
                            "duracao": duracao or "",
                            "generos": generos_list,
                            "trailer_key": trailer_key or "",
                            "audio": audio or "Dublado",
                            "sinopse": sinopse or "Sem sinopse cadastrada.",
                            "img": poster or "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=300",
                            "selo": selo_val,
                            "temporadas": {},
                            "rar_packs": rar_map.get(t_id, [])
                        }
                    
                    if cat != "filme" and temp is not None and ep is not None:
                        temp_str = str(formatar_valor_json(temp))
                        ep_formatado = formatar_valor_json(ep)
                        if temp_str not in titulos_map[t_id]["temporadas"]:
                            titulos_map[t_id]["temporadas"][temp_str] = []
                        if ep_formatado not in titulos_map[t_id]["temporadas"][temp_str]:
                            titulos_map[t_id]["temporadas"][temp_str].append(ep_formatado)

                corpo = json.dumps(list(titulos_map.values()), ensure_ascii=False).encode("utf-8")
                self._definir_headers_cors(200, "application/json; charset=utf-8", len(corpo))
                self.wfile.write(corpo)
            except Exception as e:
                logging.error(f"Erro na rota /api/catalogo: {e}")
                err_msg = str(e).encode("utf-8")
                self._definir_headers_cors(500, "text/plain; charset=utf-8", len(err_msg))
                self.wfile.write(err_msg)
        else:
            msg = b"Miniflix API Online!"
            self._definir_headers_cors(200, "text/plain", len(msg))
            self.wfile.write(msg)

    def log_message(self, format, *args):
        return

def iniciar_servidor_web():
    porta = int(os.environ.get("PORT", 8080))
    httpd = HTTPServer(("0.0.0.0", porta), ServidorWebHandler)
    logging.info(f"--> [API + WEB] Servidor HTTP rodando na porta {porta}")
    httpd.serve_forever()

# --- INGESTÃO AUTOMÁTICA DE VÍDEOS E PACOTES RAR ---
def salvar_ou_atualizar_midia(texto_completo: str, chat_id: int, message_id: int):
    if not texto_completo.startswith("#"):
        return False, False, {}

    texto_lower = texto_completo.lower()
    primeira_linha = texto_completo.split("\n")[0].strip()

    audio = "Dublado"
    if "#legendado" in texto_lower or "#leg" in texto_lower or "[leg]" in texto_lower:
        audio = "Legendado"
    elif "#dublado" in texto_lower or "#dub" in texto_lower or "[dub]" in texto_lower:
        audio = "Dublado"

    # CHECAGEM 1: PACOTES RAR (#filme_rar OU #serie_rar / #anime_rar)
    match_rar_serie = re.match(r"^#(serie|anime|cartoon)_rar\s+(.+?)\s+[sS](\d+(?:\.\d+)?)", primeira_linha, re.IGNORECASE)
    match_rar_filme = re.match(r"^#filme_rar\s+(.+)", primeira_linha, re.IGNORECASE)

    conn = get_db_connection()
    c = conn.cursor()

    if match_rar_serie or match_rar_filme:
        if match_rar_serie:
            cat, nome_bruto = match_rar_serie.group(1).lower(), match_rar_serie.group(2).strip()
            temp = float(match_rar_serie.group(3))
        else:
            cat, nome_bruto = "filme", match_rar_filme.group(1).strip()
            temp = 0.0

        nome = re.sub(r"#\w+", "", nome_bruto).strip().title()
        nome_busca = normalizar(nome)

        c.execute("SELECT id FROM titulos WHERE nome = %s;", (nome,))
        res_existente = c.fetchone()
        e_novo_titulo = res_existente is None
        selo_valor = "NOVO" if e_novo_titulo else ("ATUALIZADO" if cat == "filme" else "NOVA TEMP")

        poster_url, sinopse, ano, nota, duracao, generos, trailer_key = buscar_metadados_tmdb(nome, cat)
        generos_json = json.dumps(generos, ensure_ascii=False) if generos else "[]"

        c.execute("""
            INSERT INTO titulos (categoria, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos, trailer_key, audio, selo, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (nome) DO UPDATE SET 
                categoria = EXCLUDED.categoria,
                selo = EXCLUDED.selo,
                atualizado_em = CURRENT_TIMESTAMP;
        """, (cat, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos_json, trailer_key, audio, selo_valor))

        c.execute("SELECT id FROM titulos WHERE nome = %s;", (nome,))
        titulo_id = c.fetchone()[0]

        c.execute(
            "INSERT INTO arquivos_rar (titulo_id, temporada, chat_id, message_id) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING;",
            (titulo_id, temp, chat_id, message_id)
        )

        conn.commit()
        c.close()
        conn.close()
        print(f"--> [RAR PACK] Salva parte RAR de {nome} (Temp {temp})")
        return True, e_novo_titulo, {"nome": nome, "categoria": cat, "temporada": temp, "is_rar": True}

    # CHECAGEM 2: VÍDEOS NORMAIS PARA STREAMING
    match_ep = re.match(r"^#(serie|anime|cartoon)\s+(.+?)\s+[sS](\d+(?:\.\d+)?)[eE](\d+(?:\.\d+)?)", primeira_linha, re.IGNORECASE)
    match_filme = re.match(r"^#filme\s+(.+)", primeira_linha, re.IGNORECASE)

    if match_ep or match_filme:
        if match_ep:
            cat, nome_bruto = match_ep.group(1).lower(), match_ep.group(2).strip()
            temp, ep = float(match_ep.group(3)), float(match_ep.group(4))
        else:
            cat, nome_bruto = "filme", match_filme.group(1).strip()
            temp, ep = 0.0, 0.0

        nome = re.sub(r"#\w+", "", nome_bruto).strip().title()
        nome_busca = normalizar(nome)

        c.execute("SELECT id FROM titulos WHERE nome = %s;", (nome,))
        res_existente = c.fetchone()
        e_novo_titulo = res_existente is None
        selo_valor = "NOVO" if e_novo_titulo else ("ATUALIZADO" if cat == "filme" else "NOVA TEMP")

        poster_url, sinopse, ano, nota, duracao, generos, trailer_key = buscar_metadados_tmdb(nome, cat)
        generos_json = json.dumps(generos, ensure_ascii=False) if generos else "[]"

        c.execute("""
            INSERT INTO titulos (categoria, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos, trailer_key, audio, selo, atualizado_em)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (nome) DO UPDATE SET 
                categoria = EXCLUDED.categoria,
                selo = EXCLUDED.selo,
                atualizado_em = CURRENT_TIMESTAMP;
        """, (cat, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos_json, trailer_key, audio, selo_valor))

        c.execute("SELECT id FROM titulos WHERE nome = %s;", (nome,))
        titulo_id = c.fetchone()[0]

        c.execute(
            "INSERT INTO arquivos (titulo_id, temporada, episodio, chat_id, message_id) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (chat_id, message_id) DO UPDATE SET chat_id = EXCLUDED.chat_id;",
            (titulo_id, temp, ep, chat_id, message_id)
        )

        conn.commit()
        c.close()
        conn.close()
        print(f"--> [MIDIA] {nome} S{temp}E{ep} salvo com sucesso.")
        return True, e_novo_titulo, {"nome": nome, "categoria": cat, "temporada": temp, "episodio": ep, "is_rar": False}

    c.close()
    conn.close()
    return False, False, {}

async def processar_postagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    
    texto = (msg.caption or msg.text or "").strip()
    if not texto.startswith("#"):
        return

    sucesso, e_novo_titulo, info = salvar_ou_atualizar_midia(texto, msg.chat_id, msg.message_id)
    
    if sucesso:
        if info.get("is_rar"):
            aviso = f"📦 **Parte do arquivo .RAR recebida!**\n🎬 *{info['nome']}* teve partes compactadas adicionadas ao catálogo."
        elif e_novo_titulo:
            aviso = f"✨ **Novo Título Adicionado!**\n🎬 *{info['nome']}* já está disponível no catálogo."
        else:
            if info['categoria'] == 'filme':
                aviso = f"🔄 **Filme Atualizado!**\n🎬 *{info['nome']}* teve seus arquivos atualizados."
            else:
                temp_label = formatar_numero(info['temporada'])
                ep_label = formatar_numero(info['episodio'])
                aviso = f"🔥 **Nova Temporada Adicionada!**\n📺 *{info['nome']}* (S{temp_label}E{ep_label}) atualizado no catálogo."
            
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=aviso,
            reply_to_message_id=msg.message_id,
            parse_mode="Markdown"
        )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO usuarios (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", (user_id,))
    conn.commit()
    c.close()
    conn.close()

    teclado_app = ReplyKeyboardMarkup(
        [[KeyboardButton("🍿 Abrir Miniflix", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )

    msg_boas_vindas = (
        "🍿 **Bem-vindo ao Miniflix!**\n\n"
        "Assista a filmes e séries ou baixe temporadas completas compactadas (.rar).\n\n"
        "• 🚀 **Toque em '🍿 Abrir Miniflix'** abaixo para acessar a plataforma."
    )

    await update.message.reply_markdown(msg_boas_vindas, reply_markup=teclado_app)

async def main():
    init_db()
    threading.Thread(target=iniciar_servidor_web, daemon=True).start()

    request_config = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0)

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request_config)
        .get_updates_request(request_config)
        .build()
    )

    app.add_handler(MessageHandler(
        (filters.ChatType.CHANNEL | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.COMMAND, 
        processar_postagem
    ))
    
    app.add_handler(CommandHandler("start", start))

    async with app:
        await app.initialize()
        await app.start()
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        logging.info("--> [BOT MINIFLIX] Rodando com suporte a arquivos .RAR!")
        stop_event = asyncio.Event()
        await stop_event.wait()

if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot finalizado.")
