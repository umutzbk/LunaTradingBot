import json

import httpx

from luna.config import load_settings
from luna.notify.heartbeat import ping
from luna.notify.telegram import (
    NullNotifier,
    TelegramNotifier,
    handle_command,
    make_notifier,
)


class FakeEngine:
    def __init__(self):
        self.flags = {"kill_switch": False, "paused": False}
        self.flattened = False

    def status(self) -> str:
        return "status-ok"

    def positions(self) -> str:
        return "no positions"

    def pause(self) -> None:
        self.flags["paused"] = True

    def resume(self) -> None:
        self.flags["kill_switch"] = False
        self.flags["paused"] = False

    def kill(self) -> None:
        self.flags["kill_switch"] = True
        self.flattened = True


def _updates(*msgs):
    return {
        "ok": True,
        "result": [
            {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}} for uid, chat, text in msgs
        ],
    }


def _notifier(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TelegramNotifier("TOK", "42", client)


def test_ignores_foreign_chat():
    def handler(req):
        return httpx.Response(200, json=_updates((1, 999, "/kill"), (2, 42, "/status")))

    assert _notifier(handler).commands() == [("/status", "")]


def test_offset_tracking_and_parsing():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if len(seen) == 1:
            return httpx.Response(200, json=_updates((5, 42, "/pause@LunaBot now"), (7, 42, "hello")))
        return httpx.Response(200, json={"ok": True, "result": []})

    n = _notifier(handler)
    assert n.commands() == [("/pause", "now")]
    n.commands()
    assert "offset" not in seen[0] or seen[0]["offset"] == "0"
    assert seen[1]["offset"] == "8"


def test_send_posts_truncated_plain_text():
    got = {}

    def handler(req):
        got["url"] = str(req.url)
        got["body"] = json.loads(req.content)
        return httpx.Response(200, json={"ok": True})

    _notifier(handler).send("x" * 5000)
    assert got["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert got["body"]["chat_id"] == "42"
    assert len(got["body"]["text"]) == 4000


def test_network_errors_swallowed():
    def handler(req):
        raise httpx.ConnectError("down")

    n = _notifier(handler)
    n.send("hi")
    assert n.commands() == []


def test_kill_sets_flag_and_flattens():
    e = FakeEngine()
    assert handle_command("/kill", "", e) == "kill switch ON — flattening"
    assert e.flags["kill_switch"] and e.flattened


def test_resume_clears_kill():
    e = FakeEngine()
    handle_command("/kill", "", e)
    handle_command("/pause", "", e)
    assert handle_command("/resume", "", e) == "resumed"
    assert e.flags == {"kill_switch": False, "paused": False}


def test_other_commands():
    e = FakeEngine()
    assert handle_command("/status", "", e) == "status-ok"
    assert handle_command("/positions", "", e) == "no positions"
    assert handle_command("/pause", "", e) == "paused"
    out = handle_command("/nope", "", e)
    for c in ("/status", "/positions", "/pause", "/resume", "/kill"):
        assert c in out


def test_null_notifier_when_unconfigured():
    s = load_settings()
    s = s.model_copy(update={"telegram_token": None, "telegram_chat_id": None})
    n = make_notifier(s)
    assert isinstance(n, NullNotifier)
    n.send("x")
    assert n.commands() == []
    s2 = s.model_copy(update={"telegram_token": "t", "telegram_chat_id": "1"})
    assert isinstance(
        make_notifier(s2, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))),
        TelegramNotifier,
    )


def test_ping_never_raises():
    calls = []

    def ok(req):
        calls.append(str(req.url))
        return httpx.Response(200)

    def boom(req):
        raise httpx.ConnectError("x")

    ping(httpx.Client(transport=httpx.MockTransport(ok)), "https://hc.example/abc")
    assert calls == ["https://hc.example/abc"]
    ping(httpx.Client(transport=httpx.MockTransport(boom)), "https://hc.example/abc")
    ping(httpx.Client(transport=httpx.MockTransport(ok)), None)
    assert len(calls) == 1


def test_malformed_update_does_not_drop_the_rest():
    def handler(req):
        if req.url.params.get("offset") == "0":
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/status"}},
                {"message": {"chat": {"id": 42}, "text": "/kill"}},  # no update_id
                {"update_id": 3, "message": "not-a-dict"},
                {"update_id": 4, "message": {"chat": {"id": 42}, "text": "/positions"}},
            ]})
        return httpx.Response(200, json={"ok": True, "result": []})

    seen = []
    n = _notifier(lambda req: (seen.append(dict(req.url.params)), handler(req))[1])
    assert n.commands() == [("/status", ""), ("/positions", "")]
    n.commands()
    assert seen[1]["offset"] == "5"  # advanced per update, past the malformed ones


def test_targeted_commands_act_only_on_own_account():
    for cmd, flag in (("/kill", "kill_switch"), ("/pause", "paused")):
        for arg, acts in (("", True), ("perp", True), ("ALL", True), ("spot", False)):
            e = FakeEngine()
            out = handle_command(cmd, arg, e, account="perp")
            assert e.flags[flag] is acts, (cmd, arg)
            assert (out is None) is (not acts)
    e = FakeEngine()
    e.flags["paused"] = True
    assert handle_command("/resume", "spot", e, account="perp") is None and e.flags["paused"]
    assert handle_command("/resume", "perp", e, account="perp") == "resumed" and not e.flags["paused"]
    e = FakeEngine()
    out = handle_command("/kill", "bogus", e, account="perp")
    assert "unknown target" in out and not e.flags["kill_switch"]
    assert handle_command("/status", "spot", e, account="perp") == "status-ok"  # not targeted
