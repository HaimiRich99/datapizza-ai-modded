"""
Agente Asta.

Strategia: massimizzare il numero di ingredienti diversi acquistati.
- Legge price_hints da strategy.json (generato da auction_analyst)
- Offre il bid minimo necessario per vincere ogni ingrediente
- Ordina per prezzo crescente e compra quanti più ingredienti possibile entro il budget
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

MAX_BUDGET = 1000   # crediti massimi da spendere
DEFAULT_BID = 5     # bid di default se non ci sono price_hints


async def run_bid_agent(
    preferred_ingredients: list[str] | None = None,
    primary_count: int = 0,
    dry_run: bool = False,
) -> list[dict]:
    """
    preferred_ingredients: lista ingredienti target (se None, legge da strategy.json).
    dry_run: se True, calcola e stampa le offerte senza inviarle al server.
    """
    strategy_path = Path(__file__).parent / "explorer_data" / "strategy.json"

    # Carica strategy.json
    strategy: dict = {}
    if strategy_path.exists():
        try:
            strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Lista ingredienti target
    if preferred_ingredients is None:
        preferred_ingredients = strategy.get("target_ingredients", [])

    if not preferred_ingredients:
        print("[BID] nessun ingrediente da comprare")
        return []

    # Price hints dall'auction analyst (ingredient → bid per unità)
    price_hints: dict[str, int] = strategy.get("price_hints", {})

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        balance = float(restaurant.get("balance", 0))

    budget = min(MAX_BUDGET, balance)

    # Costruisci candidati: 1 unità per ingrediente al bid minimo necessario
    candidates = []
    for ing in preferred_ingredients:
        bid = price_hints.get(ing, DEFAULT_BID)
        candidates.append({"ingredient": ing, "quantity": 1, "bid": bid})

    # Ordina per bid crescente → massimizza il numero di ingredienti acquistabili
    candidates.sort(key=lambda x: x["bid"])

    # Selezione greedy: prendi gli ingredienti più economici finché il budget tiene
    bids = []
    spent = 0
    skipped = []
    for c in candidates:
        cost = c["bid"] * c["quantity"]
        if spent + cost <= budget:
            bids.append(c)
            spent += cost
        else:
            skipped.append(c["ingredient"])

    print(f"[BID] saldo: {balance:.0f} | budget: {budget:.0f} | "
          f"target: {len(preferred_ingredients)} | selezionati: {len(bids)} | skippati: {len(skipped)}")
    if skipped:
        print(f"[BID] ingredienti fuori budget ({len(skipped)}): {skipped[:10]}{'...' if len(skipped) > 10 else ''}")
    print(f"[BID] spesa stimata: {spent:.0f} crediti")

    print("\n=== OFFERTE ASTA ===")
    for b in bids:
        print(f"  {b['ingredient']} x{b['quantity']} @ {b['bid']}")
    print("=" * 40)

    out_path = Path(__file__).parent / "explorer_data" / "bid_list.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(bids, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  salvato -> {out_path}")

    if dry_run:
        print("[BID] dry-run: offerte NON inviate al server\n")
        return bids

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        try:
            payload = [{"ingredient": b["ingredient"], "quantity": b["quantity"], "bid": b["bid"]} for b in bids]
            result = await client.closed_bid(payload)
            print(f"[BID] offerte inviate | risposta: {result}\n")
        except Exception as exc:
            print(f"[BID] ERRORE invio offerte: {exc}\n")

    return bids


if __name__ == "__main__":
    import sys
    asyncio.run(run_bid_agent(dry_run="--dry-run" in sys.argv))
