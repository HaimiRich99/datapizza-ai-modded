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


_SYSTEM_PROMPT = """\
Sei un analista strategico per un gioco di ristorante galattico con aste chiuse (closed-bid).

Ricevi:
- Storico prezzi per ingrediente dalle aste precedenti (prezzi vincenti, n° acquirenti)
- Ricette target del nostro ristorante (ingredienti necessari)

Obiettivo: raccomandare offerte ottimali per il prossimo turno.

Regole chiave:
- Se un ingrediente ha poca concorrenza (avg_buyers ≤ 2) e prezzi bassi → offrire 10-20% sopra il minimo vincente storico per assicurarselo
- Se un ingrediente è molto conteso (avg_buyers ≥ 5) → valutare se vale il costo o trovare alternative
- Per ingredienti senza storico → offrire conservativamente (es. 10-30 crediti)
- Ingredienti NON nelle nostre ricette target → raccomandare "low" priority o escludere
- Massimizza il valore prestige / crediti spesi
"""


def _call_llm_sync(
    price_summary: dict[str, dict],
    target_recipes_info: list[dict],
    current_turn_parsed: dict[str, list[dict]],
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

    # Compatta il summary per il prompt (solo ingredienti visti)
    compact_summary = {
        ing: {k: v for k, v in data.items()}
        for ing, data in price_summary.items()
    }

    # Aggiungi stats del turno corrente (più recenti, peso maggiore)
    current_stats = {}
    for ing, bids in current_turn_parsed.items():
        stats = compute_stats(bids)
        current_stats[ing] = stats

    prompt = (
        f"=== STORICO PREZZI (tutti i turni) ===\n"
        f"{json.dumps(compact_summary, ensure_ascii=False, indent=2)}\n\n"
        f"=== RISULTATI ASTA TURNO CORRENTE (più recente) ===\n"
        f"{json.dumps(current_stats, ensure_ascii=False, indent=2)}\n\n"
        f"=== RICETTE TARGET DEL NOSTRO RISTORANTE ===\n"
        f"{json.dumps(target_recipes_info, ensure_ascii=False, indent=2)}\n\n"
        f"Analizza e fornisci raccomandazioni strategiche per il prossimo turno di aste."
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
    price_summary: dict[str, dict],
    current_turn_parsed: dict[str, list[dict]],
    target_ingredients: list[str],
) -> dict:
    """Genera raccomandazioni base senza LLM."""
    recs = {}
    low_comp = []
    high_comp = []

    all_ings = set(price_summary.keys()) | set(current_turn_parsed.keys())

    for ing in all_ings:
        hist = price_summary.get(ing, {})
        curr_stats = compute_stats(current_turn_parsed.get(ing, []))

        avg_price = hist.get("avg_winning_price") or curr_stats.get("avg_winning_price") or 10
        avg_buyers = hist.get("avg_buyers") or curr_stats.get("n_buyers") or 0

        # Raccomanda prezzo = avg_winning + 15%
        rec_bid = max(1, int((avg_price or 10) * 1.15))

        is_target = ing in target_ingredients
        priority = "high" if is_target else "low"

        if avg_buyers <= 2 and is_target:
            low_comp.append(ing)
        elif avg_buyers >= 5:
            high_comp.append(ing)

        recs[ing] = {
            "recommended_bid": rec_bid,
            "priority": priority,
            "avg_winning_price_historical": avg_price,
            "avg_buyers_historical": avg_buyers,
        }

    return {
        "recommendations": recs,
        "low_competition_ingredients": low_comp,
        "avoid_ingredients": high_comp,
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

    # 2. Aggiorna storico
    _EXPLORER_DIR.mkdir(exist_ok=True)
    history = update_history(turn_id, parsed)
    print(f"[AUCTION] storico aggiornato -> {_HISTORY_PATH}")

    # 3. Consolida summary storico
    price_summary = build_price_summary(history)

    # 4. Leggi ricette target dalla strategy
    target_recipes_info = []
    target_ingredients: list[str] = []
    if _STRATEGY_PATH.exists():
        try:
            strat = json.loads(_STRATEGY_PATH.read_text(encoding="utf-8"))
            target_ingredients = strat.get("target_ingredients", [])
            recipe_names = strat.get("target_recipes", [])
            target_recipes_info = [
                {"name": name, "ingredients_needed": [
                    ing for ing in target_ingredients
                ]}
                for name in recipe_names
            ] if recipe_names else [{"ingredients_needed": target_ingredients}]
        except Exception as exc:
            print(f"[AUCTION] WARN lettura strategy: {exc}")

    # 5. Analisi LLM
    llm_result: Optional[AuctionStrategy] = None
    if _LLM_AVAILABLE:
        try:
            llm_result = await asyncio.to_thread(
                _call_llm_sync, price_summary, target_recipes_info, parsed
            )
        except Exception as exc:
            print(f"[AUCTION] LLM errore: {exc}")

    # 6. Prepara output
    def _safe_print(text: str) -> None:
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))

    if llm_result is not None:
        _safe_print("[AUCTION] LLM analisi completata:")
        _safe_print(f"  low_competition: {llm_result.low_competition_ingredients}")
        _safe_print(f"  da evitare: {llm_result.avoid_ingredients}")
        _safe_print(f"  nota: {llm_result.opportunity_note}")

        output = {
            "method": "llm",
            "turn_id": turn_id,
            "timestamp": datetime.now().isoformat(),
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
        }
    else:
        # Fallback statistico
        stat_result = _statistical_recommendations(price_summary, parsed, target_ingredients)
        print(f"[AUCTION] analisi statistica (fallback LLM):")
        print(f"  low_competition: {stat_result['low_competition_ingredients']}")
        print(f"  alta concorrenza: {stat_result['avoid_ingredients']}")

        output = {
            "method": "statistical",
            "turn_id": turn_id,
            "timestamp": datetime.now().isoformat(),
            **stat_result,
        }

    # 7. Stampa top opportunità
    print("\n[AUCTION] === OPPORTUNITÀ PROSSIMO TURNO ===")
    if llm_result and llm_result.low_competition_ingredients:
        print(f"  Ingredienti a bassa concorrenza da sfruttare:")
        for ing in llm_result.low_competition_ingredients[:5]:
            hist = price_summary.get(ing, {})
            print(f"    - {ing} | avg_win={hist.get('avg_winning_price')} | avg_buyers={hist.get('avg_buyers')}")
    else:
        # Mostra ingredienti con meno acquirenti medi
        low_comp = sorted(
            [(ing, price_summary.get(ing, {}).get("avg_buyers", 99)) for ing in target_ingredients],
            key=lambda x: x[1]
        )[:5]
        for ing, buyers in low_comp:
            hist = price_summary.get(ing, {})
            print(f"    - {ing} | avg_win={hist.get('avg_winning_price')} | avg_buyers={buyers}")

    # 8. Salva raccomandazioni
    _RECOMMENDATIONS_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[AUCTION] raccomandazioni salvate -> {_RECOMMENDATIONS_PATH}\n")

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
