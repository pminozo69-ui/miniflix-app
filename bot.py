import json
import logging
import os
import re
import unicodedata
import psycopg2
import requests
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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

# --- INTEGRAÇÃO COM TMDB ---
def buscar_metadados_tmdb(nome_titulo: str, categoria: str):
    tipo_busca = "movie" if categoria == "filme" else "tv"
    url = f"https://api.themoviedb.org/3/search/{tipo_busca}"
    params = {
        "api_key": TMDB_API_KEY,
        "query": nome_titulo,
        "language": "pt-BR",
        "include_adult": "false"
    }

    try:
        resp = requests.get(url, params=params, timeout=5)
        dados = resp.json()
        resultados = dados.get("results", [])

        if not resultados:
            return None, None, None

        item = resultados[0]
        poster_path = item.get("poster_path")
        poster_url = f"{TMDB_IMG_BASE}{poster_path}" if poster_path else "https://images.unsplash.com/photo-1536440136628-849c177e76a1?w=300"
        sinopse = item.get("overview") or "Sem sinopse cadastrada."
        
        data_lancamento = item.get("release_date") or item.get("first_air_date") or ""
        ano = int(data_lancamento.split("-")[0]) if "-" in data_lancamento else None

        return poster_url, sinopse, ano
    except Exception as e:
        logging.error(f"Erro ao consultar TMDB: {e}")
        return None, None, None

# --- BANCO DE DADOS (SUPABASE) ---
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
            ano INTEGER
        );
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

# --- SERVIDOR WEB & API PARA O MINI APP / RENDER ---
async def api_catalogo(request):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        SELECT t.id, t.categoria, t.nome, t.poster_url, t.sinopse, t.ano,
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
        t_id, cat, nome, poster, sinopse, ano, temp, ep = r
        if t_id not in titulos_map:
            titulos_map[t_id] = {
                "id": t_id,
                "tipo": cat,
                "titulo": nome,
                "ano": ano or "N/A",
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

    lista_final = list(titulos_map.values())
    return web.json_response(lista_final, headers={"Access-Control-Allow-Origin": "*"})

async def rota_health(request):
    return web.Response(text="Miniflix Bot Online!", status=200)

async def post_init(application: Application):
    server = web.Application()
    server.router.add_get("/", rota_health)
    server.router.add_get("/health", rota_health)
    server.router.add_get("/api/catalogo", api_catalogo)
    
    runner = web.AppRunner(server)
    await runner.setup()
    
    porta = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", porta)
    await site.start()
    logging.info(f"--> [API + RENDER] Servidor rodando na porta {porta}")

# --- INGESTÃO COM TMDB ---
def salvar_ou_atualizar_midia(texto_completo: str, chat_id: int, message_id: int):
    if not texto_completo.startswith("#"):
        return False

    primeira_linha = texto_completo.split("\n")[0].strip()
    match_ep = re.match(r"^#(serie|anime|cartoon)\s+(.+?)\s+[sS](\d+)[eE](\d+)", primeira_linha, re.IGNORECASE)
    match_filme = re.match(r"^#filme\s+(.+)", primeira_linha, re.IGNORECASE)

    conn = get_db_connection()
    c = conn.cursor()

    if match_ep:
        cat, nome = match_ep.group(1).lower(), match_ep.group(2).strip().title()
        temp, ep = int(match_ep.group(3)), int(match_ep.group(4))
        nome_busca = normalizar(nome)

        poster_url, sinopse, ano = buscar_metadados_tmdb(nome, cat)

        c.execute("""
            INSERT INTO titulos (categoria, nome, nome_busca, poster_url, sinopse, ano)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (nome) DO UPDATE SET 
                categoria = EXCLUDED.categoria,
                poster_url = COALESCE(titulos.poster_url, EXCLUDED.poster_url),
                sinopse = COALESCE(titulos.sinopse, EXCLUDED.sinopse),
                ano = COALESCE(titulos.ano, EXCLUDED.ano);
        """, (cat, nome, nome_busca, poster_url, sinopse, ano))

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
        print(f"--> [TMDB + SUPABASE] {nome} (S{temp:02d}E{ep:02d}) registrado.")
        return True

    elif match_filme:
        cat = "filme"
        nome = match_filme.group(1).strip().title()
        nome_busca = normalizar(nome)

        poster_url, sinopse, ano = buscar_metadados_tmdb(nome, cat)

        c.execute("""
            INSERT INTO titulos (categoria, nome, nome_busca, poster_url, sinopse, ano)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (nome) DO UPDATE SET 
                categoria = EXCLUDED.categoria,
                poster_url = COALESCE(titulos.poster_url, EXCLUDED.poster_url),
                sinopse = COALESCE(titulos.sinopse, EXCLUDED.sinopse),
                ano = COALESCE(titulos.ano, EXCLUDED.ano);
        """, (cat, nome, nome_busca, poster_url, sinopse, ano))

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
        print(f"--> [TMDB + SUPABASE] Filme {nome} ({ano}) registrado.")
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

# --- MENUS E BOTÕES TELEGRAM ---
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
    c.close()
    conn.close()

    teclado_app = ReplyKeyboardMarkup(
        [[KeyboardButton("🍿 Abrir Miniflix", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )

    await update.message.reply_markdown(
        "🍿 **Miniflix Bot**\n\nAbra o **Catálogo Visual** abaixo ou navegue pelas categorias:",
        reply_markup=teclado_app
    )
    await update.message.reply_markdown("Categorias disponíveis:", reply_markup=menu_principal())

# --- RESPOSTA AOS CLIQUES DO MINI APP ---
async def receber_dados_webapp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dados_raw = update.effective_message.web_app_data.data
    try:
        dados = json.loads(dados_raw)
        titulo_id = int(dados.get("id"))
        tipo = dados.get("tipo")
        temp = dados.get("temporada")
        ep = dados.get("episodio")
    except (ValueError, TypeError, json.JSONDecodeError):
        return

    conn = get_db_connection()
    c = conn.cursor()

    if tipo == "filme" or temp is None or ep is None:
        c.execute("SELECT chat_id, message_id FROM arquivos WHERE titulo_id = %s LIMIT 1;", (titulo_id,))
    else:
        c.execute(
            "SELECT chat_id, message_id FROM arquivos WHERE titulo_id = %s AND temporada = %s AND episodio = %s LIMIT 1;",
            (titulo_id, int(temp), int(ep))
        )
    
    arq = c.fetchone()
    c.close()
    conn.close()

    if arq:
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=arq[0],
            message_id=arq[1]
        )
    else:
        await update.message.reply_text("Arquivo não encontrado no catálogo do Supabase.")

# --- CALLBACKS DO MENU NATIVO ---
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

# --- BUSCA DIRETA & STATS ---
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

async def cmd_meuid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_markdown(f"Seu ID do Telegram é: `{update.effective_user.id}`")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ADMIN_ID != 0 and user_id != ADMIN_ID:
        await update.message.reply_text("Acesso não autorizado.")
        return

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
        "📊 **Estatísticas do Miniflix (TMDB + Supabase)**\n\n"
        f"👥 **Total de Usuários:** `{total_users}`\n"
        f"📦 **Total de Arquivos:** `{total_arquivos}`\n\n"
        f"• 🎬 Filmes: `{por_cat.get('filme', 0)}`\n"
        f"• 📺 Séries: `{por_cat.get('serie', 0)}`\n"
        f"• ⛩️ Animes: `{por_cat.get('anime', 0)}`\n"
        f"• 🎨 Cartoons: `{por_cat.get('cartoon', 0)}`"
    )
    await update.message.reply_markdown(texto)

# --- EXECUÇÃO PRINCIPAL ---
if __name__ == "__main__":
    init_db()
    
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(MessageHandler((filters.ChatType.CHANNEL | filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP) & ~filters.COMMAND, processar_postagem))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_CHANNEL_POST | filters.UpdateType.EDITED_MESSAGE, processar_edicao))
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("meuid", cmd_meuid))

    app.add_handler(CallbackQueryHandler(tratar_callbacks))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, receber_dados_webapp))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, buscar_texto))

    app.run_polling()