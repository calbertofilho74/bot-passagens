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
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
CONFIG_FILE = "bot_config.json"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
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
        logger.error(f"Erro config: {e}")

config = load_config()

# ============================================================
# BUSCA REAL VIA FIRECRAWL
# ============================================================

def get_search_dates():
    ida = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    volta = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")
    return ida, volta

def get_kayak_url():
    ida, volta = get_search_dates()
    return f"https://www.kayak.com.br/flights/FOR-REC/{ida}/{volta}?sort=price_a"

def search_firecrawl():
    """Busca passagens REAIS via Firecrawl API + Kayak"""
    if not FIRECRAWL_API_KEY:
        logger.warning("FIRECRAWL_API_KEY vazia!")
        return None

    try:
        kayak_url = get_kayak_url()
        logger.info(f"Firecrawl buscando: {kayak_url}")

        resp = http_requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={
                "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "url": kayak_url,
                "formats": ["json"],
                "jsonOptions": {
                    "prompt": (
                        "Extract all flight options showing airline name, "
                        "price in BRL (number only), departure time, arrival time, "
                        "duration, and number of stops"
                    ),
                    "schema": {
                        "type": "object",
                        "properties": {
                            "flights": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "airline": {"type": "string"},
                                        "price_brl": {"type": "number"},
                                        "departure_time": {"type": "string"},
                                        "arrival_time": {"type": "string"},
                                        "duration": {"type": "string"},
                                        "stops": {"type": "integer"},
                                    },
                                },
                            }
                        },
                    },
                },
                "waitFor": 5000,
                "location": {"country": "BR", "languages": ["pt-BR"]},
            },
            timeout=60,
        )

        logger.info(f"Firecrawl status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                flights = data.get("data", {}).get("json", {}).get("flights", [])
                # Filtrar voos validos (preco > 200 para evitar precos genericos)
                valid = [f for f in flights if f.get("price_brl", 0) >= 200]
                if valid:
                    valid.sort(key=lambda x: x.get("price_brl", 9999))
                    logger.info(f"Firecrawl: {len(valid)} voos reais encontrados!")
                    return valid
                else:
                    logger.warning(f"Firecrawl: {len(flights)} voos mas nenhum valido")
            else:
                logger.warning(f"Firecrawl success=false")
        else:
            logger.error(f"Firecrawl erro {resp.status_code}: {resp.text[:200]}")

    except Exception as e:
        logger.error(f"Firecrawl exception: {e}")

    return None


def search_all():
    """Busca passagens reais"""
    logger.info("BUSCANDO PASSAGENS REAIS...")
    flights = search_firecrawl()
    if flights:
        return flights
    logger.warning("Firecrawl falhou, sem resultados")
    return None


# ============================================================
# FORMATACAO
# ============================================================

def build_links_text():
    kayak_url = get_kayak_url()
    links = {
        "Kayak": kayak_url,
        "Google Flights": "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL",
        "Skyscanner": "https://www.skyscanner.com.br/transport/flights/for/rec/",
        "Decolar": "https://www.decolar.com/shop/flights/results/roundtrip/FOR/REC",
    }
    return " | ".join(f'<a href="{u}">{n}</a>' for n, u in links.items())

def format_flight_msg(flights, target):
    ida, volta = get_search_dates()
    best = flights[0]
    price = best.get("price_brl", 0)
    airline = best.get("airline", "N/A")
    stops = best.get("stops", 0)
    dep = best.get("departure_time", "")
    arr = best.get("arrival_time", "")
    dur = best.get("duration", "")
    stops_txt = "Direto" if stops == 0 else f"{stops} parada(s)"

    if price <= target:
        header = "META BATIDA!"
    else:
        header = "Resultado da Busca"

    msg = f"{header}\n\n"
    msg += f"Melhor: R$ {price} - {airline}\n"
    msg += f"{stops_txt}"
    if dep and arr:
        msg += f" | {dep} - {arr}"
    if dur:
        msg += f" | {dur}"
    msg += f"\nMeta: R$ {target}"
    if price > target:
        msg += f" (falta R$ {price - target})"
    msg += f"\nDatas: {ida} a {volta}"

    # Top 5
    if len(flights) > 1:
        msg += "\n\nOutras opcoes:"
        for f in flights[1:5]:
            fp = f.get("price_brl", 0)
            fa = f.get("airline", "N/A")
            fs = "direto" if f.get("stops", 0) == 0 else f"{f.get('stops')}p"
            fd = f.get("duration", "")
            ft = f.get("departure_time", "")
            line = f"\nR$ {fp} - {fa} ({fs})"
            if ft:
                line += f" {ft}"
            if fd:
                line += f" {fd}"
            msg += line

    msg += f"\n\nComprar em:\n{build_links_text()}"
    return msg


# ============================================================
# HANDLERS
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = config.get("price_target", 190)
    msg = (
        "Bot de Monitoramento de Passagens\n\n"
        f"Rota: Fortaleza (FOR) - Recife (REC)\n"
        f"Meta: R$ {target}\n"
        f"Rastreamento: 08h, 14h, 18h\n"
        f"Fonte: Kayak (dados REAIS)\n\n"
        "Comandos:\n"
        "/search - buscar passagens agora\n"
        "/meta [valor] - alterar meta\n"
        "/status - ver status\n"
        "/links - links de busca\n"
        "/help - ajuda"
    )
    await update.message.reply_text(msg)

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando passagens REAIS no Kayak... aguarde (ate 30s)")
    flights = search_all()
    target = config.get("price_target", 190)

    if flights:
        config["last_price"] = flights[0].get("price_brl", 0)
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)
        msg = format_flight_msg(flights, target)
    else:
        msg = f"Erro ao buscar precos.\nTente manualmente:\n\n{build_links_text()}"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_meta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(f"Meta atual: R$ {config.get('price_target', 190)}\nUse: /meta 250")
        return
    try:
        novo = int(ctx.args[0])
        if novo <= 0:
            raise ValueError
        config["price_target"] = novo
        save_config(config)
        await update.message.reply_text(f"Meta alterada para R$ {novo}")
    except (ValueError, IndexError):
        await update.message.reply_text("Use: /meta 250")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = config.get("price_target", 190)
    last = config.get("last_price")
    count = config.get("search_count", 0)
    last_txt = f"R$ {last}" if last else "nenhuma busca"
    msg = f"Status\n\nOnline\nMeta: R$ {target}\nFonte: Kayak (REAL)\nBuscas: {count}\nUltimo: {last_txt}"
    await update.message.reply_text(msg)

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Links:\n\n{build_links_text()}", parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start\n/search - buscar (REAL)\n/meta [valor]\n/status\n/links\n/help")

async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /help")

# ============================================================
# AGENDAMENTO
# ============================================================

async def scheduled_search(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Busca agendada")
    flights = search_all()
    target = config.get("price_target", 190)
    if flights:
        config["last_price"] = flights[0].get("price_brl", 0)
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)
        msg = format_flight_msg(flights, target)
        try:
            await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Envio erro: {e}")

# ============================================================
# STARLETTE + APP
# ============================================================

async def health(request: Request):
    return PlainTextResponse("OK")

async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data=data, bot=application.bot)
        await application.update_queue.put(update)
    except Exception as e:
        logger.error(f"Webhook: {e}")
    return Response()

starlette_app = Starlette(routes=[
    Route("/", health),
    Route("/health", health),
    Route("/webhook", telegram_webhook, methods=["POST"]),
])

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
    logger.warning(f"Jobs: {e}")

async def main():
    logger.info("BOT PASSAGENS - DADOS REAIS via Firecrawl")
    logger.info(f"Firecrawl key: {'OK' if FIRECRAWL_API_KEY else 'FALTANDO!'}")
    await application.initialize()
    await application.start()
    if RENDER_EXTERNAL_URL:
        try:
            await application.bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/webhook", allowed_updates=Update.ALL_TYPES)
            logger.info(f"Webhook OK")
        except Exception as e:
            logger.error(f"Webhook erro: {e}")
    server = uvicorn.Server(uvicorn.Config(app=starlette_app, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await server.serve()
    finally:
        await application.stop()
        await application.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
