"""
Agente Strategia — Semplificato.

Obiettivo: comprare 1 unità di tutti e 62 gli ingredienti ogni turno.
"""

import json
from pathlib import Path

_EXPLORER_DIR = Path(__file__).parent / "explorer_data"
_STRATEGY_PATH = _EXPLORER_DIR / "strategy.json"
_INGREDIENTS_PATH = Path(__file__).parent / "lista_completa_ingredienti.txt"

ALL_INGREDIENTS = [
    line.strip()
    for line in _INGREDIENTS_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
]


async def run_strategy_agent() -> tuple[list[str], int]:
    """
    Ritorna (target_ingredients, primary_count).
    target_ingredients: tutti i 62 ingredienti.
    primary_count: sempre 0 (budget distribuito uniformemente).
    """
    print(f"[STRATEGY] target: tutti i {len(ALL_INGREDIENTS)} ingredienti, 1 unità ciascuno")

    _EXPLORER_DIR.mkdir(exist_ok=True)
    _STRATEGY_PATH.write_text(json.dumps({
        "method": "all_ingredients",
        "target_ingredients": ALL_INGREDIENTS,
        "ingredient_quantities": {ing: 1 for ing in ALL_INGREDIENTS},
        "price_hints": {},
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    return ALL_INGREDIENTS, 0


if __name__ == "__main__":
    import asyncio
    import sys

    async def main() -> None:
        target, primary_count = await run_strategy_agent()
        if "--bid" in sys.argv:
            from bid_agent import run_bid_agent
            print("\n[STRATEGY] passo ingredienti al bid agent...\n")
            await run_bid_agent(
                preferred_ingredients=target,
                primary_count=primary_count,
                dry_run="--dry-run" in sys.argv,
            )

    asyncio.run(main())
