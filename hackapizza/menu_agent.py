"""
Agente Menu — Fase waiting

Logica:
1. Calcola il costo reale degli ingredienti da bid_list.json (prezzi pagati all'asta)
2. Per ogni ricetta completabile, calcola: costo totale, copie possibili, prezzo minimo redditizio
3. L'LLM sceglie il menu e fissa i prezzi SOPRA il costo (markup minimo 40%)
4. Pubblica il menu sul server e calcola il surplus

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

MAX_MENU_SIZE = 6
MARKUP_MIN = 1.4       # markup minimo: prezzo >= costo * 1.4
MARKUP_TARGET = 1.3    # markup ideale: prezzo >= costo * 2.0
DEFAULT_COST_PER_ING = 25  # costo default se non abbiamo dati

SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"
BID_LIST_PATH = Path(__file__).parent / "explorer_data" / "bid_list.json"
STRATEGY_PATH = Path(__file__).parent / "explorer_data" / "strategy.json"

_DRY_RUN = False


# ---------------------------------------------------------------------------
# Helper: costi ingredienti
# ---------------------------------------------------------------------------

def load_ingredient_costs() -> dict[str, float]:
    """
    Legge bid_list.json per sapere quanto abbiamo pagato ogni ingrediente.
    Formato bid_list: [{"ingredient": "...", "quantity": N, "bid": P}, ...]
    Il 'bid' è il prezzo pagato per unità (in closed-bid si paga la propria offerta).
    """
    if not BID_LIST_PATH.exists():
        return {}
    try:
        bids = json.loads(BID_LIST_PATH.read_text(encoding="utf-8"))
        return {b["ingredient"]: float(b["bid"]) for b in bids if "ingredient" in b and "bid" in b}
    except Exception:
        return {}


def compute_recipe_cost(recipe: dict, ing_costs: dict[str, float]) -> float:
    """
    Calcola il costo totale degli ingredienti per UNA porzione della ricetta.
    Usa ing_costs se disponibile, altrimenti DEFAULT_COST_PER_ING.
    """
    total = 0.0
    for ing, qty in recipe.get("ingredients", {}).items():
        cost_per_unit = ing_costs.get(ing, DEFAULT_COST_PER_ING)
        total += cost_per_unit * qty
    return total


def compute_copies_possible(recipe: dict, inventory: dict[str, int]) -> int:
    """Quante copie di questa ricetta possiamo servire con l'inventario attuale."""
    ings = recipe.get("ingredients", {})
    if not ings:
        return 0
    return min(inventory.get(ing, 0) // qty for ing, qty in ings.items())


def find_completable_recipes_with_costs(
    recipes: list[dict],
    inventory: dict[str, int],
    ing_costs: dict[str, float],
) -> list[dict]:
    """Filtra le ricette completabili e aggiunge info di costo e copie."""
    result = []
    for recipe in recipes:
        needed = recipe.get("ingredients", {})
        if not all(inventory.get(ing, 0) >= qty for ing, qty in needed.items()):
            continue

        cost_per_serving = compute_recipe_cost(recipe, ing_costs)
        copies = compute_copies_possible(recipe, inventory)
        min_price = math.ceil(cost_per_serving * MARKUP_MIN)
        target_price = math.ceil(cost_per_serving * MARKUP_TARGET)

        result.append({
            "name": recipe["name"],
            "prestige": recipe.get("prestige", 0),
            "ingredients": needed,
            "cost_per_serving": round(cost_per_serving, 1),
            "copies_possible": copies,
            "min_price": min_price,       # prezzo minimo per coprire i costi (markup 40%)
            "target_price": target_price,  # prezzo ideale (markup 100%)
        })

    return result


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
# Tool per l'LLM
# ---------------------------------------------------------------------------

@tool
async def get_completable_recipes() -> str:
    """
    Recupera l'inventario e le ricette, restituendo SOLO le ricette completabili
    con informazioni di costo e pricing.
    Campi per ogni ricetta:
      - name: nome della ricetta
      - prestige: punteggio prestigio
      - cost_per_serving: costo ingredienti per 1 porzione (crediti pagati all'asta)
      - copies_possible: quante copie possiamo servire con l'inventario
      - min_price: prezzo MINIMO da applicare (costo × 1.4) per coprire i costi
      - target_price: prezzo ideale (costo × 2.0) per massimizzare il profitto
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        inventory = restaurant.get("inventory", {})

        if not inventory:
            return "Inventario vuoto. Non possiamo cucinare nulla."

        recipes = await client.get_recipes()

    ing_costs = load_ingredient_costs()
    completable = find_completable_recipes_with_costs(recipes, inventory, ing_costs)

    if not completable:
        return "Nessuna ricetta completabile con gli ingredienti in inventario."

    # Priorità alle ricette focus dalla strategy
    focus_recipes: list[str] = []
    if STRATEGY_PATH.exists():
        try:
            strat = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
            focus_recipes = strat.get("focus_recipes", [])
        except Exception:
            pass

    # Ordina: focus prima, poi per prestige
    def sort_key(r):
        is_focus = 0 if r["name"] in focus_recipes else 1
        return (is_focus, -r["prestige"])

    completable.sort(key=sort_key)

    # Rimuovi gli ingredienti dal payload (riduce token)
    output = []
    for r in completable:
        output.append({
            "name": r["name"],
            "prestige": r["prestige"],
            "cost_per_serving": r["cost_per_serving"],
            "copies_possible": r["copies_possible"],
            "min_price": r["min_price"],
            "target_price": r["target_price"],
            "is_focus": r["name"] in focus_recipes,
        })

    return json.dumps(output, ensure_ascii=False)


@tool
async def set_menu_and_surplus(items: list[dict]) -> str:
    """
    Salva il menu sul server, calcola il surplus e lo passa al mercato.

    Args:
        items: Lista di dict con i piatti scelti. Massimo 6 piatti.
               Formato: [{"name": "Nome Piatto", "price": 120.0}, ...]
               IMPORTANTE: price deve essere >= min_price restituito da get_completable_recipes.
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

        print(f"\n[MENU] Menu ({len(items)} piatti):")
        for item in items:
            recipe = next((r for r in all_recipes if r["name"] == item["name"]), None)
            if recipe:
                chosen_recipes.append(recipe)
                cost = compute_recipe_cost(recipe, ing_costs)
                price = float(item.get("price", 0))
                margin = ((price - cost) / cost * 100) if cost > 0 else 0
                copies = compute_copies_possible(recipe, inventory)
                revenue = price * copies
                print(f"  - {item['name']}")
                print(f"    prezzo={price:.0f} | costo={cost:.1f} | margine={margin:.0f}% | copie={copies} | revenue_max={revenue:.0f}")

        surplus = compute_surplus(chosen_recipes, inventory)
        SURPLUS_PATH.parent.mkdir(exist_ok=True)
        SURPLUS_PATH.write_text(json.dumps(surplus, indent=2, ensure_ascii=False), encoding="utf-8")

        if _DRY_RUN:
            return "[DRY-RUN] Menu calcolato, nessuna chiamata al server."

        try:
            result = await client.save_menu(items)
            return f"Menu salvato. Risposta: {result}"
        except Exception as exc:
            return f"Errore salvataggio menu: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_menu_agent(dry_run: bool = False) -> None:
    global _DRY_RUN
    _DRY_RUN = dry_run

    if not REGOLO_API_KEY:
        print("[MENU] REGOLO_API_KEY non trovata.")
        return

    llm_client = OpenAILikeClient(
        api_key=REGOLO_API_KEY,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
    )

    menu_agent = Agent(
        name="Menu_Manager",
        client=llm_client,
        system_prompt=(
        "Sei il responsabile del menu di un ristorante galattico. "
        "Il tuo obiettivo è vendere SEMPRE le ricette disponibili e massimizzare il profitto."

        "\n\nREGOLE OPERATIVE OBBLIGATORIE:"

        "\n1. Chiama sempre get_completable_recipes() per vedere cosa puoi cucinare."

        "\n2. Se esiste almeno UNA ricetta completabile, DEVI inserirla nel menu."
        "\n   Non lasciare MAI il menu vuoto."

        "\n3. Seleziona fino a 6 piatti, privilegiando:"
        "\n   - ricette is_focus=true"
        "\n   - alta prestige"
        "\n   - molte copies_possible"

        "\n4. PREZZO: devi vendere al prezzo PIÙ ALTO possibile."
        "\n   Regole prezzo:"
        "\n   - price <= 1000 (limite tecnico del server)"
        "\n   - price >= cost_per_serving (mai vendere in perdita)"
        "\n   - usa target_price se <= 1000"
        "\n   - se target_price > 1000 → imposta price = 1000"

        "\n5. min_price NON è un vincolo obbligatorio."
        "\n   Serve solo come riferimento di markup."

        "\n6. È SEMPRE meglio vendere a profitto ridotto che non vendere."

        "\n7. Chiama set_menu_and_surplus(items) con la lista finale."

        "\n\nNON:"
        "\n- Non lasciare il menu vuoto se una ricetta è cucinabile."
        "\n- Non impostare price > 1000."
        ),
        tools=[get_completable_recipes, set_menu_and_surplus],  # type: ignore
        max_steps=5,
    )

    print("[MENU] Elaborazione menu in corso...")
    result = await menu_agent.a_run(
        "Siamo nella fase Waiting. Controlla le ricette disponibili con i loro costi reali, "
        "scegli quelle più redditizie (copertura dei costi + massimo profitto), "
        "e salva il menu con i prezzi corretti."
    )  # type: ignore

    print("\n[MENU] Completato.")
    if result:
        print(f"[MENU] {result.text}")


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("[MENU] modalità DRY-RUN\n")
    asyncio.run(run_menu_agent(dry_run=dry))
