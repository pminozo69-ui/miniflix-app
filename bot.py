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

# --- INTEGRAÇÃO COMPLETA COM TMDB (GÊNEROS, NOTA, DURAÇÃO, TRAILER) ---
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

        # Busca detalhes aprofundados com vídeos/trailers
        url_det = f"https://api.themoviedb.org/3/{tipo_busca}/{tmdb_id}"
        params_det = {
            "api_key": TMDB_API_KEY,
            "language": "pt-BR",
            "append_to_response": "videos"
        }
        resp_det = requests.get(url_det, params=params_det, timeout=10)
        detalhes = resp_det.json() if resp_det.status_code == 200 else item

        poster_path = detalhes.get("poster_path") or item.get("poster_path")
        poster_url = f"{TMDB_IMG_BASE}{poster_path}" if poster_path else "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=300"
        sinopse = detalhes.get("overview") or item.get("overview") or "Sem sinopse cadastrada."
        
        data_lanc = detalhes.get("release_date") or detalhes.get("first_air_date") or ""
        ano = int(data_lanc.split("-")[0]) if "-" in data_lanc else None

        # Nota
        nota = round(float(detalhes.get("vote_average", 0.0)), 1)

        # Duração
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

        # Gêneros
        generos = [g.get("name") for g in detalhes.get("genres", []) if g.get("name")]

        # Trailer (YouTube)
        trailer_key = None
        videos = detalhes.get("videos", {}).get("results", [])
        for v in videos:
            if v.get("site") == "YouTube" and v.get("type") in ["Trailer", "Teaser"]:
                trailer_key = v.get("key")
                break

        # Fallback de trailer em inglês caso não haja em pt-BR
        if not trailer_key and tmdb_id:
            try:
                resp_vid_en = requests.get(
                    f"https://api.themoviedb.org/3/{tipo_busca}/{tmdb_id}/videos",
                    params={"api_key": TMDB_API_KEY, "language": "en-US"},
                    timeout=5
                )
                videos_en = resp_vid_en.json().get("results", [])
                for v in videos_en:
                    if v.get("site") == "YouTube" and v.get("type") in ["Trailer", "Teaser"]:
                        trailer_key = v.get("key")
                        break
            except Exception:
                pass

        return poster_url, sinopse, ano, nota, duracao, generos, trailer_key
    except Exception as e:
        logging.error(f"Erro ao consultar TMDB detalhado: {e}")
        return None, None, None, None, None, [], None

# --- BANCO DE DADOS (SUPABASE COM MIGRAÇÃO AUTOMÁTICA) ---
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
            audio TEXT
        );
    """)
    c.execute("""
        ALTER TABLE titulos 
        ADD COLUMN IF NOT EXISTS poster_url TEXT,
        ADD COLUMN IF NOT EXISTS sinopse TEXT,
        ADD COLUMN IF NOT EXISTS ano INTEGER,
        ADD COLUMN IF NOT EXISTS nota NUMERIC(3,1),
        ADD COLUMN IF NOT EXISTS duracao TEXT,
        ADD COLUMN IF NOT EXISTS generos TEXT,
        ADD COLUMN IF NOT EXISTS trailer_key TEXT,
        ADD COLUMN IF NOT EXISTS audio TEXT;
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS arquivos (
            id SERIAL PRIMARY KEY,
            titulo_id INTEGER REFERENCES titulos(id) ON DELETE CASCADE,
            temporada INTEGER NOT NULL,
            episodio INTEGER NOT NULL,
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
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

# --- SERVIDOR WEB NATIVO (API DO CATÁLOGO, DISPARO E HISTÓRICO) ---
class ServidorWebHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        
        # 1. Rota de disparo de mídia
        if parsed.path == "/api/play":
            try:
                params = parse_qs(parsed.query)
                user_id = int(params.get("user_id", [0])[0])
                titulo_id = int(params.get("titulo_id", [0])[0])
                tipo = params.get("tipo", ["filme"])[0]
                temp = int(params.get("temporada", [0])[0])
                ep = int(params.get("episodio", [0])[0])

                if user_id and titulo_id:
                    conn = get_db_connection()
                    c = conn.cursor()
                    
                    if tipo == "filme" or (temp == 0 and ep == 0):
                        c.execute("SELECT id, chat_id, message_id FROM arquivos WHERE titulo_id = %s LIMIT 1;", (titulo_id,))
                    else:
                        c.execute(
                            "SELECT id, chat_id, message_id FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s LIMIT 1;",
                            (titulo_id, temp, ep)
                        )
                    
                    arq = c.fetchone()
                    if arq:
                        arq_id, chat_origem, msg_origem = arq
                        
                        reply_markup = None
                        if tipo != "filme":
                            c.execute(
                                "SELECT id, episodio FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s LIMIT 1;",
                                (titulo_id, temp, ep + 1)
                            )
                            prox = c.fetchone()
                            if prox:
                                reply_markup = {
                                    "inline_keyboard": [[{
                                        "text": f"▶️ Próximo (Ep {prox[1]:02d})",
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
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Content-Length", str(len(corpo)))
                        self.end_headers()
                        self.wfile.write(corpo)
                        return
                    
                    c.close()
                    conn.close()
            except Exception as e:
                logging.error(f"Erro na rota /api/play: {e}")

            corpo = json.dumps({"ok": False}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        # 2. Rota do Catálogo com Metadados Ricos
        elif parsed.path == "/api/catalogo":
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute("""
                    SELECT t.id, t.categoria, t.nome, t.poster_url, t.sinopse, t.ano,
                           t.nota, t.duracao, t.generos, t.trailer_key, t.audio,
                           a.temporada, a.episodio
                    FROM titulos t
                    LEFT JOIN arquivos a ON t.id = a.titulo_id
                    ORDER BY t.nome ASC, a.temporada ASC, a.episodio ASC;
                """)
                linhas = c.fetchall()
                c.close()
                conn.close()

                titulos_map = {}
                for r in linhas:
                    t_id, cat, nome, poster, sinopse, ano, nota, duracao, generos_raw, trailer_key, audio, temp, ep = r
                    
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
                            "temporadas": {}
                        }
                    
                    if cat != "filme" and temp is not None and ep is not None:
                        temp_str = str(temp)
                        if temp_str not in titulos_map[t_id]["temporadas"]:
                            titulos_map[t_id]["temporadas"][temp_str] = []
                        if ep not in titulos_map[t_id]["temporadas"][temp_str]:
                            titulos_map[t_id]["temporadas"][temp_str].append(ep)

                corpo = json.dumps(list(titulos_map.values()), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(corpo)))
                self.end_headers()
                self.wfile.write(corpo)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))
        else:
            msg = b"Miniflix API Online!"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, format, *args):
        return

def iniciar_servidor_web():
    porta = int(os.environ.get("PORT", 8080))
    httpd = HTTPServer(("0.0.0.0", porta), ServidorWebHandler)
    logging.info(f"--> [API + RENDER] Servidor HTTP nativo rodando na porta {porta}")
    httpd.serve_forever()

# --- INGESTÃO AUTOMÁTICA COM DETECÇÃO DE ÁUDIO E METADADOS ---
def salvar_ou_atualizar_midia(texto_completo: str, chat_id: int, message_id: int):
    if not texto_completo.startswith("#"):
        return False

    texto_lower = texto_completo.lower()
    
    # Detecção de Áudio
    audio = "Dublado"
    if "#legendado" in texto_lower or "#leg" in texto_lower or "[leg]" in texto_lower:
        audio = "Legendado"
    elif "#dublado" in texto_lower or "#dub" in texto_lower or "[dub]" in texto_lower:
        audio = "Dublado"
    elif "#dual" in texto_lower or "[dual]" in texto_lower:
        audio = "Dublado / Legendado"

    primeira_linha = texto_completo.split("\n")[0].strip()
    match_ep = re.match(r"^#(serie|anime|cartoon)\s+(.+?)\s+[sS](\d+)[eE](\d+)", primeira_linha, re.IGNORECASE)
    match_filme = re.match(r"^#filme\s+(.+)", primeira_linha, re.IGNORECASE)

    conn = get_db_connection()
    c = conn.cursor()

    if match_ep:
        cat, nome_bruto = match_ep.group(1).lower(), match_ep.group(2).strip()
        temp, ep = int(match_ep.group(3)), int(match_ep.group(4))
        
        # Limpa tags da linha
        nome = re.sub(r"#\w+", "", nome_bruto).strip().title()
        nome_busca = normalizar(nome)

        poster_url, sinopse, ano, nota, duracao, generos, trailer_key = buscar_metadados_tmdb(nome, cat)
        generos_json = json.dumps(generos, ensure_ascii=False) if generos else "[]"

        c.execute("""
            INSERT INTO titulos (categoria, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos, trailer_key, audio)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (nome) DO UPDATE SET 
                categoria = EXCLUDED.categoria,
                poster_url = COALESCE(titulos.poster_url, EXCLUDED.poster_url),
                sinopse = COALESCE(titulos.sinopse, EXCLUDED.sinopse),
                ano = COALESCE(titulos.ano, EXCLUDED.ano),
                nota = COALESCE(EXCLUDED.nota, titulos.nota),
                duracao = COALESCE(EXCLUDED.duracao, titulos.duracao),
                generos = COALESCE(EXCLUDED.generos, titulos.generos),
                trailer_key = COALESCE(EXCLUDED.trailer_key, titulos.trailer_key),
                audio = EXCLUDED.audio;
        """, (cat, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos_json, trailer_key, audio))

        c.execute("SELECT id FROM titulos WHERE nome = %s;", (nome,))
        titulo_id = c.fetchone()[0]

        c.execute("""
            INSERT INTO arquivos (titulo_id, temporada, episodio, chat_id, message_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                titulo_id = EXCLUDED.titulo_id,
                temporada = EXCLUDED.temporada,
                episodio = EXCLUDED.episodio;
        """, (titulo_id, temp, ep, chat_id, message_id))
        conn.commit()
        c.close()
        conn.close()
        print(f"--> [TMDB + SUPABASE] {nome} (S{temp:02d}E{ep:02d}) [{audio}] registrado.")
        return True

    elif match_filme:
        cat = "filme"
        nome_bruto = match_filme.group(1).strip()
        nome = re.sub(r"#\w+", "", nome_bruto).strip().title()
        nome_busca = normalizar(nome)

        poster_url, sinopse, ano, nota, duracao, generos, trailer_key = buscar_metadados_tmdb(nome, cat)
        generos_json = json.dumps(generos, ensure_ascii=False) if generos else "[]"

        c.execute("""
            INSERT INTO titulos (categoria, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos, trailer_key, audio)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (nome) DO UPDATE SET 
                categoria = EXCLUDED.categoria,
                poster_url = COALESCE(titulos.poster_url, EXCLUDED.poster_url),
                sinopse = COALESCE(titulos.sinopse, EXCLUDED.sinopse),
                ano = COALESCE(titulos.ano, EXCLUDED.ano),
                nota = COALESCE(EXCLUDED.nota, titulos.nota),
                duracao = COALESCE(EXCLUDED.duracao, titulos.duracao),
                generos = COALESCE(EXCLUDED.generos, titulos.generos),
                trailer_key = COALESCE(EXCLUDED.trailer_key, titulos.trailer_key),
                audio = EXCLUDED.audio;
        """, (cat, nome, nome_busca, poster_url, sinopse, ano, nota, duracao, generos_json, trailer_key, audio))

        c.execute("SELECT id FROM titulos WHERE nome = %s;", (nome,))
        titulo_id = c.fetchone()[0]

        c.execute("""
            INSERT INTO arquivos (titulo_id, temporada, episodio, chat_id, message_id)
            VALUES (%s, 0, 0, %s, %s)
            ON CONFLICT (chat_id, message_id) DO UPDATE SET
                titulo_id = EXCLUDED.titulo_id,
                temporada = 0,
                episodio = 0;
        """, (titulo_id, chat_id, message_id))
        conn.commit()
        c.close()
        conn.close()
        print(f"--> [TMDB + SUPABASE] Filme {nome} ({ano}) [{audio}] registrado.")
        return True

    c.close()
    conn.close()
    return False

async def processar_postagem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if msg:
        salvar_ou_atualizar_midia((msg.caption or msg.text or "").strip(), msg.chat_id, msg.message_id)

async def processar_edicao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.edited_channel_post or update.edited_message
    if msg:
        salvar_ou_atualizar_midia((msg.caption or msg.text or "").strip(), msg.chat_id, msg.message_id)

# --- MENUS E COMANDOS NATIVOS ---
def menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Filmes", callback_data="cat:filme:0"),
         InlineKeyboardButton("📺 Séries", callback_data="cat:serie:0")],
        [InlineKeyboardButton("⛩️ Animes", callback_data="cat:anime:0"),
         InlineKeyboardButton("🎨 Cartoons", callback_data="cat:cartoon:0")],
        [InlineKeyboardButton("🚀 Abrir Catálogo Visual", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO usuarios (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;", (user_id,))
    conn.commit()

    if context.args and context.args[0].startswith("play_"):
        partes = context.args[0].split("_")
        titulo_id = int(partes[1])
        
        if len(partes) >= 4:
            temp = int(partes[2])
            ep = int(partes[3])
            c.execute(
                "SELECT id, chat_id, message_id FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s LIMIT 1;",
                (titulo_id, temp, ep)
            )
        else:
            c.execute("SELECT id, chat_id, message_id FROM arquivos WHERE titulo_id = %s LIMIT 1;", (titulo_id,))
            temp, ep = 0, 0
            
        arq = c.fetchone()
        if arq:
            arq_id, chat_origem, msg_origem = arq
            
            btn_prox = None
            if len(partes) >= 4:
                c.execute(
                    "SELECT id, episodio FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s;",
                    (titulo_id, temp, ep + 1)
                )
                prox = c.fetchone()
                if prox:
                    btn_prox = InlineKeyboardMarkup([[InlineKeyboardButton(f"▶️ Próximo (Ep {prox[1]:02d})", callback_data=f"play:{prox[0]}")]])

            await context.bot.copy_message(
                chat_id=update.effective_chat.id,
                from_chat_id=chat_origem,
                message_id=msg_origem,
                reply_markup=btn_prox
            )
            c.close()
            conn.close()
            return
        else:
            await update.message.reply_text("❌ Arquivo correspondente não foi encontrado no catálogo.")
            c.close()
            conn.close()
            return

    c.close()
    conn.close()

    teclado_app = ReplyKeyboardMarkup(
        [[KeyboardButton("🍿 Abrir Miniflix", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )

    msg_boas_vindas = (
        "🍿 **Bem-vindo ao Miniflix!**\n\n"
        "Assista a filmes, séries, animes e desenhos com reprodução instantânea.\n\n"
        "• 🚀 **Toque em '🍿 Abrir Miniflix'** abaixo para navegar pelo catálogo com capas em HD, trailers e gêneros.\n"
        "• 🔍 Ou **digite o nome** de qualquer título aqui no chat para buscar."
    )

    await update.message.reply_markdown(msg_boas_vindas, reply_markup=teclado_app)
    await update.message.reply_markdown("Categorias disponíveis:", reply_markup=menu_principal())

async def tratar_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    dados = query.data.split(":")
    acao = dados[0]

    conn = get_db_connection()
    c = conn.cursor()

    if acao == "menu":
        await query.edit_message_text("🍿 **Miniflix Bot**\nEscolha uma categoria:", reply_markup=menu_principal(), parse_mode="Markdown")

    elif acao == "cat":
        cat = dados[1]
        pagina = int(dados[2]) if len(dados) > 2 else 0

        c.execute("SELECT id, nome FROM titulos WHERE categoria = %s ORDER BY nome ASC;", (cat,))
        titulos = c.fetchall()

        if not titulos:
            teclado = [[InlineKeyboardButton("↩ Voltar ao Menu", callback_data="menu:root")]]
            await query.edit_message_text(f"Nenhum título em **{cat.capitalize()}s** ainda.", reply_markup=InlineKeyboardMarkup(teclado), parse_mode="Markdown")
            c.close()
            conn.close()
            return

        total_itens = len(titulos)
        total_paginas = (total_itens + ITENS_POR_PAGINA - 1) // ITENS_POR_PAGINA
        pagina = max(0, min(pagina, total_paginas - 1))
        titulos_pagina = titulos[pagina * ITENS_POR_PAGINA : (pagina + 1) * ITENS_POR_PAGINA]

        botoes = [[InlineKeyboardButton(t[1], callback_data=f"tit:{t[0]}")] for t in titulos_pagina]
        linha_nav = []
        if pagina > 0:
            linha_nav.append(InlineKeyboardButton("« Anterior", callback_data=f"cat:{cat}:{pagina - 1}"))
        if total_paginas > 1:
            linha_nav.append(InlineKeyboardButton(f"{pagina + 1}/{total_paginas}", callback_data="noop"))
        if pagina < total_paginas - 1:
            linha_nav.append(InlineKeyboardButton("Próximo »", callback_data=f"cat:{cat}:{pagina + 1}"))

        if linha_nav:
            botoes.append(linha_nav)

        botoes.append([InlineKeyboardButton("↩ Voltar ao Menu", callback_data="menu:root")])
        await query.edit_message_text(f"📁 **{cat.capitalize()}s** (Pág. {pagina + 1}/{total_paginas}):", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="Markdown")

    elif acao == "tit":
        titulo_id = int(dados[1])
        c.execute("SELECT nome, categoria FROM titulos WHERE id = %s;", (titulo_id,))
        res = c.fetchone()
        if not res:
            c.close()
            conn.close()
            return
        titulo, cat = res

        if cat == "filme":
            c.execute("SELECT chat_id, message_id FROM arquivos WHERE titulo_id = %s;", (titulo_id,))
            arq = c.fetchone()
            if arq:
                await context.bot.copy_message(chat_id=query.message.chat_id, from_chat_id=arq[0], message_id=arq[1])
            c.close()
            conn.close()
            return

        c.execute("SELECT DISTINCT temporada FROM arquivos WHERE titulo_id = %s ORDER BY temporada ASC;", (titulo_id,))
        temporadas = c.fetchall()
        botoes = [[InlineKeyboardButton(f"Temporada {t[0]}", callback_data=f"temp:{titulo_id}:{t[0]}")] for t in temporadas]
        botoes.append([InlineKeyboardButton("↩ Voltar", callback_data=f"cat:{cat}:0")])
        await query.edit_message_text(f"📺 **{titulo}**\nSelecione a temporada:", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="Markdown")

    elif acao == "temp":
        titulo_id, temp = int(dados[1]), int(dados[2])
        c.execute("SELECT nome, categoria FROM titulos WHERE id = %s;", (titulo_id,))
        titulo, cat = c.fetchone()

        c.execute("SELECT id, episodio FROM arquivos WHERE titulo_id = %s AND temporada = %s ORDER BY episodio ASC;", (titulo_id, temp))
        episodios = c.fetchall()

        botoes, linha = [], []
        for ep_id, ep_num in episodios:
            linha.append(InlineKeyboardButton(f"{ep_num:02d}", callback_data=f"play:{ep_id}"))
            if len(linha) == 5:
                botoes.append(linha)
                linha = []
        if linha:
            botoes.append(linha)

        botoes.append([InlineKeyboardButton("↩ Voltar às Temporadas", callback_data=f"tit:{titulo_id}")])
        await query.edit_message_text(f"🎬 **{titulo}** — *Temporada {temp}*\nEscolha o episódio:", reply_markup=InlineKeyboardMarkup(botoes), parse_mode="Markdown")

    elif acao == "play":
        arq_id = int(dados[1])
        c.execute("SELECT titulo_id, temporada, episodio, chat_id, message_id FROM arquivos WHERE id = %s;", (arq_id,))
        arq = c.fetchone()
        if arq:
            titulo_id, temp_atual, ep_atual, chat_origem, msg_origem = arq
            c.execute("SELECT id, episodio FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s;", (titulo_id, temp_atual, ep_atual + 1))
            prox = c.fetchone()
            btn_prox = InlineKeyboardMarkup([[InlineKeyboardButton(f"▶️ Próximo (Ep {prox[1]:02d})", callback_data=f"play:{prox[0]}")]]) if prox else None

            await context.bot.copy_message(
                chat_id=query.message.chat_id,
                from_chat_id=chat_origem,
                message_id=msg_origem,
                reply_markup=btn_prox,
            )

    c.close()
    conn.close()

async def buscar_texto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    termo = normalizar(update.message.text)
    if len(termo) < 2:
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, nome, categoria FROM titulos WHERE nome_busca LIKE %s LIMIT 10;", (f"%{termo}%",))
    resultados = c.fetchall()
    c.close()
    conn.close()

    if not resultados:
        await update.message.reply_text("Nenhum título encontrado com esse nome.")
        return

    botoes = [[InlineKeyboardButton(f"[{r[2].upper()}] {r[1]}", callback_data=f"tit:{r[0]}")] for r in resultados]
    await update.message.reply_markdown("🔍 **Resultados encontrados:**", reply_markup=InlineKeyboardMarkup(botoes))

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM usuarios;")
    total_users = c.fetchone()[0]

    c.execute("SELECT categoria, COUNT(*) FROM titulos GROUP BY categoria;")
    por_cat = dict(c.fetchall())

    c.execute("SELECT COUNT(*) FROM arquivos;")
    total_arquivos = c.fetchone()[0]
    c.close()
    conn.close()

    texto = (
        "📊 **Estatísticas do Miniflix**\n\n"
        f"👥 **Usuários:** `{total_users}`\n"
        f"📦 **Arquivos:** `{total_arquivos}`\n\n"
        f"• 🎬 Filmes: `{por_cat.get('filme', 0)}`\n"
        f"• 📺 Séries: `{por_cat.get('serie', 0)}`\n"
        f"• ⛩️ Animes: `{por_cat.get('anime', 0)}`\n"
        f"• 🎨 Cartoons: `{por_cat.get('cartoon', 0)}`"
    )
    await update.message.reply_markdown(texto)

# --- INICIALIZAÇÃO ---
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

    app.add_handler(MessageHandler((filters.ChatType.CHANNEL | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.COMMAND, processar_postagem))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST | filters.UpdateType.EDITED_MESSAGE, processar_edicao))
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", cmd_stats))

    app.add_handler(CallbackQueryHandler(tratar_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, buscar_texto))

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
        logging.info("--> [BOT MINIFLIX] Rodando com sucesso!")
        
        stop_event = asyncio.Event()
        await stop_event.wait()

if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot finalizado.")
