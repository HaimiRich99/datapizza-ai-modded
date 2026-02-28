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
API_KEY = os.getenv("API_KEY", "")

SNAPSHOTS_DIR = Path(__file__).parent / "explorer_data" / "snapshots"
DELAY = 0.5  # secondi tra chiamate per evitare 429


def save(folder: Path, filename: str, data: object) -> None:
    path = folder / filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"    -> {path.name}")


def fmt_inventory(inventory: dict) -> str:
    if not inventory:
        return "(vuoto)"
    return ", ".join(f"{k}:{v}" for k, v in inventory.items())


async def fetch(label: str, coro):
    """Esegue una coroutine con gestione errori e delay."""
    await asyncio.sleep(DELAY)
    try:
        return await coro
    except Exception as exc:
        print(f"    ERRORE {label}: {exc}")
        return None


async def main(turn_id: int | None = None) -> None:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = SNAPSHOTS_DIR / ts
    folder.mkdir(parents=True, exist_ok=True)

    print(f"\n=== SNAPSHOT {ts} ===")

    # Valori di fallback per il summary finale
    name = balance = is_open = "?"
    inventory: dict = {}
    menu: list = []
    recipes: list = []
    market: list = []
    competitors: dict = {}

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:

        # --- Ristorante ---
        print("\n[1] ristorante...")
        restaurant = await fetch("restaurant", client.get_restaurant())
        if restaurant is not None:
            save(folder, "restaurant.json", restaurant)
            balance = restaurant.get("balance", "?")
            inventory = restaurant.get("inventory", {})
            is_open = restaurant.get("is_open", "?")
            name = restaurant.get("name", "?")
            print(f"    nome      : {name}")
            print(f"    saldo     : {balance}")
            print(f"    aperto    : {is_open}")
            print(f"    inventario: {fmt_inventory(inventory)}")

        # --- Menu ---
        print("\n[2] menu...")
        menu_result = await fetch("menu", client.get_menu())
        if menu_result is not None:
            menu = menu_result
            save(folder, "menu.json", menu)
            for item in menu:
                if isinstance(item, dict):
                    print(f"    - {item.get('name')} @ {item.get('price')}")
                else:
                    print(f"    - {item}")
            if not menu:
                print("    (menu vuoto)")

        # --- Ricette ---
        print("\n[3] ricette...")
        recipes_result = await fetch("recipes", client.get_recipes())
        if recipes_result is not None:
            recipes = recipes_result
            save(folder, "recipes.json", recipes)
            print(f"    totale: {len(recipes)} ricette")

        # --- Tutti i ristoranti + loro menu ---
        print("\n[4] ristoranti avversari...")
        restaurants_result = await fetch("restaurants", client.get_restaurants())
        if restaurants_result is not None:
            save(folder, "restaurants.json", restaurants_result)
            for r in restaurants_result:
                rid = r.get("id")
                rname = r.get("name", str(rid))
                bal = r.get("balance", "?")
                is_op = r.get("is_open", "?")
                marker = " <-- NOI" if rid == TEAM_ID else ""
                print(f"    [{rid}] {rname} | saldo={bal} | aperto={is_op}{marker}")
                if rid != TEAM_ID:
                    comp_menu = await fetch(f"menu[{rid}]", client.get_menu_by_id(rid))
                    competitors[rname] = comp_menu if comp_menu is not None else []
            save(folder, "competitors_menus.json", competitors)

        # --- Mercato ---
        print("\n[5] mercato...")
        market_result = await fetch("market", client.get_market_entries())
        if market_result is not None:
            market = market_result
            save(folder, "market.json", market)
            buys = [e for e in market if e.get("side") == "BUY"]
            sells = [e for e in market if e.get("side") == "SELL"]
            print(f"    entry totali: {len(market)} (BUY={len(buys)} SELL={len(sells)})")
            for e in market[:10]:
                side = e.get("side", "?")
                ing = e.get("ingredient_name", "?")
                qty = e.get("quantity", "?")
                price = e.get("price", "?")
                owner = e.get("restaurant_name", "?")
                print(f"    {side:4} {ing} x{qty} @ {price} [{owner}]")
            if len(market) > 10:
                print(f"    ... e altri {len(market)-10}")
        else:
            print("    (non disponibile)")

        # --- Meals del turno ---
        print(f"\n[6] meals turno {turn_id}...")
        if turn_id is not None:
            meals_result = await fetch("meals", client.get_meals(turn_id))
            if meals_result is not None:
                save(folder, f"meals_turn{turn_id}.json", meals_result)
                print(f"    clienti: {len(meals_result)}")
                for m in meals_result:
                    cname = m.get("clientName", m.get("client_name", "?"))
                    order = m.get("orderText", m.get("order_text", "?"))
                    served = m.get("served", "?")
                    print(f"    - {cname}: {order!r} | servito={served}")
        else:
            print("    (passa turn_id come argomento per vedere i clienti)")

        # --- Bid history ---
        print(f"\n[7] bid history turno {turn_id}...")
        if turn_id is not None:
            bids_result = await fetch("bid_history", client.get_bid_history(turn_id))
            if bids_result is not None:
                save(folder, f"bid_history_turn{turn_id}.json", bids_result)
                print(f"    offerte totali: {len(bids_result)}")
                for b in bids_result[:15]:
                    team = b.get("restaurant_name", "?")
                    ing = b.get("ingredient", "?")
                    qty = b.get("quantity", "?")
                    bid_val = b.get("bid", "?")
                    won = b.get("won", "?")
                    print(f"    [{team}] {ing} x{qty} @ {bid_val} | vinto={won}")
                if len(bids_result) > 15:
                    print(f"    ... e altri {len(bids_result)-15}")
        else:
            print("    (passa turn_id come argomento per vedere le offerte)")

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
    print(f"\n=== dump salvato in: {folder} ===\n")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    turn = int(arg) if arg is not None else None
    asyncio.run(main(turn))
