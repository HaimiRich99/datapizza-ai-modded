"""
Agente Asta — Closed Bid Phase

Offre solo sugli ingredienti suggeriti da strategy_agent.
Il budget viene distribuito in modo casuale tra di loro.

Esegui standalone: python bid_agent.py
"""

import asyncio
import json
import os
import random
from pathlib import Path

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

BUDGET_FRACTION = 0.35  # % del saldo da spendere
MAX_INGREDIENTS = 20    # quanti ingredienti puntare al massimo
MIN_QTY = 1
MAX_QTY = 2


def build_bids(ingredients: list[str], balance: float, primary_count: int = 0) -> list[dict]:
    """
    Distribuisce il budget tra gli ingredienti target.
    Se primary_count > 0:
      - primi primary_count ingredienti: 70% budget, qty=1, quota uguale per ognuno
      - resto: 30% budget, distribuzione casuale
    Altrimenti: distribuzione casuale sull'intero budget.
    """
    if not ingredients or balance <= 0:
        return []

    budget = balance * BUDGET_FRACTION
    chosen = ingredients[:MAX_INGREDIENTS]
    bids = []

    n_primary = min(primary_count, len(chosen))

    if n_primary > 0:
        primary = chosen[:n_primary]
        secondary = chosen[n_primary:]

        # 70% → ingredienti primari (qty=1, bid uguale per tutti)
        primary_budget = budget * 0.70
        per_primary = max(1, int(primary_budget / n_primary))
        for ing in primary:
            bids.append({"ingredient": ing, "quantity": 1, "bid": per_primary})

        # 30% → ingredienti secondari (distribuzione casuale)
        if secondary:
            sec_budget = budget * 0.30
            weights = [random.random() for _ in secondary]
            total_w = sum(weights)
            for ing, w in zip(secondary, weights):
                qty = random.randint(MIN_QTY, MAX_QTY)
                bid_per_unit = max(1, int((w / total_w) * sec_budget / qty))
                bids.append({"ingredient": ing, "quantity": qty, "bid": bid_per_unit})
    else:
        # Distribuzione casuale sull'intero budget
        weights = [random.random() for _ in chosen]
        total_weight = sum(weights)
        for ing, w in zip(chosen, weights):
            qty = random.randint(MIN_QTY, MAX_QTY)
            bid_per_unit = max(1, int((w / total_weight) * budget / qty))
            bids.append({"ingredient": ing, "quantity": qty, "bid": bid_per_unit})

    return bids


async def run_bid_agent(preferred_ingredients: list[str] | None = None, primary_count: int = 0) -> list[dict]:
    """
    preferred_ingredients: lista di ingredienti da strategy_agent.
    primary_count: quanti dei primi ingredienti sono "primari" (70% budget).
    Se None, sceglie a caso da tutte le ricette (fallback).
    """
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

    bids = build_bids(preferred_ingredients, balance, primary_count)

    print("\n=== OFFERTE ASTA ===")
    for b in bids:
        print(f"  {b['ingredient']} x{b['quantity']} @ {b['bid']}")
    print("=" * 40)

    out_path = Path(__file__).parent / "explorer_data" / "bid_list.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  salvato -> {out_path}")

    # Invia le offerte al server
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
