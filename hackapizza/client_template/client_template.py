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
import json
from datetime import datetime
from typing import Any, Awaitable, Callable
import aiohttp
import os
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("API_KEY")

TEAM_ID = 24  # your team id
TEAM_API_KEY = api_key

BASE_URL = "https://hackapizza.datapizza.tech"

if not TEAM_API_KEY or not TEAM_ID:
    raise SystemExit("Set TEAM_API_KEY and TEAM_ID")


def log(tag: str, message: str) -> None:
    print(f"[{tag}] {datetime.now()}: {message}")


async def game_started(data: dict[str, Any]) -> None:
    turn_id = data.get("turn_id", 0)
    log("EVENT", "game started, turn id: " + str(turn_id))


async def speaking_phase_started() -> None:
    log("EVENT", "speaking phase started")


async def closed_bid_phase_started() -> None:
    log("EVENT", "closed bid phase started")


async def waiting_phase_started() -> None:
    log("EVENT", "waiting phase started")


async def serving_phase_started() -> None:
    log("EVENT", "serving phase started")


async def end_turn() -> None:
    log("EVENT", "turn ended")


async def client_spawned(data: dict[str, Any]) -> None:
    client_name = data.get("clientName", "unknown")
    order_text = str(data.get("orderText", "unknown"))
    order_text = order_text.lower().replace("i'd like a ", "").replace("i'd like ", "")
    log("EVENT", f"client={client_name} order={order_text}")


async def preparation_complete(data: dict[str, Any]) -> None:
    dish_name = data.get("dish", "unknown")
    log("EVENT", f"dish ready: {dish_name}")


async def message(data: dict[str, Any]) -> None:
    sender = data.get("sender", "unknown")
    text = data.get("payload", "")
    log("EVENT", f"message from {sender}: {text}")


async def game_phase_changed(data: dict[str, Any]) -> None:
    phase = data.get("phase", "unknown")
    handlers: dict[str, Callable[[], Awaitable[None]]] = {
        "speaking": speaking_phase_started,
        "closed_bid": closed_bid_phase_started,
        "waiting": waiting_phase_started,
        "serving": serving_phase_started,
        "stopped": end_turn,
    }
    handler = handlers.get(phase)
    if handler:
        await handler()
    else:
        log("EVENT", f"unknown phase: {phase}")


async def game_reset(data: dict[str, Any]) -> None:
    if data:
        log("EVENT", f"game reset: {data}")
    else:
        log("EVENT", "game reset")


EVENT_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "game_started": game_started,
    "game_phase_changed": game_phase_changed,
    "game_reset": game_reset,
    "client_spawned": client_spawned,
    "preparation_complete": preparation_complete,
    "message": message,
}

##########################################################################################
#                                    DANGER ZONE                                         #
##########################################################################################
# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.


# It is the central event dispatcher used by all handlers.
async def dispatch_event(event_type: str, event_data: dict[str, Any]) -> None:
    handler = EVENT_HANDLERS.get(event_type)
    if not handler:
        return
    try:
        await handler(event_data)
    except Exception as exc:
        log("ERROR", f"handler failed for {event_type}: {exc}")


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It parses SSE lines and translates them into internal events.
async def handle_line(raw_line: bytes) -> None:
    if not raw_line:
        return

    line = raw_line.decode("utf-8", errors="ignore").strip()
    if not line:
        return

    # Standard SSE data format: data: ...
    if line.startswith("data:"):
        payload = line[5:].strip()
        if payload == "connected":
            log("SSE", "connected")
            return
        line = payload

    try:
        event_json = json.loads(line)
    except json.JSONDecodeError:
        log("SSE", f"raw: {line}")
        return

    event_type = event_json.get("type", "unknown")
    event_data = event_json.get("data", {})
    if isinstance(event_data, dict):
        await dispatch_event(event_type, event_data)
    else:
        await dispatch_event(event_type, {"value": event_data})


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It owns the SSE HTTP connection lifecycle.
async def listen_once(session: aiohttp.ClientSession) -> None:
    url = f"{BASE_URL}/events/{TEAM_ID}"
    headers = {"Accept": "text/event-stream", "x-api-key": TEAM_API_KEY}

    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        log("SSE", "connection open")
        async for line in response.content:
            await handle_line(line)


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# It controls script exit behavior when the SSE connection drops.
async def listen_once_and_exit_on_drop() -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        await listen_once(session)
        log("SSE", "connection closed, exiting")


# DO NOT EDIT THE CODE BELOW until you are sure what you are doing.
# Keep this minimal to avoid changing startup behavior.
async def main() -> None:
    log("INIT", f"team={TEAM_ID} base_url={BASE_URL}")
    await listen_once_and_exit_on_drop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("INIT", "client stopped")