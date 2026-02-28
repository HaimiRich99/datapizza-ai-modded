"""
Snapshot dello stato della partita.

Usa: python snapshot.py [turn_id]

Salva tutto in explorer_data/snapshots/YYYY-MM-DD_HH-MM-SS/
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

SNAPSHOTS_DIR = Path(__file__).parent / "explorer_data" / "snapshots"
DELAY = 0.5  # secondi tra chiamate per evitare 429


def save(folder: Path, filename: str, data: object) -> None:
    path = folder / filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def fmt_inventory(inventory: dict) -> str:
    if not inventory:
        return "(vuoto)"
    return ", ".join(f"{k}:{v}" for k, v in inventory.items())


async def fetch(label: str, coro):
    """Esegue una coroutine con gestione errori e delay."""
    await asyncio.sleep(DELAY)
    try:
        return await coro
    except Exception:
        return None


async def main(turn_id: int | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = SNAPSHOTS_DIR / ts
    folder.mkdir(parents=True, exist_ok=True)

    # Valori di fallback per il summary finale
    name = balance = is_open = "?"
    inventory: dict = {}
    menu: list = []
    recipes: list = []
    market: list = []
    competitors: dict = {}

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:

        # --- Ristorante ---
        restaurant = await fetch("restaurant", client.get_restaurant())
        if restaurant is not None:
            save(folder, "restaurant.json", restaurant)
            balance = restaurant.get("balance", "?")
            inventory = restaurant.get("inventory", {})
            is_open = restaurant.get("is_open", "?")
            name = restaurant.get("name", "?")

        # --- Menu ---
        menu_result = await fetch("menu", client.get_menu())
        if menu_result is not None:
            menu = menu_result
            save(folder, "menu.json", menu)

        # --- Ricette ---
        recipes_result = await fetch("recipes", client.get_recipes())
        if recipes_result is not None:
            recipes = recipes_result
            save(folder, "recipes.json", recipes)

        # --- Tutti i ristoranti + loro menu ---
        restaurants_result = await fetch("restaurants", client.get_restaurants())
        if restaurants_result is not None:
            save(folder, "restaurants.json", restaurants_result)
            for r in restaurants_result:
                rid = r.get("id")
                rname = r.get("name", str(rid))
                if rid != TEAM_ID:
                    comp_menu = await fetch(f"menu[{rid}]", client.get_menu_by_id(rid))
                    competitors[rname] = comp_menu if comp_menu is not None else []
            save(folder, "competitors_menus.json", competitors)

        # --- Mercato ---
        market_result = await fetch("market", client.get_market_entries())
        if market_result is not None:
            market = market_result
            save(folder, "market.json", market)

        # --- Meals del turno ---
        if turn_id is not None:
            meals_result = await fetch("meals", client.get_meals(turn_id, TEAM_ID))
            if meals_result is not None:
                save(folder, f"meals_turn{turn_id}.json", meals_result)

        # --- Bid history ---
        if turn_id is not None:
            bids_result = await fetch("bid_history", client.get_bid_history(turn_id))
            if bids_result is not None:
                save(folder, f"bid_history_turn{turn_id}.json", bids_result)

    # --- Summary ---
    summary = {
        "timestamp": ts,
        "turn_id": turn_id,
        "restaurant": {"name": name, "balance": balance, "is_open": is_open, "inventory": inventory},
        "menu_count": len(menu),
        "recipes_count": len(recipes),
        "market_entries": len(market),
        "competitors": list(competitors.keys()),
    }
    save(folder, "summary.json", summary)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    turn = int(arg) if arg is not None else None
    asyncio.run(main(turn))
