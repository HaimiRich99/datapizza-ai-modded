"""
Agente Strategia — Focus su 1-2 ricette da ripetere più volte.

Logica:
  1. Scegli la ricetta con il miglior rapporto prestige/n_ingredienti (LLM o fallback)
  2. Punta a produrre N_COPIES_TARGET copie di quella ricetta
  3. Aggiungi opzionalmente una ricetta di backup (stessi ingredienti il più possibile)
  4. Output: ingredient_quantities {ing: qty_necessaria_totale} per bid_agent

Return type: tuple[list[str], int]
  - list[str]: ingredienti target (focus prima, backup dopo)
  - int: quanti dei primi N sono "primari" (focus recipe)

Esegui standalone: python strategy_agent.py [--bid]
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

N_COPIES_TARGET = 3  # quante copie della ricetta focus vogliamo preparare

# ---------------------------------------------------------------------------
# Setup datapizza framework paths
# ---------------------------------------------------------------------------

_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "datapizza-ai-core"))
sys.path.insert(0, str(_repo_root / "datapizza-ai-clients" / "datapizza-ai-clients-openai-like"))

try:
    from datapizza.clients.openai_like import OpenAILikeClient
    _LLM_AVAILABLE = True
except ImportError:
    _LLM_AVAILABLE = False
    print("[STRATEGY] WARN: datapizza framework non trovato — solo fallback algoritmico")

# ---------------------------------------------------------------------------
# Pydantic models per LLM
# ---------------------------------------------------------------------------


class FocusStrategyPlan(BaseModel):
    focus_recipe_name: str = Field(
        description="Nome ESATTO della ricetta principale da ripetere più volte (alta prestige, pochi ingredienti unici)"
    )
    backup_recipe_name: Optional[str] = Field(
        None,
        description="Nome ESATTO di una ricetta secondaria opzionale (preferibilmente con ingredienti sovrapposti alla focus). None se non serve."
    )
    copies_target: int = Field(
        description="Numero di copie da preparare per la ricetta focus (tipicamente 2-4, in base al budget stimato)"
    )
    reasoning: str = Field(
        description="Spiegazione della scelta in 1-2 righe"
    )


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Sei un agente strategico per un gioco di ristorante galattico con aste chiuse.

Ricevi l'inventario attuale e la lista delle ricette (con ingredienti e prestige).

OBIETTIVO: identificare 1 ricetta da produrre più volte (massimizzare le copie vendute),
più opzionalmente 1 ricetta di backup con ingredienti sovrapposti.

Criteri di selezione della ricetta focus:
- Alta prestige (più punti per serving)
- Pochi ingredienti unici da comprare (meno costi di approvvigionamento)
- Rapporto prestige/n_ingredienti_mancanti il più alto possibile
- Ingredienti già parzialmente in inventario = bonus

Criteri del backup:
- Ingredienti il più sovrapposti possibile con la focus (condivisione ingredienti = efficienza)
- Buona prestige

copies_target: stima quante copie della focus possiamo permetterci.
Se il saldo stimato è alto (>2000) → 3-4 copie. Se basso (<1000) → 2 copie.
"""


def _compact_recipes(recipes: list[dict], inventory: dict[str, int]) -> list[dict]:
    out = []
    for r in recipes:
        ings = r.get("ingredients", {})
        missing = [ing for ing, qty in ings.items() if inventory.get(ing, 0) < qty]
        out.append({
            "name": r["name"],
            "prestige": r.get("prestige", 0),
            "ingredients": list(ings.keys()),
            "missing": missing,
            "score": round(r.get("prestige", 0) / max(1, len(missing)), 1),
        })
    # Ordina per score decrescente per aiutare l'LLM
    out.sort(key=lambda x: -x["score"])
    return out


def _call_llm_sync(
    recipes: list[dict],
    inventory: dict[str, int],
    balance: float,
) -> Optional[FocusStrategyPlan]:
    regolo_key = os.getenv("REGOLO_API_KEY")
    if not regolo_key:
        return None

    client = OpenAILikeClient(
        api_key=regolo_key,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
        system_prompt=_SYSTEM_PROMPT,
    )

    compact = _compact_recipes(recipes, inventory)
    prompt = (
        f"Saldo stimato: {balance}\n"
        f"Inventario attuale: {json.dumps(inventory, ensure_ascii=False) if inventory else '(vuoto)'}\n\n"
        f"Ricette disponibili (ordinate per score prestige/ingredienti_mancanti):\n"
        f"{json.dumps(compact[:40], ensure_ascii=False, indent=2)}"
    )

    print("[STRATEGY] LLM in elaborazione...")
    response = client.structured_response(input=prompt, output_cls=FocusStrategyPlan)

    raw = response.structured_data
    if isinstance(raw, FocusStrategyPlan):
        return raw
    if isinstance(raw, list) and raw:
        item = raw[0]
        if isinstance(item, FocusStrategyPlan):
            return item
        if isinstance(item, dict):
            return FocusStrategyPlan(**item)
    if isinstance(raw, dict):
        return FocusStrategyPlan(**raw)
    return None


# ---------------------------------------------------------------------------
# Fallback algoritmico
# ---------------------------------------------------------------------------

def _best_recipe_by_score(
    recipes: list[dict],
    inventory: dict[str, int],
    exclude: set[str] | None = None,
) -> dict:
    """Sceglie la ricetta con score = prestige / n_ingredienti_mancanti più alto."""
    exclude = exclude or set()
    best = None
    best_score = -1.0

    for r in recipes:
        if r["name"] in exclude:
            continue
        ings = r.get("ingredients", {})
        missing = [ing for ing, qty in ings.items() if inventory.get(ing, 0) < qty]
        score = r.get("prestige", 0) / max(1, len(missing))
        if score > best_score:
            best_score = score
            best = r

    return best  # type: ignore


def _overlap_score(recipe_a: dict, recipe_b: dict) -> float:
    """Frazione di ingredienti condivisi tra due ricette."""
    ings_a = set(recipe_a.get("ingredients", {}).keys())
    ings_b = set(recipe_b.get("ingredients", {}).keys())
    if not ings_a or not ings_b:
        return 0.0
    return len(ings_a & ings_b) / len(ings_a | ings_b)


def _fallback_plan(
    recipes: list[dict],
    inventory: dict[str, int],
) -> FocusStrategyPlan:
    focus = _best_recipe_by_score(recipes, inventory)
    print(f"[STRATEGY] focus algoritmico: {focus['name']!r} | prestige={focus.get('prestige')}")

    # Backup: massima sovrapposizione con focus e buona prestige
    focus_ings = set(focus.get("ingredients", {}).keys())
    backup = None
    best_backup_score = -1.0

    for r in recipes:
        if r["name"] == focus["name"]:
            continue
        overlap = _overlap_score(focus, r)
        combined = overlap * 0.6 + r.get("prestige", 0) / 100 * 0.4
        if combined > best_backup_score:
            best_backup_score = combined
            backup = r

    backup_name = backup["name"] if backup and best_backup_score > 0.2 else None
    if backup_name:
        print(f"[STRATEGY] backup algoritmico: {backup_name!r}")

    return FocusStrategyPlan(
        focus_recipe_name=focus["name"],
        backup_recipe_name=backup_name,
        copies_target=N_COPIES_TARGET,
        reasoning="Selezione algoritmica per massimo prestige/ingredienti_mancanti",
    )


# ---------------------------------------------------------------------------
# Costruzione ingredient_quantities
# ---------------------------------------------------------------------------

def build_ingredient_quantities(
    focus_recipe: dict,
    backup_recipe: Optional[dict],
    inventory: dict[str, int],
    copies_target: int,
) -> tuple[dict[str, int], list[str], int]:
    """
    Calcola le quantità di ingredienti da comprare.
    Ritorna:
      - ingredient_quantities: {ing: qty_totale_da_comprare}
      - target_ings: lista ordinata (focus prima, backup dopo)
      - primary_count: n ingredienti focus
    """
    quantities: dict[str, int] = {}

    # Focus: copies_target copie, meno quello che già abbiamo
    focus_ings_ordered: list[str] = []
    for ing, qty_per_copy in focus_recipe.get("ingredients", {}).items():
        total_needed = qty_per_copy * copies_target
        already_have = inventory.get(ing, 0)
        to_buy = max(0, total_needed - already_have)
        if to_buy > 0:
            quantities[ing] = to_buy
            focus_ings_ordered.append(ing)

    primary_count = len(focus_ings_ordered)

    # Backup: 1-2 copie, ingredienti non già coperti dalla focus
    backup_ings_ordered: list[str] = []
    if backup_recipe:
        backup_copies = max(1, copies_target - 1)
        for ing, qty_per_copy in backup_recipe.get("ingredients", {}).items():
            total_needed = qty_per_copy * backup_copies
            already_have = inventory.get(ing, 0)
            # Considera anche quello che acquistiamo per la focus
            already_getting = quantities.get(ing, 0)
            to_buy = max(0, total_needed - already_have - already_getting)
            if to_buy > 0:
                quantities[ing] = quantities.get(ing, 0) + to_buy
                if ing not in focus_ings_ordered:
                    backup_ings_ordered.append(ing)

    target_ings = focus_ings_ordered + backup_ings_ordered
    return quantities, target_ings, primary_count


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_strategy_agent() -> tuple[list[str], int]:
    """
    Ritorna (list[str], int): ingredienti target e quanti sono "primari".
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        recipes = await client.get_recipes()
        restaurant = await client.get_restaurant()

    inventory: dict[str, int] = restaurant.get("inventory", {})
    balance = float(restaurant.get("balance", 0))
    print(f"[STRATEGY] ricette: {len(recipes)} | saldo: {balance:.0f} | "
          f"inventario: {len(inventory)} ingredienti")

    recipe_map: dict[str, dict] = {r["name"]: r for r in recipes}
    all_ings: set[str] = {ing for r in recipes for ing in r.get("ingredients", {}).keys()}

    # --- LLM o fallback ---
    plan: Optional[FocusStrategyPlan] = None

    if _LLM_AVAILABLE:
        try:
            plan = await asyncio.to_thread(_call_llm_sync, recipes, inventory, balance)
        except Exception as exc:
            print(f"[STRATEGY] LLM errore: {exc} — uso fallback")

    if plan is None:
        plan = _fallback_plan(recipes, inventory)

    # Valida nomi ricette
    focus_name = plan.focus_recipe_name
    if focus_name not in recipe_map:
        # Cerca case-insensitive
        match = next((n for n in recipe_map if n.lower() == focus_name.lower()), None)
        if match:
            focus_name = match
        else:
            print(f"[STRATEGY] WARN ricetta focus non trovata: {focus_name!r} — uso fallback")
            focus_name = _fallback_plan(recipes, inventory).focus_recipe_name

    backup_name = plan.backup_recipe_name
    if backup_name and backup_name not in recipe_map:
        match = next((n for n in recipe_map if n.lower() == backup_name.lower()), None)
        backup_name = match  # None se non trovato

    focus_recipe = recipe_map[focus_name]
    backup_recipe = recipe_map.get(backup_name) if backup_name else None
    copies_target = max(1, plan.copies_target)

    print(f"\n[STRATEGY] FOCUS: {focus_name!r} | prestige={focus_recipe.get('prestige')} | copie={copies_target}")
    if backup_recipe:
        print(f"[STRATEGY] BACKUP: {backup_name!r} | prestige={backup_recipe.get('prestige')}")
    print(f"[STRATEGY] reasoning: {plan.reasoning}")

    # --- Calcola ingredienti e quantità ---
    ingredient_quantities, target_ings, primary_count = build_ingredient_quantities(
        focus_recipe, backup_recipe, inventory, copies_target
    )

    print(f"\n[STRATEGY] ingredienti da comprare ({len(target_ings)}) | primari focus: {primary_count}:")
    for i, ing in enumerate(target_ings, 1):
        qty = ingredient_quantities.get(ing, 0)
        have = inventory.get(ing, 0)
        tag = "FOCUS" if i <= primary_count else "BACK"
        print(f"  {i:2}. [{tag}] {ing}: vuoi={qty + have} | hai={have} | compra={qty}")

    # --- Leggi raccomandazioni prezzi dall'auction analyst ---
    _recommendations_path = Path(__file__).parent / "explorer_data" / "bid_recommendations.json"
    price_hints: dict[str, int] = {}
    avoid_ings: set[str] = set()
    if _recommendations_path.exists():
        try:
            rec_data = json.loads(_recommendations_path.read_text(encoding="utf-8"))
            method = rec_data.get("method", "")
            if method == "llm":
                for r in rec_data.get("recommendations", []):
                    ing = r.get("ingredient")
                    bid = r.get("recommended_bid")
                    if ing and bid:
                        price_hints[ing] = bid
                avoid_ings = set(rec_data.get("avoid_ingredients", []))
            else:
                for ing, data in rec_data.get("recommendations", {}).items():
                    bid = data.get("recommended_bid")
                    if bid:
                        price_hints[ing] = bid
                avoid_ings = set(rec_data.get("avoid_ingredients", []))
            if price_hints:
                print(f"[STRATEGY] price_hints da auction analyst: {len(price_hints)} ingredienti")
        except Exception as exc:
            print(f"[STRATEGY] WARN lettura bid_recommendations: {exc}")

    # --- Salva output ---
    focus_recipes_list = [focus_name]
    if backup_name:
        focus_recipes_list.append(backup_name)

    out: dict = {
        "method": "llm" if not isinstance(plan, FocusStrategyPlan) or _LLM_AVAILABLE else "algorithmic",
        "focus_recipes": focus_recipes_list,
        "copies_target": copies_target,
        "primary_recipe": focus_name,
        "primary_count": primary_count,
        "target_ingredients": target_ings,
        "ingredient_quantities": ingredient_quantities,
        "price_hints": price_hints,
        "avoid_ingredients": list(avoid_ings),
    }

    out_path = Path(__file__).parent / "explorer_data" / "strategy.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[STRATEGY] analisi salvata -> {out_path}")

    return target_ings, primary_count


# ---------------------------------------------------------------------------
# Standalone
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bid_flag = "--bid" in sys.argv

    async def main() -> None:
        target, primary_count = await run_strategy_agent()
        if bid_flag:
            from bid_agent import run_bid_agent
            print("\n[STRATEGY] passo ingredienti al bid agent...\n")
            await run_bid_agent(preferred_ingredients=target, primary_count=primary_count)
        else:
            print("\nUsa --bid per eseguire anche il bid agent.")

    asyncio.run(main())
