"""
Agente Strategia — Focus su 1-2 ricette da ripetere più volte.

Logica:
  1. L'agente chiama get_situation() per vedere budget, inventario e le 10 ricette più semplici
  2. Sceglie la ricetta focus (alta prestige, pochi ingredienti) e un backup opzionale
  3. Chiama save_strategy() che calcola ingredient_quantities e salva strategy.json
  4. Output: (list[str], int) — ingredienti target e quanti sono "primari"

Esegui standalone: python strategy_agent.py [--bid]
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from datapizza.agents import Agent
from datapizza.tools import tool
from datapizza.clients.openai_like import OpenAILikeClient

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

N_COPIES_TARGET = 1  # 1 copia della focus; le extra ricette vengono aggiunte automaticamente

_EXPLORER_DATA = Path(__file__).parent / "explorer_data"
_STRATEGY_PATH = _EXPLORER_DATA / "strategy.json"
_RECOMMENDATIONS_PATH = _EXPLORER_DATA / "bid_recommendations.json"
_INSIGHTS_PATH = _EXPLORER_DATA / "turn_insights.json"
_HISTORY_PATH = _EXPLORER_DATA / "turn_history.json"

# State condiviso tra i tool (popolato da get_situation, letto da save_strategy)
_state: dict = {}


# ---------------------------------------------------------------------------
# Pydantic model (usato solo dal fallback algoritmico)
# ---------------------------------------------------------------------------

class FocusStrategyPlan(BaseModel):
    focus_recipe_name: str = Field(description="Nome ESATTO della ricetta principale")
    backup_recipe_name: Optional[str] = Field(None, description="Nome ESATTO ricetta secondaria (None se non serve)")
    copies_target: int = Field(description="Numero di copie della ricetta focus (1 o 2; il sistema aggiunge automaticamente 1 copia delle altre ricette per la varietà)")
    reasoning: str = Field(description="Spiegazione della scelta in 1-2 righe")


# ---------------------------------------------------------------------------
# Helper puri (nessuna dipendenza LLM)
# ---------------------------------------------------------------------------

def _get_simplest_recipes(recipes: list[dict], n: int = 10) -> list[dict]:
    """Seleziona le n ricette con il minor numero assoluto di ingredienti e quantità totali."""
    def sort_key(r):
        ings = r.get("ingredients", {})
        return (len(ings), sum(ings.values()), r.get("preparationTimeMs", 0), -r.get("prestige", 0))
    return sorted(recipes, key=sort_key)[:n]


def _format_simplest_recipes(recipes: list[dict]) -> str:
    lines = []
    for i, r in enumerate(recipes, 1):
        lines.append(f"Ricetta {i}: {r['name']}")
        lines.append(f"  - Prestige: {r.get('prestige', 0)}")
        lines.append(f"  - Tempo di preparazione: {r.get('preparationTimeMs', 0)} ms")
        lines.append("  - Ingredienti necessari:")
        for ing, qty in r.get("ingredients", {}).items():
            lines.append(f"      * {ing}: {qty}")
        lines.append("")
    return "\n".join(lines).strip()


def _best_recipe_by_score(recipes: list[dict], inventory: dict[str, int], exclude: set[str] | None = None) -> dict:
    """Sceglie la ricetta con score = prestige / n_ingredienti_mancanti più alto."""
    exclude = exclude or set()
    best, best_score = None, -1.0
    for r in recipes:
        if r["name"] in exclude:
            continue
        missing = [ing for ing, qty in r.get("ingredients", {}).items() if inventory.get(ing, 0) < qty]
        score = r.get("prestige", 0) / max(1, len(missing))
        if score > best_score:
            best_score, best = score, r
    return best  # type: ignore


def _overlap_score(recipe_a: dict, recipe_b: dict) -> float:
    """Frazione di ingredienti condivisi tra due ricette."""
    ings_a = set(recipe_a.get("ingredients", {}).keys())
    ings_b = set(recipe_b.get("ingredients", {}).keys())
    if not ings_a or not ings_b:
        return 0.0
    return len(ings_a & ings_b) / len(ings_a | ings_b)


def _fallback_plan(recipes: list[dict], inventory: dict[str, int]) -> FocusStrategyPlan:
    focus = _best_recipe_by_score(recipes, inventory)
    backup, best_backup_score = None, -1.0
    for r in recipes:
        if r["name"] == focus["name"]:
            continue
        combined = _overlap_score(focus, r) * 0.6 + r.get("prestige", 0) / 100 * 0.4
        if combined > best_backup_score:
            best_backup_score, backup = combined, r
    return FocusStrategyPlan(
        focus_recipe_name=focus["name"],
        backup_recipe_name=backup["name"] if backup and best_backup_score > 0.2 else None,
        copies_target=N_COPIES_TARGET,
        reasoning="Selezione algoritmica: massimo prestige/ingredienti_mancanti",
    )


def build_ingredient_quantities(
    focus_recipe: dict,
    backup_recipe: Optional[dict],
    inventory: dict[str, int],
    copies_target: int,
    extra_recipes: list[dict] | None = None,
) -> tuple[dict[str, int], list[str], int]:
    """
    Calcola le quantità di ingredienti da comprare.
    - focus: copies_target copie (di solito 1-2)
    - backup: copies_target-1 copie (se fornita)
    - extra_recipes: 1 copia ciascuna per diversificare il menu
    Ritorna: (ingredient_quantities, target_ings, primary_count)
    """
    quantities: dict[str, int] = {}
    focus_ings_ordered: list[str] = []

    for ing, qty_per_copy in focus_recipe.get("ingredients", {}).items():
        to_buy = max(0, qty_per_copy * copies_target - inventory.get(ing, 0))
        if to_buy > 0:
            quantities[ing] = to_buy
            focus_ings_ordered.append(ing)

    primary_count = len(focus_ings_ordered)
    backup_ings_ordered: list[str] = []

    if backup_recipe:
        backup_copies = max(1, copies_target - 1)
        for ing, qty_per_copy in backup_recipe.get("ingredients", {}).items():
            already_getting = quantities.get(ing, 0)
            to_buy = max(0, qty_per_copy * backup_copies - inventory.get(ing, 0) - already_getting)
            if to_buy > 0:
                quantities[ing] = quantities.get(ing, 0) + to_buy
                if ing not in focus_ings_ordered:
                    backup_ings_ordered.append(ing)

    # Ricette extra: 1 copia ciascuna per diversificare le offerte del menu
    covered = {focus_recipe.get("name"), backup_recipe.get("name") if backup_recipe else None}
    if extra_recipes:
        for recipe in extra_recipes:
            if recipe.get("name") in covered:
                continue
            for ing, qty_per_copy in recipe.get("ingredients", {}).items():
                have = inventory.get(ing, 0)
                already_getting = quantities.get(ing, 0)
                to_buy = max(0, qty_per_copy - have - already_getting)
                if to_buy > 0:
                    quantities[ing] = already_getting + to_buy

    return quantities, focus_ings_ordered + backup_ings_ordered, primary_count


# ---------------------------------------------------------------------------
# Core della save strategy (richiamabile sia dal tool che dal fallback)
# ---------------------------------------------------------------------------

async def _do_save_strategy(
    focus_recipe_name: str,
    copies_target: int,
    reasoning: str,
    backup_recipe_name: Optional[str] = None,
) -> str:
    recipes: list[dict] = _state.get("recipes", [])
    inventory: dict[str, int] = _state.get("inventory", {})
    recipe_map: dict[str, dict] = _state.get("recipe_map", {})

    if not recipe_map:
        return "Errore: stato non inizializzato. Chiama prima get_situation()."

    # Valida focus (case-insensitive fallback)
    if focus_recipe_name not in recipe_map:
        match = next((n for n in recipe_map if n.lower() == focus_recipe_name.lower()), None)
        focus_recipe_name = match or _fallback_plan(recipes, inventory).focus_recipe_name

    # Valida backup
    if backup_recipe_name and backup_recipe_name not in recipe_map:
        match = next((n for n in recipe_map if n.lower() == backup_recipe_name.lower()), None)
        backup_recipe_name = match

    focus_recipe = recipe_map[focus_recipe_name]
    backup_recipe = recipe_map.get(backup_recipe_name) if backup_recipe_name else None
    copies_target = max(1, min(copies_target, 2))  # cap a 2: varietà > volume

    print(f"\n[STRATEGY] FOCUS: {focus_recipe_name!r} | prestige={focus_recipe.get('prestige')} | copie={copies_target}")
    if backup_recipe:
        print(f"[STRATEGY] BACKUP: {backup_recipe_name!r} | prestige={backup_recipe.get('prestige')}")

    # Include le 10 ricette più semplici come extra (1 copia ciascuna per varietà menu)
    simplest = _get_simplest_recipes(recipes, n=10)
    print(f"[STRATEGY] Ricette extra per varietà: {len(simplest)}")

    ingredient_quantities, target_ings, primary_count = build_ingredient_quantities(
        focus_recipe, backup_recipe, inventory, copies_target, extra_recipes=simplest
    )

    # Leggi price hints dall'auction analyst
    price_hints: dict[str, int] = {}
    avoid_ings: set[str] = set()
    if _RECOMMENDATIONS_PATH.exists():
        try:
            rec_data = json.loads(_RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
            if "price_hints" in rec_data:
                price_hints = rec_data["price_hints"]
            else:
                method = rec_data.get("method", "")
                if method == "llm":
                    for r in rec_data.get("recommendations", []):
                        if r.get("ingredient") and r.get("recommended_bid"):
                            price_hints[r["ingredient"]] = r["recommended_bid"]
                    avoid_ings = set(rec_data.get("avoid_ingredients", []))
                else:
                    for ing, data in rec_data.get("recommendations", {}).items():
                        if data.get("recommended_bid"):
                            price_hints[ing] = data["recommended_bid"]
                    avoid_ings = set(rec_data.get("avoid_ingredients", []))
        except Exception:
            pass

    out = {
        "method": "llm",
        "focus_recipes": [focus_recipe_name] + ([backup_recipe_name] if backup_recipe_name else []),
        "copies_target": copies_target,
        "primary_recipe": focus_recipe_name,
        "primary_count": primary_count,
        "target_ingredients": target_ings,
        "ingredient_quantities": ingredient_quantities,
        "price_hints": price_hints,
        "avoid_ingredients": list(avoid_ings),
        "simplest_recipes": _get_simplest_recipes(recipes, n=10),
        "reasoning": reasoning,
    }

    _STRATEGY_PATH.parent.mkdir(exist_ok=True)
    _STRATEGY_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    _state["target_ings"] = target_ings
    _state["primary_count"] = primary_count

    return (
        f"Strategia salvata! Focus: {focus_recipe_name!r} ({copies_target} cop.) + "
        f"{len(simplest)} ricette extra × 1 copia. "
        f"Ingredienti totali da acquistare: {len(ingredient_quantities)}. "
        f"Motivazione: {reasoning}"
    )


# ---------------------------------------------------------------------------
# Tool del framework datapizza
# ---------------------------------------------------------------------------

@tool
async def get_situation() -> str:
    """
    Recupera ricette, inventario e saldo corrente dal server.
    Restituisce le 10 ricette più semplici come shortlist strategica.
    Chiama questo tool PRIMA di save_strategy.
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        recipes = await client.get_recipes()
        restaurant = await client.get_restaurant()

    inventory: dict[str, int] = restaurant.get("inventory", {})
    balance = float(restaurant.get("balance", 0))

    _state["recipes"] = recipes
    _state["inventory"] = inventory
    _state["recipe_map"] = {r["name"]: r for r in recipes}

    simplest = _get_simplest_recipes(recipes, n=10)
    formatted = _format_simplest_recipes(simplest)
    inv_str = json.dumps(inventory, ensure_ascii=False) if inventory else "(vuoto)"

    # Leggi storico turni (balance, clienti, ricetta focus)
    history_str = ""
    max_clients_seen = 0
    if _HISTORY_PATH.exists():
        try:
            history = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            recent = history[-5:]
            if recent:
                lines = []
                for e in recent:
                    delta = f"{e['balance_delta']:+.0f}" if e.get("balance_delta") is not None else "?"
                    rank = e.get("rank")
                    n_comp = e.get("competitors_active", 0)
                    rank_str = f", rank={rank}/{n_comp + 1}" if rank is not None else ""
                    # supporta sia il vecchio campo dishes_served che i nuovi clients_*
                    ct = e.get("clients_total", e.get("dishes_served", "?"))
                    cs = e.get("clients_served", "?")
                    if isinstance(ct, int):
                        max_clients_seen = max(max_clients_seen, ct)
                    lines.append(
                        f"  t{e['turn_id']}: clienti={cs}/{ct} (serviti/arrivati), "
                        f"balance={e['balance_end']} (Δ{delta}){rank_str}, focus={e['focus_recipe']}"
                    )
                demand_hint = (
                    f"\n  → Max clienti osservati negli ultimi turni: {max_clients_seen}. "
                    f"Scegli copies_target = {max_clients_seen + 2} (max_osservato + buffer 2)."
                    if max_clients_seen > 0 else ""
                )
                history_str = "\n\nSTORICO ULTIMI TURNI (usa per calibrare copies_target):\n" + "\n".join(lines) + demand_hint
        except Exception:
            pass

    # Leggi gli ultimi insight dai turni precedenti (score avversari, chat, ecc.)
    insights_str = ""
    if _INSIGHTS_PATH.exists():
        try:
            all_insights = json.loads(_INSIGHTS_PATH.read_text(encoding="utf-8"))
            recent = all_insights[-10:]  # ultimi 10
            if recent:
                lines = []
                for ins in recent:
                    if ins["type"] == "server_insight":
                        lines.append(f"  [server t{ins.get('turn_id',0)}] {ins['payload'][:200]}")
                if lines:
                    insights_str = "\n\nINSIGHT TURNI PRECEDENTI (usa per calibrare la strategia):\n" + "\n".join(lines)
        except Exception:
            pass

    return (
        f"BUDGET ATTUALE: {balance} Crediti Galattici\n"
        f"INVENTARIO ATTUALE: {inv_str}\n\n"
        f"LE 10 RICETTE PIÙ SEMPLICI (SHORTLIST D'ASSALTO):\n{formatted}"
        f"{history_str}"
        f"{insights_str}"
    )


@tool
async def save_strategy(
    focus_recipe_name: str,
    copies_target: int,
    reasoning: str,
    backup_recipe_name: Optional[str] = None,
) -> str:
    """
    Salva il piano strategico scelto. Calcola gli ingredienti necessari e
    scrive strategy.json per gli agenti successivi.

    Args:
        focus_recipe_name: Nome ESATTO della ricetta principale su cui puntare (alta prestige, pochi ingredienti).
        copies_target: Numero di copie della ricetta focus (1 o 2; il sistema aggiunge 1 copia delle altre ricette per la varietà).
        reasoning: Spiegazione della scelta in 1-2 righe.
        backup_recipe_name: Nome ESATTO di una ricetta secondaria opzionale (None se non serve).
    """
    return await _do_save_strategy(focus_recipe_name, copies_target, reasoning, backup_recipe_name)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """Sei l'Executive Chef e Procurement Manager Supremo del nostro ristorante nel Multiverso Gastronomico.
Il tuo obiettivo è dominare il mercato massimizzando il volume di piatti serviti, applicando la "Strategia della Semplicità Spietata".

CICLO OPERATIVO OBBLIGATORIO (due passi, niente di più):

1. Chiama get_situation() per conoscere budget, inventario e le 10 ricette più semplici disponibili.

2. Analizza la shortlist e scegli:
   - focus_recipe_name: la ricetta su cui puntare (priorità: alto prestige, pochi ingredienti diversi, bassa quantità totale).
   - copies_target: quante copie della ricetta FOCUS produrre. Usa 1 (massimo 2).
       Il sistema acquisterà automaticamente 1 copia delle altre ricette semplici per diversificare.
       Il collo di bottiglia è la domanda clienti (~3-7/turno), non il budget.
   - backup_recipe_name (opzionale): una ricetta secondaria con ingredienti sovrapposti alla focus.
   Poi chiama save_strategy() con il piano definitivo.

CRITERI DI SCELTA:
- Favorisci ricette con meno ingredienti diversi e quantità totali basse (più facile da reperire).
- A parità d'ingredienti, preferisci quella con prestige più alto.
- Il backup è opzionale: usalo solo se condivide molti ingredienti con la focus (riduce il rischio di spreco).
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_strategy_agent() -> tuple[list[str], int]:
    """
    Ritorna (list[str], int): ingredienti target e quanti sono "primari".
    """
    regolo_key = os.getenv("REGOLO_API_KEY")

    if not regolo_key:
        # Fallback algoritmico (nessun LLM disponibile)
        async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
            recipes = await client.get_recipes()
            restaurant = await client.get_restaurant()

        inventory: dict[str, int] = restaurant.get("inventory", {})
        _state.update({
            "recipes": recipes,
            "inventory": inventory,
            "recipe_map": {r["name"]: r for r in recipes},
        })

        fallback = _fallback_plan(recipes, inventory)
        await _do_save_strategy(
            focus_recipe_name=fallback.focus_recipe_name,
            copies_target=fallback.copies_target,
            reasoning=fallback.reasoning,
            backup_recipe_name=fallback.backup_recipe_name,
        )
        return _state.get("target_ings", []), _state.get("primary_count", 0)

    llm_client = OpenAILikeClient(
        api_key=regolo_key,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
    )

    strategy_agent = Agent(
        name="Strategy_Chef_Supremo",
        client=llm_client,
        system_prompt=_SYSTEM_PROMPT,
        tools=[get_situation, save_strategy],
        max_steps=5,
    )

    await strategy_agent.a_run(
        "Avvia la fase di strategia: analizza la situazione e scegli la migliore ricetta focus. "
        "Prima chiama get_situation(), poi chiama save_strategy() con il piano definitivo."
    )

    target_ings: list[str] = _state.get("target_ings", [])
    primary_count: int = _state.get("primary_count", 0)

    # Fallback di lettura se il tool non ha salvato nulla in _state
    if not target_ings and _STRATEGY_PATH.exists():
        try:
            data = json.loads(_STRATEGY_PATH.read_text(encoding="utf-8"))
            target_ings = data.get("target_ingredients", [])
            primary_count = data.get("primary_count", 0)
        except Exception:
            pass

    return target_ings, primary_count


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    async def main() -> None:
        target, primary_count = await run_strategy_agent()
        if "--bid" in sys.argv:
            from bid_agent import run_bid_agent
            await run_bid_agent(preferred_ingredients=target, primary_count=primary_count)

    asyncio.run(main())
