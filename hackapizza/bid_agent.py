"""
Agente Asta — Closed Bid Phase

Offre sugli ingredienti suggeriti da strategy_agent, con le quantità
esatte calcolate per fare N copie della ricetta focus.

Esegui standalone: python bid_agent.py
"""

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

MAX_BUDGET = 100_000.0     # cap teorico di sicurezza
BUDGET_FRACTION = 0.20     # non spendere più del 20% del saldo a turno
MAX_COPIES_TO_STOCK = 2    # max copie per ricetta: varietà > volume
MAX_SCALE = 1              # nessuno scaling: la diversità è già in ingredient_quantities
MAX_INGREDIENTS = 20     # quanti ingredienti puntare al massimo
DEFAULT_BID = 20         # offerta di fallback se non ci sono hint di prezzo

OPPORTUNISTIC_BID = 2    # prezzo unitario bid opportunistici (ingredienti non target)
OPPORTUNISTIC_QTY = 3    # quantità per ogni bid opportunistico

_INGREDIENTS_PATH = Path(__file__).parent / "lista_completa_ingredienti.txt"


def _load_all_ingredients() -> list[str]:
    """Legge la lista completa degli ingredienti del gioco."""
    if not _INGREDIENTS_PATH.exists():
        return []
    return [
        line.strip()
        for line in _INGREDIENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _add_opportunistic_bids(main_bids: list[dict], all_ingredients: list[str]) -> list[dict]:
    """
    Aggiunge bid minimi su tutti gli ingredienti non già coperti dai bid principali.
    Obiettivo: sfruttare vuoti di mercato senza impattare il budget principale.
    """
    already_bidding = {b["ingredient"] for b in main_bids}
    extra = [
        {"ingredient": ing, "quantity": OPPORTUNISTIC_QTY, "bid": OPPORTUNISTIC_BID}
        for ing in all_ingredients
        if ing not in already_bidding
    ]
    return main_bids + extra

def _budget_scale_quantities(
    ingredient_quantities: dict[str, int],
    copies_target: int,
    balance: float,
    price_hints: dict[str, int] | None = None,
) -> dict[str, int]:
    """
    Scala le quantità dalla strategy fino a MAX_COPIES_TO_STOCK copie,
    limitato da MAX_SCALE e dal budget (max BUDGET_FRACTION del saldo).
    Il cap è demand-driven (quanti clienti ci aspettiamo per turno), non budget-driven.
    """
    if copies_target <= 0 or not ingredient_quantities:
        return ingredient_quantities
    price_hints = price_hints or {}

    # Costo stimato per una singola copia della ricetta focus
    cost_per_copy = sum(
        (qty / copies_target) * (max(1, int(price_hints[ing] * 1.05)) if price_hints.get(ing) else DEFAULT_BID)
        for ing, qty in ingredient_quantities.items()
    )
    if cost_per_copy <= 0:
        return ingredient_quantities

    # Cap per domanda: non stoccare più di MAX_COPIES_TO_STOCK copie
    demand_cap = MAX_COPIES_TO_STOCK // copies_target  # quante volte moltiplicare il target base

    # Cap per budget: non superare BUDGET_FRACTION del saldo
    budget_cap = int(balance * BUDGET_FRACTION / (cost_per_copy * copies_target)) if cost_per_copy > 0 else MAX_SCALE

    scale = max(1, min(demand_cap, budget_cap, MAX_SCALE))
    if scale <= 1:
        return ingredient_quantities

    total_cost = cost_per_copy * copies_target * scale
    print(f"[BID] scaling {scale}x → {copies_target * scale} copie totali | costo stimato {total_cost:.0f}")
    return {ing: qty * scale for ing, qty in ingredient_quantities.items()}


def build_bids_legacy(
    ingredients: list[str],
    balance: float,
    primary_count: int = 0,
    price_hints: dict[str, int] | None = None,
) -> list[dict]:
    """Fallback di emergenza: distribuisce il budget sugli ingredienti forniti."""
    if not ingredients or balance <= 0:
        return []

    price_hints = price_hints or {}
    budget = min(MAX_BUDGET, float(balance) * BUDGET_FRACTION)
    chosen = ingredients[:MAX_INGREDIENTS]
    bids = []
    n_primary = min(primary_count, len(chosen))

    def _bid_for(ing: str, budget_share: float) -> dict:
        hint = price_hints.get(ing)
        bid_per_unit = max(1, int(hint * 1.05)) if hint else max(DEFAULT_BID, int(budget_share))
        return {"ingredient": ing, "quantity": 1, "bid": bid_per_unit}

    if n_primary > 0:
        primary, secondary = chosen[:n_primary], chosen[n_primary:]
        per_primary = budget * 0.70 / n_primary
        for ing in primary:
            bids.append(_bid_for(ing, per_primary))
        if secondary:
            per_secondary = budget * 0.30 / len(secondary)
            for ing in secondary:
                bids.append(_bid_for(ing, per_secondary))
    else:
        per_ing = budget / len(chosen)
        for ing in chosen:
            bids.append(_bid_for(ing, per_ing))

    return bids


def build_bids_from_quantities(
    ingredient_quantities: dict[str, int],
    price_hints: dict[str, int] | None = None,
) -> list[dict]:
    """
    Path primario: usa direttamente ingredient_quantities dalla strategy.
    Le quantità sono già calcolate per (copies_target * qty_per_copy - inventario_attuale).
    È il path più preciso e rispetta la priorità focus vs backup.
    """
    price_hints = price_hints or {}
    bids = []
    for ing, qty in ingredient_quantities.items():
        if qty <= 0:
            continue
        hint = price_hints.get(ing)
        bid_per_unit = max(1, int(hint * 1.05)) if hint else DEFAULT_BID
        bids.append({"ingredient": ing, "quantity": qty, "bid": bid_per_unit})
    return bids


def build_bids_from_recipes(
    simplest_recipes: list[dict],
    balance: float,
    price_hints: dict[str, int] | None = None,
) -> list[dict]:
    """
    Costruisce le offerte basandosi sugli 'stock' completi delle ricette più semplici.
    Cerca di dividere equamente il budget tra le ricette e calcola quante copie (stock)
    può acquistare per ciascuna.
    """
    if not simplest_recipes or balance <= 0:
        return []

    price_hints = price_hints or {}
    budget = min(MAX_BUDGET, float(balance) * BUDGET_FRACTION)
    bids_map: dict[str, dict] = {}  # ing -> {quantity, bid}

    # Dividiamo il budget equamente tra le ricette fornite
    budget_per_recipe = budget / len(simplest_recipes)

    for recipe in simplest_recipes:
        ings = recipe.get("ingredients", {})
        if not ings:
            continue
            
        # Calcolo costo stimato per UNO stock (1 copia della ricetta)
        cost_per_stock = 0.0
        for ing, qty in ings.items():
            hint = price_hints.get(ing)
            # Stimiamo il prezzo unitario
            estimated_price = max(1, int(hint * 1.05)) if hint else DEFAULT_BID
            cost_per_stock += estimated_price * qty
            
        print(f"[BID] Ricetta {recipe['name']} | Costo stimato per stock: {cost_per_stock}")

        # Quanti stock interi ci possiamo permettere?
        stocks_to_buy = int(budget_per_recipe // cost_per_stock) if cost_per_stock > 0 else 0
        stocks_to_buy = max(1, stocks_to_buy)  # Almeno 1 per provare
        print(f"[BID] -> Budget assegnato: {budget_per_recipe:.0f} -> Stock stimati: {stocks_to_buy}")

        # Genera le quantità per gli ingredienti di questa ricetta
        for ing, qty_per_stock in ings.items():
            total_qty = qty_per_stock * stocks_to_buy
            
            hint = price_hints.get(ing)
            bid_per_unit = max(1, int(hint * 1.05)) if hint else DEFAULT_BID

            if ing in bids_map:
                # Se l'ingrediente serve per più ricette, sommiamo le quantità
                # Manteniamo il bid più alto (o lo stesso)
                bids_map[ing] = {
                    "quantity": bids_map[ing]["quantity"] + total_qty,
                    "bid": max(bids_map[ing]["bid"], bid_per_unit)
                }
            else:
                bids_map[ing] = {"quantity": total_qty, "bid": bid_per_unit}

    # Converti la mappa nella lista finale
    bids = [{"ingredient": ing, "quantity": data["quantity"], "bid": data["bid"]} 
            for ing, data in bids_map.items()]
            
    # Ordina per grandezza totale (opzionale, per log visivo)
    bids.sort(key=lambda x: -(x["quantity"] * x["bid"]))
    return bids


async def run_bid_agent(
    preferred_ingredients: list[str] | None = None,
    primary_count: int = 0,
) -> list[dict]:
    """
    preferred_ingredients: lista di ingredienti da strategy_agent.
    primary_count: quanti dei primi ingredienti sono "primari" (70% budget).
    Se None, sceglie a caso da tutte le ricette (fallback).
    """
    # Leggi price_hints, ingredient_quantities e simplest_recipes dalla strategy
    price_hints: dict[str, int] = {}
    ingredient_quantities: dict[str, int] = {}
    simplest_recipes: list[dict] = []
    copies_target: int = 3
    _strategy_path = Path(__file__).parent / "explorer_data" / "strategy.json"
    if _strategy_path.exists():
        try:
            strat = json.loads(_strategy_path.read_text(encoding="utf-8"))
            price_hints = strat.get("price_hints", {})
            ingredient_quantities = strat.get("ingredient_quantities", {})
            simplest_recipes = strat.get("simplest_recipes", [])
            copies_target = int(strat.get("copies_target", 3))
            if price_hints:
                print(f"[BID] price_hints da auction analyst: {len(price_hints)} ingredienti")
            if ingredient_quantities:
                print(f"[BID] ingredient_quantities dalla strategy: {len(ingredient_quantities)} ingredienti")
            if simplest_recipes:
                print(f"[BID] ricette in memoria lette dalla strategy: {len(simplest_recipes)}")
        except Exception:
            pass

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        balance = float(restaurant.get("balance", 0))
        print(f"[BID] saldo attuale: {balance}")

        if ingredient_quantities:
            # Path primario: scala le quantità in base al budget, poi costruisci i bid
            scaled = _budget_scale_quantities(ingredient_quantities, copies_target, balance, price_hints)
            bids = build_bids_from_quantities(scaled, price_hints)
            print(f"[BID] path principale: ingredient_quantities ({len(bids)} ingredienti target)")
        elif simplest_recipes:
            # Fallback: nessuna quantity esplicita, usa le 10 ricette più semplici
            bids = build_bids_from_recipes(simplest_recipes, balance, price_hints)
            print(f"[BID] fallback simplest_recipes ({len(bids)} ingredienti)")
        else:
            # Fallback legacy
            print("[BID] fallback legacy")
            if preferred_ingredients is None:
                recipes = await client.get_recipes()
                preferred_ingredients = list({
                    ing
                    for recipe in recipes
                    for ing in recipe.get("ingredients", {})
                    if ing
                })
            bids = build_bids_legacy(preferred_ingredients, balance, primary_count, price_hints)

    # Aggiungi bid opportunistici su tutti gli ingredienti non già coperti
    all_ingredients = _load_all_ingredients()
    if all_ingredients:
        bids_before = len(bids)
        bids = _add_opportunistic_bids(bids, all_ingredients)
        print(f"[BID] bid opportunistici aggiunti: {len(bids) - bids_before} ingredienti @ {OPPORTUNISTIC_BID}x{OPPORTUNISTIC_QTY}")

    print("\n=== OFFERTE ASTA ===")
    for b in bids:
        print(f"  {b['ingredient']} x{b['quantity']} @ {b['bid']}")
    print("=" * 40)

    out_path = Path(__file__).parent / "explorer_data" / "bid_list.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  salvato -> {out_path}")

    if bids:
        payload = [{"ingredient": b["ingredient"], "quantity": b["quantity"], "bid": b["bid"]} for b in bids]
        async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
            try:
                result = await client.closed_bid(payload)
                print(f"[BID] offerte inviate al server | risposta: {result}\n")
            except Exception as exc:
                print(f"[BID] ERRORE invio offerte: {exc}\n")
    else:
        print("[BID] nessuna offerta da inviare\n")

    return bids


if __name__ == "__main__":
    asyncio.run(run_bid_agent())
