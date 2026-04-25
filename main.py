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

# ============================================================
# BUSCA DE VOOS REAIS VIA ANTHROPIC API + FIRECRAWL MCP
# ============================================================

def search_real_flights():
    """Busca passagens REAIS usando a API da Anthropic com Firecrawl MCP"""
    logger.info("Buscando passagens REAIS via Claude + Firecrawl...")

    try:
        api_url = "https://api.anthropic.com/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2025-01-01",
        }

        # Datas de busca
        ida = (datetime.now() + timedelta(days=7)).strftime("%d/%m/%Y")
        volta = (datetime.now() + timedelta(days=12)).strftime("%d/%m/%Y")

        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Busque passagens aereas de Fortaleza (FOR) para Recife (REC), "
                        f"ida e volta, em classe economica, 1 adulto. "
                        f"Datas aproximadas: ida {ida}, volta {volta}. "
                        f"Responda SOMENTE em JSON puro (sem markdown, sem ```), "
                        f"com este formato exato: "
                        f'{{"flights":[{{"airline":"nome","price":123,"departure":"horario","arrival":"horario","date":"data","stops":0,"duration":"Xh"}}]}}'
                    ),
                }
            ],
            "mcp_servers": [
                {
                    "type": "url",
                    "url": "https://mcp.firecrawl.dev/fc-6c8e0be264054987865bc8d09e5921b2/v2/mcp",
                    "name": "firecrawl",
                }
            ],
            "tools": [
                {"type": "web_search_20250305", "name": "web_search"}
            ],
        }

        response = http_requests.post(api_url, headers=headers, json=payload, timeout=60)

        if response.status_code == 200:
            data = response.json()
            # Extrair texto da resposta
            text_parts = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block["text"])

            full_text = " ".join(text_parts)

            # Tentar extrair JSON da resposta
            json_match = re.search(r'\{.*"flights".*\}', full_text, re.DOTALL)
            if json_match:
                try:
                    flights_data = json.loads(json_match.group())
                    flights = flights_data.get("flights", [])
                    if flights:
                        # Ordenar por preco
                        flights.sort(key=lambda x: x.get("price", 9999))
                        logger.info(f"Encontrados {len(flights)} voos reais!")
                        return flights
                except json.JSONDecodeError:
                    pass

            # Se nao conseguiu JSON, tentar extrair precos do texto
            prices = re.findall(r'R\$\s*([\d.,]+)', full_text)
            if prices:
                clean_prices = []
                for p in prices:
                    try:
                        val = int(p.replace(".", "").replace(",", ""))
                        if 80 < val < 5000:
                            clean_prices.append(val)
                    except:
                        pass
                if clean_prices:
                    return [{"airline": "Melhor oferta", "price": min(clean_prices), "stops": 0, "date": ida}]

        logger.warning(f"API retornou status {response.status_code}")

    except Exception as e:
        logger.error(f"Erro na busca real: {e}")

    # Fallback: scraping direto do Google Flights
    return search_fallback()


def search_fallback():
    """Fallback: scraping direto se a API falhar"""
    logger.info("Usando fallback (scraping direto)...")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9",
        }
        url = "https://www.skyscanner.com.br/transport/flights/for/rec/"
        resp = http_requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            prices = re.findall(r'R\$\s*([\d.,]+)', resp.text)
            valid = []
            for p in prices:
                try:
                    val = int(p.replace(".", "").replace(",", ""))
                    if 80 < val < 5000:
                        valid.append(val)
                except:
                    pass
            if valid:
                return [{"airline": "Skyscanner", "price": min(valid), "stops": 0}]
    except Exception as e:
        logger.warning(f"Fallback erro: {e}")
    return None


# ============================================================
# LINKS DE BUSCA
# ============================================================

SEARCH_LINKS = {
    "Google Flights": "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL",
    "Skyscanner": "https://www.skyscanner.com.br/transport/flights/for/rec/",
    "Kayak": "https://www.kayak.com.br/flights/FOR-REC",
    "Decolar": "https://www.decolar.com/shop/flights/results/roundtrip/FOR/REC",
}

def build_links_text():
    parts = []
    for name, url in SEARCH_LINKS.items():
        parts.append(f'<a href="{url}">{name}</a>')
    return " | ".join(parts)


# ============================================================
# HANDLERS DO BOT
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = config.get("price_target", 190)
    msg = (
        "Bot de Monitoramento de Passagens\n\n"
        f"Rota: Fortaleza - Recife\n"
        f"Meta: R$ {target}\n"
        f"Rastreamento: 08:00, 14:00, 18:00\n"
        f"Dados: REAIS (Google Flights/Skyscanner)\n\n"
        "Comandos:\n"
        "/search - buscar agora\n"
        "/meta [valor] - alterar meta\n"
        "/status - ver status\n"
        "/links - ver links\n"
        "/help - ajuda"
    )
    await update.message.reply_text(msg)

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando passagens REAIS... aguarde (pode levar ate 30s)")
    flights = search_real_flights()
    target = config.get("price_target", 190)

    if flights and isinstance(flights, list) and len(flights) > 0:
        best = flights[0]
        price = best.get("price", 0)
        airline = best.get("airline", "N/A")
        stops = best.get("stops", 0)
        date = best.get("date", "")
        departure = best.get("departure", "")
        duration = best.get("duration", "")

        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)

        stops_txt = "Direto" if stops == 0 else f"{stops} parada(s)"

        if price <= target:
            msg = f"META BATIDA!\n\nR$ {price} - {airline}\n{stops_txt}"
            if date:
                msg += f"\nData: {date}"
            if departure:
                msg += f"\nPartida: {departure}"
            if duration:
                msg += f"\nDuracao: {duration}"
            msg += f"\n\nBuscar em:\n{build_links_text()}"
        else:
            msg = f"Resultado da Busca\n\nMelhor preco: R$ {price}\nMeta: R$ {target}\nDiferenca: +R$ {price - target}\nCompanhia: {airline}\n{stops_txt}"
            if date:
                msg += f"\nData: {date}"
            if departure:
                msg += f"\nPartida: {departure}"
            if duration:
                msg += f"\nDuracao: {duration}"
            msg += f"\n\nBuscar em:\n{build_links_text()}"

        # Se tem mais voos, mostrar top 3
        if len(flights) > 1:
            msg += "\n\nOutras opcoes:"
            for f in flights[1:3]:
                fp = f.get("price", 0)
                fa = f.get("airline", "N/A")
                msg += f"\n  R$ {fp} - {fa}"

    else:
        msg = f"Nao encontrei precos automaticamente.\nTente buscar manualmente:\n\n{build_links_text()}"

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

async def scheduled_search(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Busca agendada iniciada")
    flights = search_real_flights()
    target = config.get("price_target", 190)
    if flights and isinstance(flights, list) and len(flights) > 0:
        best = flights[0]
        price = best.get("price", 0)
        airline = best.get("airline", "N/A")
        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)
        if price <= target:
            msg = f"META BATIDA!\n\nR$ {price} - {airline}\nFortaleza - Recife\n\nBuscar em:\n{build_links_text()}"
        else:
            msg = f"Busca Automatica\n\nMelhor: R$ {price} ({airline})\nMeta: R$ {target}\nFalta: R$ {price - target}"
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
    logger.info("BOT DE PASSAGENS AEREAS - INICIANDO (DADOS REAIS)")
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
