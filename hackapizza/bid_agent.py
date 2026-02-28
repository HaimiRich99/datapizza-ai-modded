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
API_KEY = os.getenv("API_KEY", "")

BUDGET_FRACTION = 0.3   # % del saldo da spendere
MAX_INGREDIENTS = 8     # quanti ingredienti puntare al massimo
MIN_QTY = 1
MAX_QTY = 3


def build_bids(ingredients: list[str], balance: float) -> list[dict]:
    """Distribuisce il budget tra gli ingredienti target con pesi casuali."""
    if not ingredients or balance <= 0:
        return []

    budget = balance * BUDGET_FRACTION
    chosen = random.sample(ingredients, min(MAX_INGREDIENTS, len(ingredients)))
    weights = [random.random() for _ in chosen]
    total_weight = sum(weights)

    bids = []
    for ing, w in zip(chosen, weights):
        qty = random.randint(MIN_QTY, MAX_QTY)
        bid_per_unit = max(1, int((w / total_weight) * budget / qty))
        bids.append({"ingredient": ing, "quantity": qty, "bid": bid_per_unit})
    return bids


async def run_bid_agent(preferred_ingredients: list[str] | None = None) -> list[dict]:
    """
    preferred_ingredients: lista di ingredienti da strategy_agent.
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
            print(f"[BID] pool da strategy_agent: {len(preferred_ingredients)} ingredienti")

    bids = build_bids(preferred_ingredients, balance)

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
