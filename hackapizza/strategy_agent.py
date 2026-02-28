"""
Agente Strategia — Focus su 1-2 ricette da ripetere più volte.

Usa il framework datapizza-ai Agent con due tool:
  - load_context()   → carica inventario, ricette, raccomandazioni d'asta
  - save_strategy()  → valida le ricette, calcola ingredienti, salva e termina

Flusso agente:
  1. L'agente chiama load_context() per avere tutto il contesto
  2. L'LLM sceglie 1 focus + eventuale backup basandosi sui dati d'asta
  3. L'agente chiama save_strategy() che salva strategy.json e termina

Fallback algoritmico: se LLM non disponibile o non chiama save_strategy,
viene eseguita la selezione algoritmica (priorità alle ricette da auction analyst).

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
    from datapizza.agents import Agent
    from datapizza.clients.openai_like import OpenAILikeClient
    from datapizza.tools import tool as _tool_decorator
    _LLM_AVAILABLE = True
except ImportError:
    _LLM_AVAILABLE = False
    print("[STRATEGY] WARN: datapizza framework non trovato — solo fallback algoritmico")

    # Dummy @tool per poter definire le funzioni senza errori di import
    def _tool_decorator(func=None, *, end=False, name=None, description=None, strict=False):
        if func is not None:
            return func
        def decorator(f):
            return f
        return decorator

# Alias corto per uso nei decorator
tool = _tool_decorator

# ---------------------------------------------------------------------------
# Stato condiviso tra i tool (pattern come menu_agent._DRY_RUN)
# ---------------------------------------------------------------------------

_loaded_recipes: list[dict] = []
_loaded_inventory: dict[str, int] = {}
_strategy_result: dict = {}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_EXPLORER_DIR = Path(__file__).parent / "explorer_data"
_RECOMMENDATIONS_PATH = _EXPLORER_DIR / "bid_recommendations.json"
_STRATEGY_PATH = _EXPLORER_DIR / "strategy.json"
_RECIPES_CACHE_PATH = _EXPLORER_DIR / "recipes.json"
_PIATTI_DISTANTI_PATH = Path(__file__).parent / "piatti_distanti.json"

# ---------------------------------------------------------------------------
# Helper functions (pure, niente side-effect)
# ---------------------------------------------------------------------------


def _compact_recipes(recipes: list[dict], inventory: dict[str, int]) -> list[dict]:
    """Compatta le ricette con score = prestige / n_ingredienti_mancanti."""
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
    out.sort(key=lambda x: -x["score"])
    return out


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

    focus_ings_ordered: list[str] = []
    for ing, qty_per_copy in focus_recipe.get("ingredients", {}).items():
        total_needed = qty_per_copy * copies_target
        to_buy = max(0, total_needed - inventory.get(ing, 0))
        if to_buy > 0:
            quantities[ing] = to_buy
            focus_ings_ordered.append(ing)

    primary_count = len(focus_ings_ordered)

    backup_ings_ordered: list[str] = []
    if backup_recipe:
        backup_copies = max(1, copies_target - 1)
        for ing, qty_per_copy in backup_recipe.get("ingredients", {}).items():
            total_needed = qty_per_copy * backup_copies
            to_buy = max(0, total_needed - inventory.get(ing, 0) - quantities.get(ing, 0))
            if to_buy > 0:
                quantities[ing] = quantities.get(ing, 0) + to_buy
                if ing not in focus_ings_ordered:
                    backup_ings_ordered.append(ing)

    return quantities, focus_ings_ordered + backup_ings_ordered, primary_count


def _load_auction_data() -> tuple[list[str], list[dict], dict[str, int], set[str]]:
    """
    Legge bid_recommendations.json e ritorna:
    (auction_recommended, recipe_scores, price_hints, avoid_ings)
    """
    auction_recommended: list[str] = []
    recipe_scores: list[dict] = []
    price_hints: dict[str, int] = {}
    avoid_ings: set[str] = set()

    if not _RECOMMENDATIONS_PATH.exists():
        return auction_recommended, recipe_scores, price_hints, avoid_ings

    try:
        rec_data = json.loads(_RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
        auction_recommended = rec_data.get("recommended_recipes", [])
        recipe_scores = rec_data.get("recipe_scores", [])
        avoid_ings = set(rec_data.get("avoid_ingredients", []))
        method = rec_data.get("method", "")
        if method == "llm":
            for r in rec_data.get("recommendations", []):
                ing, bid = r.get("ingredient"), r.get("recommended_bid")
                if ing and bid:
                    price_hints[ing] = bid
        else:
            for ing, data in rec_data.get("recommendations", {}).items():
                bid = data.get("recommended_bid")
                if bid:
                    price_hints[ing] = bid
    except Exception as exc:
        print(f"[STRATEGY] WARN lettura bid_recommendations: {exc}")

    return auction_recommended, recipe_scores, price_hints, avoid_ings


def _load_piatti_distanti() -> list[dict]:
    """Carica i piatti candidati da piatti_distanti.json."""
    if not _PIATTI_DISTANTI_PATH.exists():
        return []
    try:
        return json.loads(_PIATTI_DISTANTI_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[STRATEGY] WARN lettura piatti_distanti: {exc}")
        return []


def _load_least_wanted() -> list[dict]:
    """Legge least_wanted_ingredients da bid_recommendations.json."""
    if not _RECOMMENDATIONS_PATH.exists():
        return []
    try:
        rec_data = json.loads(_RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
        return rec_data.get("least_wanted_ingredients", [])
    except Exception:
        return []


def _select_best_piatto(piatti: list[dict], least_wanted: list[dict]) -> dict | None:
    """
    Seleziona il piatto con più ingredienti nel ranking least_wanted.
    Gli ingredienti in cima alla ranking (meno competitivi) valgono di più.
    In caso di parità, vince il piatto con prestige più alto.
    """
    if not piatti or not least_wanted:
        return None

    top_n = len(least_wanted)
    # posizione 0 (meno ricercato) = top_n punti, posizione n-1 = 1 punto
    ranking_score: dict[str, int] = {
        item["ingredient"]: top_n - i
        for i, item in enumerate(least_wanted)
    }

    best: dict | None = None
    best_score = -1
    for dish in piatti:
        ings = set(dish.get("ingredients", {}).keys())
        score = sum(ranking_score.get(ing, 0) for ing in ings)
        # tiebreak: prestige più alta
        if score > best_score or (score == best_score and best is not None
                                   and dish.get("prestige", 0) > best.get("prestige", 0)):
            best_score = score
            best = dish

    return best


def _save_piatto_strategy_json(
    piatto: dict,
    inventory: dict[str, int],
    price_hints: dict[str, int],
    avoid_ings: set[str],
    method: str,
) -> tuple[list[str], int]:
    """
    Salva strategy.json con TUTTI gli ingredienti del piatto selezionato.
    Ritorna (target_ings, primary_count).
    """
    ings = piatto.get("ingredients", {})
    target_ings: list[str] = list(ings.keys())
    ingredient_quantities: dict[str, int] = {}

    for ing, qty_needed in ings.items():
        have = inventory.get(ing, 0)
        to_buy = max(0, qty_needed - have)
        if to_buy > 0:
            ingredient_quantities[ing] = to_buy

    primary_count = len(target_ings)

    print(f"\n[STRATEGY] Piatto selezionato da piatti_distanti: {piatto['name']!r} | prestige={piatto.get('prestige')}")
    print(f"[STRATEGY] ingredienti ({primary_count}):")
    for ing in target_ings:
        qty = ingredient_quantities.get(ing, 0)
        have = inventory.get(ing, 0)
        print(f"  - {ing}: hai={have} | compra={qty}")

    out: dict = {
        "method": method,
        "focus_recipes": [piatto["name"]],
        "copies_target": 1,
        "primary_recipe": piatto["name"],
        "primary_count": primary_count,
        "target_ingredients": target_ings,
        "ingredient_quantities": ingredient_quantities,
        "price_hints": price_hints,
        "avoid_ingredients": list(avoid_ings),
    }

    _EXPLORER_DIR.mkdir(exist_ok=True)
    _STRATEGY_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[STRATEGY] analisi salvata -> {_STRATEGY_PATH}")

    return target_ings, primary_count


def _save_strategy_json(
    focus_recipe: dict,
    backup_recipe: Optional[dict],
    inventory: dict[str, int],
    copies: int,
    price_hints: dict[str, int],
    avoid_ings: set[str],
    method: str,
) -> tuple[list[str], int]:
    """Calcola ingredienti, stampa piano e salva strategy.json. Ritorna (target_ings, primary_count)."""
    ingredient_quantities, target_ings, primary_count = build_ingredient_quantities(
        focus_recipe, backup_recipe, inventory, copies
    )

    print(f"\n[STRATEGY] ingredienti da comprare ({len(target_ings)}) | primari focus: {primary_count}:")
    for i, ing in enumerate(target_ings, 1):
        qty = ingredient_quantities.get(ing, 0)
        have = inventory.get(ing, 0)
        tag = "FOCUS" if i <= primary_count else "BACK"
        print(f"  {i:2}. [{tag}] {ing}: vuoi={qty + have} | hai={have} | compra={qty}")

    focus_recipes_list = [focus_recipe["name"]]
    if backup_recipe:
        focus_recipes_list.append(backup_recipe["name"])

    out: dict = {
        "method": method,
        "focus_recipes": focus_recipes_list,
        "copies_target": copies,
        "primary_recipe": focus_recipe["name"],
        "primary_count": primary_count,
        "target_ingredients": target_ings,
        "ingredient_quantities": ingredient_quantities,
        "price_hints": price_hints,
        "avoid_ingredients": list(avoid_ings),
    }

    _EXPLORER_DIR.mkdir(exist_ok=True)
    _STRATEGY_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[STRATEGY] analisi salvata -> {_STRATEGY_PATH}")

    return target_ings, primary_count


# ---------------------------------------------------------------------------
# System prompt agente
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
Sei un agente strategico per un gioco di ristorante galattico con aste chiuse.

WORKFLOW:
1. Chiama load_context() per ottenere inventario, ricette e dati d'asta
2. Analizza il contesto e scegli 1 piatto/ricetta
3. Chiama save_strategy() con la tua scelta

STRATEGIA PRINCIPALE — PIATTI_DISTANTI:
Il contesto contiene "recommended_piatto_distante" e "piatti_distanti_scored".
- Questi sono piatti con ingredienti poco contesi nell'ultima asta (least_wanted).
- PRIORITÀ ASSOLUTA: scegli il "recommended_piatto_distante" come focus_recipe.
- Usa il nome ESATTO del piatto come focus_recipe in save_strategy().
- Non serve backup_recipe quando usi un piatto_distante.
- Copies_target = 1 (è sufficiente per raccogliere gli ingredienti).

FALLBACK (solo se recommended_piatto_distante è null):
1. Se auction_recommended contiene ricette → preferirle
2. Alta prestige + pochi ingredienti mancanti
3. Saldo > 2000 → 3-4 copie | Saldo < 1000 → 2 copie

IMPORTANTE: scegliere SOLO 1 ricetta/piatto. Non disperdere su molti piatti diversi.
"""


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@tool
async def load_context() -> str:
    """
    Carica inventario, ricette e raccomandazioni d'asta.
    Da chiamare per primo: fornisce tutto il contesto necessario per scegliere la strategia.
    """
    global _loaded_recipes, _loaded_inventory

    async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
        _loaded_recipes = await client.get_recipes()
        restaurant = await client.get_restaurant()

    _loaded_inventory = restaurant.get("inventory", {})
    balance = float(restaurant.get("balance", 0))

    # Salva ricette per auction_analyst
    _EXPLORER_DIR.mkdir(exist_ok=True)
    _RECIPES_CACHE_PATH.write_text(
        json.dumps(_loaded_recipes, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Leggi dati d'asta
    auction_recommended, recipe_scores, _, _ = _load_auction_data()
    least_wanted = _load_least_wanted()

    compact = _compact_recipes(_loaded_recipes, _loaded_inventory)

    # Calcola il piatto consigliato da piatti_distanti
    piatti = _load_piatti_distanti()
    best_piatto = _select_best_piatto(piatti, least_wanted) if piatti and least_wanted else None
    piatti_scored = []
    if piatti and least_wanted:
        top_n = len(least_wanted)
        ranking_score = {item["ingredient"]: top_n - i for i, item in enumerate(least_wanted)}
        for dish in piatti:
            ings = set(dish.get("ingredients", {}).keys())
            score = sum(ranking_score.get(ing, 0) for ing in ings)
            piatti_scored.append({"name": dish["name"], "prestige": dish.get("prestige"), "overlap_score": score, "ingredients": list(ings)})
        piatti_scored.sort(key=lambda x: -x["overlap_score"])

    print(f"[STRATEGY] ricette: {len(_loaded_recipes)} | saldo: {balance:.0f} | "
          f"inventario: {len(_loaded_inventory)} ingredienti")
    if best_piatto:
        print(f"[STRATEGY] piatto consigliato (piatti_distanti): {best_piatto['name']!r}")
    if auction_recommended:
        print(f"[STRATEGY] ricette raccomandate da auction analyst: {auction_recommended}")

    return json.dumps({
        "balance": balance,
        "inventory": _loaded_inventory,
        "recipes_by_score": compact[:30],
        "auction_recommended": auction_recommended,
        "auction_recipe_scores": recipe_scores[:10],
        "least_wanted_ingredients": least_wanted,
        "piatti_distanti_scored": piatti_scored,
        "recommended_piatto_distante": best_piatto["name"] if best_piatto else None,
    }, ensure_ascii=False, indent=2)


@tool(end=True)
async def save_strategy(
    focus_recipe: str,
    copies_target: int,
    reasoning: str,
    backup_recipe: str = "",
) -> str:
    """
    Valida le ricette scelte, calcola ingredienti da comprare e salva la strategia.
    Termina l'agente.

    Args:
        focus_recipe: Nome ESATTO della ricetta principale da ripetere più volte
        copies_target: Numero di copie da preparare (2-4)
        reasoning: Motivazione della scelta in 1-2 frasi
        backup_recipe: Nome ESATTO della ricetta secondaria (stringa vuota se non serve)
    """
    global _strategy_result

    recipe_map = {r["name"]: r for r in _loaded_recipes}
    piatti = _load_piatti_distanti()
    piatti_map = {p["name"]: p for p in piatti}

    # Controlla se è un piatto_distante (priorità)
    piatto_obj: dict | None = None
    if focus_recipe in piatti_map:
        piatto_obj = piatti_map[focus_recipe]
    elif focus_recipe.lower() in {n.lower(): n for n in piatti_map}:
        real_name = next(n for n in piatti_map if n.lower() == focus_recipe.lower())
        piatto_obj = piatti_map[real_name]

    if piatto_obj is not None:
        print(f"\n[STRATEGY] PIATTO DISTANTE: {piatto_obj['name']!r} | prestige={piatto_obj.get('prestige')}")
        print(f"[STRATEGY] reasoning: {reasoning}")
        _, _, price_hints, avoid_ings = _load_auction_data()
        target_ings, primary_count = _save_piatto_strategy_json(
            piatto_obj, _loaded_inventory, price_hints, avoid_ings, method="agent_piatti_distanti"
        )
    else:
        # Ricetta normale da API
        if focus_recipe not in recipe_map:
            match = next((n for n in recipe_map if n.lower() == focus_recipe.lower()), None)
            if match:
                focus_recipe = match
            else:
                fallback = _best_recipe_by_score(_loaded_recipes, _loaded_inventory)
                focus_recipe = fallback["name"]
                print(f"[STRATEGY] WARN: ricetta non trovata → fallback {focus_recipe!r}")

        backup_name: str | None = backup_recipe.strip() or None
        if backup_name and backup_name not in recipe_map:
            match = next((n for n in recipe_map if n.lower() == backup_name.lower()), None)
            backup_name = match

        focus_obj = recipe_map[focus_recipe]
        backup_obj = recipe_map.get(backup_name) if backup_name else None
        copies = max(1, copies_target)

        print(f"\n[STRATEGY] FOCUS: {focus_recipe!r} | prestige={focus_obj.get('prestige')} | copie={copies}")
        if backup_obj:
            print(f"[STRATEGY] BACKUP: {backup_name!r} | prestige={backup_obj.get('prestige')}")
        print(f"[STRATEGY] reasoning: {reasoning}")

        _, _, price_hints, avoid_ings = _load_auction_data()
        target_ings, primary_count = _save_strategy_json(
            focus_obj, backup_obj, _loaded_inventory, copies, price_hints, avoid_ings, method="agent"
        )

    _strategy_result = {
        "target_ingredients": target_ings,
        "primary_count": primary_count,
    }

    if piatto_obj is not None:
        return f"Strategia salvata: piatto_distante={piatto_obj['name']!r}, ingredienti={len(target_ings)}"
    return f"Strategia salvata: focus={focus_recipe!r}, backup={backup_name!r}, copie={copies}"


# ---------------------------------------------------------------------------
# Fallback algoritmico (quando LLM non disponibile o non chiama save_strategy)
# ---------------------------------------------------------------------------

async def _run_fallback() -> tuple[list[str], int]:
    """
    Selezione algoritmica:
    1. Prima prova piatti_distanti × least_wanted_ingredients (strategia principale)
    2. Fallback su score prestige/mancanti con auction_recommended
    """
    global _loaded_recipes, _loaded_inventory

    if not _loaded_recipes:
        async with HackapizzaClient(BASE_URL, API_KEY, TEAM_ID) as client:
            _loaded_recipes = await client.get_recipes()
            restaurant = await client.get_restaurant()
        _loaded_inventory = restaurant.get("inventory", {})

        _EXPLORER_DIR.mkdir(exist_ok=True)
        _RECIPES_CACHE_PATH.write_text(
            json.dumps(_loaded_recipes, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    _, _, price_hints, avoid_ings = _load_auction_data()

    # --- Strategia primaria: piatti_distanti × least_wanted ---
    piatti = _load_piatti_distanti()
    least_wanted = _load_least_wanted()

    if piatti and least_wanted:
        best_piatto = _select_best_piatto(piatti, least_wanted)
        if best_piatto:
            print(f"[STRATEGY] Strategia piatti_distanti: {best_piatto['name']!r}")
            return _save_piatto_strategy_json(
                best_piatto, _loaded_inventory, price_hints, avoid_ings, method="piatti_distanti"
            )

    # --- Fallback tradizionale: auction_recommended o score prestige ---
    print("[STRATEGY] Nessun dato piatti_distanti/least_wanted — uso fallback tradizionale")
    recipe_map = {r["name"]: r for r in _loaded_recipes}
    auction_recommended, _, _, _ = _load_auction_data()

    focus_obj = None
    if auction_recommended:
        for rec_name in auction_recommended:
            if rec_name in recipe_map:
                focus_obj = recipe_map[rec_name]
                print(f"[STRATEGY] focus da auction analyst: {focus_obj['name']!r} | prestige={focus_obj.get('prestige')}")
                break

    if focus_obj is None:
        focus_obj = _best_recipe_by_score(_loaded_recipes, _loaded_inventory)
        print(f"[STRATEGY] focus algoritmico: {focus_obj['name']!r} | prestige={focus_obj.get('prestige')}")

    backup_obj: Optional[dict] = None
    if auction_recommended and len(auction_recommended) >= 2:
        second = auction_recommended[1]
        if second in recipe_map and second != focus_obj["name"]:
            backup_obj = recipe_map[second]
            print(f"[STRATEGY] backup da auction analyst: {backup_obj['name']!r}")

    if backup_obj is None:
        best_score = -1.0
        for r in _loaded_recipes:
            if r["name"] == focus_obj["name"]:
                continue
            combined = _overlap_score(focus_obj, r) * 0.6 + r.get("prestige", 0) / 100 * 0.4
            if combined > best_score:
                best_score = combined
                backup_obj = r
        if backup_obj and best_score <= 0.2:
            backup_obj = None
        if backup_obj:
            print(f"[STRATEGY] backup algoritmico: {backup_obj['name']!r}")

    return _save_strategy_json(
        focus_obj, backup_obj, _loaded_inventory, N_COPIES_TARGET,
        price_hints, avoid_ings, method="algorithmic"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_strategy_agent() -> tuple[list[str], int]:
    """
    Ritorna (list[str], int): ingredienti target e quanti sono "primari".
    """
    global _strategy_result, _loaded_recipes, _loaded_inventory
    _strategy_result = {}
    _loaded_recipes = []
    _loaded_inventory = {}

    if not _LLM_AVAILABLE:
        return await _run_fallback()

    regolo_key = os.getenv("REGOLO_API_KEY")
    if not regolo_key:
        print("[STRATEGY] REGOLO_API_KEY non trovata — uso fallback algoritmico")
        return await _run_fallback()

    llm_client = OpenAILikeClient(
        api_key=regolo_key,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
    )

    agent = Agent(
        name="Strategy_Manager",
        client=llm_client,
        system_prompt=_SYSTEM_PROMPT,
        tools=[load_context, save_strategy],
        max_steps=4,
    )

    print("[STRATEGY] agente in esecuzione...")
    result = await agent.a_run(
        "Analizza le ricette disponibili e le opportunità d'asta per il prossimo turno. "
        "Chiama load_context() per vedere il contesto, poi scegli 1 ricetta focus "
        "(massimo 1 backup). Chiama save_strategy() con la tua scelta."
    )

    if result:
        print(f"\n[STRATEGY] {result.text}")

    # Fallback se l'agente non ha chiamato save_strategy
    if not _strategy_result:
        print("[STRATEGY] WARN: agente non ha salvato strategia — uso fallback algoritmico")
        return await _run_fallback()

    return _strategy_result["target_ingredients"], _strategy_result["primary_count"]


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
