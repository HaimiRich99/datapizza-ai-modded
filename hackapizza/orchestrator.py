"""
Orchestratore — ascolta gli eventi SSE e lancia l'agente giusto per ogni fase.

Fasi e agenti:
  speaking    → snapshot (osservazione, nessuna azione)
  closed_bid  → strategy_agent → bid_agent  (sceglie ingredienti e offre)
  waiting     → menu_agent → market_agent   (compone menu, compra/vende)
  serving     → serving_agent               (apre ristorante, prepara e serve)
  stopped     → snapshot finale del turno

Esegui: python orchestrator.py
"""

import asyncio
import json
import os
from datetime import datetime
from typing import Any

import aiohttp
from dotenv import load_dotenv

from bid_agent import run_bid_agent
from market_agent import run_market_agent
from menu_agent import run_menu_agent
from server_client import HackapizzaClient
import serving_agent as _serving
from snapshot import main as run_snapshot
from strategy_agent import run_strategy_agent

load_dotenv()

TEAM_ID: int = 24
API_KEY: str = os.getenv("API_KEY", "")
BASE_URL: str = "https://hackapizza.datapizza.tech"

if not API_KEY:
    raise SystemExit("Imposta API_KEY nel file .env")


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {datetime.now().strftime('%H:%M:%S')} {msg}")


# ---------------------------------------------------------------------------
# Stato condiviso
# ---------------------------------------------------------------------------

current_turn_id: int = 0
_running_task: asyncio.Task | None = None  # un solo agente alla volta per fase
_in_serving: bool = False                  # True durante la fase serving


def _cancel_running() -> None:
    """Annulla l'agente precedente se ancora in esecuzione."""
    global _running_task
    if _running_task and not _running_task.done():
        log("ORCH", "annullo task precedente ancora in esecuzione")
        _running_task.cancel()
    _running_task = None


def _run(coro) -> None:
    """Lancia una coroutine come task in background (fire-and-forget con log errori)."""
    global _running_task
    _cancel_running()

    async def _wrap():
        try:
            await coro
        except asyncio.CancelledError:
            log("ORCH", "task annullato")
        except Exception as exc:
            log("ERROR", f"agente fallito: {exc}")

    _running_task = asyncio.create_task(_wrap())


# ---------------------------------------------------------------------------
# Handler per ogni fase
# ---------------------------------------------------------------------------

async def on_speaking() -> None:
    log("PHASE", "speaking — snapshot osservazione")
    await run_snapshot(current_turn_id)


async def on_closed_bid() -> None:
    log("PHASE", "closed_bid — avvio strategy + bid agent")
    target_ingredients = await run_strategy_agent()
    if target_ingredients:
        await run_bid_agent(preferred_ingredients=target_ingredients)
    else:
        log("BID", "strategy_agent non ha prodotto ingredienti, uso fallback random")
        await run_bid_agent()


async def on_waiting() -> None:
    log("PHASE", "waiting — avvio menu + market agent")
    await run_menu_agent()
    await run_market_agent()


async def on_serving() -> None:
    global _in_serving
    _in_serving = True
    log("PHASE", "serving — avvio serving agent")
    await _serving.run_serving_agent(current_turn_id)


async def on_stopped() -> None:
    global _in_serving
    _in_serving = False
    log("PHASE", "stopped — snapshot fine turno")
    await run_snapshot(current_turn_id)


# ---------------------------------------------------------------------------
# Dispatcher eventi SSE
# ---------------------------------------------------------------------------

PHASE_HANDLERS = {
    "speaking":   on_speaking,
    "closed_bid": on_closed_bid,
    "waiting":    on_waiting,
    "serving":    on_serving,
    "stopped":    on_stopped,
}


async def on_game_started(data: dict[str, Any]) -> None:
    global current_turn_id
    current_turn_id = data.get("turn_id", 0)
    log("EVENT", f"game started | turn_id={current_turn_id}")


async def on_game_phase_changed(data: dict[str, Any]) -> None:
    global current_turn_id
    phase = data.get("phase", "unknown")
    if "turn_id" in data:
        current_turn_id = data["turn_id"]
    log("EVENT", f"phase changed → {phase} | turn_id={current_turn_id}")
    handler = PHASE_HANDLERS.get(phase)
    if handler:
        _run(handler())
    else:
        log("ORCH", f"fase sconosciuta: {phase!r}")


async def on_game_reset(data: dict[str, Any]) -> None:
    global current_turn_id
    current_turn_id = 0
    _cancel_running()
    log("EVENT", "game reset")


async def on_client_spawned(data: dict[str, Any]) -> None:
    log("CLIENT", f"{data.get('clientName')} | {data.get('orderText', '')!r}")
    if _in_serving:
        await _serving.handle_new_client(data)


async def on_preparation_complete(data: dict[str, Any]) -> None:
    log("KITCHEN", f"piatto pronto: {data.get('dish')}")
    if _in_serving:
        await _serving.handle_dish_ready(data)


async def on_new_message(data: dict[str, Any]) -> None:
    log("MSG", f"da {data.get('senderName')}: {data.get('text')}")


async def on_message(data: dict[str, Any]) -> None:
    log("MSG", str(data))


EVENT_HANDLERS: dict[str, Any] = {
    "game_started":         on_game_started,
    "game_phase_changed":   on_game_phase_changed,
    "game_reset":           on_game_reset,
    "client_spawned":       on_client_spawned,
    "preparation_complete": on_preparation_complete,
    "new_message":          on_new_message,
    "message":              on_message,
}


async def dispatch(event_type: str, event_data: dict[str, Any]) -> None:
    handler = EVENT_HANDLERS.get(event_type)
    if not handler:
        if event_type != "heartbeat":
            log("EVENT", f"(ignorato) {event_type}")
        return
    try:
        await handler(event_data)
    except Exception as exc:
        log("ERROR", f"handler {event_type}: {exc}")


async def handle_line(raw: bytes) -> None:
    if not raw:
        return
    line = raw.decode("utf-8", errors="ignore").strip()
    if not line:
        return
    if line.startswith("data:"):
        line = line[5:].strip()
    if line == "connected":
        log("SSE", "connesso")
        return
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        log("SSE", f"raw: {line}")
        return
    etype = obj.get("type", "unknown")
    edata = obj.get("data", {})
    await dispatch(etype, edata if isinstance(edata, dict) else {"value": edata})


# ---------------------------------------------------------------------------
# SSE loop con reconnect
# ---------------------------------------------------------------------------

async def listen_once(session: aiohttp.ClientSession) -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": API_KEY}
    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        log("SSE", "connessione aperta")
        async for line in resp.content:
            await handle_line(line)


async def listen_loop() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    while True:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await listen_once(session)
        except aiohttp.ClientError as exc:
            log("SSE", f"connessione persa: {exc} — riconnessione in 5s")
        except Exception as exc:
            log("ERROR", f"SSE inatteso: {exc} — riconnessione in 5s")
        else:
            log("SSE", "connessione chiusa dal server — riconnessione in 5s")
        await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    log("INIT", f"team={TEAM_ID} | url={BASE_URL}")
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            info = await client.get_restaurant()
            log("INIT", f"ristorante: {info.get('name')} | saldo: {info.get('balance')}")
        except Exception as exc:
            log("INIT", f"impossibile ottenere info: {exc}")
    await listen_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INIT", "orchestratore fermato")
