"""
Agente Asta — Closed Bid Phase (strategia random)

Ignora menu e competitor. Distribuisce il budget in modo casuale
sugli ingredienti disponibili nelle ricette.

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

# Quanta parte del saldo vogliamo spendere (0.0 - 1.0)
BUDGET_FRACTION = 0.3
# Quanti ingredienti distinti puntare al massimo
MAX_INGREDIENTS = 8
# Quantità per offerta
MIN_QTY = 1
MAX_QTY = 3


def build_random_bids(ingredients: list[str], balance: float) -> list[dict]:
    """Distribuisce il budget in modo casuale su un sottoinsieme di ingredienti."""
    if not ingredients or balance <= 0:
        return []

    budget = balance * BUDGET_FRACTION
    chosen = random.sample(ingredients, min(MAX_INGREDIENTS, len(ingredients)))

    # Assegna pesi casuali per distribuire il budget
    weights = [random.random() for _ in chosen]
    total_weight = sum(weights)

    bids = []
    for ing, w in zip(chosen, weights):
        qty = random.randint(MIN_QTY, MAX_QTY)
        allocated = (w / total_weight) * budget
        bid_per_unit = round(allocated / qty, 2)
        if bid_per_unit < 0.01:
            continue
        bids.append({
            "ingredient": ing,
            "quantity": qty,
            "bid": bid_per_unit,
            "reason": "random",
        })

    return bids


async def run_bid_agent() -> list[dict]:
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        balance = float(restaurant.get("balance", 0))
        print(f"[BID] saldo attuale: {balance}")

        recipes = await client.get_recipes()

    # Raccogli tutti gli ingredienti unici da tutte le ricette
    all_ingredients: set[str] = set()
    for recipe in recipes:
        for ing in recipe.get("ingredients", []):
            name = ing if isinstance(ing, str) else ing.get("name", "")
            if name:
                all_ingredients.add(name)

    print(f"[BID] ingredienti trovati: {len(all_ingredients)}")

    bids = build_random_bids(list(all_ingredients), balance)

    # Stampa
    print("\n=== OFFERTE ASTA (random) ===")
    for b in bids:
        print(f"  {b['ingredient']} x{b['quantity']} @ {b['bid']}")
    print("=" * 40)

    # Salva su file
    out_path = Path(__file__).parent.parent / "explorer_data" / "bid_list.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  salvato -> {out_path}\n")

    return bids


if __name__ == "__main__":
    bids = asyncio.run(run_bid_agent())
    print("\n=== OFFERTE FINALI ===")
    for b in bids:
        print(f"  {b['ingredient']} x{b['quantity']} @ {b['bid']}")
