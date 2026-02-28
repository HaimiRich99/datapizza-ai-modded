"""
Agente Serving — Fase serving

Logica:
1. Apre il ristorante (update_restaurant_is_open)
2. Carica il menu attivo in memoria
3. All'arrivo di ogni cliente (client_spawned, via orchestratore):
   - Abbina l'orderText a un piatto nel menu
   - Avvia la preparazione (prepare_dish)
4. Quando il piatto è pronto (preparation_complete, via orchestratore):
   - Serve il piatto al cliente (serve_dish)
5. Alla cancellazione del task (fase terminata): chiude il ristorante

L'agente espone due callback asincrone pensate per essere chiamate
dall'orchestratore sugli eventi SSE:
  - handle_new_client(data)      ← client_spawned
  - handle_dish_ready(data)      ← preparation_complete

Esegui standalone: python serving_agent.py [turn_id]
"""

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("API_KEY", "")

POLL_INTERVAL = 5.0   # secondi tra polling fallback dei meals


# ---------------------------------------------------------------------------
# Stato condiviso per la fase serving
# ---------------------------------------------------------------------------

# {client_id: {"name": str, "order_text": str, "dish": str}}
_pending_clients: dict[str, dict] = {}

# {dish_name: [client_id, ...]}  — FIFO: il primo in lista è il prossimo da servire
_dish_queue: dict[str, list[str]] = {}

# piatti nel menu: {nome_lowercase: nome_originale}
_menu_names: dict[str, str] = {}

# client_id già processati (per evitare duplicati dal polling)
_seen_clients: set[str] = set()


# ---------------------------------------------------------------------------
# Matching ordine → piatto del menu
# ---------------------------------------------------------------------------

def _match_dish(order_text: str) -> str | None:
    """
    Cerca nel menu il piatto che meglio corrisponde all'orderText del cliente.
    Prima prova match per sottostringa, poi per parole in comune.
    """
    if not order_text or not _menu_names:
        return None

    order_lower = order_text.lower()

    # 1. Sottostringa diretta
    for name_lower, name_orig in _menu_names.items():
        if name_lower in order_lower or order_lower in name_lower:
            return name_orig

    # 2. Parole in comune (score = intersezione)
    order_words = set(order_lower.split())
    best_name: str | None = None
    best_score = 0
    for name_lower, name_orig in _menu_names.items():
        dish_words = set(name_lower.split())
        score = len(order_words & dish_words)
        if score > best_score:
            best_score = score
            best_name = name_orig

    return best_name if best_score > 0 else None


# ---------------------------------------------------------------------------
# Helpers apertura/chiusura
# ---------------------------------------------------------------------------

async def _set_open(is_open: bool) -> None:
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            await client.update_restaurant_is_open(is_open)
            stato = "aperto" if is_open else "chiuso"
            print(f"[SERVING] ristorante {stato}")
        except Exception as exc:
            print(f"[SERVING] WARN update_restaurant_is_open({is_open}): {exc}")


# ---------------------------------------------------------------------------
# Callback per l'orchestratore
# ---------------------------------------------------------------------------

async def handle_new_client(data: dict[str, Any]) -> None:
    """
    Chiamata dall'orchestratore quando arriva evento client_spawned.
    Abbina l'ordine a un piatto e avvia la preparazione.
    """
    # Prova vari campi possibili per l'id cliente
    client_id = (
        data.get("clientId")
        or data.get("id")
        or data.get("clientName")
    )
    client_name = data.get("clientName", str(client_id))
    order_text = data.get("orderText", "")

    if not client_id:
        print(f"[SERVING] client_spawned senza id: {data}")
        return

    if client_id in _seen_clients:
        return  # già gestito
    _seen_clients.add(client_id)

    dish = _match_dish(order_text)
    print(
        f"[SERVING] cliente {client_name!r} | ordine: {order_text!r} "
        f"→ {dish or 'NESSUN MATCH'}"
    )

    if not dish:
        print("[SERVING] SKIP: nessun piatto nel menu corrisponde")
        return

    _pending_clients[client_id] = {
        "name": client_name,
        "order_text": order_text,
        "dish": dish,
    }

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            result = await client.prepare_dish(dish)
            print(f"[SERVING] prepare_dish({dish!r}) → {result}")
            _dish_queue.setdefault(dish, []).append(client_id)
        except Exception as exc:
            print(f"[SERVING] ERRORE prepare_dish({dish!r}): {exc}")
            _pending_clients.pop(client_id, None)


async def handle_dish_ready(data: dict[str, Any]) -> None:
    """
    Chiamata dall'orchestratore quando arriva evento preparation_complete.
    Serve il piatto pronto al primo cliente in coda.
    """
    dish = (
        data.get("dish")
        or data.get("dishName")
        or data.get("name")
    )
    if not dish:
        print(f"[SERVING] preparation_complete senza dish: {data}")
        return

    waiting = _dish_queue.get(dish, [])
    if not waiting:
        print(f"[SERVING] {dish!r} pronto ma nessun cliente in coda — ignorato")
        return

    client_id = waiting.pop(0)
    client_info = _pending_clients.pop(client_id, {})
    client_name = client_info.get("name", str(client_id))

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            result = await client.serve_dish(dish, client_id)
            print(f"[SERVING] serve_dish({dish!r}, {client_id}) → {client_name} | {result}")
        except Exception as exc:
            print(f"[SERVING] ERRORE serve_dish({dish!r}, {client_id}): {exc}")


# ---------------------------------------------------------------------------
# Entry point principale
# ---------------------------------------------------------------------------

async def run_serving_agent(turn_id: int = 0) -> None:
    """
    Apre il ristorante, carica il menu, poi gira in polling finché
    il task non viene cancellato dall'orchestratore (cambio fase).
    """
    global _pending_clients, _dish_queue, _menu_names, _seen_clients

    # Reset stato per il nuovo turno
    _pending_clients = {}
    _dish_queue = {}
    _menu_names = {}
    _seen_clients = set()

    # Carica menu (il ristorante è già stato aperto nella fase waiting)
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            menu_raw = await client.get_menu()
            for item in menu_raw:
                name = item.get("name", "") if isinstance(item, dict) else str(item)
                if name:
                    _menu_names[name.lower()] = name
            print(f"[SERVING] menu: {list(_menu_names.values()) or '(vuoto)'}")
        except Exception as exc:
            print(f"[SERVING] WARN caricamento menu: {exc}")

    print(f"[SERVING] in attesa di clienti (turno {turn_id})…")

    # Polling fallback: raccoglie clienti arrivati che l'SSE potrebbe aver perso
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
                    meals = await client.get_meals(turn_id)
                for meal in meals:
                    cid = meal.get("id") or meal.get("clientId")
                    if cid and cid not in _seen_clients:
                        await handle_new_client({
                            "clientId": cid,
                            "clientName": meal.get("clientName", str(cid)),
                            "orderText": meal.get("orderText", ""),
                        })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[SERVING] WARN polling meals: {exc}")

    except asyncio.CancelledError:
        print("[SERVING] fase terminata — chiudo ristorante")
        await _set_open(False)


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    tid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    asyncio.run(run_serving_agent(tid))
