"""
Agente Strategia — Cluster Ingredienti

Logica:
1. Conta quante ricette usano ogni ingrediente (frequenza)
2. Divide gli ingredienti in 3 cluster per terzili di frequenza:
   ALTA  (top 33%)   — ingredienti "universali", usati in molte ricette
   MEDIA (mid 33%)   — ingredienti moderatamente diffusi
   BASSA (bot 33%)   — ingredienti di nicchia, poca concorrenza
3. Guarda il nostro inventario: in quale cluster cadono la maggior parte
   degli ingredienti che già possediamo? → scegli quel cluster.
   Se l'inventario è vuoto → scegli un cluster a caso.
4. Ritorna i top N ingredienti del cluster scelto (ordinati per freq desc).

Usalo standalone o chiamalo da main.py durante la closed_bid phase.

Esegui standalone: python strategy_agent.py
"""

import asyncio
import json
import os
import random
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("API_KEY", "")

# Quanti ingredienti passare al bid agent
TOP_N = 12

CLUSTER_NAMES = ["ALTA", "MEDIA", "BASSA"]


# ---------------------------------------------------------------------------
# Analisi
# ---------------------------------------------------------------------------

def count_ingredient_frequency(recipes: list[dict]) -> Counter:
    """Conta in quante ricette appare ogni ingrediente."""
    freq: Counter = Counter()
    for recipe in recipes:
        for ing in recipe.get("ingredients", {}):
            freq[ing] += 1
    return freq


def build_clusters(freq: Counter) -> dict[str, list[str]]:
    """
    Divide gli ingredienti in 3 cluster per terzili di frequenza.
    Ritorna {"ALTA": [...], "MEDIA": [...], "BASSA": [...]}.
    """
    sorted_ings = [ing for ing, _ in freq.most_common()]  # freq desc
    n = len(sorted_ings)
    third = n // 3
    return {
        "ALTA":  sorted_ings[:third],
        "MEDIA": sorted_ings[third: third * 2],
        "BASSA": sorted_ings[third * 2:],
    }


def choose_cluster(
    clusters: dict[str, list[str]],
    inventory: dict[str, int],
) -> str:
    """
    Sceglie il cluster in base all'inventario attuale.
    Se l'inventario è vuoto sceglie a caso.
    """
    if not inventory:
        chosen = random.choice(CLUSTER_NAMES)
        print(f"[STRATEGY] inventario vuoto → cluster scelto a caso: {chosen}")
        return chosen

    # Conta quanti ingredienti dell'inventario cadono in ogni cluster
    scores: Counter = Counter()
    for ing in inventory:
        for name, members in clusters.items():
            if ing in members:
                scores[name] += 1
                break

    if not scores:
        chosen = random.choice(CLUSTER_NAMES)
        print(f"[STRATEGY] nessun match inventario → cluster a caso: {chosen}")
        return chosen

    chosen = scores.most_common(1)[0][0]
    print(f"[STRATEGY] match inventario per cluster: {dict(scores)} → scelto: {chosen}")
    return chosen


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_strategy_agent() -> list[str]:
    """
    Analizza le ricette e ritorna la lista degli ingredienti target
    (top N del cluster scelto, ordinati per frequenza discendente).
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        recipes = await client.get_recipes()
        restaurant = await client.get_restaurant()

    inventory: dict[str, int] = restaurant.get("inventory", {})
    balance = restaurant.get("balance", 0)
    print(f"[STRATEGY] ricette: {len(recipes)} | saldo: {balance} | "
          f"inventario: {len(inventory)} ingredienti")

    # 1. Frequenza
    freq = count_ingredient_frequency(recipes)
    print(f"[STRATEGY] ingredienti unici: {len(freq)}")

    # 2. Cluster
    clusters = build_clusters(freq)
    for name, members in clusters.items():
        top3 = members[:3]
        print(f"[STRATEGY] cluster {name:5} ({len(members):3} ing) "
              f"— es: {', '.join(top3)}")

    # 3. Scelta cluster
    chosen = choose_cluster(clusters, inventory)
    target_ings = clusters[chosen][:TOP_N]

    print(f"\n[STRATEGY] ingredienti target (top {TOP_N} del cluster {chosen}):")
    for i, ing in enumerate(target_ings, 1):
        print(f"  {i:2}. {ing} (appare in {freq[ing]} ricette)")

    # Salva per debug
    out = {
        "cluster_scelto": chosen,
        "frequenze": {k: v for k, v in freq.most_common()},
        "clusters": {k: v for k, v in clusters.items()},
        "target_ingredients": target_ings,
    }
    out_path = Path(__file__).parent / "explorer_data" / "strategy.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[STRATEGY] analisi salvata -> {out_path}")

    return target_ings


# ---------------------------------------------------------------------------
# Standalone: analisi + bid
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    bid_only = "--bid" in sys.argv

    async def main() -> None:
        target = await run_strategy_agent()

        if bid_only:
            # Import qui per evitare dipendenze circolari
            from bid_agent import run_bid_agent
            print("\n[STRATEGY] passo gli ingredienti al bid agent...\n")
            bids = await run_bid_agent(preferred_ingredients=target)
            print("\n=== OFFERTE FINALI ===")
            for b in bids:
                print(f"  {b['ingredient']} x{b['quantity']} @ {b['bid']}")
        else:
            print("\nUsa --bid per eseguire anche il bid agent con questi ingredienti.")

    asyncio.run(main())
