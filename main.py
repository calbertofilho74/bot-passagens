#!/usr/bin/env python3
"""
Bot Telegram - Monitoramento de Passagens Aéreas
Fortaleza (FOR) ↔ Recife (REC)
Deploy: Render.com (Free Tier) com Webhook
"""

import asyncio
import logging
import os
import json
import re
from datetime import datetime, timedelta

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

# ============================================================
# CONFIGURAÇÃO (via variáveis de ambiente)
# ============================================================

TOKEN = os.environ.get("BOT_TOKEN", "8773736229:AAFJfpmemTEwH7YpiBe7rxi1Qpqtf2SmNUo")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "507830852"))
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

CONFIG_FILE = "bot_config.json"

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURAÇÃO PERSISTENTE
# ============================================================

def load_config():
    """Carrega configuração"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"price_target": 190, "last_price": None, "search_count": 0}


def save_config(cfg):
    """Salva configuração"""
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        logger.error(f"Erro ao salvar config: {e}")


config = load_config()

# ============================================================
# WEB SCRAPING REAL
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _extract_prices(text: str) -> list[int]:
    """Extrai preços em reais de um texto HTML."""
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


def scrape_google_flights() -> dict | None:
    """Tenta extrair preços do Google Flights."""
    try:
        tomorrow = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        return_date = (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d")
        url = (
            f"https://www.google.com/travel/flights/search"
            f"?tfs=CBwQAhooagwIAxIIL20vMDN3cDkSCjIwMjYtMDUtMDJyDAgDEggvbS8wN3VqMRIoagwIAxIIL20vMDd1ajESCjIwMjYtMDUtMDdyDAgDEggvbS8wM3dwOXABggELCP___________wFAAUgBmAEB"
        )
        resp = http_requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            prices = _extract_prices(resp.text)
            if prices:
                return {"source": "Google Flights", "price": prices[0], "link": url}
    except Exception as e:
        logger.warning(f"Google Flights erro: {e}")
    return None


def scrape_skyscanner() -> dict | None:
    """Tenta extrair preços do Skyscanner."""
    try:
        url = "https://www.skyscanner.com.br/transport/flights/for/rec/"
        resp = http_requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            prices = _extract_prices(resp.text)
            if prices:
                return {"source": "Skyscanner", "price": prices[0], "link": url}
    except Exception as e:
        logger.warning(f"Skyscanner erro: {e}")
    return None


def scrape_kayak() -> dict | None:
    """Tenta extrair preços do Kayak."""
    try:
        url = "https://www.kayak.com.br/flights/FOR-REC"
        resp = http_requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            prices = _extract_prices(resp.text)
            if prices:
                return {"source": "Kayak", "price": prices[0], "link": url}
    except Exception as e:
        logger.warning(f"Kayak erro: {e}")
    return None


def search_all_sources() -> dict | None:
    """Busca em todas as fontes e retorna o melhor preço."""
    logger.info("🔍 Iniciando busca em múltiplas fontes...")
    results: list[dict] = []

    for fn in (scrape_google_flights, scrape_skyscanner, scrape_kayak):
        r = fn()
        if r:
            results.append(r)
            logger.info(f"  ✅ {r['source']}: R$ {r['price']}")

    if results:
        best = min(results, key=lambda x: x["price"])
        all_sources = ", ".join(
            f"{r['source']} R${r['price']}" for r in sorted(results, key=lambda x: x["price"])
        )
        best["all_sources"] = all_sources
        best["total_sources"] = len(results)
        return best

    logger.warning("  ❌ Nenhum preço encontrado")
    return None


# ============================================================
# LINKS DE BUSCA
# ============================================================

SEARCH_LINKS = {
    "Google Flights": "https://www.google.com/travel/flights?tfs=CBwQAhooagwIAxIIL20vMDN3cDkSCjIwMjYtMDUtMDJyDAgDEggvbS8wN3VqMRIoagwIAxIIL20vMDd1ajESCjIwMjYtMDUtMDdyDAgDEggvbS8wM3dwOXABggELCP___________wFAAUgBmAEB",
    "Skyscanner": "https://www.skyscanner.com.br/transport/flights/for/rec/",
    "Kayak": "https://www.kayak.com.br/flights/FOR-REC",
    "Decolar": "https://www.decolar.com/shop/flights/results/roundtrip/FOR/REC",
}


def build_links_text() -> str:
    """Monta texto com links de busca."""
    parts = []
    for name, url in SEARCH_LINKS.items():
        parts.append(f'<a href="{url}">{name}</a>')
    return " | ".join(parts)


# ============================================================
# HANDLERS DO BOT
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    target = config.get("price_target", 190)
    msg = (
        "👋 <b>Bot de Monitoramento de Passagens</b>\n\n"
        f"📍 <b>Rota:</b> Fortaleza → Recife (ida e volta)\n"
        f"💰 <b>Meta:</b> R$ {target}\n"
        f"⏰ <b>Rastreamento:</b> 3× ao dia (08:00 · 14:00 · 18:00)\n\n"
        "<b>Comandos:</b>\n"
        "/search — buscar passagens agora\n"
        "/meta [valor] — alterar meta de preço\n"
        "/status — ver status do bot\n"
        "/links — ver links de busca\n"
        "/help — ajuda"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔍 Buscando passagens... aguarde")

    result = search_all_sources()
    target = config.get("price_target", 190)

    if result:
        price = result["price"]
        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)

        if price <= target:
            msg = (
                f"🎉 <b>META BATIDA!</b>\n\n"
                f"💰 <b>R$ {price}</b> ✅\n"
                f"✈️ Fortaleza → Recife\n"
                f"🏢 Fonte: {result['source']}\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                f"🔗 {build_links_text()}"
            )
        else:
            msg = (
                f"✈️ <b>Resultado da Busca</b>\n\n"
                f"💰 Melhor preço: <b>R$ {price}</b>\n"
                f"🎯 Meta: R$ {target}\n"
                f"📊 Diferença: +R$ {price - target}\n"
                f"🏢 Fonte: {result['source']}\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                f"🔗 {build_links_text()}"
            )
    else:
        msg = (
            "❌ <b>Nenhum preço encontrado</b>\n\n"
            "Os sites podem estar bloqueando a busca automática.\n"
            "Tente buscar manualmente:\n\n"
            f"🔗 {build_links_text()}"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_meta(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        target = config.get("price_target", 190)
        await update.message.reply_text(
            f"💰 <b>Meta atual:</b> R$ {target}\n\nPara alterar: <code>/meta 250</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        novo = int(ctx.args[0])
        if novo <= 0:
            raise ValueError
        config["price_target"] = novo
        save_config(config)
        await update.message.reply_text(
            f"✅ Meta alterada para <b>R$ {novo}</b>", parse_mode=ParseMode.HTML
        )
        logger.info(f"Meta alterada para R$ {novo}")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Valor inválido. Use: /meta 250")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    target = config.get("price_target", 190)
    last = config.get("last_price")
    count = config.get("search_count", 0)
    last_txt = f"R$ {last}" if last else "nenhuma busca ainda"
    msg = (
        f"📊 <b>Status do Bot</b>\n\n"
        f"✅ Bot online\n"
        f"💰 Meta: R$ {target}\n"
        f"📍 Rota: FOR → REC\n"
        f"⏰ Rastreamento: 08:00 · 14:00 · 18:00\n"
        f"🔍 Buscas realizadas: {count}\n"
        f"📌 Último preço: {last_txt}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_links(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "🔗 <b>Links de Busca Manual</b>\n\n"
        "Clique para ver preços reais:\n\n"
        f"🔗 {build_links_text()}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "<b>📖 Ajuda</b>\n\n"
        "/start — iniciar o bot\n"
        "/search — buscar passagens agora\n"
        "/meta [valor] — alterar meta\n"
        "/status — ver status\n"
        "/links — ver links de busca\n"
        "/help — esta mensagem\n\n"
        "<b>Como funciona:</b>\n"
        "O bot busca automaticamente 3× ao dia e envia alertas "
        "quando encontra preços ≤ sua meta."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Use /help para ver os comandos disponíveis.")


# ============================================================
# BUSCA AGENDADA (3× ao dia)
# ============================================================

async def scheduled_search(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Busca automática — roda nos horários agendados."""
    logger.info("⏰ Busca agendada iniciada")
    result = search_all_sources()
    target = config.get("price_target", 190)

    if result:
        price = result["price"]
        config["last_price"] = price
        config["search_count"] = config.get("search_count", 0) + 1
        save_config(config)

        if price <= target:
            msg = (
                f"🎉 <b>META BATIDA!</b>\n\n"
                f"💰 <b>R$ {price}</b> ✅\n"
                f"✈️ Fortaleza → Recife\n"
                f"🏢 {result['source']}\n"
                f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
                f"🔗 {build_links_text()}"
            )
            await ctx.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            # Envia relatório mesmo quando não bate meta
            msg = (
                f"📊 <b>Busca Automática</b>\n\n"
                f"💰 Melhor: R$ {price}\n"
                f"🎯 Meta: R$ {target}\n"
                f"📊 Falta: R$ {price - target}\n"
                f"🏢 {result['source']}\n"
                f"⏰ {datetime.now().strftime('%H:%M')}"
            )
            await ctx.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
    else:
        logger.warning("Busca agendada: nenhum resultado")


# ============================================================
# STARLETTE (servidor web para webhook + health check)
# ============================================================

async def health(_: Request) -> PlainTextResponse:
    """Health check para o Render não derrubar o serviço."""
    return PlainTextResponse("OK")


async def telegram_webhook(request: Request) -> Response:
    """Recebe updates do Telegram via webhook."""
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
# MAIN
# ============================================================

# Build application globally so webhook handler can reference it
application = Application.builder().token(TOKEN).updater(None).build()

# Register command handlers
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("search", cmd_search))
application.add_handler(CommandHandler("meta", cmd_meta))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("links", cmd_links))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback))

# Schedule searches (3x/day) — Brasilia timezone offsets
from datetime import time as dt_time

application.job_queue.run_daily(scheduled_search, time=dt_time(hour=11, minute=0))  # 08:00 BRT
application.job_queue.run_daily(scheduled_search, time=dt_time(hour=17, minute=0))  # 14:00 BRT
application.job_queue.run_daily(scheduled_search, time=dt_time(hour=21, minute=0))  # 18:00 BRT


async def main() -> None:
    """Inicia o bot com webhook."""
    logger.info("=" * 60)
    logger.info("🚀 BOT DE PASSAGENS AÉREAS — INICIANDO")
    logger.info(f"   Meta: R$ {config.get('price_target', 190)}")
    logger.info(f"   Chat ID: {ADMIN_CHAT_ID}")
    logger.info(f"   Porta: {PORT}")
    logger.info("=" * 60)

    # Inicializa o bot
    await application.initialize()
    await application.start()

    # Configura webhook
    webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
    if RENDER_EXTERNAL_URL:
        await application.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
        logger.info(f"✅ Webhook configurado: {webhook_url}")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL não definida — webhook não configurado")

    # Roda Starlette
    config_uvicorn = uvicorn.Config(
        app=starlette_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config_uvicorn)
    await server.serve()

    # Cleanup
    await application.stop()
    await application.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
