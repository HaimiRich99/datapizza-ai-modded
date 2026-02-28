"""
Agente Menu — Fase waiting

Logica:
1. Legge inventario attuale
2. Trova tutte le ricette completabili con gli ingredienti che abbiamo
3. Le ordina per prestige e sceglie le migliori
4. Pubblica il menu con save_menu (prezzo = prestige * PRICE_MULTIPLIER)
5. Calcola ingredienti in surplus (in inventario ma non usati dal menu)
6. Salva il surplus in explorer_data/surplus_ingredients.json
   → il market_agent lo leggerà per mettere in vendita queste eccedenze

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

# Quante ricette massimo nel menu
MAX_MENU_SIZE = 6
# Prezzo = prestige * questo moltiplicatore (arrotondato)
PRICE_MULTIPLIER = 1.5
PRICE_MIN = 10.0

SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"


# ---------------------------------------------------------------------------
# Logica
# ---------------------------------------------------------------------------

def find_completable_recipes(
    recipes: list[dict],
    inventory: dict[str, int],
) -> list[dict]:
    """Ritorna le ricette per cui abbiamo TUTTI gli ingredienti necessari."""
    completable = []
    for recipe in recipes:
        needed = recipe.get("ingredients", {})
        if all(inventory.get(ing, 0) >= qty for ing, qty in needed.items()):
            completable.append(recipe)
    return completable


def compute_surplus(
    menu_recipes: list[dict],
    inventory: dict[str, int],
) -> dict[str, int]:
    """
    Ritorna {ingrediente: quantità_in_surplus} — ingredienti presenti
    nell'inventario che non vengono usati da nessuna ricetta nel menu.
    """
    used: dict[str, int] = defaultdict(int)
    for recipe in menu_recipes:
        for ing, qty in recipe.get("ingredients", {}).items():
            used[ing] += qty

    surplus: dict[str, int] = {}
    for ing, qty_have in inventory.items():
        qty_used = used.get(ing, 0)
        leftover = qty_have - qty_used
        if leftover > 0:
            surplus[ing] = leftover
    return surplus


def recipe_to_menu_item(recipe: dict) -> dict:
    prestige = recipe.get("prestige", 10)
    price = max(PRICE_MIN, round(prestige * PRICE_MULTIPLIER, 2))
    return {"name": recipe["name"], "price": price}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_menu_agent(dry_run: bool = False) -> tuple[list[dict], dict[str, int]]:
    """
    Ritorna (menu_items, surplus_ingredients).
    menu_items: lista di {name, price} pubblicata sul server.
    surplus_ingredients: {ingrediente: qty} da vendere sul mercato.
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        inventory: dict[str, int] = restaurant.get("inventory", {})
        balance = restaurant.get("balance", 0)
        print(f"[MENU] saldo: {balance} | ingredienti in inventario: {len(inventory)}")

        if not inventory:
            print("[MENU] inventario vuoto — nessun menu da comporre")
            return [], {}

        recipes = await client.get_recipes()

    # 1. Ricette completabili
    completable = find_completable_recipes(recipes, inventory)
    print(f"[MENU] ricette completabili: {len(completable)} / {len(recipes)}")

    if not completable:
        print("[MENU] nessuna ricetta completabile con l'inventario attuale")
        _save_surplus({}, dry_run)
        return [], {}

    # 2. Ordina per prestige desc, prendi le migliori
    completable.sort(key=lambda r: r.get("prestige", 0), reverse=True)
    chosen = completable[:MAX_MENU_SIZE]

    print(f"\n[MENU] ricette scelte per il menu ({len(chosen)}):")
    menu_items = []
    for r in chosen:
        item = recipe_to_menu_item(r)
        menu_items.append(item)
        print(f"  - {item['name']} | prestige={r.get('prestige')} | prezzo={item['price']}")

    # 3. Surplus
    surplus = compute_surplus(chosen, inventory)
    print(f"\n[MENU] ingredienti in surplus ({len(surplus)}):")
    if surplus:
        for ing, qty in surplus.items():
            print(f"  - {ing}: {qty} unità (non usate dal menu)")
    else:
        print("  (nessuno — tutto l'inventario è coperto dal menu)")

    # 4. Salva surplus per market_agent
    _save_surplus(surplus, dry_run)

    # 5. Pubblica menu
    if dry_run:
        print("\n[MENU] DRY-RUN: save_menu NON chiamato")
    else:
        async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
            try:
                result = await client.save_menu(menu_items)
                print(f"\n[MENU] menu pubblicato | risposta: {result}")
            except Exception as exc:
                print(f"\n[MENU] ERRORE save_menu: {exc}")

    return menu_items, surplus


def _save_surplus(surplus: dict[str, int], dry_run: bool) -> None:
    SURPLUS_PATH.parent.mkdir(exist_ok=True)
    SURPLUS_PATH.write_text(
        json.dumps(surplus, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tag = "[DRY-RUN] " if dry_run else ""
    print(f"\n[MENU] {tag}surplus salvato -> {SURPLUS_PATH}")


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("[MENU] modalità DRY-RUN: nessuna chiamata al server\n")
    asyncio.run(run_menu_agent(dry_run=dry))
