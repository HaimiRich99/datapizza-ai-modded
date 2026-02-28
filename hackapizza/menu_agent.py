"""
Agente Menu — Fase waiting (logica deterministica)

Logica:
1. Recupera inventario e ricette dal server
2. Filtra le ricette completabili (tutti gli ingredienti disponibili)
3. Ordina per prestige decrescente, prende fino a MAX_MENU_SIZE
4. Calcola il prezzo: max(MIN_PRICE, min(MAX_PRICE, prestige * PRESTIGE_PRICE_FACTOR))
5. Pubblica il menu sul server

Esegui standalone: python menu_agent.py [--dry-run]
"""

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

MAX_MENU_SIZE = 6
MIN_PRICE = 100          # prezzo minimo assoluto per piatto
MAX_PRICE = 1000         # limite tecnico del server
PRESTIGE_PRICE_FACTOR = 50  # ogni punto prestige vale N crediti extra (es. prestige=5 → 350)

SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"
STRATEGY_PATH = Path(__file__).parent / "explorer_data" / "strategy.json"

_DRY_RUN = False


# ---------------------------------------------------------------------------
# Logica deterministica
# ---------------------------------------------------------------------------

def compute_copies_possible(recipe: dict, inventory: dict[str, int]) -> int:
    """Quante porzioni possiamo servire con l'inventario attuale."""
    ings = recipe.get("ingredients", {})
    if not ings:
        return 0
    return min(inventory.get(ing, 0) // qty for ing, qty in ings.items())


def price_for_recipe(recipe: dict) -> int:
    """Calcola il prezzo basandosi sul prestige, con un minimo di MIN_PRICE."""
    prestige = recipe.get("prestige", 0)
    price = MIN_PRICE + int(prestige) * PRESTIGE_PRICE_FACTOR
    return max(MIN_PRICE, min(MAX_PRICE, price))


def find_completable_recipes(
    recipes: list[dict],
    inventory: dict[str, int],
    focus_recipes: list[str],
) -> list[dict]:
    """Filtra le ricette completabili e le ordina (focus prima, poi prestige desc)."""
    completable = []
    for recipe in recipes:
        needed = recipe.get("ingredients", {})
        if not needed:
            continue
        if all(inventory.get(ing, 0) >= qty for ing, qty in needed.items()):
            completable.append(recipe)

    def sort_key(r):
        is_focus = 0 if r["name"] in focus_recipes else 1
        return (is_focus, -r.get("prestige", 0))

    completable.sort(key=sort_key)
    return completable


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
# Entry point
# ---------------------------------------------------------------------------

async def run_menu_agent(dry_run: bool = False) -> None:
    global _DRY_RUN
    _DRY_RUN = dry_run

    print("[MENU] Elaborazione menu in corso...")

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        inventory = restaurant.get("inventory", {})

        if not inventory:
            print("[MENU] Inventario vuoto. Nessun menu possibile.")
            return

        all_recipes = await client.get_recipes()

    # Carica focus_recipes dalla strategy se disponibile
    focus_recipes: list[str] = []
    if STRATEGY_PATH.exists():
        try:
            strat = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
            focus_recipes = strat.get("focus_recipes", [])
        except Exception:
            pass

    completable = find_completable_recipes(all_recipes, inventory, focus_recipes)

    if not completable:
        print("[MENU] Nessuna ricetta completabile con l'inventario attuale.")
        return

    selected = completable[:MAX_MENU_SIZE]

    menu_items = []
    print(f"\n[MENU] Menu ({len(selected)} piatti):")
    for recipe in selected:
        price = price_for_recipe(recipe)
        copies = compute_copies_possible(recipe, inventory)
        print(f"  - {recipe['name']} | prestige={recipe.get('prestige', 0)} | prezzo={price} | copie={copies}")
        menu_items.append({"name": recipe["name"], "price": float(price)})

    # Salva surplus
    surplus = compute_surplus(selected, inventory)
    SURPLUS_PATH.parent.mkdir(exist_ok=True)
    SURPLUS_PATH.write_text(json.dumps(surplus, indent=2, ensure_ascii=False), encoding="utf-8")

    if _DRY_RUN:
        print("\n[MENU] DRY-RUN: menu calcolato, nessuna chiamata al server.")
        return

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            result = await client.save_menu(menu_items)
            print(f"\n[MENU] Menu salvato. Risposta: {result}")
        except Exception as exc:
            print(f"\n[MENU] Errore salvataggio menu: {exc}")


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("[MENU] modalità DRY-RUN\n")
    asyncio.run(run_menu_agent(dry_run=dry))
