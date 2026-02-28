"""
Debug /meals endpoint — stampa la risposta raw per vedere i nomi dei campi.

Uso: python debug_meals.py [turn_id]
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")


async def fetch_meals_raw(turn_id: int) -> list[dict] | None:
    """Ritorna i meals o None in caso di errore."""
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}
    params = {"turn_id": turn_id, "restaurant_id": TEAM_ID}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"{BASE_URL}/meals", headers=headers, params=params) as resp:
            body = await resp.text()
            print(f"  turn_id={turn_id} → HTTP {resp.status}")
            if resp.status == 200:
                return json.loads(body)
            else:
                print(f"  risposta: {body[:200]}")
                return None


async def main(turn_id: int | None) -> None:
    if turn_id is not None:
        candidates = [turn_id]
    else:
        # Prova gli ultimi 10 turni (più recenti prima)
        candidates = list(range(20, 0, -1))
        print("Nessun turn_id specificato — cerco l'ultimo turno con dati...\n")

    meals = None
    used_turn = None
    for tid in candidates:
        result = await fetch_meals_raw(tid)
        if result is not None:
            meals = result
            used_turn = tid
            if meals:
                break  # trovato turno con dati

    if meals is None:
        print("\nNessun turno raggiungibile.")
        return

    print(f"\n=== /meals turn_id={used_turn} | {len(meals)} record ===\n")

    if not meals:
        print("(nessun meal per questo turno)")
        return

    first = meals[0]
    print(f"Campi disponibili: {list(first.keys())}\n")

    for i, m in enumerate(meals, 1):
        print(f"[{i}] {json.dumps(m, ensure_ascii=False, indent=2)}")
        print()


if __name__ == "__main__":
    tid = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(main(tid))
