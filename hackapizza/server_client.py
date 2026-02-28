"""
Hackapizza 2.0 - Server Client
Wrapper per tutti gli endpoint HTTP GET e MCP tools del server di gioco.
"""

import json
import uuid
from typing import Any

import aiohttp


class HackapizzaClient:
    """Client per interagire con il server Hackapizza 2.0."""

    def __init__(self, base_url: str, api_key: str, restaurant_id: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.restaurant_id = restaurant_id
        self._session: aiohttp.ClientSession | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Content-Type": "application/json"}

    async def __aenter__(self) -> "HackapizzaClient":
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Use HackapizzaClient as async context manager (async with ...)")
        return self._session

    # -------------------------------------------------------------------------
    # HTTP GET endpoints
    # -------------------------------------------------------------------------

    async def get_meals(self, turn_id: int) -> list[dict[str, Any]]:
        """GET /meals — richieste clienti per turno e ristorante."""
        session = self._require_session()
        params = {"turn_id": turn_id, "restaurant_id": self.restaurant_id}
        async with session.get(
            f"{self.base_url}/meals", headers=self._headers, params=params
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_restaurants(self) -> list[dict[str, Any]]:
        """GET /restaurants — overview di tutti i ristoranti in gioco."""
        session = self._require_session()
        async with session.get(
            f"{self.base_url}/restaurants", headers=self._headers
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_recipes(self) -> list[dict[str, Any]]:
        """GET /recipes — array ricette con ingredienti e tempi."""
        session = self._require_session()
        async with session.get(
            f"{self.base_url}/recipes", headers=self._headers
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_bid_history(self, turn_id: int) -> list[dict[str, Any]]:
        """GET /bid_history — storico bid di tutti i team per un dato turno."""
        session = self._require_session()
        async with session.get(
            f"{self.base_url}/bid_history",
            headers=self._headers,
            params={"turn_id": turn_id},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_restaurant(self) -> dict[str, Any]:
        """GET /restaurant/:id — dettaglio del proprio ristorante."""
        session = self._require_session()
        async with session.get(
            f"{self.base_url}/restaurant/{self.restaurant_id}", headers=self._headers
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_menu(self) -> list[dict[str, Any]]:
        """GET /restaurant/:id/menu — voci del menu del proprio ristorante."""
        session = self._require_session()
        async with session.get(
            f"{self.base_url}/restaurant/{self.restaurant_id}/menu",
            headers=self._headers,
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_market_entries(self) -> list[dict[str, Any]]:
        """GET /market/entries — entry di mercato attive/chiuse."""
        session = self._require_session()
        async with session.get(
            f"{self.base_url}/market/entries", headers=self._headers
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    # -------------------------------------------------------------------------
    # MCP tools (POST /mcp, JSON-RPC)
    # -------------------------------------------------------------------------

    async def _call_mcp(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Chiamata generica MCP via JSON-RPC."""
        session = self._require_session()
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        async with session.post(
            f"{self.base_url}/mcp",
            headers=self._headers,
            data=json.dumps(payload),
        ) as resp:
            resp.raise_for_status()
            result = await resp.json()
        # Unwrap JSON-RPC result
        if "error" in result:
            raise RuntimeError(f"MCP error: {result['error']}")
        inner = result.get("result", {})
        if inner.get("isError"):
            msg = inner.get("content", [{}])[0].get("text", "unknown error")
            raise RuntimeError(f"MCP tool error [{tool_name}]: {msg}")
        return inner

    async def closed_bid(self, bids: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Invia le offerte per l'asta cieca.
        bids: lista di {ingredient: str, bid: float, quantity: int}
        """
        return await self._call_mcp("closed_bid", {"bids": bids})

    async def save_menu(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Imposta/aggiorna il menu.
        items: lista di {name: str, price: float}
        """
        return await self._call_mcp("save_menu", {"items": items})

    async def create_market_entry(
        self,
        side: str,
        ingredient_name: str,
        quantity: int,
        price: float,
    ) -> dict[str, Any]:
        """
        Crea una proposta di acquisto (BUY) o vendita (SELL) sul mercato.
        side: "BUY" | "SELL"
        """
        return await self._call_mcp(
            "create_market_entry",
            {
                "side": side,
                "ingredient_name": ingredient_name,
                "quantity": quantity,
                "price": price,
            },
        )

    async def execute_transaction(self, market_entry_id: int) -> dict[str, Any]:
        """Accetta un'entry di mercato esistente."""
        return await self._call_mcp("execute_transaction", {"market_entry_id": market_entry_id})

    async def delete_market_entry(self, market_entry_id: int) -> dict[str, Any]:
        """Rimuove una propria entry di mercato."""
        return await self._call_mcp("delete_market_entry", {"market_entry_id": market_entry_id})

    async def prepare_dish(self, dish_name: str) -> dict[str, Any]:
        """Avvia la preparazione di un piatto (solo in serving phase)."""
        return await self._call_mcp("prepare_dish", {"dish_name": dish_name})

    async def serve_dish(self, dish_name: str, client_id: str) -> dict[str, Any]:
        """Serve un piatto a un cliente (solo in serving phase)."""
        return await self._call_mcp("serve_dish", {"dish_name": dish_name, "client_id": client_id})

    async def update_restaurant_is_open(self, is_open: bool) -> dict[str, Any]:
        """Apre o chiude il ristorante."""
        return await self._call_mcp("update_restaurant_is_open", {"is_open": is_open})

    async def send_message(self, recipient_id: int, text: str) -> dict[str, Any]:
        """Invia un messaggio diretto a un altro team."""
        return await self._call_mcp("send_message", {"recipient_id": recipient_id, "text": text})
