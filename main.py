import asyncio
import logging
import os
import json
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
FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
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
if not FIRECRAWL_KEY:
    logger.warning("FIRECRAWL_API_KEY nao configurada!")

def load_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"price_target": 190, "last_price": None, "search_count": 0}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception:
        pass

config = load_config()

def get_dates():
    ida = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    volta = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")
    return ida, volta

def get_kayak_url():
    ida, volta = get_dates()
    return f"https://www.kayak.com.br/flights/FOR-REC/{ida}/{volta}?sort=price_a"

def build_links():
    kayak = get_kayak_url()
    links = [
        f'<a href="{kayak}">Kayak</a>',
        '<a href="https://www.google.com/travel/flights?hl=pt-BR&curr=BRL">Google Flights</a>',
        '<a href="https://www.skyscanner.com.br/transport/flights/for/rec/">Skyscanner</a>',
        '<a href="https://www.decolar.com/shop/flights/results/roundtrip/FOR/REC">Decolar</a>',
    ]
    return " | ".join(links)

# ============================================================
# BUSCA REAL
# ============================================================

def search_flights():
    if not FIRECRAWL_KEY:
        logger.error("Sem FIRECRAWL_API_KEY")
        return None

    kayak = get_kayak_url()
    logger.info(f"Buscando: {kayak}")
    logger.info(f"Key: {FIRECRAWL_KEY[:10]}...")

    try:
        r = http_requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {FIRECRAWL_KEY}", "Content-Type": "application/json"},
            json={
                "url": kayak,
                "formats": ["json"],
                "jsonOptions": {
                    "prompt": "Extract all flight results with airline, price in BRL as number, departure time, arrival time, flight duration, stops count",
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
                                        "stops": {"type": "integer"}
                                    }
                                }
                            }
                        }
                    }
                },
                "waitFor": 5000,
                "location": {"country": "BR", "languages": ["pt-BR"]}
            },
            timeout=90
        )

        logger.info(f"Firecrawl HTTP {r.status_code}")

        if r.status_code != 200:
            logger.error(f"Firecrawl erro: {r.text[:200]}")
            return None

        data = r.json()
        if not data.get("success"):
            logger.error(f"Firecrawl success=false: {json.dumps(data)[:200]}")
            return None

        flights = data.get("data", {}).get("json", {}).get("flights", [])
        valid = [f for f in flights if isinstance(f.get("price_brl"), (int, float)) and f["price_brl"] >= 200]
        valid.sort(key=lambda x: x["price_brl"])

        logger.info(f"Encontrados: {len(flights)} total, {len(valid)} validos")
        if valid:
            logger.info(f"Melhor: R$ {valid[0]['price_brl']} - {valid[0].get('airline','?')}")

        return valid if valid else None

    except http_requests.Timeout:
        logger.error("Firecrawl TIMEOUT")
        return None
    except Exception as e:
        logger.error(f"Firecrawl exception: {type(e).__name__}: {e}")
        return None

# ============================================================
# HANDLERS
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = config.get("price_target", 190)
    await update.message.reply_text(
        f"Bot de Passagens Aereas\n\n"
        f"Rota: Fortaleza - Recife\n"
        f"Meta: R$ {t}\n"
        f"Busca: 08h, 14h, 18h\n"
        f"Fonte: Kayak (REAL)\n\n"
        f"/search - buscar agora\n"
        f"/meta [valor] - alterar meta\n"
        f"/status - status\n"
        f"/links - links\n"
        f"/help - ajuda"
    )

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando passagens REAIS no Kayak... (ate 60s)")

    flights = search_flights()
    t = config.get("price_target", 190)
    ida, volta = get_dates()

    if not flights:
        await update.message.reply_text(
            f"Erro ao buscar. Tente manualmente:\n\n{build_links()}",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        return

    best = flights[0]
    p = int(best["price_brl"])
    a = best.get("airline", "?")
    s = best.get("stops", 0)
    dep = best.get("departure_time", "")
    arr = best.get("arrival_time", "")
    dur = best.get("duration", "")
    st = "Direto" if s == 0 else f"{s} parada(s)"

    config["last_price"] = p
    config["search_count"] = config.get("search_count", 0) + 1
    save_config(config)

    kayak_url = get_kayak_url()

    header = "META BATIDA! CORRE!" if p <= t else "Resultado da Busca"
    msg = f"{header}\n\nR$ {p} - {a}\n{st}"
    if dep and arr:
        msg += f" | {dep}-{arr}"
    if dur:
        msg += f" | {dur}"
    msg += f"\nMeta: R$ {t}"
    if p > t:
        msg += f" (falta R$ {p - t})"
    msg += f"\nDatas: {ida} a {volta}"

    # Link direto de compra
    msg += f'\n\n<a href="{kayak_url}">CLIQUE AQUI PARA COMPRAR NO KAYAK</a>'

    if len(flights) > 1:
        msg += "\n\nOutras opcoes:"
        for f in flights[1:5]:
            fp = int(f["price_brl"])
            fa = f.get("airline", "?")
            fs = "direto" if f.get("stops", 0) == 0 else f"{f.get('stops')}p"
            fd = f.get("duration", "")
            ft = f.get("departure_time", "")
            msg += f"\nR$ {fp} - {fa} ({fs})"
            if ft:
                msg += f" {ft}"
            if fd:
                msg += f" {fd}"

    msg += f"\n\nOutros sites:\n{build_links()}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_meta(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(f"Meta: R$ {config.get('price_target', 190)}\nUse: /meta 250")
        return
    try:
        n = int(ctx.args[0])
        if n <= 0:
            raise ValueError
        config["price_target"] = n
        save_config(config)
        await update.message.reply_text(f"Meta: R$ {n}")
    except (ValueError, IndexError):
        await update.message.reply_text("Use: /meta 250")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = config.get("price_target", 190)
    lp = config.get("last_price")
    c = config.get("search_count", 0)
    lt = f"R$ {lp}" if lp else "nenhuma"
    fc = "OK" if FIRECRAWL_KEY else "FALTANDO"
    await update.message.reply_text(f"Status\n\nOnline\nMeta: R$ {t}\nFirecrawl: {fc}\nBuscas: {c}\nUltimo: {lt}")

async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Links:\n\n{build_links()}", parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start\n/search\n/meta [valor]\n/status\n/links\n/help")

async def fallback_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /help")

# ============================================================
# AGENDAMENTO
# ============================================================

async def scheduled_search(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Busca agendada")
    flights = search_flights()
    t = config.get("price_target", 190)
    if not flights:
        return
    best = flights[0]
    p = int(best["price_brl"])
    a = best.get("airline", "?")
    s = "direto" if best.get("stops", 0) == 0 else f"{best.get('stops')}p"
    config["last_price"] = p
    config["search_count"] = config.get("search_count", 0) + 1
    save_config(config)

    if p <= t:
        kayak = get_kayak_url()
        dep = best.get("departure_time", "")
        dur = best.get("duration", "")
        msg = f"META BATIDA! CORRE!\n\nR$ {p} - {a} ({s})"
        if dep:
            msg += f" | {dep}"
        if dur:
            msg += f" | {dur}"
        msg += f'\n\n<a href="{kayak}">CLIQUE AQUI PARA COMPRAR</a>'
        msg += f"\n\nOutros sites:\n{build_links()}"
    else:
        msg = f"Busca Automatica\n\nR$ {p} - {a} ({s})\nMeta: R$ {t} | Falta: R$ {p - t}"

    try:
        await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Envio: {e}")

# ============================================================
# SERVER
# ============================================================

async def health(req: Request):
    return PlainTextResponse("OK")

async def webhook(req: Request):
    try:
        data = await req.json()
        update = Update.de_json(data=data, bot=app_bot.bot)
        await app_bot.update_queue.put(update)
    except Exception as e:
        logger.error(f"WH: {e}")
    return Response()

web = Starlette(routes=[
    Route("/", health),
    Route("/health", health),
    Route("/webhook", webhook, methods=["POST"]),
])

app_bot = Application.builder().token(TOKEN).updater(None).build()
app_bot.add_handler(CommandHandler("start", cmd_start))
app_bot.add_handler(CommandHandler("search", cmd_search))
app_bot.add_handler(CommandHandler("meta", cmd_meta))
app_bot.add_handler(CommandHandler("status", cmd_status))
app_bot.add_handler(CommandHandler("links", cmd_links))
app_bot.add_handler(CommandHandler("help", cmd_help))
app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_msg))

try:
    if app_bot.job_queue is not None:
        app_bot.job_queue.run_daily(scheduled_search, time=dt_time(hour=11, minute=0))
        app_bot.job_queue.run_daily(scheduled_search, time=dt_time(hour=17, minute=0))
        app_bot.job_queue.run_daily(scheduled_search, time=dt_time(hour=21, minute=0))
except Exception as e:
    logger.warning(f"Jobs: {e}")

async def main():
    logger.info("=" * 50)
    logger.info("BOT PASSAGENS - DADOS REAIS")
    logger.info(f"Firecrawl: {'OK (' + FIRECRAWL_KEY[:10] + '...)' if FIRECRAWL_KEY else 'FALTANDO!'}")
    logger.info(f"Porta: {PORT}")
    logger.info("=" * 50)

    await app_bot.initialize()
    await app_bot.start()

    if RENDER_EXTERNAL_URL:
        try:
            await app_bot.bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/webhook", allowed_updates=Update.ALL_TYPES)
            logger.info("Webhook OK")
        except Exception as e:
            logger.error(f"Webhook: {e}")

    srv = uvicorn.Server(uvicorn.Config(app=web, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await srv.serve()
    finally:
        await app_bot.stop()
        await app_bot.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
