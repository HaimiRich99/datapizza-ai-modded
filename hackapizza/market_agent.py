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

STRATEGY_PATH = Path(__file__).parent / "explorer_data" / "strategy.json"

TOP_TARGET_RECIPES = 5   # usato solo nel fallback senza strategy
MAX_BUY_ROUNDS = 5       # massimo numero di round di polling del mercato


def load_simplest_recipes() -> list[dict]:
    """Legge la lista di ricette scelte come target dallo strategy agent."""
    if not STRATEGY_PATH.exists():
        return []
    try:
        data = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
        return data.get("simplest_recipes", [])
    except Exception:
        return []

def load_focus_strategy() -> tuple[list[str], int]:
    """Fallback legacy se simplest_recipes non esiste."""
    if not STRATEGY_PATH.exists():
        return [], 1
    try:
        data = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
        return data.get("focus_recipes", []), data.get("copies_target", 1)
    except Exception:
        return [], 1


def find_target_recipes(
    recipes: list[dict],
    inventory: dict[str, int],
) -> list[dict]:
    """
    Ritorna le ricette focus dalla strategy.
    Fallback: ricette più vicine al completamento.
    """
    focus_names, _ = load_focus_strategy()

    if focus_names:
        recipe_map = {r["name"]: r for r in recipes}
        targets = [recipe_map[name] for name in focus_names if name in recipe_map]
        if targets:
            return targets

    # Fallback: ricette con maggiore copertura inventario
    def coverage(recipe):
        needed = recipe.get("ingredients", {})
        if not needed:
            return 0.0
        covered = sum(1 for ing, qty in needed.items() if inventory.get(ing, 0) >= qty)
        return covered / len(needed)

    if inventory:
        scored = sorted(recipes, key=lambda r: (-coverage(r), -r.get("prestige", 0)))
        candidates = [r for r in scored if coverage(r) > 0]
        if candidates:
            return candidates[:TOP_TARGET_RECIPES]

    return sorted(recipes, key=lambda r: len(r.get("ingredients", {})))[:TOP_TARGET_RECIPES]


def compute_missing(
    target_recipes: list[dict],
    inventory: dict[str, int],
    copies_target: int = 1,
) -> dict[str, int]:
    """
    Calcola gli ingredienti mancanti per fare copies_target copie
    di ciascuna ricetta target.
    """
    missing: dict[str, int] = {}
    for recipe in target_recipes:
        for ing, qty_per_copy in recipe.get("ingredients", {}).items():
            total_needed = qty_per_copy * copies_target
            have = inventory.get(ing, 0)
            if have < total_needed:
                missing[ing] = max(missing.get(ing, 0), total_needed - have)
    return missing


def find_best_entries(
    missing: dict[str, int],
    market: list[dict],
    budget: float,
) -> list[dict]:
    spent: float = 0.0
    to_buy: list[dict] = []

    sell_by_ing: dict[str, list[dict]] = {}
    for entry in market:
        if entry.get("side") != "SELL":
            continue
        ing = entry.get("ingredient_name", "")
        if ing:
            if ing not in sell_by_ing:
                sell_by_ing[ing] = []
            sell_by_ing[ing].append(entry)
            
    for ing in sell_by_ing:
        sell_by_ing[ing].sort(key=lambda e: float(e.get("price", 9999)))

    for ing, qty_needed in missing.items():
        entries = sell_by_ing.get(ing, [])
        if not entries:
            continue
        qty_left: int = qty_needed
        for entry in entries:
            if qty_left <= 0:
                break
            price = float(entry.get("price", 9999))
            if price > MAX_PRICE_PER_UNIT:
                continue
            entry_qty = int(entry.get("quantity", 0))
            cost: float = price * entry_qty  # Paghi l'intera entry, non puoi splittare
            if spent + cost > budget:
                continue
            to_buy.append(entry)
            spent += cost
            qty_left -= entry_qty
            print(f"  [BUY]  {ing} x{min(entry_qty, qty_needed)} @ {price} "
                  f"(entry_id={entry.get('id')}) | speso: {spent:.2f}")
    return to_buy



async def sell_surplus(
    client: "HackapizzaClient",
    surplus: dict[str, int],
    purchased_inv: dict[str, int],
    dry_run: bool,
) -> list[dict]:
    if not surplus:
        return []

    created: list[dict] = []
    for ing, qty in surplus.items():
        # Calcoliamo il prezzo di vendita: +5% rispetto all'asta, altrimenti il fittizio
        cost_paid = purchased_inv.get(ing, 0)
        sell_price = max(1, int(cost_paid * 1.05)) if cost_paid > 0 else SELL_PRICE
        
        print(f"  SELL  {ing} x{qty} @ {sell_price}")
        if dry_run:
            created.append({"ingredient": ing, "quantity": qty, "price": sell_price})
            continue
        try:
            result = await client.create_market_entry(
                side="SELL",
                ingredient_name=ing,
                quantity=qty,
                price=sell_price,
            )
            created.append(result)
        except Exception as exc:
            print(f"[MARKET] ERRORE SELL {ing}: {exc}")
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

        simplest_recipes = load_simplest_recipes()
        if not simplest_recipes:
             # Fallback
             recipes = await client.get_recipes()
             simplest_recipes = find_target_recipes(recipes, inventory)

        virtual_inv = dict(inventory)
        to_buy: dict[str, int] = {}

        # 1. Calcolo del surplus e degli ingredienti da comprare in modo dinamico
        # Ordiniamo le ricette per prestigio in modo da dare priorità alle più remunerative
        sorted_recipes = sorted(simplest_recipes, key=lambda r: r.get("prestige", 0), reverse=True)
        
        for recipe in sorted_recipes:
            ings = recipe.get("ingredients", {})
            if not ings:
                continue
            
            while True:
                missing_for_one = {}
                for ing, req_qty in ings.items():
                    if virtual_inv.get(ing, 0) < req_qty:
                        missing_for_one[ing] = req_qty - virtual_inv.get(ing, 0)
                
                total_req = sum(ings.values())
                total_missing = sum(missing_for_one.values())
                covered = total_req - total_missing

                if total_missing == 0:
                    # Abbiamo tutto per produrre una copia! Alloca l'inventario.
                    for ing, req_qty in ings.items():
                        virtual_inv[ing] -= req_qty
                
                elif len(missing_for_one) == 1 and covered > 0:
                    # Manca solo un tipo di ingrediente per chiudere! Ne abbiamo i rimanenti in inventario.
                    missing_ing, missing_qty = list(missing_for_one.items())[0]
                    to_buy[missing_ing] = to_buy.get(missing_ing, 0) + missing_qty

                    for ing, req_qty in ings.items():
                        virtual_inv[ing] -= req_qty
                        if virtual_inv[ing] < 0:
                            virtual_inv[ing] = 0
                else:
                    # Ci mancano 2 o più elementi, o abbiamo 0 pezzi dell'intera ricetta. Stop.
                    break
        
        # Quello che rimane in virtual_inv (non allocato a nessuna "quasi ricetta") è il NOSTRO SURPLUS REALE!
        surplus = {ing: qty for ing, qty in virtual_inv.items() if qty > 0}
        
        # Recupera prezzi d'asta dal bid agent (file salvato in run_bid_agent)
        purchased_inv: dict[str, int] = {}
        purchased_path = Path(__file__).parent / "explorer_data" / "purchased_inventory.json"
        if purchased_path.exists():
            try:
                purchased_inv = json.loads(purchased_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # --- 2. Vendi surplus UNA sola volta (solo se sell=True) ---
        sold: list[dict] = []
        if sell:
            sold = await sell_surplus(client, surplus, purchased_inv, dry_run)
        else:
            pass

        # --- 3. Loop acquisti (max MAX_BUY_ROUNDS round) ---
        purchased_all: list[dict] = []
        budget = balance * BUDGET_FRACTION

        if not to_buy:
            pass
        else:
            for round_num in range(1, MAX_BUY_ROUNDS + 1):
                if not to_buy:
                    break
                market = await client.get_market_entries()

                # Troviamo gli acquisti ottimali dalla wishlist `to_buy`

                buy_list = find_best_entries(to_buy, market, budget)
                if not buy_list:
                    break

                bought_this_round: list[dict] = []
                for entry in buy_list:
                    ing = entry.get("ingredient_name", "?")
                    if to_buy.get(ing, 0) <= 0:
                        continue
                        
                    entry_id = entry.get("id")
                    price = float(entry.get("price", 0))
                    qty_entry = int(entry.get("quantity", 1))

                    if dry_run:
                        qty_act = min(qty_entry, to_buy[ing])
                        bought_this_round.append(entry)
                        to_buy[ing] -= qty_act
                        if to_buy[ing] <= 0:
                            to_buy[ing] = 0
                        budget -= price * qty_entry
                    else:
                        try:
                            result = await client.execute_transaction(entry_id)
                            print(f"  [OK] comprato {ing} @ {price} | {result}")
                            bought_this_round.append(entry)
                            virtual_inv[ing] = virtual_inv.get(ing, 0) + qty_entry
                            to_buy[ing] -= qty_entry
                            if to_buy[ing] <= 0:
                                to_buy[ing] = 0
                            budget -= price * qty_entry
                        except Exception as exc:
                            print(f"  [ERR] execute_transaction({entry_id}): {exc}")

                purchased_all.extend(bought_this_round)

                if not bought_this_round:
                    break
                if budget <= 1:
                    break

    log_data = {"purchased": purchased_all, "sold": sold}
    log_path = Path(__file__).parent / "explorer_data" / "market_log.json"
    log_path.parent.mkdir(exist_ok=True)
    log_path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return purchased_all


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    asyncio.run(run_market_agent(dry_run=dry))
