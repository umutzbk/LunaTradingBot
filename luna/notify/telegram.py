"""Telegram notifier + remote command handling (spec §10)."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

from luna.config import Settings
from luna.http import make_client

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_TEXT = 4000

HELP = (
    "Commands:\n"
    "/status - engine status\n"
    "/positions - open positions\n"
    "/pause - stop opening new positions\n"
    "/resume - clear kill switch and pause, reset peak equity\n"
    "/kill - kill switch: flatten everything"
)


class EngineControl(Protocol):
    """Remote-control surface implemented by LiveEngine.

    Contract: resume() clears BOTH kill_switch and paused, and resets peak_equity to the
    current equity (so a drawdown halt does not immediately re-trigger). kill() sets the
    kill switch and flattens all positions.
    """

    def status(self) -> str: ...
    def positions(self) -> str: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def kill(self) -> None: ...


class Notifier(Protocol):
    def send(self, text: str) -> None: ...
    def commands(self) -> list[tuple[str, str]]: ...


class NullNotifier:
    """Used when Telegram is not configured: logs only."""

    def send(self, text: str) -> None:
        log.info("notify (telegram disabled): %s", text)

    def commands(self) -> list[tuple[str, str]]:
        return []


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, client: httpx.Client):
        self._base = f"{API}/bot{token}"
        self._chat_id = str(chat_id)
        self._client = client
        self._offset = 0

    def send(self, text: str) -> None:
        try:
            r = self._client.post(
                f"{self._base}/sendMessage",
                json={"chat_id": self._chat_id, "text": text[:MAX_TEXT]},
            )
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("telegram send failed: %s", type(e).__name__)

    def commands(self) -> list[tuple[str, str]]:
        try:
            r = self._client.get(f"{self._base}/getUpdates", params={"offset": self._offset, "timeout": 0})
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as e:  # noqa: BLE001
            log.warning("telegram getUpdates failed: %s", type(e).__name__)
            return []
        out: list[tuple[str, str]] = []
        for u in updates:
            self._offset = max(self._offset, int(u["update_id"]) + 1)
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self._chat_id:
                log.warning("ignoring telegram message from foreign chat")
                continue
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            head, _, arg = text.partition(" ")
            out.append((head.split("@", 1)[0].lower(), arg.strip()))
        return out


def make_notifier(settings: Settings, client: httpx.Client | None = None) -> Notifier:
    if settings.telegram_token and settings.telegram_chat_id:
        return TelegramNotifier(settings.telegram_token, settings.telegram_chat_id, client or make_client())
    return NullNotifier()


def handle_command(cmd: str, arg: str, engine: EngineControl) -> str:
    if cmd == "/status":
        return engine.status()
    if cmd == "/positions":
        return engine.positions()
    if cmd == "/pause":
        engine.pause()
        return "paused"
    if cmd == "/resume":
        engine.resume()
        return "resumed"
    if cmd == "/kill":
        engine.kill()
        return "kill switch ON — flattening"
    return HELP
