"""
Agente Menu — Fase waiting (Competitivo & Spietato)

Logica:
1. Calcola il costo reale degli ingredienti da bid_list.json.
2. Analizza i prezzi di mercato (min, max, avg) dall'ultimo snapshot disponibile.
3. Per ogni ricetta, valuta i costi e calcola il guadagno minimo garantito (default 1.2x).
4. L'LLM sceglie il menu e posiziona i prezzi in modo competitivo ma redditizio.
5. Pubblica il menu sul server e calcola il surplus.

Esegui standalone: python menu_agent.py [--dry-run]
"""

import asyncio
import json
import math
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from datapizza.agents import Agent
from datapizza.tools import tool
from datapizza.clients.openai_like import OpenAILikeClient

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY", "")

MAX_MENU_SIZE = 50
MARKUP_SAFE = 1.2      # Prezzo minimo per sopravvivere (costo * 1.2)
MARKUP_TARGET = 2.0    # Prezzo target se siamo in monopolio
DEFAULT_COST_PER_ING = 25

SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"
BID_LIST_PATH = Path(__file__).parent / "explorer_data" / "bid_list.json"
STRATEGY_PATH = Path(__file__).parent / "explorer_data" / "strategy.json"
SNAPSHOTS_DIR = Path(__file__).parent / "explorer_data" / "snapshots"

_DRY_RUN = False

# ---------------------------------------------------------------------------
# Helper: costi ingredienti
# ---------------------------------------------------------------------------

def load_ingredient_costs() -> dict[str, float]:
    if not BID_LIST_PATH.exists():
        return {}
    try:
        bids = json.loads(BID_LIST_PATH.read_text(encoding="utf-8"))
        return {b["ingredient"]: float(b["bid"]) for b in bids if "ingredient" in b and "bid" in b}
    except Exception:
        return {}

def compute_recipe_cost(recipe: dict, ing_costs: dict[str, float]) -> float:
    total = 0.0
    for ing, qty in recipe.get("ingredients", {}).items():
        cost_per_unit = ing_costs.get(ing, DEFAULT_COST_PER_ING)
        total += cost_per_unit * qty
    return total

def compute_copies_possible(recipe: dict, inventory: dict[str, int]) -> int:
    ings = recipe.get("ingredients", {})
    if not ings:
        return 0
    return min(inventory.get(ing, 0) // qty for ing, qty in ings.items())

def compute_surplus(menu_recipes: list[dict], inventory: dict[str, int]) -> dict[str, int]:
    used: dict[str, int] = defaultdict(int)
    for recipe in menu_recipes:
        for ing, qty in recipe.get("ingredients", {}).items():
            used[ing] += qty

    surplus: dict[str, int] = {}
    for ing, qty_have in inventory.items():
        leftover = qty_have - used.get(ing, 0)
        if leftover > 0:
            surplus[ing] = leftover
    return surplus

# ---------------------------------------------------------------------------
# Tool per l'LLM (sfruttando Datapizza)
# ---------------------------------------------------------------------------

@tool
async def get_market_prices() -> str:
    """
    Spia i prezzi della concorrenza! Analizza l'ultimo snapshot disponibile per 
    calcolare min, max e avg dei prezzi per ogni ricetta nel mercato galattico.
    Restituisce un JSON con i dati di mercato. Usa queste info per settare i prezzi!
    """
    if not SNAPSHOTS_DIR.exists() or not SNAPSHOTS_DIR.is_dir():
        return json.dumps({"error": "Nessun dato di mercato disponibile (cartella snapshots mancante)."})

    subdirs = sorted([d for d in SNAPSHOTS_DIR.iterdir() if d.is_dir()])
    if not subdirs:
        return json.dumps({"error": "Nessuno snapshot trovato."})
    
    latest_snapshot_path = subdirs[-1] / "restaurants.json"
    if not latest_snapshot_path.exists():
        return json.dumps({"error": f"File restaurants.json non trovato in {subdirs[-1].name}"})

    try:
        restaurants = json.loads(latest_snapshot_path.read_text(encoding="utf-8"))
        market_data = defaultdict(list)

        for res in restaurants:
            # Escludiamo noi stessi per non falsare le statistiche
            if res.get("id") == str(TEAM_ID) or res.get("id") == TEAM_ID:
                continue
            
            menu_data = res.get("menu", [])
            menu_items = menu_data.get("items", []) if isinstance(menu_data, dict) else menu_data
            
            for item in menu_items:
                name = item.get("name")
                price = item.get("price")
                if name and price is not None:
                    market_data[name].append(float(price))

        stats = {}
        for name, prices in market_data.items():
            stats[name] = {
                "min": min(prices),
                "max": max(prices),
                "avg": round(sum(prices) / len(prices), 1),
                "competitors_selling_this": len(prices)
            }

        return json.dumps(stats, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Errore durante l'analisi dei prezzi: {str(e)}"})

@tool
async def get_completable_recipes() -> str:
    """
    Recupera l'inventario e restituisce SOLO le ricette completabili con info di costo reale.
    Ritorna un JSON con: cost_per_serving, copies_possible, safe_min_price (il nostro pavimento di sicurezza 1.2x)
    e target_price.
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        inventory = restaurant.get("inventory", {})

        if not inventory:
            return "Inventario vuoto. Impossibile cucinare."

        recipes = await client.get_recipes()

    ing_costs = load_ingredient_costs()
    
    focus_recipes: list[str] = []
    if STRATEGY_PATH.exists():
        try:
            strat = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
            focus_recipes = strat.get("focus_recipes", [])
        except Exception:
            pass

    completable = []
    for recipe in recipes:
        needed = recipe.get("ingredients", {})
        if not all(inventory.get(ing, 0) >= qty for ing, qty in needed.items()):
            continue

        cost_per_serving = compute_recipe_cost(recipe, ing_costs)
        copies = compute_copies_possible(recipe, inventory)
        safe_min_price = math.ceil(cost_per_serving * MARKUP_SAFE)
        target_price = math.ceil(cost_per_serving * MARKUP_TARGET)

        completable.append({
            "name": recipe["name"],
            "prestige": recipe.get("prestige", 0),
            "cost_per_serving": round(cost_per_serving, 1),
            "copies_possible": copies,
            "safe_min_price": safe_min_price, 
            "target_price": target_price,
            "is_focus": recipe["name"] in focus_recipes,
        })

    if not completable:
        return "Nessuna ricetta completabile con l'inventario attuale."

    completable.sort(key=lambda r: (0 if r["is_focus"] else 1, -r["prestige"]))
    return json.dumps(completable, ensure_ascii=False)


@tool
async def set_menu_and_surplus(items: list[dict]) -> str:
    """
    Salva il menu finale sul server e calcola il surplus.
    Argomento richiesto 'items': lista di dizionari con formato [{"name": "Piatto", "price": 120}, ...].
    """
    if not items:
        return "Nessun piatto specificato. Menu non salvato."

    items = items[:MAX_MENU_SIZE]

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        inventory = restaurant.get("inventory", {})
        all_recipes = await client.get_recipes()

        chosen_recipes = []
        ing_costs = load_ingredient_costs()

        print(f"\n[MENU] Menu sceltro ({len(items)} piatti):")
        for item in items:
            recipe = next((r for r in all_recipes if r["name"] == item["name"]), None)
            if recipe:
                chosen_recipes.append(recipe)
                cost = compute_recipe_cost(recipe, ing_costs)
                price = float(item.get("price", 0))
                margin = ((price - cost) / cost * 100) if cost > 0 else 0
                copies = compute_copies_possible(recipe, inventory)
                print(f"  - {item['name']}")
                print(f"    prezzo={price:.0f} | costo={cost:.1f} | margine={margin:.0f}% | copie={copies}")

        surplus = compute_surplus(chosen_recipes, inventory)
        SURPLUS_PATH.parent.mkdir(exist_ok=True)
        SURPLUS_PATH.write_text(json.dumps(surplus, indent=2, ensure_ascii=False), encoding="utf-8")

        if _DRY_RUN:
            return "[DRY-RUN] Menu calcolato, nessuna chiamata al server effettuata."

        try:
            result = await client.save_menu(items)
            return f"Menu salvato. Risposta: {result}"
        except Exception as exc:
            return f"Errore salvataggio menu: {exc}"


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Sei il Chief Financial Officer (CFO) di un ristorante galattico iper-competitivo.
Il tuo scopo è mettere a menu TUTTE le ricette che possiamo cucinare e prezzarle in modo redditizio.

ISTRUZIONI OPERATIVE (tre passi, nell'ordine):
1. Chiama get_completable_recipes() per vedere cosa puoi cucinare.
2. Chiama get_market_prices() per spiare quanto fanno pagare gli altri.
3. Includi MASSIMO 50 ricette completabili nel menu (nessuna esclusa), fino al limite di slot disponibili.
   Ordinale per prestige decrescente se devi sceglierne alcune.

STRATEGIA PREZZI OBBLIGATORIA:
- DEVI garantire un profitto minimo. Imposta sempre price >= safe_min_price (1.2x sul costo).
- Se la concorrenza vende un piatto, posizionati attorno al loro 'avg': stai leggermente sotto per rubare mercato,
  o vicino al 'max' se il piatto ha alto prestige e noi abbiamo monopolio.
- Se nessun concorrente vende quel piatto, sfrutta il monopolio e usa il 'target_price'.
- Nessun prezzo può superare i 1000 crediti.

Infine, chiama set_menu_and_surplus(items) per confermare le tue scelte."""


# ---------------------------------------------------------------------------
# Entry point dell'Agente
# ---------------------------------------------------------------------------

async def run_menu_agent(dry_run: bool = False) -> None:
    global _DRY_RUN
    _DRY_RUN = dry_run

    if not REGOLO_API_KEY:
        return

    llm_client = OpenAILikeClient(
        api_key=REGOLO_API_KEY,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
    )

    menu_agent = Agent(
        name="Menu_Manager_Spietato",
        client=llm_client,
        system_prompt=_SYSTEM_PROMPT,
        tools=[get_completable_recipes, get_market_prices, set_menu_and_surplus],  # type: ignore
        max_steps=8,
    )

    await menu_agent.a_run(
        "Fase Waiting attivata. Controlla il magazzino, analizza i prezzi del mercato dai log e "
        "imposta un menu che garantisca margini ma sconfigga i competitor. Chiudi l'operazione salvando il menu."
    )  # type: ignore


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    asyncio.run(run_menu_agent(dry_run=dry))