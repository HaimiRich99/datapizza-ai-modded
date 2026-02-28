"""
Agente Serving — Fase serving

Logica:
1. Carica il menu attivo in memoria (il ristorante viene aperto dall'orchestratore prima di avviare questo agente)
2. All'arrivo di ogni cliente (client_spawned, via orchestratore):
   - Abbina l'orderText a un piatto nel menu
   - Avvia la preparazione (prepare_dish)
3. Quando il piatto è pronto (preparation_complete, via orchestratore):
   - Risolve l'ID numerico del cliente via /meals (serve_dish richiede l'ID numerico)
   - Serve il piatto al cliente (serve_dish)
4. Alla cancellazione del task (fase terminata): chiude il ristorante

Nota sull'ID cliente:
  L'evento SSE client_spawned contiene solo clientName, non l'ID numerico.
  L'endpoint /meals restituisce l'ID numerico. Il dict _name_to_id fa da cache
  nome→id e viene aggiornato ad ogni ciclo di polling.

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
API_KEY = os.getenv("TEAM_API_KEY", "")

POLL_INTERVAL = 5.0   # secondi tra polling fallback dei meals


# ---------------------------------------------------------------------------
# Stato condiviso per la fase serving
# ---------------------------------------------------------------------------

# {client_name: {"name": str, "order_text": str, "dish": str}}
_pending_clients: dict[str, dict] = {}

# {dish_name: [client_name, ...]}  — FIFO: il primo in lista è il prossimo da servire
_dish_queue: dict[str, list[str]] = {}

# piatti nel menu: {nome_lowercase: nome_originale}
_menu_names: dict[str, str] = {}

# clientName già processati (per evitare duplicati SSE/polling)
_seen_clients: set[str] = set()

# clientName → ID numerico recuperato da /meals (serve per serve_dish)
_name_to_id: dict[str, str] = {}

# turno corrente (usato per /meals nel fallback di risoluzione ID)
_current_turn_id: int = 0


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


def _meal_name(meal: dict) -> str:
    """Estrae il nome cliente dal record /meals (prova camelCase e snake_case)."""
    return meal.get("clientName") or meal.get("client_name") or ""


def _meal_id(meal: dict) -> str:
    """Estrae l'ID numerico dal record /meals."""
    return str(meal.get("id") or meal.get("clientId") or "")


def _update_name_to_id(meals: list[dict]) -> None:
    """Aggiorna _name_to_id da una lista di meal record."""
    for m in meals:
        cid = _meal_id(m)
        cname = _meal_name(m)
        if cid and cname:
            _name_to_id[cname] = cid


async def _resolve_numeric_id(client_name: str) -> str | None:
    """
    Cerca l'ID numerico del cliente in _name_to_id; se mancante interroga /meals.
    Ritorna l'ID come stringa oppure None.
    """
    if client_name in _name_to_id:
        return _name_to_id[client_name]
    try:
        async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as c:
            meals = await c.get_meals(_current_turn_id)
        _update_name_to_id(meals)
    except Exception as exc:
        print(f"[SERVING] WARN risoluzione id per {client_name!r}: {exc}")
    return _name_to_id.get(client_name)


# ---------------------------------------------------------------------------
# Callback per l'orchestratore
# ---------------------------------------------------------------------------

async def handle_new_client(data: dict[str, Any]) -> None:
    """
    Chiamata dall'orchestratore quando arriva evento client_spawned.
    Abbina l'ordine a un piatto e avvia la preparazione.
    Usa clientName come chiave di dedup (l'SSE non fornisce l'ID numerico).
    """
    client_name = data.get("clientName", "")
    order_text = data.get("orderText", "")

    # Aggiorna cache nome→id se l'evento contiene già un ID numerico
    numeric_id = str(data.get("clientId") or data.get("id") or "")
    if numeric_id and numeric_id.isdigit():
        _name_to_id[client_name] = numeric_id

    if not client_name:
        print(f"[SERVING] client_spawned senza nome: {data}")
        return

    if client_name in _seen_clients:
        return  # già gestito
    _seen_clients.add(client_name)

    dish = _match_dish(order_text)
    print(
        f"[SERVING] cliente {client_name!r} | ordine: {order_text!r} "
        f"→ {dish or 'NESSUN MATCH'}"
    )

    if not dish:
        print("[SERVING] SKIP: nessun piatto nel menu corrisponde")
        return

    _pending_clients[client_name] = {
        "name": client_name,
        "order_text": order_text,
        "dish": dish,
    }

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            result = await client.prepare_dish(dish)
            print(f"[SERVING] prepare_dish({dish!r}) → {result}")
            _dish_queue.setdefault(dish, []).append(client_name)
        except Exception as exc:
            print(f"[SERVING] ERRORE prepare_dish({dish!r}): {exc}")
            _pending_clients.pop(client_name, None)


async def handle_dish_ready(data: dict[str, Any]) -> None:
    """
    Chiamata dall'orchestratore quando arriva evento preparation_complete.
    Serve il piatto pronto al primo cliente in coda, usando l'ID numerico
    recuperato da /meals.
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

    client_name = waiting.pop(0)
    client_info = _pending_clients.pop(client_name, {})

    # Risolvi l'ID numerico richiesto da serve_dish
    serve_id = await _resolve_numeric_id(client_name)
    if not serve_id:
        print(f"[SERVING] WARN: ID numerico non trovato per {client_name!r}, uso nome come fallback")
        serve_id = client_name

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            result = await client.serve_dish(dish, serve_id)
            print(f"[SERVING] serve_dish({dish!r}, id={serve_id}) → {client_name} | {result}")
        except Exception as exc:
            print(f"[SERVING] ERRORE serve_dish({dish!r}, id={serve_id}): {exc}")


# ---------------------------------------------------------------------------
# Entry point principale
# ---------------------------------------------------------------------------

async def run_serving_agent(turn_id: int = 0) -> None:
    """
    Carica il menu, poi gira in polling finché il task non viene cancellato
    dall'orchestratore (cambio fase).
    """
    global _pending_clients, _dish_queue, _menu_names, _seen_clients, _name_to_id, _current_turn_id

    # Reset stato per il nuovo turno
    _pending_clients = {}
    _dish_queue = {}
    _menu_names = {}
    _seen_clients = set()
    _name_to_id = {}
    _current_turn_id = turn_id

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
    # e aggiorna la cache nome→id
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
                    meals = await client.get_meals(turn_id)

                # Aggiorna sempre la cache nome→id (serve per serve_dish)
                _update_name_to_id(meals)

                # Processa i clienti non ancora visti (dedup per clientName)
                for meal in meals:
                    cname = _meal_name(meal)
                    if cname and cname not in _seen_clients:
                        await handle_new_client({
                            "clientId": _meal_id(meal),
                            "clientName": cname,
                            "orderText": meal.get("orderText", meal.get("order_text", "")),
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
