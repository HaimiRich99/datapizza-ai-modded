"""
Auction Analyst — analizza l'ULTIMA asta e produce price_hints per il prossimo turno.

Output: dizionario {ingrediente: bid_per_unità} per tutti gli ingredienti noti.

Logica bid consigliato:
  - Ingrediente con vincitori → min_winning_price (il prezzo più basso che ha vinto)
  - Ingrediente con soli "Insufficient funds" → 1 (era budget, non prezzo il problema)
  - Ingrediente non apparso nell'asta → 1 (nessuno lo ha voluto)

I price_hints vengono scritti in:
  - bid_recommendations.json  (letto da strategy_agent al turno successivo)
  - strategy.json["price_hints"] (letto direttamente da bid_agent)

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
    Calcola il bid consigliato per un ingrediente.
    - Almeno un vincitore → min_winning_price
    - Solo "Insufficient funds" → DEFAULT_BID (era problema di budget, non di prezzo)
    """
    winning_prices = [b["price"] for b in bids if b["bought"]]
    return min(winning_prices) if winning_prices else DEFAULT_BID


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
    Aggiorna bid_recommendations.json e strategy.json.
    """
    print(f"\n[AUCTION] analisi asta turno {turn_id}")

    parsed = parse_auction_results(auction_text)
    if not parsed:
        print("[AUCTION] nessun dato parsato")
        return {}

    n_won = sum(1 for bids in parsed.values() if any(b["bought"] for b in bids))
    n_failed = len(parsed) - n_won
    print(f"[AUCTION] {len(parsed)} ingredienti | {n_won} comprati | {n_failed} solo insufficient funds")

    # Lista completa ingredienti (include anche quelli non apparsi in asta)
    all_ingredients = load_all_ingredients() or list(parsed.keys())

    # Genera price_hints per TUTTI gli ingredienti noti
    price_hints: dict[str, int] = {}
    for ing in all_ingredients:
        bids = parsed.get(ing)
        price_hints[ing] = recommended_bid(bids) if bids else DEFAULT_BID
    # Ingredienti nell'asta ma non nella lista master
    for ing, bids in parsed.items():
        if ing not in price_hints:
            price_hints[ing] = recommended_bid(bids)

    # Riepilogo
    above_one = {ing: p for ing, p in price_hints.items() if p > DEFAULT_BID}
    print(f"[AUCTION] price_hints: {len(price_hints) - len(above_one)} @ bid={DEFAULT_BID} | {len(above_one)} con bid>{DEFAULT_BID}")
    if above_one:
        print("[AUCTION] ingredienti con bid>1:")
        for ing, p in sorted(above_one.items(), key=lambda x: -x[1]):
            print(f"  {ing}: {p}")

    # Salva bid_recommendations.json (letto da strategy_agent)
    _EXPLORER_DIR.mkdir(exist_ok=True)
    _RECOMMENDATIONS_PATH.write_text(
        json.dumps({"turn_id": turn_id, "timestamp": datetime.now().isoformat(), "price_hints": price_hints},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Aggiorna anche strategy.json (letto direttamente da bid_agent se strategy_agent non gira)
    strategy: dict = {}
    if _STRATEGY_PATH.exists():
        try:
            strategy = json.loads(_STRATEGY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    strategy["price_hints"] = price_hints
    _STRATEGY_PATH.write_text(json.dumps(strategy, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[AUCTION] price_hints salvati ({len(price_hints)} ingredienti)\n")
    return price_hints


if __name__ == "__main__":
    if len(sys.argv) > 1:
        auction_text = Path(sys.argv[1]).read_text(encoding="utf-8")
        turn_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    else:
        print("Uso: python auction_analyst.py <file_risultati.txt> [turn_id]")
        sys.exit(1)

    asyncio.run(run_auction_analyst(auction_text, turn_id))
