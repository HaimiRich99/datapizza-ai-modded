"""
Auction Analyst — Analizza i risultati dell'asta e raccomanda strategie future.

Flusso:
  1. Parsa il testo dei risultati asta (messaggio server dopo closed_bid)
  2. Accumula storico prezzi per ingrediente in auction_history.json
  3. Usa LLM per analizzare pattern e generare raccomandazioni di offerta
  4. Salva raccomandazioni in bid_recommendations.json (letto da strategy_agent)

Esegui standalone: python auction_analyst.py <testo_risultati.txt>
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

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
    print("[AUCTION] WARN: datapizza framework non trovato — solo analisi statistica")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_EXPLORER_DIR = Path(__file__).parent / "explorer_data"
_HISTORY_PATH = _EXPLORER_DIR / "auction_history.json"
_RECOMMENDATIONS_PATH = _EXPLORER_DIR / "bid_recommendations.json"
_STRATEGY_PATH = _EXPLORER_DIR / "strategy.json"
_RECIPES_PATH = _EXPLORER_DIR / "recipes.json"
_INGREDIENTS_PATH = Path(__file__).parent / "ingredienti_unici.txt"


# ---------------------------------------------------------------------------
# Parsing risultati asta
# ---------------------------------------------------------------------------

# Pattern: "Restaurant 7 try to buy:15 Radici di Gravità at single price of: 41 result:Bought 15 Radici di Gravità for 615"
# oppure   "Restaurant 7 try to buy:15 Carne X at single price of: 41 result:Insufficient funds"
_LINE_RE = re.compile(
    r"Restaurant\s+(\d+)\s+try to buy:(\d+)\s+(.+?)\s+at single price of:\s+(\d+)\s+result:(.+)"
)


def parse_auction_results(text: str) -> dict[str, list[dict]]:
    """
    Parsa il testo dei risultati asta.
    Ritorna: {ingredient_name: [{"restaurant": int, "quantity": int, "price": int, "bought": bool}]}
    """
    results: dict[str, list[dict]] = {}

    for line in text.strip().split("\n"):
        line = line.strip()
        m = _LINE_RE.match(line)
        if not m:
            continue
        restaurant = int(m.group(1))
        quantity = int(m.group(2))
        ingredient = m.group(3).strip()
        price = int(m.group(4))
        result_text = m.group(5).strip()
        bought = result_text.startswith("Bought")

        if ingredient not in results:
            results[ingredient] = []
        results[ingredient].append({
            "restaurant": restaurant,
            "quantity": quantity,
            "price": price,
            "bought": bought,
        })

    return results


def compute_stats(bids: list[dict]) -> dict:
    """Calcola statistiche per un ingrediente dato la lista di offerte."""
    winning = [b for b in bids if b["bought"]]
    losing = [b for b in bids if not b["bought"]]

    if not winning:
        return {
            "n_buyers": 0,
            "min_winning_price": None,
            "max_winning_price": None,
            "avg_winning_price": None,
            "lowest_losing_price": min((b["price"] for b in losing), default=None),
            "total_bought_qty": 0,
        }

    prices = [b["price"] for b in winning]
    return {
        "n_buyers": len(winning),
        "min_winning_price": min(prices),
        "max_winning_price": max(prices),
        "avg_winning_price": round(sum(prices) / len(prices), 1),
        "lowest_losing_price": min((b["price"] for b in losing), default=None),
        "total_bought_qty": sum(b["quantity"] for b in winning),
    }


# ---------------------------------------------------------------------------
# Storia prezzi (JSON accumulativo)
# ---------------------------------------------------------------------------

def load_history() -> dict:
    if _HISTORY_PATH.exists():
        try:
            return json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def update_history(turn_id: int, parsed: dict[str, list[dict]]) -> dict:
    """Aggiunge i risultati del turno corrente allo storico."""
    history = load_history()
    turn_key = f"turn_{turn_id}"
    history[turn_key] = {
        "timestamp": datetime.now().isoformat(),
        "ingredients": {}
    }
    for ingredient, bids in parsed.items():
        stats = compute_stats(bids)
        history[turn_key]["ingredients"][ingredient] = {
            "stats": stats,
            "bids": bids,
        }
    _HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return history


def build_price_summary(history: dict) -> dict[str, dict]:
    """
    Consolida lo storico in un riassunto per ingrediente:
    {ingredient: {turns_seen, avg_winning_price, min_winning_price, max_winning_price, avg_buyers}}
    """
    summary: dict[str, dict] = {}
    for turn_data in history.values():
        for ing, data in turn_data.get("ingredients", {}).items():
            stats = data.get("stats", {})
            if ing not in summary:
                summary[ing] = {
                    "turns_seen": 0,
                    "winning_prices": [],
                    "n_buyers_list": [],
                }
            s = summary[ing]
            s["turns_seen"] += 1
            if stats.get("avg_winning_price") is not None:
                s["winning_prices"].append(stats["avg_winning_price"])
            if stats.get("n_buyers") is not None:
                s["n_buyers_list"].append(stats["n_buyers"])

    result = {}
    for ing, s in summary.items():
        prices = s["winning_prices"]
        buyers = s["n_buyers_list"]
        result[ing] = {
            "turns_seen": s["turns_seen"],
            "avg_winning_price": round(sum(prices) / len(prices), 1) if prices else None,
            "min_winning_price": min(prices) if prices else None,
            "max_winning_price": max(prices) if prices else None,
            "avg_buyers": round(sum(buyers) / len(buyers), 1) if buyers else 0,
        }
    return result


def load_recipes() -> list[dict]:
    """Carica le ricette salvate da strategy_agent. Lista vuota se non disponibili."""
    if _RECIPES_PATH.exists():
        try:
            return json.loads(_RECIPES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def load_unique_ingredients() -> list[str]:
    """Carica la lista completa degli ingredienti da ingredienti_unici.txt."""
    if not _INGREDIENTS_PATH.exists():
        print(f"[AUCTION] WARN: {_INGREDIENTS_PATH} non trovato")
        return []
    return [
        line.strip()
        for line in _INGREDIENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def rank_least_wanted(
    all_ingredients: list[str],
    current_parsed: dict[str, list[dict]],
    top_n: int = 10,
) -> list[dict]:
    """
    Classifica gli ingredienti per 'meno ricercati' basandosi SOLO sull'ultima asta.

    Logica:
    - Ingrediente NON apparso nell'asta → n_buyers=0, n_bids=0 → nessuno lo ha voluto
    - Ingrediente apparso con pochi acquirenti → poco richiesto
    - Sort: n_buyers ASC, poi n_bids totali ASC

    Ritorna top_n ingredienti con meno concorrenza.
    """
    ranked = []
    for ing in all_ingredients:
        bids = current_parsed.get(ing, [])
        if not bids:
            ranked.append({
                "ingredient": ing,
                "n_buyers": 0,
                "n_bids": 0,
                "avg_winning_price": None,
                "status": "not_bid",
            })
        else:
            stats = compute_stats(bids)
            ranked.append({
                "ingredient": ing,
                "n_buyers": stats["n_buyers"],
                "n_bids": len(bids),
                "avg_winning_price": stats["avg_winning_price"],
                "status": "bid",
            })

    ranked.sort(key=lambda x: (x["n_buyers"], x["n_bids"]))
    return ranked[:top_n]


def score_recipes_by_auction(
    recipes: list[dict],
    current_parsed: dict[str, list[dict]],
) -> list[dict]:
    """
    Per ogni ricetta calcola un punteggio di opportunità basato SOLO sull'ultima asta.

    - safe_fraction: % ingredienti non contesi (n_buyers=0 o <=1) nell'ultima asta
    - estimated_cost: stima costo per copia (ingredienti non apparsi → prezzo basso)
    - auction_score: prestige * safe_fraction / costo_normalizzato

    Ingredienti non apparsi nell'asta → nessuno li vuole → costo minimo (5 crediti).
    """
    # Stats solo dall'asta corrente
    current_stats: dict[str, dict] = {}
    for ing, bids in current_parsed.items():
        stats = compute_stats(bids)
        current_stats[ing] = {
            "avg_buyers": stats.get("n_buyers") or 0,
            "avg_price": stats.get("avg_winning_price") or 0,
        }

    scored = []
    for recipe in recipes:
        ings = recipe.get("ingredients", {})
        if not ings:
            continue

        low_comp = []    # nell'asta, avg_buyers <= 1
        contested = []   # nell'asta, avg_buyers > 1
        unseen = []      # non apparso nell'asta → nessuno lo ha voluto

        total_cost = 0
        for ing, qty in ings.items():
            s = current_stats.get(ing)
            if s is None:
                # Non apparso → nessuno lo ha voluto → cheapest
                unseen.append(ing)
                total_cost += qty * 5
            elif s["avg_buyers"] <= 1:
                low_comp.append(ing)
                total_cost += qty * max(1, s["avg_price"] or 10)
            else:
                contested.append(ing)
                total_cost += qty * max(1, s["avg_price"] or 30)

        n_ings = len(ings)
        safe_fraction = (len(low_comp) + len(unseen)) / n_ings

        prestige = recipe.get("prestige", 0)
        cost_norm = max(1, total_cost / 50)
        auction_score = round(prestige * safe_fraction / cost_norm, 2)

        scored.append({
            "name": recipe["name"],
            "prestige": prestige,
            "auction_score": auction_score,
            "safe_fraction": round(safe_fraction, 2),
            "low_comp_ingredients": low_comp,
            "contested_ingredients": contested,
            "unseen_ingredients": unseen,
            "estimated_cost_per_copy": round(total_cost),
        })

    scored.sort(key=lambda x: -x["auction_score"])
    return scored


# ---------------------------------------------------------------------------
# Pydantic models per LLM
# ---------------------------------------------------------------------------

class IngredientBidRec(BaseModel):
    ingredient: str = Field(description="Nome esatto dell'ingrediente")
    recommended_bid: int = Field(description="Prezzo offerta raccomandato per unità (basato su storico)")
    priority: str = Field(description="high/medium/low — quanto è importante per le nostre ricette target")
    rationale: str = Field(description="Breve motivazione (1 frase)")


class AuctionStrategy(BaseModel):
    recommendations: list[IngredientBidRec] = Field(
        description="Lista di raccomandazioni offerta per ingrediente (solo quelli rilevanti)"
    )
    low_competition_ingredients: list[str] = Field(
        description="Ingredienti con poca concorrenza (pochi acquirenti, prezzi bassi) da sfruttare"
    )
    avoid_ingredients: list[str] = Field(
        description="Ingredienti molto contesi (molti acquirenti, prezzi alti) da evitare o sovra-offrire con cautela"
    )
    opportunity_note: str = Field(
        description="Nota strategica sulle opportunità principali per il prossimo turno (2-3 righe)"
    )
    recommended_recipes: list[str] = Field(
        default_factory=list,
        description=(
            "Nomi ESATTI di 1-2 ricette da usare come focus nel prossimo turno. "
            "Scegliere ricette con alta safe_fraction (ingredienti liberi) e buona prestige. MASSIMO 2."
        ),
    )


_SYSTEM_PROMPT = """\
Sei un analista strategico per un gioco di ristorante galattico con aste chiuse (closed-bid).

Ricevi i dati dell'ULTIMA asta (non storico):
- least_wanted_ingredients: top 10 ingredienti MENO richiesti nell'ultima asta
  (n_buyers=0 → nessuno li ha voluti; status=not_bid → non sono nemmeno apparsi)
- recipe_scores: ricette ordinate per opportunità (safe_fraction alta = usa ingredienti poco contesi)
- current_auction_stats: dati grezzi dell'ultima asta per riferimento prezzi

Obiettivo: raccomandare offerte ottimali E le 1-2 ricette migliori su cui concentrarsi.

Regole chiave:
- Ingredienti con n_buyers=0 e status=not_bid → NESSUNO li vuole → offerta MINIMA (5-15 crediti)
- Ingredienti con n_buyers=1 → POCA concorrenza → offerta moderata (prezzo_corrente + 10%)
- Ingredienti con n_buyers>=4 → MOLTO CONTESI → evitare o solo se indispensabili
- Per recommended_recipes: scegliere MASSIMO 2 ricette con safe_fraction >= 0.6 e buona prestige
- Priorità assoluta alle ricette i cui ingredienti sono in least_wanted_ingredients
- Massimizza prestige / crediti spesi
"""


def _call_llm_sync(
    current_turn_parsed: dict[str, list[dict]],
    least_wanted: list[dict],
    recipe_scores: list[dict],
) -> Optional[AuctionStrategy]:
    regolo_key = os.getenv("REGOLO_API_KEY")
    if not regolo_key:
        print("[AUCTION] REGOLO_API_KEY non trovata — skip analisi LLM")
        return None

    client = OpenAILikeClient(
        api_key=regolo_key,
        model="gpt-oss-120b",
        base_url="https://api.regolo.ai/v1",
        system_prompt=_SYSTEM_PROMPT,
    )

    current_stats = {ing: compute_stats(bids) for ing, bids in current_turn_parsed.items()}

    prompt = (
        f"=== TOP 10 INGREDIENTI MENO RICHIESTI (ultima asta) ===\n"
        f"(n_buyers=0 + status=not_bid → nessuno li ha voluti → offerta minima)\n"
        f"{json.dumps(least_wanted, ensure_ascii=False, indent=2)}\n\n"
        f"=== CLASSIFICA RICETTE PER OPPORTUNITÀ (top 15) ===\n"
        f"(auction_score alto = usa ingredienti poco contesi + buona prestige)\n"
        f"{json.dumps(recipe_scores[:15], ensure_ascii=False, indent=2)}\n\n"
        f"=== DATI GREZZI ULTIMA ASTA (riferimento prezzi) ===\n"
        f"{json.dumps(current_stats, ensure_ascii=False, indent=2)}\n\n"
        f"Analizza e fornisci:\n"
        f"1. Raccomandazioni offerta per gli ingredienti in least_wanted (prezzi bassi!)\n"
        f"2. Le 1-2 ricette migliori in recommended_recipes (nomi ESATTI dalla classifica)"
    )

    print("[AUCTION] LLM analisi in corso...")
    response = client.structured_response(
        input=prompt,
        output_cls=AuctionStrategy,
    )

    raw = response.structured_data
    if isinstance(raw, AuctionStrategy):
        return raw
    if isinstance(raw, list) and raw:
        item = raw[0]
        if isinstance(item, AuctionStrategy):
            return item
        if isinstance(item, dict):
            return AuctionStrategy(**item)
    if isinstance(raw, dict):
        return AuctionStrategy(**raw)
    return None


# ---------------------------------------------------------------------------
# Analisi statistica fallback (senza LLM)
# ---------------------------------------------------------------------------

def _statistical_recommendations(
    current_turn_parsed: dict[str, list[dict]],
    least_wanted: list[dict],
    recipe_scores: list[dict],
) -> dict:
    """Genera raccomandazioni base senza LLM, usando solo l'ultima asta."""
    recs = {}
    low_comp = []
    high_comp = []

    # Raccomandazioni prezzi basate sull'ultima asta
    for ing, bids in current_turn_parsed.items():
        stats = compute_stats(bids)
        avg_price = stats.get("avg_winning_price") or 10
        n_buyers = stats.get("n_buyers") or 0

        # 0 acquirenti → bid minimo; pochi → +10%; molti → +20%
        if n_buyers == 0:
            rec_bid = max(1, int(avg_price * 1.05))
        elif n_buyers <= 2:
            rec_bid = max(1, int(avg_price * 1.10))
        else:
            rec_bid = max(1, int(avg_price * 1.20))

        if n_buyers <= 1:
            low_comp.append(ing)
        elif n_buyers >= 4:
            high_comp.append(ing)

        recs[ing] = {
            "recommended_bid": rec_bid,
            "n_buyers_last_auction": n_buyers,
            "avg_winning_price_last_auction": avg_price,
        }

    # Aggiungi ingredienti least_wanted non ancora nell'asta (bid minimo assoluto)
    for entry in least_wanted:
        ing = entry["ingredient"]
        if ing not in recs and entry["status"] == "not_bid":
            recs[ing] = {
                "recommended_bid": 5,
                "n_buyers_last_auction": 0,
                "avg_winning_price_last_auction": None,
            }

    recommended_recipes = [r["name"] for r in recipe_scores[:2]]

    return {
        "recommendations": recs,
        "low_competition_ingredients": low_comp,
        "avoid_ingredients": high_comp,
        "recommended_recipes": recommended_recipes,
        "method": "statistical",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_auction_analyst(
    auction_text: str,
    turn_id: int = 0,
) -> dict:
    """
    Analizza i risultati dell'asta e salva raccomandazioni per il turno successivo.
    Ritorna il dizionario delle raccomandazioni.
    """
    print(f"\n[AUCTION] analisi risultati asta turno {turn_id}")

    # 1. Parsa
    parsed = parse_auction_results(auction_text)
    if not parsed:
        print("[AUCTION] nessun dato parsato — testo non riconosciuto")
        return {}

    n_ingredients = len(parsed)
    n_transactions = sum(len(v) for v in parsed.values())
    print(f"[AUCTION] parsati {n_ingredients} ingredienti, {n_transactions} transazioni")

    # 2. Aggiorna storico (manteniamo il file ma non lo usiamo per l'analisi)
    _EXPLORER_DIR.mkdir(exist_ok=True)
    update_history(turn_id, parsed)

    # 3. Carica lista completa ingredienti e calcola ranking least_wanted
    all_ingredients = load_unique_ingredients()
    if not all_ingredients:
        # Fallback: usa gli ingredienti apparsi nell'asta
        all_ingredients = list(parsed.keys())
        print("[AUCTION] WARN: ingredienti_unici.txt non trovato — ranking parziale")

    least_wanted = rank_least_wanted(all_ingredients, parsed, top_n=10)

    print(f"\n[AUCTION] === TOP 10 INGREDIENTI MENO RICHIESTI (asta {turn_id}) ===")
    not_bid_count = sum(1 for e in least_wanted if e["status"] == "not_bid")
    print(f"  ({not_bid_count} non apparsi nell'asta, {10 - not_bid_count} con pochi acquirenti)")
    for i, entry in enumerate(least_wanted, 1):
        status = "NON APPARSO" if entry["status"] == "not_bid" else f"{entry['n_buyers']} acquirenti"
        price_info = f" | prezzo={entry['avg_winning_price']}" if entry["avg_winning_price"] else ""
        print(f"  {i:2}. {entry['ingredient']!r} — {status}{price_info}")

    # 4. Carica ricette e calcola score usando SOLO l'asta corrente
    recipes = load_recipes()
    recipe_scores: list[dict] = []
    if recipes:
        recipe_scores = score_recipes_by_auction(recipes, parsed)
        print(f"\n[AUCTION] classificate {len(recipe_scores)} ricette per opportunità d'asta")
        print("[AUCTION] top 5 ricette:")
        for r in recipe_scores[:5]:
            print(f"  {r['name']!r} | score={r['auction_score']} | safe={r['safe_fraction']} | cost≈{r['estimated_cost_per_copy']}")
    else:
        print("[AUCTION] WARN: nessuna ricetta trovata in recipes.json — skip recipe scoring")

    # 5. Analisi LLM
    llm_result: Optional[AuctionStrategy] = None
    if _LLM_AVAILABLE:
        try:
            llm_result = await asyncio.to_thread(
                _call_llm_sync, parsed, least_wanted, recipe_scores
            )
        except Exception as exc:
            print(f"[AUCTION] LLM errore: {exc}")

    # 6. Prepara output
    def _safe_print(text: str) -> None:
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))

    if llm_result is not None:
        _safe_print("\n[AUCTION] LLM analisi completata:")
        _safe_print(f"  ricette raccomandate: {llm_result.recommended_recipes}")
        _safe_print(f"  bassa concorrenza: {llm_result.low_competition_ingredients}")
        _safe_print(f"  da evitare: {llm_result.avoid_ingredients}")
        _safe_print(f"  nota: {llm_result.opportunity_note}")

        output = {
            "method": "llm",
            "turn_id": turn_id,
            "timestamp": datetime.now().isoformat(),
            "least_wanted_ingredients": least_wanted,
            "recommended_recipes": llm_result.recommended_recipes,
            "recommendations": [
                {
                    "ingredient": r.ingredient,
                    "recommended_bid": r.recommended_bid,
                    "priority": r.priority,
                    "rationale": r.rationale,
                }
                for r in llm_result.recommendations
            ],
            "low_competition_ingredients": llm_result.low_competition_ingredients,
            "avoid_ingredients": llm_result.avoid_ingredients,
            "opportunity_note": llm_result.opportunity_note,
            "recipe_scores": recipe_scores[:10],
        }
    else:
        stat_result = _statistical_recommendations(parsed, least_wanted, recipe_scores)
        print(f"\n[AUCTION] analisi statistica (fallback LLM):")
        print(f"  ricette raccomandate: {stat_result['recommended_recipes']}")
        print(f"  bassa concorrenza: {stat_result['low_competition_ingredients']}")
        print(f"  alta concorrenza: {stat_result['avoid_ingredients']}")

        output = {
            "method": "statistical",
            "turn_id": turn_id,
            "timestamp": datetime.now().isoformat(),
            "least_wanted_ingredients": least_wanted,
            "recipe_scores": recipe_scores[:10],
            **stat_result,
        }

    # 7. Stampa riepilogo ricette raccomandate
    print("\n[AUCTION] === RICETTE RACCOMANDATE PROSSIMO TURNO ===")
    recommended = output.get("recommended_recipes", [])
    if recommended:
        for rec_name in recommended:
            r = next((x for x in recipe_scores if x["name"] == rec_name), None)
            if r:
                print(f"  -> {rec_name!r} | safe={r['safe_fraction']} | cost≈{r['estimated_cost_per_copy']}")
                if r.get("unseen_ingredients"):
                    print(f"     non contesi: {r['unseen_ingredients']}")
                if r.get("low_comp_ingredients"):
                    print(f"     low_comp: {r['low_comp_ingredients']}")
                if r.get("contested_ingredients"):
                    print(f"     contesi: {r['contested_ingredients']}")
    else:
        print("  (nessuna ricetta raccomandata)")

    # 8. Salva raccomandazioni
    _RECOMMENDATIONS_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[AUCTION] raccomandazioni salvate -> {_RECOMMENDATIONS_PATH}\n")

    return output


# ---------------------------------------------------------------------------
# Standalone (per test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Leggi da file di testo
        text_path = Path(sys.argv[1])
        auction_text = text_path.read_text(encoding="utf-8")
        turn_id = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    else:
        # Esempio minimal per test
        auction_text = (
            "Restaurant 7 try to buy:15 Radici di Gravità at single price of: 41 result:Bought 15 Radici di Gravità for 615\n"
            "Restaurant 3 try to buy:2 Radici di Gravità at single price of: 40 result:Bought 2 Radici di Gravità for 80\n"
            "Restaurant 24 try to buy:2 Radici di Gravità at single price of: 15 result:Bought 2 Radici di Gravità for 30\n"
            "Restaurant 7 try to buy:15 Essenza di Tachioni at single price of: 41 result:Insufficient funds\n"
            "Restaurant 24 try to buy:1 Essenza di Tachioni at single price of: 154 result:Bought 1 Essenza di Tachioni for 154\n"
        )
        turn_id = 9

    asyncio.run(run_auction_analyst(auction_text, turn_id))
