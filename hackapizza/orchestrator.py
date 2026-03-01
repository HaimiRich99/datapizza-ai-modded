"""
Orchestratore — ascolta gli eventi SSE e lancia l'agente giusto per ogni fase.

Fasi e agenti:
  speaking    → strategy_agent (avvio anticipato) + snapshot
  closed_bid  → bid_agent legge strategy.json già pronto
  waiting     → menu_agent → market_agent   (compone menu, compra/vende)
  serving     → serving_agent               (apre ristorante, prepara e serve)
  stopped     → snapshot finale del turno

Esegui: python orchestrator.py
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

from auction_analyst import run_auction_analyst
from bid_agent import run_bid_agent
from menu_agent import run_menu_agent
from server_client import HackapizzaClient
import serving_agent as _serving
from snapshot import main as run_snapshot
from strategy_agent import run_strategy_agent

load_dotenv()

TEAM_ID: int = 24
TEAM_API_KEY: str = os.getenv("TEAM_API_KEY", "")
BASE_URL: str = "https://hackapizza.datapizza.tech"

if not TEAM_API_KEY:
    raise SystemExit("Imposta TEAM_API_KEY nel file .env")


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {datetime.now().strftime('%H:%M:%S')} {msg}")


# ---------------------------------------------------------------------------
# Stato condiviso
# ---------------------------------------------------------------------------

current_turn_id: int = 0
_running_task: asyncio.Task | None = None  # un solo agente alla volta per fase
_in_serving: bool = False                  # True durante la fase serving
_strategy_task: asyncio.Task | None = None # avviato in speaking, letto in closed_bid

_STRATEGY_PATH = Path(__file__).parent / "explorer_data" / "strategy.json"


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
    global _strategy_task
    log("PHASE", "speaking — avvio anticipato strategy + snapshot")
    # Avvia la strategy subito come task indipendente (non cancellata da _cancel_running)
    if _strategy_task and not _strategy_task.done():
        _strategy_task.cancel()
    _strategy_task = asyncio.create_task(run_strategy_agent())
    # Snapshot in parallelo (può essere cancellato senza problemi)
    await run_snapshot(current_turn_id)


async def on_closed_bid() -> None:
    global _strategy_task
    log("PHASE", "closed_bid — attendo strategy e invio offerte")

    target_ingredients: list[str] | None = None
    primary_count: int = 0

    # 1. Aspetta la strategy task se ancora in corso
    if _strategy_task is not None and not _strategy_task.done():
        log("BID", "strategy ancora in corso — attendo...")
        try:
            target_ingredients, primary_count = await _strategy_task
        except asyncio.CancelledError:
            log("BID", "strategy cancellata")
        except Exception as exc:
            log("BID", f"strategy errore: {exc}")
    elif _strategy_task is not None and not _strategy_task.cancelled():
        try:
            target_ingredients, primary_count = _strategy_task.result()
        except Exception:
            pass
    _strategy_task = None

    # 2. Fallback: leggi strategy.json scritto in precedenza
    if not target_ingredients and _STRATEGY_PATH.exists():
        try:
            data = json.loads(_STRATEGY_PATH.read_text(encoding="utf-8"))
            target_ingredients = data.get("target_ingredients") or []
            primary_count = data.get("primary_count", 0)
            if target_ingredients:
                log("BID", f"strategia letta da file ({len(target_ingredients)} ingredienti)")
        except Exception as exc:
            log("BID", f"errore lettura strategy.json: {exc}")

    if target_ingredients:
        await run_bid_agent(preferred_ingredients=target_ingredients, primary_count=primary_count)
    else:
        log("BID", "nessuna strategy disponibile — fallback random")
        await run_bid_agent()


async def on_waiting() -> None:
    log("PHASE", "waiting — market (vendi+compra) → menu (componi) → apri → market (compra mancanti)")
    # 3. apri il ristorante subito dopo aver composto il menu
    async with HackapizzaClient(BASE_URL, TEAM_API_KEY, TEAM_ID) as client:
        try:
            await client.update_restaurant_is_open(True)
            log("WAIT", "ristorante aperto")
        except Exception as exc:
            log("WAIT", f"WARN apertura ristorante: {exc}")


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
    # Rileva il messaggio server con i risultati dell'asta (arriva dopo closed_bid)
    sender = data.get("sender", "")
    payload = data.get("payload", "")
    if sender == "server" and "try to buy:" in payload and "result:" in payload:
        log("AUCTION", "risultati asta ricevuti — avvio analisi in background")
        asyncio.create_task(
            _safe_auction_analysis(payload, current_turn_id)
        )


async def _safe_auction_analysis(text: str, turn_id: int) -> None:
    try:
        await run_auction_analyst(text, turn_id)
    except Exception as exc:
        log("AUCTION", f"analisi fallita: {exc}")


EVENT_HANDLERS: dict[str, Any] = {
    "game_started":         on_game_started,
    "game_phase_changed":   on_game_phase_changed,
    "game_reset":           on_game_reset,
    "client_spawned":       on_client_spawned,
    "preparation_complete": on_preparation_complete,
    "new_message":          on_new_message,
    "message":              on_message,
}


##########################################################################################
#                                    DANGER ZONE                                         #
##########################################################################################
# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.


# It is the central event dispatcher used by all handlers.
async def dispatch_event(event_type: str, event_data: dict[str, Any]) -> None:
    handler = EVENT_HANDLERS.get(event_type)
    if not handler:
        return
    try:
        await handler(event_data)
    except Exception as exc:
        log("ERROR", f"handler failed for {event_type}: {exc}")


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It parses SSE lines and translates them into internal events.
async def handle_line(raw_line: bytes) -> None:
    if not raw_line:
        return

    line = raw_line.decode("utf-8", errors="ignore").strip()
    if not line:
        return

    # Standard SSE data format: data: ...
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "connected":
            log("SSE", "connected")
            return
        line = payload

    try:
        event_json = json.loads(line)
    except json.JSONDecodeError:
        log("SSE", f"raw: {line}")
        return

    event_type = event_json.get("type", "unknown")
    event_data = event_json.get("data", {})
    if isinstance(event_data, dict):
        await dispatch_event(event_type, event_data)
    else:
        await dispatch_event(event_type, {"value": event_data})


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It owns the SSE HTTP connection lifecycle.
async def listen_once(session: aiohttp.ClientSession) -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": TEAM_API_KEY}

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connection open")
        async for line in response.content:
            await handle_line(line)


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It controls script exit behavior when the SSE connection drops.
async def listen_once_and_exit_on_drop() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        await listen_once(session)
        log("SSE", "connection closed, exiting")


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# Keep this minimal to avoid changing startup behavior.
async def main() -> None:
    log("INIT", f"team={TEAM_ID} base_url={BASE_URL}")
    await listen_once_and_exit_on_drop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INIT", "client stopped")
