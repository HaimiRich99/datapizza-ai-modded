# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "datapizza-ai",
#     "datapizza-ai-clients-openai-like",
#     "python-dotenv"
# ]
# ///

import asyncio
import os
import aiohttp
from dotenv import load_dotenv
from datapizza.clients.openai_like import OpenAILikeClient

load_dotenv()

TEAM_ID = os.getenv("TEAM_ID")
API_KEY = os.getenv("API_KEY")
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY")

BASE_URL = "https://hackapizza.datapizza.tech"

async def test_http_endpoints():
    if not API_KEY or not TEAM_ID:
        print("Missing API_KEY or TEAM_ID in .env file.")
        return

    headers = {"x-api-key": API_KEY}
    
    async with aiohttp.ClientSession() as session:
        # Test GET /restaurant/:id
        print(f"--- Fetching Restaurant {TEAM_ID} Info ---")
        async with session.get(f"{BASE_URL}/restaurant/{TEAM_ID}", headers=headers) as resp:
            if resp.status == 200:
                import json
                print(json.dumps(await resp.json(), indent=2))
            else:
                print(f"Error {resp.status}: {await resp.text()}")

        print("\n--- Fetching Recipes ---")
        async with session.get(f"{BASE_URL}/recipes", headers=headers) as resp:
            if resp.status == 200:
                recipes = await resp.json()
                print(f"Found {len(recipes)} recipes. First recipe:")
                if recipes:
                    import json
                    print(json.dumps(recipes[0], indent=2))
            else:
                print(f"Error {resp.status}: {await resp.text()}")

def test_regolo():
    if not REGOLO_API_KEY:
        print("\nMissing REGOLO_API_KEY in .env file.")
        return
        
    print("\n--- Testing Regolo AI Client ---")
    try:
        client = OpenAILikeClient(
            api_key=REGOLO_API_KEY,
            model="gpt-oss-120b",
            base_url="https://api.regolo.ai/v1",
        )
        print("Regolo AI client initialized successfully!")
    except Exception as e:
        print(f"Error initializing Regolo AI: {e}")

if __name__ == "__main__":
    print("Testing connection to game server...")
    asyncio.run(test_http_endpoints())
    test_regolo()
