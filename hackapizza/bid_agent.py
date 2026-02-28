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

BUDGET_FRACTION = 0.35   # % del saldo da spendere all'asta
MAX_INGREDIENTS = 20     # quanti ingredienti puntare al massimo
DEFAULT_BID = 20         # offerta di fallback se non ci sono hint di prezzo


def build_bids(
    ingredients: list[str],
    balance: float,
    primary_count: int = 0,
    price_hints: dict[str, int] | None = None,
    ingredient_quantities: dict[str, int] | None = None,
) -> list[dict]:
    """
    Costruisce le offerte per l'asta.

    - primary_count: quanti dei primi ingredienti sono della ricetta focus (70% budget)
    - price_hints: {ing: recommended_bid_per_unit} dall'auction analyst
    - ingredient_quantities: {ing: qty_totale_da_comprare} dalla strategy
    """
    if not ingredients or balance <= 0:
        return []

    price_hints = price_hints or {}
    ingredient_quantities = ingredient_quantities or {}
    budget = balance * BUDGET_FRACTION
    chosen = ingredients[:MAX_INGREDIENTS]
    bids = []

    n_primary = min(primary_count, len(chosen))

    def _bid_for(ing: str, budget_share: float) -> dict:
        """Costruisce una singola offerta per un ingrediente."""
        qty = ingredient_quantities.get(ing, 1)
        qty = max(1, qty)

        hint = price_hints.get(ing)
        if hint:
            # Usa il prezzo raccomandato dall'analisi storica (+5% margine sicurezza)
            bid_per_unit = max(1, int(hint * 1.05))
        else:
            # Distribuisce la quota budget sull'ingrediente
            bid_per_unit = max(1, int(budget_share / qty)) if qty > 0 else DEFAULT_BID
            bid_per_unit = max(bid_per_unit, DEFAULT_BID)

        return {"ingredient": ing, "quantity": qty, "bid": bid_per_unit}

    if n_primary > 0:
        primary = chosen[:n_primary]
        secondary = chosen[n_primary:]

        # 70% budget → ingredienti focus (critici per la ricetta principale)
        primary_budget = budget * 0.70
        per_primary = primary_budget / n_primary
        for ing in primary:
            bids.append(_bid_for(ing, per_primary))

        # 30% budget → ingredienti backup
        if secondary:
            sec_budget = budget * 0.30
            per_secondary = sec_budget / len(secondary)
            for ing in secondary:
                bids.append(_bid_for(ing, per_secondary))
    else:
        # Nessuna distinzione primari/secondari
        per_ing = budget / len(chosen)
        for ing in chosen:
            bids.append(_bid_for(ing, per_ing))

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
    # Leggi price_hints e ingredient_quantities dalla strategy
    price_hints: dict[str, int] = {}
    ingredient_quantities: dict[str, int] = {}
    _strategy_path = Path(__file__).parent / "explorer_data" / "strategy.json"
    if _strategy_path.exists():
        try:
            strat = json.loads(_strategy_path.read_text(encoding="utf-8"))
            price_hints = strat.get("price_hints", {})
            ingredient_quantities = strat.get("ingredient_quantities", {})
            if price_hints:
                print(f"[BID] price_hints da auction analyst: {len(price_hints)} ingredienti")
            if ingredient_quantities:
                print(f"[BID] ingredient_quantities dalla strategy: {len(ingredient_quantities)} ingredienti")
        except Exception:
            pass

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        balance = float(restaurant.get("balance", 0))
        print(f"[BID] saldo attuale: {balance}")

        if preferred_ingredients is None:
            recipes = await client.get_recipes()
            preferred_ingredients = list({
                ing
                for recipe in recipes
                for ing in recipe.get("ingredients", {})
                if ing
            })
            print(f"[BID] nessun suggerimento — pool random: {len(preferred_ingredients)} ingredienti")
        else:
            print(f"[BID] pool da strategy_agent: {len(preferred_ingredients)} ingredienti | primari: {primary_count}")

    bids = build_bids(preferred_ingredients, balance, primary_count, price_hints, ingredient_quantities)

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
