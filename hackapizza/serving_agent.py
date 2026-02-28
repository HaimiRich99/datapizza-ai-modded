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
from pydantic import BaseModel, Field

# Setup imports per l'LLM
try:
    from datapizza.clients.openai_like import OpenAILikeClient
except ImportError:
    pass

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY", "")

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
_recipes: dict[str, dict] = {}

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
# LLM Intolleranze Check
# ---------------------------------------------------------------------------

class IntoleranceCheck(BaseModel):
    has_intolerance: bool = Field(description="True se l'ordine contiene un'intolleranza o allergia in conflitto con gli ingredienti del piatto, False altrimenti.")
    reasoning: str = Field(description="Spiegazione concisa")

async def _check_intolerances(order_text: str, dish_name: str, dish_ingredients: list[str]) -> bool:
    if not REGOLO_API_KEY:
        print("[SERVING] WARN: REGOLO_API_KEY mancante, salto check intolleranze.")
        return False
        
    try:
        # Eseguiamo l'LLM in un thread per non bloccare l'event loop, dato che structured_response è sincrono
        client = OpenAILikeClient(
            api_key=REGOLO_API_KEY,
            model="gpt-oss-120b",
            base_url="https://api.regolo.ai/v1",
            system_prompt="Sei un assistente di un ristorante. Verifica se l'ordine del cliente contiene richieste per intolleranze o allergie in conflitto con gli ingredienti del piatto. Rispondi in JSON secondo la struttura richiesta."
        )
        
        prompt = f"Ordine del cliente: {order_text}\nPiatto: {dish_name}\nIngredienti del piatto: {dish_ingredients}"
        
        def run_llm():
            return client.structured_response(input=prompt, output_cls=IntoleranceCheck)
            
        response = await asyncio.to_thread(run_llm)
        raw = response.structured_data
        
        if isinstance(raw, IntoleranceCheck):
            print(f"[SERVING] LLM Check per {dish_name}: intolleranza={raw.has_intolerance} ({raw.reasoning})")
            return raw.has_intolerance
        elif isinstance(raw, dict):
            parsed = IntoleranceCheck(**raw)
            print(f"[SERVING] LLM Check per {dish_name}: intolleranza={parsed.has_intolerance} ({parsed.reasoning})")
            return parsed.has_intolerance
            
    except Exception as exc:
        print(f"[SERVING] ERRORE durante check intolleranze: {exc}")
        
    return False


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
    """
    Estrae il nome cliente dal record /meals.
    Struttura reale: {"customer": {"name": "..."}, ...}
    """
    customer = meal.get("customer")
    if isinstance(customer, dict):
        name = customer.get("name", "")
        if name:
            return name
    # Fallback per altri formati
    return meal.get("clientName") or meal.get("client_name") or ""


def _meal_id(meal: dict) -> str:
    """
    Estrae il customerId dal record /meals.
    Struttura reale: {"customerId": 147, ...}
    """
    return str(meal.get("customerId") or meal.get("clientId") or meal.get("id") or "")


def _update_name_to_id(meals: list[dict]) -> None:
    """Aggiorna _name_to_id da una lista di meal record."""
    for m in meals:
        cid = _meal_id(m)
        cname = _meal_name(m)
        if cid and cname:
            _name_to_id[cname] = cid


async def _resolve_numeric_id(client_name: str, max_attempts: int = 3) -> str | None:
    """
    Cerca l'ID numerico del cliente in _name_to_id; se mancante interroga /meals.
    Ritenta fino a max_attempts volte con breve pausa tra i tentativi.
    Ritorna l'ID come stringa oppure None.
    """
    if client_name in _name_to_id:
        return _name_to_id[client_name]

    for attempt in range(1, max_attempts + 1):
        try:
            async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as c:
                meals = await c.get_meals(_current_turn_id, TEAM_ID)
            print(f"[SERVING] /meals turn={_current_turn_id}: {len(meals)} record (tentativo {attempt})")
            if meals:
                # Log campi disponibili al primo tentativo per debug
                if attempt == 1:
                    print(f"[SERVING] meal fields: {list(meals[0].keys())}")
                _update_name_to_id(meals)
        except Exception as exc:
            print(f"[SERVING] WARN /meals tentativo {attempt}: {exc}")

        if client_name in _name_to_id:
            return _name_to_id[client_name]

        if attempt < max_attempts:
            await asyncio.sleep(1.0)

    print(f"[SERVING] ID non trovato per {client_name!r} dopo {max_attempts} tentativi")
    return None


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

    # Log campi extra dell'evento (debug: per vedere se server manda già clientId)
    extra = {k: v for k, v in data.items() if k not in ("clientName", "orderText")}
    if extra:
        print(f"[SERVING] client_spawned extra fields: {extra}")

    # Aggiorna cache nome→id se l'evento contiene già un ID numerico
    numeric_id = str(data.get("clientId") or data.get("id") or data.get("client_id") or "")
    if numeric_id and str(numeric_id).lstrip("-").isdigit():
        print(f"[SERVING] ID numerico trovato nell'evento SSE: {client_name} → {numeric_id}")
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
        print(f"[SERVING] ERRORE: ID numerico non trovato per {client_name!r} — piatto non servito")
        print(f"[SERVING] DEBUG: _name_to_id attuale = {_name_to_id}")
        return

    # Call LLM per intolleranze "prima di servire"
    dish_info = _recipes.get(dish, {})
    dish_ings = list(dish_info.get("ingredients", {}).keys())
    order_text = client_info.get("order_text", "")
    
    if order_text:
        has_intol = await _check_intolerances(order_text, dish, dish_ings)
        if has_intol:
            print(f"[SERVING] STOP ORDINE: Intolleranza rilevata per {client_name} (Ordine: {order_text!r} | Piatto: {dish})")
            return

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
    global _pending_clients, _dish_queue, _menu_names, _recipes, _seen_clients, _name_to_id, _current_turn_id

    # Reset stato per il nuovo turno
    _pending_clients = {}
    _dish_queue = {}
    _menu_names = {}
    _recipes = {}
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
                    
            recipes_raw = await client.get_recipes()
            for r in recipes_raw:
                _recipes[r.get("name", "")] = r
                
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
                    meals = await client.get_meals(turn_id, TEAM_ID)

                # Aggiorna sempre la cache nome→id (serve per serve_dish)
                _update_name_to_id(meals)

                # Processa i clienti non ancora visti (dedup per clientName)
                for meal in meals:
                    cname = _meal_name(meal)
                    if cname and cname not in _seen_clients:
                        # Struttura reale: order_text è in "request"
                        order = meal.get("request") or meal.get("orderText") or meal.get("order_text") or ""
                        await handle_new_client({
                            "clientId": _meal_id(meal),
                            "clientName": cname,
                            "orderText": order,
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
