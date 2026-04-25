import asyncio
import logging
import os
import json
import re
import sys
from datetime import datetime, timedelta, time as dt_time

import requests as http_requests
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
CONFIG_FILE = "bot_config.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

if not TOKEN:
    logger.error("BOT_TOKEN nao configurado!")
    sys.exit(1)
if ADMIN_CHAT_ID == 0:
    logger.error("ADMIN_CHAT_ID nao configurado!")
    sys.exit(1)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"price_target": 190, "last_price": None, "search_count": 0}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        logger.error(f"Erro ao salvar config: {e}")

config = load_config()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

def extract_prices(text):
    raw = re.findall(r"R\$\s*([\d.,]+)", text)
    prices = []
    for p in raw:
        clean = p.replace(".", "").replace(",", "")
        try:
            val = int(clean)
            if 80 < val < 5000:
                prices.append(val)
        except ValueError:
            continue
    return sorted(set(prices))

def scrape_source(name, url):
    try:
        resp = http_requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            prices = extract_prices(resp.text)
            if prices:
                return {"source": name, "price": prices[0], "link": url}
    except Exception as e:
        logger.warning(f"{name} erro: {e}")
    return None

def search_all_sources():
    logger.info("Buscando passagens...")
    sources = [
        ("Skyscanner", "https://www.skyscanner.com.br/transport/flights/for/rec/"),
        ("Kayak", "https://www.kayak.com.br/flights/FOR-REC"),
        ("Google Flights", "https://www.google.com/travel/flights"),
    ]
    results = []
    for name, url in sources:
        r = scrape_source(name, url)
        if r:
            results.append(r)
    if results:
        return min(results, key=lambda x: x["price"])
    return None

SEARCH_LINKS = {
    "Google Flights": "https://www.google.com/travel/flights",
    "Skyscanner": "https://www.skyscanner.com.br/transport/flights/for/rec/",
    "Kayak": "https://www.kayak.com.br/flights/FOR-REC",
    "Decolar": "https://www.decolar.com/shop/flights/results/roundtrip/FOR/REC",
}

def build_links_text():
    parts = []
    for name, url in SEARCH_LINKS.items():
        parts.append(f'<a href="{url}">{name}</a>')
    return " | ".join(parts)

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = config.get("price_target", 190)
    msg = (
        "Bot de Monitoramento de Passagens\n\n"
        f"Rota: Fortaleza - Recife\n"
        f"Meta: R$ {target}\n"
        f"Rastreamento: 08:00, 14:00, 18:00\n\n"
        "Comandos:\n"
        "/search - buscar agora\n"
        "/meta [valor] - alterar meta\n"
        "/status - ver status\n"
        "/links - ver links\n"
        "/help - ajuda"
    )
    await update.message.reply_text(msg)

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando passagens... aguarde")
    result = search_all_sources()
    target = config.get("price_target", 190)
    if result:
        price = result["price"]
        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)
        if price <= target:
            msg = f"META BATIDA!\n\nR$ {price}\nFortaleza - Recife\nFonte: {result['source']}\n\nBuscar em:\n{build_links_text()}"
        else:
            msg = f"Resultado da Busca\n\nMelhor preco: R$ {price}\nMeta: R$ {target}\nDiferenca: +R$ {price - target}\nFonte: {result['source']}\n\nBuscar em:\n{build_links_text()}"
    else:
        msg = f"Nenhum preco encontrado automaticamente.\nTente buscar manualmente:\n\n{build_links_text()}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_meta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(f"Meta atual: R$ {config.get('price_target', 190)}\n\nPara alterar: /meta 250")
        return
    try:
        novo = int(ctx.args[0])
        if novo <= 0:
            raise ValueError
        config["price_target"] = novo
        save_config(config)
        await update.message.reply_text(f"Meta alterada para R$ {novo}")
    except (ValueError, IndexError):
        await update.message.reply_text("Valor invalido. Use: /meta 250")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = config.get("price_target", 190)
    last = config.get("last_price")
    count = config.get("search_count", 0)
    last_txt = f"R$ {last}" if last else "nenhuma busca ainda"
    msg = f"Status do Bot\n\nBot online\nMeta: R$ {target}\nRota: FOR - REC\nBuscas: {count}\nUltimo preco: {last_txt}"
    await update.message.reply_text(msg)

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = f"Links de Busca Manual\n\n{build_links_text()}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = "/start - iniciar\n/search - buscar agora\n/meta [valor] - alterar meta\n/status - ver status\n/links - ver links\n/help - ajuda"
    await update.message.reply_text(msg)

async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /help para ver os comandos.")

async def scheduled_search(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Busca agendada iniciada")
    result = search_all_sources()
    target = config.get("price_target", 190)
    if result:
        price = result["price"]
        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)
        if price <= target:
            msg = f"META BATIDA!\n\nR$ {price}\nFortaleza - Recife\nFonte: {result['source']}\n\nBuscar em:\n{build_links_text()}"
        else:
            msg = f"Busca Automatica\n\nMelhor: R$ {price}\nMeta: R$ {target}\nFalta: R$ {price - target}\nFonte: {result['source']}"
        try:
            await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Erro ao enviar: {e}")

async def health(request: Request):
    return PlainTextResponse("OK")

async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data=data, bot=application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return Response()

starlette_app = Starlette(
    routes=[
        Route("/", health),
        Route("/health", health),
        Route("/webhook", telegram_webhook, methods=["POST"]),
    ],
)

application = Application.builder().token(TOKEN).updater(None).build()
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("search", cmd_search))
application.add_handler(CommandHandler("meta", cmd_meta))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("links", cmd_links))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

try:
    if application.job_queue is not None:
        application.job_queue.run_daily(scheduled_search, time=dt_time(hour=11, minute=0))
        application.job_queue.run_daily(scheduled_search, time=dt_time(hour=17, minute=0))
        application.job_queue.run_daily(scheduled_search, time=dt_time(hour=21, minute=0))
except Exception as e:
    logger.warning(f"Job queue erro: {e}")

async def main():
    logger.info("BOT DE PASSAGENS AEREAS - INICIANDO")
    logger.info(f"Meta: R$ {config.get('price_target', 190)}")
    logger.info(f"Chat ID: {ADMIN_CHAT_ID}")
    logger.info(f"Porta: {PORT}")

    await application.initialize()
    await application.start()

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        try:
            await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
            logger.info(f"Webhook configurado: {webhook_url}")
        except Exception as e:
            logger.error(f"Erro webhook: {e}")

    server = uvicorn.Server(uvicorn.Config(app=starlette_app, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await server.serve()
    finally:
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
