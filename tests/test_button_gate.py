"""Tests for hermes_button_gate.

Hermetic: no network, no Slack, no Hermes runtime. We exercise the pure
validation / parsing / rendering logic and the async pick resolver along its
no-network path (message_ts unset → returns before any Slack call), so
``slack-sdk`` need not be installed to run these.
"""

import asyncio
import json
import sys
import threading
import types

import pytest

import hermes_button_gate as bg


# --- session-key parsing --------------------------------------------------

@pytest.mark.parametrize(
    "session_key, expected",
    [
        ("agent:main:slack:channel:C123", ("C123", None)),
        ("agent:main:slack:channel:C123:1700000000.000100", ("C123", "1700000000.000100")),
        ("agent:main:cli:local", (None, None)),          # not a slack session
        ("agent:main:slack:channel", (None, None)),       # too short
    ],
)
def test_channel_and_thread(session_key, expected):
    assert bg._channel_and_thread(session_key) == expected


# --- input validation (returns before any Hermes/Slack import) ------------

def _err(result_json):
    return json.loads(result_json).get("error")


def test_button_gate_rejects_no_groups():
    assert "no groups" in _err(bg._button_gate({}))


def test_button_gate_rejects_duplicate_keys():
    args = {"groups": [
        {"key": "x", "options": [{"label": "a", "value": "a"}]},
        {"key": "x", "options": [{"label": "b", "value": "b"}]},
    ]}
    assert "unique" in _err(bg._button_gate(args))


def test_button_gate_rejects_group_without_options():
    args = {"groups": [{"key": "x", "options": []}]}
    assert "no options" in _err(bg._button_gate(args))


# --- block rendering ------------------------------------------------------

def _make_entry(token, spec, results=None):
    bg._PENDING[token] = {
        "event": threading.Event(),
        "results": results or {},
        "expected": {g["key"] for g in spec["groups"]},
        "spec": spec,
        "channel": "C123",
        "message_ts": None,
    }


def test_render_blocks_pending_has_button_with_token_value():
    token = "tok1"
    spec = {"question": "pick", "groups": [
        {"key": "g1", "options": [
            {"label": "A", "value": "va"},
            {"label": "B", "value": "vb"},
        ]},
    ]}
    _make_entry(token, spec)
    try:
        blocks = bg._render_blocks(token)
    finally:
        bg._PENDING.pop(token, None)

    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    buttons = actions[0]["elements"]
    assert [e["value"] for e in buttons] == [f"{token}|g1|va", f"{token}|g1|vb"]
    assert buttons[0]["action_id"] == "gate_pick:g1:0"


def test_render_blocks_resolved_group_is_locked():
    token = "tok2"
    spec = {"question": "pick", "groups": [
        {"key": "g1", "options": [{"label": "A", "value": "va"}]},
    ]}
    _make_entry(token, spec, results={"g1": "va"})
    try:
        blocks = bg._render_blocks(token)
    finally:
        bg._PENDING.pop(token, None)

    # No buttons once resolved; a ✅ section is shown instead.
    assert not any(b["type"] == "actions" for b in blocks)
    assert any("✅" in b.get("text", {}).get("text", "") for b in blocks)


# --- async pick resolver --------------------------------------------------

class _Ack:
    def __init__(self):
        self.called = False

    async def __call__(self):
        self.called = True


def _run(coro):
    return asyncio.run(coro)


def test_on_pick_resolves_group_and_sets_event():
    token = "tok3"
    spec = {"question": "pick", "groups": [
        {"key": "g1", "options": [{"label": "A", "value": "va"}]},
    ]}
    _make_entry(token, spec)  # message_ts=None → no Slack call path
    ack = _Ack()
    try:
        _run(bg._on_pick(ack, {}, {"value": f"{token}|g1|va"}))
        entry = bg._PENDING[token]
        assert ack.called
        assert entry["results"] == {"g1": "va"}
        assert entry["event"].is_set()
    finally:
        bg._PENDING.pop(token, None)


def test_on_pick_waits_for_all_groups():
    token = "tok4"
    spec = {"question": "pick", "groups": [
        {"key": "g1", "options": [{"label": "A", "value": "va"}]},
        {"key": "g2", "options": [{"label": "B", "value": "vb"}]},
    ]}
    _make_entry(token, spec)
    try:
        _run(bg._on_pick(_Ack(), {}, {"value": f"{token}|g1|va"}))
        entry = bg._PENDING[token]
        assert entry["results"] == {"g1": "va"}
        assert not entry["event"].is_set()  # g2 still pending

        _run(bg._on_pick(_Ack(), {}, {"value": f"{token}|g2|vb"}))
        assert entry["event"].is_set()
    finally:
        bg._PENDING.pop(token, None)


def test_on_pick_ignores_stale_token():
    # No _PENDING entry for this token → must return without error.
    _run(bg._on_pick(_Ack(), {}, {"value": "ghost|g1|va"}))


def test_on_pick_is_idempotent():
    token = "tok5"
    spec = {"question": "pick", "groups": [
        {"key": "g1", "options": [
            {"label": "A", "value": "va"},
            {"label": "B", "value": "vb"},
        ]},
    ]}
    _make_entry(token, spec)
    try:
        _run(bg._on_pick(_Ack(), {}, {"value": f"{token}|g1|va"}))
        _run(bg._on_pick(_Ack(), {}, {"value": f"{token}|g1|vb"}))  # second click ignored
        assert bg._PENDING[token]["results"] == {"g1": "va"}
    finally:
        bg._PENDING.pop(token, None)


def test_on_pick_malformed_value_is_ignored():
    _run(bg._on_pick(_Ack(), {}, {"value": "not-a-valid-triplet"}))


# --- timeout path (complete=false, missing populated) ---------------------

def test_button_gate_timeout_reports_incomplete(monkeypatch):
    """No pick before timeout → complete=false with the unpicked keys listed.

    The Slack runtime is stubbed so this stays hermetic: get_current_session_key
    (from tools.approval) yields a slack session, and slack_sdk.WebClient's post
    is a no-op returning a ts. timeout_sec=0 makes the wait return immediately.
    """
    approval = types.ModuleType("tools.approval")
    approval.get_current_session_key = lambda: "agent:main:slack:channel:C123"
    tools_pkg = types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)

    slack_sdk = types.ModuleType("slack_sdk")

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def chat_postMessage(self, *a, **k):
            return {"ts": "1700000000.000200"}

    slack_sdk.WebClient = _FakeClient
    monkeypatch.setitem(sys.modules, "slack_sdk", slack_sdk)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    args = {"groups": [{"key": "g1", "options": [{"label": "A", "value": "va"}]}],
            "timeout_sec": 0}
    out = json.loads(bg._button_gate(args))
    assert out["complete"] is False
    assert out["picks"] == {}
    assert out["missing"] == ["g1"]
