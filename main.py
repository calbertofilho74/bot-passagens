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

# ============================================================
# BUSCA REAL VIA FIRECRAWL API
# ============================================================

def search_firecrawl():
    """Busca passagens REAIS usando Firecrawl API"""
    if not FIRECRAWL_API_KEY:
        logger.warning("FIRECRAWL_API_KEY nao configurada")
        return None

    try:
        ida = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        volta = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")

        url = f"https://www.kayak.com.br/flights/FOR-REC/{ida}/{volta}?sort=price_a"
        logger.info(f"Firecrawl buscando: {url}")

        api_url = "https://api.firecrawl.dev/v1/scrape"
        headers = {
            "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "url": url,
            "formats": ["json"],
            "jsonOptions": {
                "prompt": "Extract all flight options with airline name, price in BRL, departure time, arrival time, duration, and number of stops",
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
        }

        resp = http_requests.post(api_url, headers=headers, json=payload, timeout=45)

        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                json_data = data.get("data", {}).get("json", {})
                flights = json_data.get("flights", [])
                if flights:
                    flights.sort(key=lambda x: x.get("price_brl", 9999))
                    logger.info(f"Firecrawl encontrou {len(flights)} voos!")
                    return flights
        else:
            logger.warning(f"Firecrawl status {resp.status_code}: {resp.text[:200]}")

    except Exception as e:
        logger.error(f"Firecrawl erro: {e}")

    return None


def search_kayak_direct():
    """Fallback: scraping direto do Kayak"""
    try:
        ida = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        volta = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        url = f"https://www.kayak.com.br/flights/FOR-REC/{ida}/{volta}?sort=price_a"
        resp = http_requests.get(url, headers=headers, timeout=15)

        if resp.status_code == 200:
            prices = re.findall(r'R\$\s*([\d.,]+)', resp.text)
            valid = []
            for p in prices:
                try:
                    v = int(p.replace(".", "").replace(",", ""))
                    if 80 < v < 5000:
                        valid.append(v)
                except:
                    pass

            airlines_found = []
            for airline in ["GOL", "LATAM", "Azul", "Avianca", "VOEPASS"]:
                if airline.lower() in resp.text.lower():
                    airlines_found.append(airline)

            if valid:
                best_price = min(valid)
                airline = airlines_found[0] if airlines_found else "N/A"
                return [{"airline": airline, "price_brl": best_price, "stops": 0, "departure_time": "", "arrival_time": "", "duration": ""}]

    except Exception as e:
        logger.warning(f"Kayak direto erro: {e}")

    return None


def search_all():
    """Busca em Firecrawl primeiro, fallback para Kayak direto"""
    logger.info("=" * 40)
    logger.info("BUSCANDO PASSAGENS REAIS...")

    # Tenta Firecrawl (dados detalhados)
    flights = search_firecrawl()
    if flights:
        return flights

    # Fallback Kayak direto (menos detalhes)
    logger.info("Tentando fallback Kayak direto...")
    flights = search_kayak_direct()
    if flights:
        return flights

    logger.warning("Nenhuma fonte retornou dados")
    return None


# ============================================================
# LINKS DE BUSCA
# ============================================================

def build_links_text():
    ida = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    volta = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")
    links = {
        "Google Flights": "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL",
        "Kayak": f"https://www.kayak.com.br/flights/FOR-REC/{ida}/{volta}?sort=price_a",
        "Skyscanner": "https://www.skyscanner.com.br/transport/flights/for/rec/",
        "Decolar": "https://www.decolar.com/shop/flights/results/roundtrip/FOR/REC",
    }
    return " | ".join(f'<a href="{u}">{n}</a>' for n, u in links.items())


# ============================================================
# HANDLERS DO BOT
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = config.get("price_target", 190)
    msg = (
        "Bot de Monitoramento de Passagens\n\n"
        f"Rota: Fortaleza - Recife (ida e volta)\n"
        f"Meta: R$ {target}\n"
        f"Rastreamento: 08:00, 14:00, 18:00\n"
        f"Dados: REAIS (Kayak via Firecrawl)\n\n"
        "Comandos:\n"
        "/search - buscar agora\n"
        "/meta [valor] - alterar meta\n"
        "/status - ver status\n"
        "/links - ver links\n"
        "/help - ajuda"
    )
    await update.message.reply_text(msg)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando passagens REAIS... aguarde (ate 30s)")
    flights = search_all()
    target = config.get("price_target", 190)

    if flights and len(flights) > 0:
        best = flights[0]
        price = best.get("price_brl", 0)
        airline = best.get("airline", "N/A")
        stops = best.get("stops", 0)
        dep = best.get("departure_time", "")
        arr = best.get("arrival_time", "")
        dur = best.get("duration", "")

        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)

        stops_txt = "Direto" if stops == 0 else f"{stops} parada(s)"

        if price <= target:
            msg = f"META BATIDA!\n\nR$ {price} - {airline}\n{stops_txt}"
        else:
            msg = f"Resultado da Busca\n\nMelhor preco: R$ {price}\nMeta: R$ {target}\nDiferenca: +R$ {price - target}\nCompanhia: {airline}\n{stops_txt}"

        if dep:
            msg += f"\nPartida: {dep}"
        if arr:
            msg += f"\nChegada: {arr}"
        if dur:
            msg += f"\nDuracao: {dur}"

        # Top 5 voos
        if len(flights) > 1:
            msg += "\n\nOutras opcoes:"
            for f in flights[1:5]:
                fp = f.get("price_brl", 0)
                fa = f.get("airline", "N/A")
                fs = "direto" if f.get("stops", 0) == 0 else f"{f.get('stops')}p"
                fd = f.get("duration", "")
                msg += f"\n  R$ {fp} - {fa} ({fs}) {fd}"

        msg += f"\n\nBuscar em:\n{build_links_text()}"
    else:
        msg = f"Nao encontrei precos automaticamente.\nTente manualmente:\n\n{build_links_text()}"

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
    msg = f"Status do Bot\n\nBot online\nMeta: R$ {target}\nRota: FOR - REC\nDados: REAIS\nBuscas: {count}\nUltimo preco: {last_txt}"
    await update.message.reply_text(msg)


async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = f"Links de Busca Manual\n\n{build_links_text()}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = "/start - iniciar\n/search - buscar agora (DADOS REAIS)\n/meta [valor] - alterar meta\n/status - ver status\n/links - ver links\n/help - ajuda"
    await update.message.reply_text(msg)


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /help para ver os comandos.")


# ============================================================
# BUSCA AGENDADA
# ============================================================

async def scheduled_search(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Busca agendada iniciada")
    flights = search_all()
    target = config.get("price_target", 190)

    if flights and len(flights) > 0:
        best = flights[0]
        price = best.get("price_brl", 0)
        airline = best.get("airline", "N/A")
        stops = best.get("stops", 0)
        dep = best.get("departure_time", "")
        dur = best.get("duration", "")

        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)

        stops_txt = "direto" if stops == 0 else f"{stops}p"

        if price <= target:
            msg = f"META BATIDA!\n\nR$ {price} - {airline} ({stops_txt})"
            if dep:
                msg += f"\nPartida: {dep}"
            if dur:
                msg += f"\nDuracao: {dur}"
            msg += f"\n\nBuscar em:\n{build_links_text()}"
        else:
            msg = f"Busca Automatica\n\nMelhor: R$ {price} - {airline} ({stops_txt})\nMeta: R$ {target}\nFalta: R$ {price - target}"

        try:
            await ctx.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            logger.error(f"Erro ao enviar: {e}")


# ============================================================
# STARLETTE
# ============================================================

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

# ============================================================
# APPLICATION
# ============================================================

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
    logger.info("BOT PASSAGENS AEREAS - DADOS REAIS")
    logger.info(f"Meta: R$ {config.get('price_target', 190)}")
    logger.info(f"Firecrawl: {'SIM' if FIRECRAWL_API_KEY else 'NAO'}")

    await application.initialize()
    await application.start()

    if RENDER_EXTERNAL_URL:
        try:
            await application.bot.set_webhook(url=f"{RENDER_EXTERNAL_URL}/webhook", allowed_updates=Update.ALL_TYPES)
            logger.info(f"Webhook OK: {RENDER_EXTERNAL_URL}/webhook")
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
