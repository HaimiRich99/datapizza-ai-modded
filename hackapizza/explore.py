"""
Script per esplorare lo stato del server e salvare i risultati come file JSON.
Esegui: python explore.py
I file vengono salvati nella cartella ./data/
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
API_KEY = os.getenv("API_KEY", "")

OUT_DIR = Path(__file__).parent / "explorer_data"


def save(filename: str, data: object) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  salvato -> {path}")


async def main() -> None:
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:

        print("ristorante...")
        save("restaurant.json", await client.get_restaurant())

        print("menu...")
        save("menu.json", await client.get_menu())

        print("ricette...")
        save("recipes.json", await client.get_recipes())

        print("ristoranti...")
        save("restaurants.json", await client.get_restaurants())

        print("mercato...")
        save("market.json", await client.get_market_entries())

    print("\ndone.")


if __name__ == "__main__":
    asyncio.run(main())
