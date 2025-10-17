#!/usr/bin/env python3
"""
HIGH FLYER backend (Python)
Conecta al WS de pragmaticplay, procesa resultados y envía alertas a Telegram / WhatsApp.
Configurar variables de entorno (ver README_DEPLOY.md).
"""

import asyncio
import json
import logging
import os
from datetime import datetime

import aiohttp
import requests
import websockets

# ---------- Config ----------
TELEGRAM_TOKEN = "7146220577:AAFigkvKan9mEN_PUxZukcop-OiSJ8wA-Gc"  # required for Telegram alerts
TELEGRAM_CHAT_IDS = os.environ.get("TELEGRAM_CHAT_IDS", "-4942057806").split(",")  # CSV
WHATSAPP_WEBHOOK = os.environ.get("WHATSAPP_WEBHOOK")  # optional: endpoint that accepts {"groupName","message"}
WS_URI = os.environ.get("WS_URI", "wss://dga.pragmaticplaylive.net/ws")
CASINO_ID = os.environ.get("CASINO_ID", "ppcdk00000005349")
KEYS = json.loads(os.environ.get("WS_KEYS", "[2201]"))  # default [2201]
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "800"))  # max hidden items to keep
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5"))  # seconds

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("highflyer")

# ---------- State ----------
hidden_table_data = []
game_records = {}  # gameId -> result
eventos2xx = []  # list of events dict {round, alerted9, alerted10, alerted11, done, result}
processed_event_rounds = set()
total_aciertos = 0
total_fallos = 0

# Lock for concurrency safety
state_lock = asyncio.Lock()

# ---------- Helpers ----------
def slice_gameid_to_round(game_id: str):
    try:
        if len(game_id) <= 2:
            return None
        return int(game_id[:-2])
    except Exception:
        return None

def fmt_time_iso(ts_str):
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        try:
            v = float(ts_str)
            if v > 1e12:
                v = v / 1000.0
            dt = datetime.utcfromtimestamp(v)
        except Exception:
            dt = datetime.utcnow()
    return dt.strftime("%H:%M:%S")

# ---------- Notifications ----------
def send_telegram_sync(message: str):
    token = TELEGRAM_TOKEN
    if not token:
        log.warning("TELEGRAM_TOKEN no configurado. Ignorando envío a Telegram.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {"chat_id": chat_id.strip(), "text": message}
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                log.error("Error Telegram %s -> %s", r.status_code, r.text)
        except Exception as e:
            log.exception("Excepción enviando a Telegram: %s", e)

async def send_telegram(message: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_telegram_sync, message)

async def send_whatsapp(group_name: str, message: str):
    if not WHATSAPP_WEBHOOK:
        log.debug("WHATSAPP_WEBHOOK no configurado. Ignorando WhatsApp.")
        return
    try:
        async with aiohttp.ClientSession() as session:
            payload = {"groupName": group_name, "message": message}
            async with session.post(WHATSAPP_WEBHOOK, json=payload, timeout=10) as resp:
                if resp.status >= 300:
                    text = await resp.text()
                    log.error("WhatsApp webhook error %s: %s", resp.status, text)
    except Exception as e:
        log.exception("Error enviando a WhatsApp: %s", e)

# ---------- Event registration ----------
def registrar_eventos_2xx_locked():
    existing_rounds = {ev["round"] for ev in eventos2xx}
    for item in hidden_table_data:
        try:
            round_num = int(item[0])
            value = float(item[1])
        except Exception:
            continue
        if 2.0 <= value < 3.0 and round_num not in processed_event_rounds and round_num not in existing_rounds:
            eventos2xx.append({
                "round": round_num,
                "alerted9": False,
                "alerted10": False,
                "alerted11": False,
                "done": False,
                "result": None
            })
            existing_rounds.add(round_num)
            log.info("Evento 2xx registrado para round %s (valor %.2f)", round_num, value)

# ---------- Event processing ----------
async def procesar_eventos_2xx_locked():
    global total_aciertos, total_fallos, eventos2xx

    for ev in list(eventos2xx):
        if ev.get("done"):
            continue

        try:
            base_index = next(
                i for i, d in enumerate(hidden_table_data)
                if float(d[0]) == float(ev["round"])
            )
        except StopIteration:
            continue

        # Verificar fuerza (pos 1–5)
        tiene_fuerza = any(
            float(hidden_table_data[j][1]) > 2.99
            for j in range(base_index + 1, min(base_index + 6, len(hidden_table_data)))
        )
        if not tiene_fuerza:
            processed_event_rounds.add(ev["round"])
            continue

        # Verificar debilidad (pos 6–8)
        debilidad = False
        if base_index + 8 < len(hidden_table_data):
            v6, v7, v8 = map(lambda x: float(hidden_table_data[x][1]), range(base_index + 6, base_index + 9))
            if v6 < 2 and v7 < 2 and v8 < 2:
                debilidad = True
        if debilidad:
            processed_event_rounds.add(ev["round"])
            continue

        pos9, pos10, pos11 = base_index + 9, base_index + 10, base_index + 11

        # POS9
        if not ev["alerted9"] and pos9 < len(hidden_table_data):
            ev["alerted9"] = True
            val9 = float(hidden_table_data[pos9][1])
            log.info(f"🚨 ALERTA POS9 round={ev['round']} -> {val9:.2f}X (Entrar ahora)")

            message = f"🚨 JUEGO HIGH FLYER 🚨\n🚨 ALERTA IMPORTANTE 🚨\nCoeficiente: {val9:.2f}X\n¡ENTRAR AHORA!"
            await send_whatsapp('SP_BETS_HIGHFLYER', message)
            await send_telegram(message)
            continue

        # POS10
        if not ev["alerted10"] and pos10 < len(hidden_table_data):
            ev["alerted10"] = True
            val10 = float(hidden_table_data[pos10][1])
            if val10 >= 1.50:
                ev["done"] = True
                ev["result"] = "success"
                total_aciertos += 1
                processed_event_rounds.add(ev["round"])
                log.info(f"✅ Ganada en Primer Intento round={ev['round']} val={val10:.2f}X")

                message = f"✈️ HF Coeficiente: {val10:.2f}X\n✅ Ganada 💰\nGanadas: {total_aciertos} Perdidas: {total_fallos}"
                await send_whatsapp('SP_BETS_HIGHFLYER', message)
                await send_telegram(message)
            else:
                log.info(f"🛬 Segundo Intento round={ev['round']} val={val10:.2f}X")
                message = f"🛬 HF Coeficiente: {val10:.2f}X\n📉 Segundo Intento"
                await send_whatsapp('SP_BETS_HIGHFLYER', message)
                await send_telegram(message)
            continue

        # POS11
        if not ev["alerted11"] and pos11 < len(hidden_table_data):
            ev["alerted11"] = True
            val11 = float(hidden_table_data[pos11][1])
            if val11 >= 1.50:
                ev["result"] = "success"
                total_aciertos += 1
                log.info(f"✈️ Ganada en Segundo Intento round={ev['round']} val={val11:.2f}X")

                message = f"✈️ HF Coeficiente: {val11:.2f}X\n✅ Ganada en Segundo Intento 💰\nGanadas: {total_aciertos} Perdidas: {total_fallos}"
                await send_whatsapp('SP_BETS_HIGHFLYER', message)
                await send_telegram(message)
            else:
                ev["result"] = "fail"
                total_fallos += 1
                log.info(f"❌ Perdida round={ev['round']} val={val11:.2f}X")

                message = f"🛬 HF Coeficiente: {val11:.2f}X\n❌ PERDIDA!!!\nGanadas: {total_aciertos} Perdidas: {total_fallos}"
                await send_whatsapp('SP_BETS_HIGHFLYER', message)
                await send_telegram(message)

            ev["done"] = True
            processed_event_rounds.add(ev["round"])

    # limpieza final
    before = len(eventos2xx)
    eventos2xx = [ev for ev in eventos2xx if not ev.get("done")]
    after = len(eventos2xx)
    if before != after:
        log.info(f"🧹 Limpieza eventos2xx: {before} → {after}")

# ---------- WebSocket handling ----------
async def handle_ws():
    global hidden_table_data, game_records
    while True:
        try:
            log.info("Conectando a WS %s ...", WS_URI)
            async with websockets.connect(WS_URI, ping_interval=20, ping_timeout=10) as ws:
                req = {"type": "subscribe", "casinoId": CASINO_ID, "key": KEYS}
                await ws.send(json.dumps(req))
                log.info("Suscrito al feed: casinoId=%s key=%s", CASINO_ID, KEYS)

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue

                    game_results = data.get("gameResult", [])
                    if not game_results:
                        continue

                    async with state_lock:
                        changed = False
                        for result in game_results:
                            gid = result.get("gameId")
                            res = result.get("result")
                            time = result.get("time") or result.get("timestamp") or datetime.utcnow().isoformat()
                            if gid is None or res is None:
                                continue
                            try:
                                resf = float(res)
                            except Exception:
                                try:
                                    resf = float(str(res).replace("x", ""))
                                except:
                                    continue
                            if gid not in game_records:
                                game_records[gid] = resf
                                round_id = slice_gameid_to_round(gid)
                                if round_id is None:
                                    continue
                                hidden_table_data.append([str(round_id), f"{resf:.2f}", fmt_time_iso(time)])
                                if len(hidden_table_data) > HISTORY_LIMIT:
                                    hidden_table_data = hidden_table_data[-HISTORY_LIMIT:]
                                changed = True
                                log.info("Nuevo resultado: round=%s res=%s time=%s", round_id, f"{resf:.2f}", fmt_time_iso(time))

                        if changed:
                            registrar_eventos_2xx_locked()
                            await procesar_eventos_2xx_locked()

        except Exception as e:
            log.exception("WebSocket error / desconexión: %s", e)
            log.info("Reconectando en %s segundos...", RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)

# ---------- HTTP status ----------
from aiohttp import web

async def handle_status(request):
    async with state_lock:
        return web.json_response({
            "ok": True,
            "hidden_count": len(hidden_table_data),
            "events_pending": len(eventos2xx),
            "total_aciertos": total_aciertos,
            "total_fallos": total_fallos,
            "last_records": hidden_table_data[-6:]
        })

async def start_web_server(app_host="0.0.0.0", app_port=int(os.environ.get("PORT", "8000"))):
    app = web.Application()
    app.router.add_get("/status", handle_status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, app_host, app_port)
    await site.start()
    log.info("Status HTTP server listening on %s:%s", app_host, app_port)

# ---------- Main ----------
async def main():
    await start_web_server()
    await handle_ws()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario. Saliendo...")
