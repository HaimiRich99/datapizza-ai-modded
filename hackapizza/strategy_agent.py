"""
Agente Strategia — Ricetta Primaria + Cluster LLM/Jaccard

Garantisce almeno 1 ricetta completabile concentrando il budget sulla ricetta
più vicina al completamento ("ricetta primaria"), poi aggiunge ingredienti del
cluster secondario per massimizzare le opportunità.

Return type: tuple[list[str], int]
  - list[str]: ingredienti target (primari prima, secondari dopo)
  - int: quanti dei primi N ingredienti sono "primari" (ricetta garantita)

Fallback automatico: clustering Jaccard puro se REGOLO_API_KEY mancante.

Esegui standalone: python strategy_agent.py [--bid]
"""

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from server_client import HackapizzaClient

load_dotenv()

TEAM_ID = 24
BASE_URL = "https://hackapizza.datapizza.tech"
API_KEY = os.getenv("TEAM_API_KEY", "")

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
    print("[STRATEGY] WARN: datapizza framework non trovato — solo fallback Jaccard")

# ---------------------------------------------------------------------------
# Pydantic models per structured_response
# ---------------------------------------------------------------------------


class IngredientPriority(BaseModel):
    name: str = Field(description="Nome esatto dell'ingrediente come appare nelle ricette")
    used_by: int = Field(description="Numero di ricette del cluster che usano questo ingrediente")


class StrategyPlan(BaseModel):
    target_recipes: list[str] = Field(
        description="Nomi delle 5-8 ricette nel cluster scelto (simili tra loro, massima efficienza prestige/ingredienti)"
    )
    ingredients: list[IngredientPriority] = Field(
        description="Ingredienti da comprare all'asta, ordinati per priorità decrescente (prima quelli condivisi da più ricette del cluster)"
    )
    reasoning: str = Field(
        description="Spiegazione in 1-2 righe della strategia scelta"
    )


# ---------------------------------------------------------------------------
# LLM call (sincrona — viene eseguita in asyncio.to_thread)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Sei un agente strategico per un gioco di ristorante galattico.

Ricevi:
- L'inventario attuale del ristorante
- La lista completa delle ricette con ingredienti e punteggio prestige

Obiettivo: trovare il CLUSTER OTTIMALE di 5-8 ricette simili (che condividono molti ingredienti)
che massimizza: prestige_totale / n_ingredienti_unici_da_comprare.

Regole:
- Privilegia ricette che già condividono ingredienti con l'inventario attuale
- Ingredienti già in inventario non vanno comprati (non includerli come priorità alta)
- Nella lista ingredients, metti prima gli ingredienti usati da più ricette del cluster
- Usa i nomi esatti degli ingredienti come appaiono nel JSON
"""


def _compact_recipes(recipes: list[dict]) -> list[dict]:
    """Riduce ogni ricetta ai campi essenziali per il prompt LLM."""
    return [
        {
            "name": r["name"],
            "prestige": r.get("prestige", 0),
            "ingredients": list(r.get("ingredients", {}).keys()),
        }
        for r in recipes
    ]


def _call_llm_sync(
    recipes: list[dict],
    inventory: dict[str, int],
) -> Optional[StrategyPlan]:
    """
    Chiama l'LLM in modo sincrono.
    Eseguita tramite asyncio.to_thread per non bloccare l'event loop.
    """
    regolo_key = os.getenv("REGOLO_API_KEY")
    if not regolo_key:
        print("[STRATEGY] REGOLO_API_KEY non trovata — uso fallback Jaccard")
        return None

    client = OpenAILikeClient(
        api_key=regolo_key,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
        system_prompt=_SYSTEM_PROMPT,
    )

    prompt = (
        f"Inventario attuale: {json.dumps(inventory, ensure_ascii=False) if inventory else '(vuoto)'}\n\n"
        f"Ricette disponibili:\n"
        f"{json.dumps(_compact_recipes(recipes), ensure_ascii=False, indent=2)}"
    )

    print("[STRATEGY] LLM in elaborazione...")
    response = client.structured_response(
        input=prompt,
        output_cls=StrategyPlan,
    )

    raw = response.structured_data
    if isinstance(raw, StrategyPlan):
        return raw
    if isinstance(raw, list) and raw:
        item = raw[0]
        if isinstance(item, StrategyPlan):
            return item
        if isinstance(item, dict):
            return StrategyPlan(**item)
    if isinstance(raw, dict):
        return StrategyPlan(**raw)
    return None


# ---------------------------------------------------------------------------
# Fallback: clustering Jaccard puro (nessuna dipendenza LLM)
# ---------------------------------------------------------------------------

_SEED_COUNT = 40
_MAX_CLUSTER = 8
_MIN_SIM = 0.25


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _fallback_cluster(recipes: list[dict], inventory: dict[str, int]) -> list[str]:
    seeds = sorted(recipes, key=lambda r: -r.get("prestige", 0))[:_SEED_COUNT]
    best_cluster: list[dict] = []
    best_score = -1.0

    for seed in seeds:
        seed_ings = set(seed.get("ingredients", {}).keys())
        candidates = [
            (r, _jaccard(seed_ings, set(r.get("ingredients", {}).keys())))
            for r in recipes if r is not seed
        ]
        candidates = sorted(
            [(r, s) for r, s in candidates if s >= _MIN_SIM],
            key=lambda x: (-x[1], -x[0].get("prestige", 0)),
        )
        cluster = [seed] + [r for r, _ in candidates[:_MAX_CLUSTER - 1]]

        unique_ings: set[str] = set()
        total_prestige = 0
        for r in cluster:
            unique_ings.update(r.get("ingredients", {}).keys())
            total_prestige += r.get("prestige", 0)
        to_buy = unique_ings - set(inventory.keys())
        score = total_prestige / max(1, len(to_buy))

        if score > best_score:
            best_score = score
            best_cluster = cluster

    freq: Counter = Counter()
    for r in best_cluster:
        for ing in r.get("ingredients", {}):
            freq[ing] += 1

    print(f"[STRATEGY] cluster Jaccard ({len(best_cluster)} ricette | score={best_score:.1f}):")
    for r in best_cluster:
        ings = r.get("ingredients", {})
        covered = sum(1 for ing in ings if ing in inventory)
        print(f"  - {r['name']} | prestige={r.get('prestige')} | {covered}/{len(ings)} in inventario")

    return [ing for ing, _ in freq.most_common()]


# ---------------------------------------------------------------------------
# Post-processing: normalizza nomi ingredienti
# ---------------------------------------------------------------------------

def _normalize(plan: StrategyPlan, valid_ings: set[str]) -> list[str]:
    """Filtra/corregge i nomi restituiti dall'LLM contro i nomi reali."""
    result = []
    for item in plan.ingredients:
        name = item.name
        if name in valid_ings:
            result.append(name)
        else:
            # Prova match case-insensitive
            match = next((v for v in valid_ings if v.lower() == name.lower()), None)
            if match:
                result.append(match)
            else:
                print(f"[STRATEGY] WARN ingrediente LLM non trovato: {name!r} — ignorato")
    return result


# ---------------------------------------------------------------------------
# Ricetta primaria: la più vicina al completamento
# ---------------------------------------------------------------------------


def find_primary_recipe(recipes: list[dict], inventory: dict[str, int]) -> dict:
    """
    Sceglie la ricetta più vicina al completamento (meno ingredienti mancanti).
    Tiebreak: prestige decrescente.
    """
    def missing_count(recipe: dict) -> int:
        ings = recipe.get("ingredients", {})
        return sum(1 for ing, qty in ings.items() if inventory.get(ing, 0) < qty)

    return min(recipes, key=lambda r: (missing_count(r), -r.get("prestige", 0)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_strategy_agent() -> tuple[list[str], int]:
    """
    Ritorna (list[str], int): ingredienti target ordinati per priorità e
    quanti dei primi sono "primari" (ingredienti della ricetta garantita).
    """
    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        recipes = await client.get_recipes()
        restaurant = await client.get_restaurant()

    inventory: dict[str, int] = restaurant.get("inventory", {})
    balance = restaurant.get("balance", 0)
    print(f"[STRATEGY] ricette: {len(recipes)} | saldo: {balance} | "
          f"inventario: {len(inventory)} ingredienti")

    all_ings: set[str] = {
        ing for r in recipes for ing in r.get("ingredients", {}).keys()
    }

    # --- Ricetta primaria: garantisce almeno 1 completabile ---
    primary = find_primary_recipe(recipes, inventory)
    primary_missing = [
        ing for ing, qty in primary.get("ingredients", {}).items()
        if inventory.get(ing, 0) < qty
    ]
    print(
        f"[STRATEGY] ricetta primaria: {primary['name']!r} | "
        f"prestige={primary.get('prestige')} | mancanti: {primary_missing}"
    )

    # --- Cluster secondario (LLM o Jaccard) ---
    plan: Optional[StrategyPlan] = None

    if _LLM_AVAILABLE:
        try:
            plan = await asyncio.to_thread(_call_llm_sync, recipes, inventory)
        except Exception as exc:
            print(f"[STRATEGY] LLM errore: {exc} — uso fallback Jaccard")
            plan = None

    if plan is not None:
        print(f"\n[STRATEGY] cluster LLM ({len(plan.target_recipes)} ricette):")
        for r in plan.target_recipes:
            print(f"  - {r}")
        print(f"[STRATEGY] reasoning: {plan.reasoning}")
        cluster_ings = _normalize(plan, all_ings)
    else:
        print("[STRATEGY] uso clustering Jaccard (fallback)")
        cluster_ings = _fallback_cluster(recipes, inventory)

    # --- Merge: primari prima, poi cluster (deduplicati) ---
    seen: set[str] = set(primary_missing)
    extra = [ing for ing in cluster_ings if ing not in seen]
    target_ings = primary_missing + extra
    primary_count = len(primary_missing)

    print(f"\n[STRATEGY] ingredienti target ({len(target_ings)}) | primari: {primary_count}:")
    for i, ing in enumerate(target_ings, 1):
        have = "✓" if ing in inventory else " "
        tag = "P" if i <= primary_count else " "
        print(f"  {i:2}. [{have}][{tag}] {ing}")

    out: dict = {
        "method": "llm" if plan is not None else "jaccard_fallback",
        "primary_recipe": primary.get("name"),
        "primary_count": primary_count,
        "target_ingredients": target_ings,
    }
    if plan is not None:
        out["target_recipes"] = plan.target_recipes
        out["reasoning"] = plan.reasoning

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
