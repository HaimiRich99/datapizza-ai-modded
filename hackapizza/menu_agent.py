"""
Agente Menu (Briatore Galattico) — Fase waiting

Logica con Datapizza AI:
1. L'Agente usa il tool `get_completable_recipes` per scoprire cosa si può cucinare con l'inventario attuale.
2. L'Agente ragiona sul pricing in base al prestigio (Astrobaroni vs Esploratori).
3. L'Agente chiama `set_menu_and_surplus` passando le sue scelte.
4. Il tool in automatico calcola il surplus, lo salva per il market_agent e pubblica il menu sul server.

Esegui standalone: python menu_agent.py [--dry-run]
"""

import asyncio
import json
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

# Importiamo Datapizza AI
from datapizza.agents import Agent
from datapizza.tools import tool
from datapizza.clients.openai_like import OpenAILikeClient

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")
REGOLO_API_KEY = os.getenv("REGOLO_API_KEY", "")

MAX_MENU_SIZE = 6
SURPLUS_PATH = Path(__file__).parent / "explorer_data" / "surplus_ingredients.json"

# Variabile globale per la modalità dry_run nei tool
_DRY_RUN = False


# ---------------------------------------------------------------------------
# Funzioni Helper Deterministiche (La matematica che l'LLM odia fare)
# ---------------------------------------------------------------------------

def find_completable_recipes(recipes: list[dict], inventory: dict[str, int]) -> list[dict]:
    """Filtra le ricette tenendo solo quelle per cui abbiamo tutti gli ingredienti."""
    completable = []
    for recipe in recipes:
        needed = recipe.get("ingredients", {})
        if all(inventory.get(ing, 0) >= qty for ing, qty in needed.items()):
            completable.append(recipe)
    return completable

def compute_surplus(menu_recipes: list[dict], inventory: dict[str, int]) -> dict[str, int]:
    """Calcola gli ingredienti in inventario che non verranno usati dal menu."""
    used: dict[str, int] = defaultdict(int)
    for recipe in menu_recipes:
        for ing, qty in recipe.get("ingredients", {}).items():
            used[ing] += qty

    surplus: dict[str, int] = {}
    for ing, qty_have in inventory.items():
        qty_used = used.get(ing, 0)
        leftover = qty_have - qty_used
        if leftover > 0:
            surplus[ing] = leftover
    return surplus


# ---------------------------------------------------------------------------
# Tool per l'Agente Datapizza AI
# ---------------------------------------------------------------------------

@tool
async def get_completable_recipes() -> str:
    """
    Recupera l'inventario attuale e le ricette, restituendo SOLO le ricette 
    che il ristorante può effettivamente cucinare in questo turno.
    Usa questa funzione prima di decidere il menu.
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        restaurant = await client.get_restaurant()
        inventory = restaurant.get("inventory", {})
        
        if not inventory:
            return "Inventario vuoto. Non possiamo cucinare nulla."
            
        recipes = await client.get_recipes()
        completable = find_completable_recipes(recipes, inventory)
        
        if not completable:
            return "Nessuna ricetta completabile con gli ingredienti in inventario."
            
        # Riduciamo il payload per non sprecare token
        options = []
        for r in completable:
            options.append({
                "name": r["name"],
                "prestige": r.get("prestige", 0),
                "ingredients": r.get("ingredients", {})
            })
            
        return json.dumps(options, ensure_ascii=False)


@tool
async def set_menu_and_surplus(items: list[dict]) -> str:
    """
    Salva il menu sul server della Federazione, calcola il surplus e lo passa al mercato.
    
    Args:
        items: Lista di dizionari con i piatti scelti e i prezzi decisi. 
               Massimo 6 piatti.
               Esempio: [{"name": "Pizza Quantistica", "price": 45.0}, ...]
    """
    if not items:
        return "Nessun piatto specificato. Menu non salvato."
        
    # Tronca a MAX_MENU_SIZE per sicurezza
    items = items[:MAX_MENU_SIZE]
    
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        # Recuperiamo i dati originali per fare i conti del surplus
        restaurant = await client.get_restaurant()
        inventory = restaurant.get("inventory", {})
        all_recipes = await client.get_recipes()
        
        # Mappiamo i nomi scelti dall'agente alle ricette complete
        chosen_recipes = []
        for item in items:
            recipe = next((r for r in all_recipes if r["name"] == item["name"]), None)
            if recipe:
                chosen_recipes.append(recipe)
                
        # Calcolo Surplus
        surplus = compute_surplus(chosen_recipes, inventory)
        
        # Salvataggio Surplus in locale
        SURPLUS_PATH.parent.mkdir(exist_ok=True)
        SURPLUS_PATH.write_text(json.dumps(surplus, indent=2, ensure_ascii=False), encoding="utf-8")
        
        print(f"\n[MENU AGENT] Menu generato dall'AI ({len(items)} piatti). Surplus salvato.")
        for item in items:
            print(f"  - {item['name']} | Prezzo: {item['price']}")
        
        # Pubblicazione
        if _DRY_RUN:
            return "[DRY-RUN] Menu e surplus calcolati, ma nessuna API chiamata sul server."
        else:
            try:
                result = await client.save_menu(items)
                return f"Menu salvato con successo sul server. Risposta: {result}"
            except Exception as exc:
                return f"Errore durante il salvataggio del menu: {exc}"


# ---------------------------------------------------------------------------
# Entry point & Configurazione Agente
# ---------------------------------------------------------------------------

async def run_menu_agent(dry_run: bool = False) -> None:
    """Innesca l'agente datapizza per la scelta del menu."""
    global _DRY_RUN
    _DRY_RUN = dry_run
    
    if not REGOLO_API_KEY:
        print("[MENU AGENT] REGOLO_API_KEY non trovata. Impossibile avviare l'agente.")
        return

    # 1. Configurazione del Client Regolo
    llm_client = OpenAILikeClient(
        api_key=REGOLO_API_KEY,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
    )

    # 2. Inizializzazione Agente
    menu_agent = Agent(
        name="Briatore_Galattico",
        client=llm_client,
        system_prompt=(
            "Sei il Pricing & Menu Manager di un ristorante spaziale nel Ciclo Cosmico 790. "
            "Il tuo unico obiettivo è il Saldo (profitto). "
            "Regole:"
            "1. Chiama get_completable_recipes() per sapere cosa PUOI cucinare adesso. "
            "2. Scegli al massimo 6 piatti tra quelli disponibili. Favorisci quelli con Prestige alto. "
            "3. Decidi i prezzi strategicamente: per piatti con prestige alto (>20) spara prezzi molto alti (es. 50-80 crediti) per attirare gli Astrobaroni. "
            "Per piatti poveri tieni prezzi bassi (es. 10-25 crediti) per gli Esploratori Galattici. "
            "4. Chiama set_menu_and_surplus(items) per confermare la tua decisione. "
            "Non aggiungere MAI piatti che non ti sono stati restituiti da get_completable_recipes."
        ),
        tools=[get_completable_recipes, set_menu_and_surplus], #type: ignore
        max_steps=5,
    )
    
    print("[MENU AGENT] L'Agente sta pensando al menu perfetto...")
    
    # 3. Esecuzione Asincrona dell'Agente
    result = await menu_agent.a_run(
        "Siamo nella Waiting Phase. Controlla cosa possiamo cucinare, scegli i piatti migliori, decidi i prezzi e salva il menu!"
    )  # type: ignore

    print("\n[MENU AGENT] Pensiero dell'agente completato.")
    if result:
        print(f"[MENU AGENT] Risultato finale: {result.text}")


if __name__ == "__main__":
    import sys
    dry = "--dry-run" in sys.argv
    if dry:
        print("[MENU] modalità DRY-RUN: nessuna chiamata di modifica al server (lettura consentita)\n")
    
    asyncio.run(run_menu_agent(dry_run=dry))