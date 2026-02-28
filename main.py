# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "datapizza-ai",
#     "datapizza-ai-clients-openai-like",
#     "python-dotenv"
# ]
# ///

import asyncio
import json
import os
from datetime import datetime
from typing import Any, Awaitable, Callable

import aiohttp
from dotenv import load_dotenv

from hackapizza.server_client import HackapizzaClient
from hackapizza.gameorchestrator import GameOrchestrator

load_dotenv()

TEAM_ID: int = 24  # <-- imposta il tuo team ID
TEAM_API_KEY: str = os.getenv("API_KEY", "")
BASE_URL: str = "https://hackapizza.datapizza.tech"

if not TEAM_API_KEY or not TEAM_ID:
    raise SystemExit("Imposta API_KEY nel file .env e TEAM_ID in main.py")


def log(tag: str, message: str) -> None:
    print(f"[{tag}] {datetime.now()}: {message}")


# Stato condiviso del turno corrente
current_turn_id: int = 0
pending_orders: list[dict[str, Any]] = []   # clienti in attesa durante serving
prepared_dishes: list[str] = []             # piatti pronti da servire


# -------------------------------------------------------------------------
# Event handlers — modifica qui la logica dell'agente
# -------------------------------------------------------------------------

async def game_started(data: dict[str, Any]) -> None:
    global current_turn_id
    current_turn_id = data.get("turn_id", 0)
    log("EVENT", f"game started | turn_id={current_turn_id}")


async def speaking_phase_started() -> None:
    log("PHASE", "speaking")
    async with HackapizzaClient(BASE_URL, TEAM_API_KEY, TEAM_ID) as client:
        info = await client.get_restaurant()
        log("INFO", f"saldo={info.get('balance')} | inventario={info.get('inventory')}")


async def closed_bid_phase_started() -> None:
    log("PHASE", "closed_bid")
    async with HackapizzaClient(BASE_URL, TEAM_API_KEY, TEAM_ID) as client:
        recipes = await client.get_recipes()
        log("INFO", f"ricette disponibili: {len(recipes)}")

        # Esempio: offerta placeholder — sostituisci con la logica reale dell'agente
        # bids = [{"ingredient": "Farina Cosmica", "bid": 10.0, "quantity": 2}]
        # await client.closed_bid(bids)


async def waiting_phase_started() -> None:
    log("PHASE", "waiting")
    async with HackapizzaClient(BASE_URL, TEAM_API_KEY, TEAM_ID) as client:
        inventory = (await client.get_restaurant()).get("inventory", {})
        log("INFO", f"inventario aggiornato: {inventory}")

        # Esempio: imposta il menu — sostituisci con la logica reale dell'agente
        # await client.save_menu([{"name": "Pizza Cosmica", "price": 25.0}])


async def serving_phase_started() -> None:
    log("PHASE", "serving")
    pending_orders.clear()
    prepared_dishes.clear()


async def end_turn() -> None:
    log("PHASE", "stopped — turno terminato")
    pending_orders.clear()
    prepared_dishes.clear()


async def client_spawned(data: dict[str, Any]) -> None:
    client_name = data.get("clientName", "unknown")
    client_id = data.get("clientId", "")
    order_text = str(data.get("orderText", ""))
    log("CLIENT", f"nome={client_name} | ordine={order_text}")

    pending_orders.append({
        "client_id": client_id,
        "client_name": client_name,
        "order_text": order_text,
    })

    # Esempio: prepara e servi — sostituisci con logica reale dell'agente
    # async with HackapizzaClient(BASE_URL, TEAM_API_KEY, TEAM_ID) as client:
    #     await client.prepare_dish("Pizza Cosmica")


async def preparation_complete(data: dict[str, Any]) -> None:
    dish_name = data.get("dish", "unknown")
    log("KITCHEN", f"piatto pronto: {dish_name}")
    prepared_dishes.append(dish_name)

    # Esempio: servi il piatto al primo cliente in attesa
    # if pending_orders:
    #     order = pending_orders.pop(0)
    #     async with HackapizzaClient(BASE_URL, TEAM_API_KEY, TEAM_ID) as client:
    #         await client.serve_dish(dish_name, order["client_id"])


async def message(data: dict[str, Any]) -> None:
    sender = data.get("sender", "unknown")
    payload = data.get("payload", "")
    log("MSG", f"da={sender}: {payload}")


async def new_message(data: dict[str, Any]) -> None:
    sender_name = data.get("senderName", "unknown")
    text = data.get("text", "")
    log("MSG", f"privato da={sender_name}: {text}")


async def game_phase_changed(data: dict[str, Any]) -> None:
    phase = data.get("phase", "unknown")
    handlers: dict[str, Callable[[], Awaitable[None]]] = {
        "speaking": speaking_phase_started,
        "closed_bid": closed_bid_phase_started,
        "waiting": waiting_phase_started,
        "serving": serving_phase_started,
        "stopped": end_turn,
    }
    handler = handlers.get(phase)
    if handler:
        await handler()
    else:
        log("EVENT", f"fase sconosciuta: {phase}")


async def game_reset(data: dict[str, Any]) -> None:
    global current_turn_id
    current_turn_id = 0
    pending_orders.clear()
    log("EVENT", "game reset")


EVENT_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "game_started": game_started,
    "game_phase_changed": game_phase_changed,
    "game_reset": game_reset,
    "client_spawned": client_spawned,
    "preparation_complete": preparation_complete,
    "message": message,
    "new_message": new_message,
}

# -------------------------------------------------------------------------
# DANGER ZONE — non modificare sotto questa riga
# -------------------------------------------------------------------------

async def dispatch_event(event_type: str, event_data: dict[str, Any]) -> None:
    handler = EVENT_HANDLERS.get(event_type)
    if not handler:
        if event_type not in ("heartbeat",):  # ignora heartbeat silenziosamente
            log("EVENT", f"nessun handler per: {event_type}")
        return
    try:
        await handler(event_data)
    except Exception as exc:
        log("ERROR", f"handler fallito per {event_type}: {exc}")


async def handle_line(raw_line: bytes) -> None:
    if not raw_line:
        return
    line = raw_line.decode("utf-8", errors="ignore").strip()
    if not line:
        return
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "connected":
            log("SSE", "connesso")
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


async def listen_once(session: aiohttp.ClientSession) -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": TEAM_API_KEY}
    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connessione aperta")
        async for line in response.content:
            await handle_line(line)


async def listen_with_reconnect() -> None:
    """SSE con reconnect automatico in caso di caduta della connessione."""
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
    while True:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await listen_once(session)
        except aiohttp.ClientError as exc:
            log("SSE", f"connessione persa: {exc} — riconnessione in 5s")
            await asyncio.sleep(5)
        except Exception as exc:
            log("ERROR", f"errore inatteso SSE: {exc} — riconnessione in 5s")
            await asyncio.sleep(5)
        else:
            log("SSE", "connessione chiusa dal server — riconnessione in 5s")
            await asyncio.sleep(5)


async def main():
    # Prendi API Key e ID (li forniremo tramite .env o hardcoded durante la gara)
    API_KEY = os.getenv("TEAM_API_KEY", "tua-chiave-qui")
    RESTAURANT_ID = os.getenv("RESTAURANT_ID", "tuo-id-qui")
    
    orchestrator = GameOrchestrator(api_key=API_KEY, restaurant_id=RESTAURANT_ID)
    
    # Avvia l'ascolto infinito degli eventi
    await orchestrator.listen_and_route()

if __name__ == "__main__":
    asyncio.run(main())


