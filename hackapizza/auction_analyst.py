"""
Auction Analyst — analizza l'ULTIMA asta e produce price_hints per il prossimo turno.

Output: dizionario {ingrediente: bid_per_unità} per tutti gli ingredienti noti.

Logica bid consigliato:
  - Ingrediente con vincitori → min_winning_price (il prezzo più basso che ha vinto)
  - Ingrediente con soli "Insufficient funds" → 1 (era budget, non prezzo il problema)
  - Ingrediente non apparso nell'asta → 1 (nessuno lo ha voluto)

Esegui standalone: python auction_analyst.py <testo_risultati.txt> [turn_id]
"""

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

_EXPLORER_DIR = Path(__file__).parent / "explorer_data"
_RECOMMENDATIONS_PATH = _EXPLORER_DIR / "bid_recommendations.json"
_STRATEGY_PATH = _EXPLORER_DIR / "strategy.json"
_INGREDIENTS_PATH = Path(__file__).parent / "lista_completa_ingredienti.txt"

DEFAULT_BID = 1  # bid per ingredienti senza dati

_LINE_RE = re.compile(
    r"Restaurant\s+(\d+)\s+try to buy:(\d+)\s+(.+?)\s+at single price of:\s+(\d+)\s+result:(.+)"
)


def parse_auction_results(text: str) -> dict[str, list[dict]]:
    """Parsa il testo dell'asta → {ingrediente: [{"price": int, "bought": bool}]}"""
    results: dict[str, list[dict]] = {}
    for line in text.strip().split("\n"):
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        ingredient = m.group(3).strip()
        results.setdefault(ingredient, []).append({
            "price": int(m.group(4)),
            "bought": m.group(5).strip().startswith("Bought"),
        })
    return results


def recommended_bid(bids: list[dict]) -> int:
    """
    Calcola il bid consigliato per un ingrediente dato i suoi bid nell'ultima asta.
    - Se c'è almeno un vincitore → min_winning_price (il più basso che ha vinto)
    - Altrimenti (solo Insufficient funds) → DEFAULT_BID
    """
    winning_prices = [b["price"] for b in bids if b["bought"]]
    if winning_prices:
        return min(winning_prices)
    return DEFAULT_BID


def load_all_ingredients() -> list[str]:
    if not _INGREDIENTS_PATH.exists():
        print(f"[AUCTION] WARN: {_INGREDIENTS_PATH} non trovato")
        return []
    return [
        line.strip()
        for line in _INGREDIENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def run_auction_analyst(auction_text: str, turn_id: int = 0) -> dict[str, int]:
    """
    Analizza l'ultima asta e ritorna price_hints: {ingrediente: bid_consigliato}.
    Aggiorna strategy.json e bid_recommendations.json.
    """
    print(f"\n[AUCTION] analisi asta turno {turn_id}")

    parsed = parse_auction_results(auction_text)
    if not parsed:
        print("[AUCTION] nessun dato parsato")
        return {}

    # Stats rapide
    n_won = sum(1 for bids in parsed.values() if any(b["bought"] for b in bids))
    n_failed = len(parsed) - n_won
    print(f"[AUCTION] {len(parsed)} ingredienti nell'asta | {n_won} comprati | {n_failed} solo insufficient funds")

    # Carica lista completa ingredienti
    all_ingredients = load_all_ingredients()
    if not all_ingredients:
        all_ingredients = list(parsed.keys())

    # Genera price_hints per TUTTI gli ingredienti
    price_hints: dict[str, int] = {}

    for ing in all_ingredients:
        bids = parsed.get(ing)
        price_hints[ing] = recommended_bid(bids) if bids else DEFAULT_BID

    # Ingredienti nell'asta ma non nella lista master
    for ing, bids in parsed.items():
        if ing not in price_hints:
            price_hints[ing] = recommended_bid(bids)

    # Riepilogo
    at_one = sum(1 for p in price_hints.values() if p == 1)
    above_one = {ing: p for ing, p in price_hints.items() if p > 1}
    print(f"[AUCTION] price_hints: {at_one} ingredienti @ bid=1 | {len(above_one)} con bid>1")
    if above_one:
        print("[AUCTION] ingredienti con bid>1:")
        for ing, p in sorted(above_one.items(), key=lambda x: -x[1]):
            print(f"  {ing}: {p}")

    # Salva su strategy.json
    _EXPLORER_DIR.mkdir(exist_ok=True)
    strategy: dict = {}
    if _STRATEGY_PATH.exists():
        try:
            strategy = json.loads(_STRATEGY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    strategy["price_hints"] = price_hints
    _STRATEGY_PATH.write_text(json.dumps(strategy, indent=2, ensure_ascii=False), encoding="utf-8")

    # Salva bid_recommendations.json
    output = {
        "turn_id": turn_id,
        "timestamp": datetime.now().isoformat(),
        "price_hints": price_hints,
    }
    _RECOMMENDATIONS_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[AUCTION] salvato price_hints ({len(price_hints)} ingredienti)\n")

    return price_hints


if __name__ == "__main__":
    if len(sys.argv) > 1:
        auction_text = Path(sys.argv[1]).read_text(encoding="utf-8")
        turn_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    else:
        auction_text = ""
        turn_id = 0

    if not auction_text:
        print("Uso: python auction_analyst.py <file_risultati.txt> [turn_id]")
        sys.exit(1)

    asyncio.run(run_auction_analyst(auction_text, turn_id))
