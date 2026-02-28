"""
Agente Mercato — Fase waiting/market

Logica ACQUISTI:
1. Carica inventario attuale e saldo
2. Guarda le ricette nel nostro menu
3. Calcola gli ingredienti mancanti per completare ogni ricetta
4. Cerca sul mercato entry SELL per quegli ingredienti
5. Compra se il prezzo e ragionevole e il budget lo permette

Logica VENDITE (surplus):
6. Legge explorer_data/surplus_ingredients.json (scritto da menu_agent)
7. Per ogni ingrediente in surplus crea una entry SELL sul mercato

Esegui standalone: python market_agent.py [--dry-run]
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
API_KEY = os.getenv("API_KEY", "")

# Prezzo massimo accettabile per unita di ingrediente (acquisti)
MAX_PRICE_PER_UNIT = 80.0
# Frazione massima del saldo da spendere in totale (acquisti)
BUDGET_FRACTION = 0.7
# Prezzo a cui vendiamo il surplus (per unita)
SELL_PRICE = 25.0

SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"


def compute_missing(
    menu_recipe_names: list[str],
    recipes: list[dict],
    inventory: dict[str, int],
) -> dict[str, int]:
    recipe_map = {r["name"]: r for r in recipes}
    missing: dict[str, int] = defaultdict(int)
    for recipe_name in menu_recipe_names:
        recipe = recipe_map.get(recipe_name)
        if recipe is None:
            print(f"  [WARN] ricetta non trovata: {recipe_name!r}")
            continue
        for ing, qty_needed in recipe.get("ingredients", {}).items():
            have = inventory.get(ing, 0)
            if have < qty_needed:
                missing[ing] += qty_needed - have
    return dict(missing)


def find_best_entries(
    missing: dict[str, int],
    market: list[dict],
    balance: float,
) -> list[dict]:
    budget = balance * BUDGET_FRACTION
    spent = 0.0
    to_buy: list[dict] = []

    sell_by_ing: dict[str, list[dict]] = defaultdict(list)
    for entry in market:
        if entry.get("side") != "SELL":
            continue
        ing = entry.get("ingredient_name", "")
        if ing:
            sell_by_ing[ing].append(entry)
    for ing in sell_by_ing:
        sell_by_ing[ing].sort(key=lambda e: float(e.get("price", 9999)))

    for ing, qty_needed in missing.items():
        entries = sell_by_ing.get(ing, [])
        if not entries:
            print(f"  [MARKET] {ing}: non disponibile sul mercato")
            continue
        qty_left = qty_needed
        for entry in entries:
            if qty_left <= 0:
                break
            price = float(entry.get("price", 9999))
            if price > MAX_PRICE_PER_UNIT:
                print(f"  [SKIP] {ing} @ {price} troppo caro (max {MAX_PRICE_PER_UNIT})")
                continue
            entry_qty = int(entry.get("quantity", 0))
            cost = price * min(entry_qty, qty_left)
            if spent + cost > budget:
                print(f"  [SKIP] {ing} @ {price} budget esaurito")
                continue
            to_buy.append(entry)
            spent += cost
            qty_left -= entry_qty
            print(f"  [BUY]  {ing} x{min(entry_qty, qty_needed)} @ {price} "
                  f"(entry_id={entry.get('id')}) | speso: {spent:.2f}")
        if qty_left > 0:
            print(f"  [INFO] {ing}: copertura parziale, mancano ancora {qty_left}")

    print(f"  acquisti pianificati: {len(to_buy)} | spesa totale: {spent:.2f}")
    return to_buy


def load_surplus() -> dict[str, int]:
    if not SURPLUS_PATH.exists():
        print(f"[MARKET] {SURPLUS_PATH.name} non trovato — nessun surplus da vendere")
        return {}
    data = json.loads(SURPLUS_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


async def sell_surplus(
    client: "HackapizzaClient",
    surplus: dict[str, int],
    dry_run: bool,
) -> list[dict]:
    if not surplus:
        print("[MARKET] nessun surplus da mettere in vendita")
        return []

    print(f"[MARKET] surplus da vendere ({len(surplus)} ingredienti) @ {SELL_PRICE}/u:")
    created: list[dict] = []
    for ing, qty in surplus.items():
        print(f"  SELL  {ing} x{qty} @ {SELL_PRICE}")
        if dry_run:
            created.append({"ingredient": ing, "quantity": qty, "price": SELL_PRICE})
            continue
        try:
            result = await client.create_market_entry(
                side="SELL",
                ingredient_name=ing,
                quantity=qty,
                price=SELL_PRICE,
            )
            print(f"    -> entry creata: {result}")
            created.append(result)
        except Exception as exc:
            print(f"    -> ERRORE: {exc}")
    return created


async def run_market_agent(dry_run: bool = False) -> list[dict]:
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        balance = float(restaurant.get("balance", 0))
        inventory: dict[str, int] = restaurant.get("inventory", {})
        print(f"[MARKET] saldo: {balance} | inventario: {inventory or '(vuoto)'}")

        menu_raw = await client.get_menu()
        menu_names: list[str] = []
        for item in menu_raw:
            name = item.get("name", "") if isinstance(item, dict) else str(item)
            if name:
                menu_names.append(name)

        purchased: list[dict] = []
        if not menu_names:
            print("[MARKET] menu vuoto — salto acquisti")
        else:
            print(f"[MARKET] ricette nel menu: {menu_names}")
            recipes = await client.get_recipes()
            missing = compute_missing(menu_names, recipes, inventory)

            if not missing:
                print("[MARKET] inventario completo, niente da comprare")
            else:
                print(f"[MARKET] ingredienti mancanti:")
                for ing, qty in missing.items():
                    print(f"  - {ing}: {qty}")
                market = await client.get_market_entries()
                print(f"[MARKET] entry sul mercato: {len(market)}")
                to_buy = find_best_entries(missing, market, balance)
                for entry in to_buy:
                    entry_id = entry.get("id")
                    ing = entry.get("ingredient_name", "?")
                    price = entry.get("price", "?")
                    if dry_run:
                        print(f"  [DRY-RUN] execute_transaction({entry_id}) — {ing} @ {price}")
                        purchased.append(entry)
                    else:
                        try:
                            result = await client.execute_transaction(entry_id)
                            print(f"  [OK] comprato {ing} @ {price} | {result}")
                            purchased.append(entry)
                        except Exception as exc:
                            print(f"  [ERR] execute_transaction({entry_id}): {exc}")

        surplus = load_surplus()
        sold = await sell_surplus(client, surplus, dry_run)

    log = {"purchased": purchased, "sold": sold}
    log_path = Path(__file__).parent / "explorer_data" / "market_log.json"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[MARKET] log salvato -> {log_path}")
    return purchased


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("[MARKET] modalita DRY-RUN: nessuna transazione reale")
    asyncio.run(run_market_agent(dry_run=dry))
