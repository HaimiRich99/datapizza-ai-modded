"""
Agente Mercato — Fase waiting/market

Logica VENDITE:
1. Legge explorer_data/surplus_ingredients.json (scritto da menu_agent)
2. Crea UNA SOLA volta le entry SELL sul mercato

Logica ACQUISTI (loop):
3. Carica inventario attuale e saldo
4. Calcola gli ingredienti mancanti per le ricette più vicine al completamento
5. Fetcha il mercato e compra ciò che manca (rispettando budget e prezzo max)
6. Aggiorna inventario virtuale ed esegue altri round finché:
   - tutti gli ingredienti mancanti sono coperti, oppure
   - nessuna entry utile sul mercato, oppure
   - budget esaurito, oppure
   - raggiunto MAX_BUY_ROUNDS

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
API_KEY = os.getenv("TEAM_API_KEY", "")

# Prezzo massimo accettabile per unita di ingrediente (acquisti)
MAX_PRICE_PER_UNIT = 80.0
# Frazione massima del saldo da spendere in totale (acquisti)
BUDGET_FRACTION = 0.7
# Prezzo a cui vendiamo il surplus (per unita)
SELL_PRICE = 25.0

SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"

TOP_TARGET_RECIPES = 5   # quante ricette "prossime al completamento" consideriamo
MAX_BUY_ROUNDS = 5       # massimo numero di round di polling del mercato


def find_nearest_recipes(
    recipes: list[dict],
    inventory: dict[str, int],
) -> list[dict]:
    """
    Ritorna le ricette con la maggiore frazione di ingredienti già coperti
    dall'inventario, ordinate per (coverage desc, prestige desc).
    Con inventario vuoto, prende le ricette con meno ingredienti totali.
    """
    def coverage(recipe):
        needed = recipe.get("ingredients", {})
        if not needed:
            return 0.0
        covered = sum(1 for ing, qty in needed.items() if inventory.get(ing, 0) >= qty)
        return covered / len(needed)

    if inventory:
        scored = sorted(recipes, key=lambda r: (-coverage(r), -r.get("prestige", 0)))
        # prendi solo quelle con almeno un ingrediente già coperto
        candidates = [r for r in scored if coverage(r) > 0]
        if candidates:
            return candidates[:TOP_TARGET_RECIPES]
    # fallback: ricette con meno ingredienti distinti (più facili da completare)
    return sorted(recipes, key=lambda r: len(r.get("ingredients", {})))[:TOP_TARGET_RECIPES]


def compute_missing(
    target_recipes: list[dict],
    inventory: dict[str, int],
) -> dict[str, int]:
    """Calcola gli ingredienti mancanti aggregati su tutte le ricette target."""
    missing: dict[str, int] = defaultdict(int)
    for recipe in target_recipes:
        for ing, qty_needed in recipe.get("ingredients", {}).items():
            have = inventory.get(ing, 0)
            if have < qty_needed:
                missing[ing] = max(missing[ing], qty_needed - have)
    return dict(missing)


def find_best_entries(
    missing: dict[str, int],
    market: list[dict],
    budget: float,
) -> list[dict]:
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


async def run_market_agent(dry_run: bool = False, sell: bool = True) -> list[dict]:
    """
    sell=True  → vende il surplus UNA volta, poi esegue il loop acquisti.
    sell=False → solo loop acquisti (nessuna vendita), utile per la seconda
                 chiamata dall'orchestratore dopo menu_agent.
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        balance = float(restaurant.get("balance", 0))
        inventory: dict[str, int] = dict(restaurant.get("inventory", {}))
        print(f"[MARKET] saldo: {balance} | inventario: {inventory or '(vuoto)'}")

        recipes = await client.get_recipes()
        targets = find_nearest_recipes(recipes, inventory)
        print(f"[MARKET] ricette target ({len(targets)}):")
        for r in targets:
            needed = r.get("ingredients", {})
            covered = sum(1 for ing, qty in needed.items() if inventory.get(ing, 0) >= qty)
            print(f"  - {r['name']} | {covered}/{len(needed)} ingredienti coperti | prestige={r.get('prestige')}")

        # --- 1. Vendi surplus UNA sola volta (solo se sell=True) ---
        sold: list[dict] = []
        if sell:
            surplus = load_surplus()
            sold = await sell_surplus(client, surplus, dry_run)
        else:
            print("[MARKET] sell=False — salto vendita surplus")

        # --- 2. Loop acquisti (max MAX_BUY_ROUNDS round) ---
        purchased_all: list[dict] = []
        virtual_inv: dict[str, int] = dict(inventory)
        budget = balance * BUDGET_FRACTION

        for round_num in range(1, MAX_BUY_ROUNDS + 1):
            missing = compute_missing(targets, virtual_inv)
            if not missing:
                print(f"[MARKET] round {round_num}: inventario completo — stop")
                break

            print(f"\n[MARKET] round {round_num} | budget: {budget:.2f} | mancanti: {len(missing)}")
            for ing, qty in missing.items():
                print(f"  - {ing}: {qty}")

            market = await client.get_market_entries()
            print(f"[MARKET] entry sul mercato: {len(market)}")

            to_buy = find_best_entries(missing, market, budget)
            if not to_buy:
                print(f"[MARKET] round {round_num}: nessun acquisto possibile — stop")
                break

            bought_this_round: list[dict] = []
            for entry in to_buy:
                entry_id = entry.get("id")
                ing = entry.get("ingredient_name", "?")
                price = float(entry.get("price", 0))
                qty_entry = int(entry.get("quantity", 1))

                if dry_run:
                    print(f"  [DRY-RUN] execute_transaction({entry_id}) — {ing} @ {price}")
                    bought_this_round.append(entry)
                    virtual_inv[ing] = virtual_inv.get(ing, 0) + qty_entry
                    budget -= price * qty_entry
                else:
                    try:
                        result = await client.execute_transaction(entry_id)
                        print(f"  [OK] comprato {ing} @ {price} | {result}")
                        bought_this_round.append(entry)
                        virtual_inv[ing] = virtual_inv.get(ing, 0) + qty_entry
                        budget -= price * qty_entry
                    except Exception as exc:
                        print(f"  [ERR] execute_transaction({entry_id}): {exc}")

            purchased_all.extend(bought_this_round)

            if not bought_this_round:
                print(f"[MARKET] round {round_num}: tutti gli acquisti falliti — stop")
                break
            if budget <= 1:
                print(f"[MARKET] round {round_num}: budget esaurito — stop")
                break
        else:
            print(f"[MARKET] raggiunti {MAX_BUY_ROUNDS} round — stop")

    log_data = {"purchased": purchased_all, "sold": sold}
    log_path = Path(__file__).parent / "explorer_data" / "market_log.json"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[MARKET] log salvato -> {log_path}")
    return purchased_all


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("[MARKET] modalita DRY-RUN: nessuna transazione reale")
    asyncio.run(run_market_agent(dry_run=dry))
