"""
Script per esplorare lo stato del server manualmente.
Esegui: python explore.py
"""

import asyncio
import json
import os

from dotenv import load_dotenv

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("API_KEY", "")


def pretty(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


async def main() -> None:
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:

        print("\n" + "=" * 60)
        print("RISTORANTE")
        print("=" * 60)
        pretty(await client.get_restaurant())

        print("\n" + "=" * 60)
        print("MENU ATTUALE")
        print("=" * 60)
        pretty(await client.get_menu())

        print("\n" + "=" * 60)
        print("RICETTE DISPONIBILI")
        print("=" * 60)
        pretty(await client.get_recipes())

        print("\n" + "=" * 60)
        print("TUTTI I RISTORANTI")
        print("=" * 60)
        pretty(await client.get_restaurants())

        print("\n" + "=" * 60)
        print("MERCATO")
        print("=" * 60)
        pretty(await client.get_market_entries())


if __name__ == "__main__":
    asyncio.run(main())
