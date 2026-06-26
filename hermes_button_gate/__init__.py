"""button-gate — general HITL choice gate as Slack Block Kit buttons.

Post one or more groups of options (each option an optional image + button)
and block the agent until the user clicks a pick for every group. A generic
alternative to the core ``clarify`` tool, which on Slack falls back to a
numbered-text list (type "2") and can't show images, >4 options, or multiple
independent picks in one message. Any HITL choice can use it; the POV content
pipeline is one consumer (candidate pick / approve / a-b-per-cut).

  * N image options per group (not just A/B).
  * "groups" model — each group is one independent pick:
      - single group, N options          → single-select gate
      - single group, approve/revise      → approval gate
      - many groups, 2 options each       → one pick per row (e.g. a/b per cut)
  * Lock-after-pick: clicking resolves that group and ``chat_update``s the
    message so its buttons disappear (no re-click / no double-resolve).
  * Stale-button guard: each call gets a fresh ``token``; clicks whose token
    is no longer pending are ignored, so a leftover button from an earlier
    gate can't resolve a later one.

Bridge mechanism (works in gateway mode unlike the CLI-only
``inject_message``): the sync tool blocks on a ``threading.Event`` keyed by
token; the async Slack action handler (gateway loop) sets results and the
Event when every group is resolved.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger("button-gate")

# token -> gate entry. See _button_gate for shape.
_PENDING: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

_MAX_BLOCKS = 50  # Slack hard limit per message.


def _j(obj: Any) -> str:
    """Tool handlers must return a JSON string (Hermes convention)."""
    import json
    return json.dumps(obj, ensure_ascii=False)


def _wait_with_heartbeat(event: threading.Event, timeout: float) -> bool:
    """Block until ``event`` is set or ``timeout`` elapses, polling in 1-second
    slices and touching the activity callback each idle slice.

    A flat ``event.wait(timeout=1800)`` blocks the agent thread with zero
    activity touches, so the gateway's inactivity watchdog kills the blocked
    tool mid-wait. Mirrors ``clarify_gateway.wait_for_response``. Returns True
    if the event fired, False on timeout.
    """
    try:
        from tools.environments.base import touch_activity_if_due
    except Exception:  # pragma: no cover - optional / non-gateway contexts
        touch_activity_if_due = None

    deadline = time.monotonic() + max(timeout, 0.0)
    state = {"last_touch": time.monotonic(), "start": time.monotonic()}
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if event.wait(timeout=min(1.0, remaining)):
            return True
        if touch_activity_if_due is not None:
            touch_activity_if_due(state, "waiting for button-gate pick")

_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "Header text shown above the gate (e.g. '인물 골라줘').",
        },
        "groups": {
            "type": "array",
            "description": (
                "Independent picks. ONE group = single-select gate (gate 1/2). "
                "MANY groups = one pick per group (gate 3: one group per cut)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Unique id for this pick (e.g. 'choice', 'c1').",
                    },
                    "label": {
                        "type": "string",
                        "description": "Optional row label shown above this group's options.",
                    },
                    "options": {
                        "type": "array",
                        "description": "Buttons for this group; each may carry one image.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Button text (e.g. '1', 'A', '승인')."},
                                "value": {"type": "string", "description": "Value returned when picked."},
                                "image_url": {"type": "string", "description": "Public image URL (Higgsfield CDN ok)."},
                            },
                            "required": ["label", "value"],
                        },
                    },
                },
                "required": ["key", "options"],
            },
        },
        "timeout_sec": {
            "type": "integer",
            "description": "How long to block waiting for all picks (default 1800).",
        },
    },
    "required": ["groups"],
}


def _render_blocks(token: str) -> List[Dict[str, Any]]:
    """Build the message blocks authoritatively from current pending state.

    Resolved groups render as a locked '✅ picked' line (buttons gone);
    pending groups render their option images + buttons. Rebuilding the whole
    message on every update makes concurrent clicks race-tolerant — each
    chat_update reflects the full current state, not a stale body snapshot.
    """
    entry = _PENDING[token]
    spec = entry["spec"]
    results: Dict[str, str] = entry["results"]
    blocks: List[Dict[str, Any]] = []

    question = spec.get("question") or "선택해줘"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{question}*"}})

    for g in spec["groups"]:
        key = g["key"]
        glabel = g.get("label") or key
        options = g["options"]

        if key in results:
            chosen_val = results[key]
            chosen = next((o for o in options if o["value"] == chosen_val), None)
            chosen_label = chosen["label"] if chosen else chosen_val
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"✅ *{glabel}*: {chosen_label}"},
            })
            if chosen and chosen.get("image_url"):
                blocks.append({
                    "type": "image",
                    "image_url": chosen["image_url"],
                    "alt_text": str(chosen_label)[:1990],
                })
            continue

        if g.get("label"):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{glabel}*"}})
        for o in options:
            if o.get("image_url"):
                blocks.append({
                    "type": "image",
                    "image_url": o["image_url"],
                    "alt_text": str(o["label"])[:1990],
                    "title": {"type": "plain_text", "text": str(o["label"])[:1990]},
                })
        elements = []
        for i, o in enumerate(options):
            elements.append({
                "type": "button",
                "text": {"type": "plain_text", "text": str(o["label"])[:75]},
                "action_id": f"gate_pick:{key}:{i}",
                "value": f"{token}|{key}|{o['value']}",
            })
        blocks.append({"type": "actions", "block_id": f"gateb:{token}:{key}", "elements": elements})

    return blocks


def _channel_and_thread(session_key: str) -> tuple[Optional[str], Optional[str]]:
    """Parse 'agent:main:slack:<type>:<channel>:<thread_ts?>' -> (channel, thread_ts)."""
    parts = session_key.split(":")
    if "slack" not in parts:
        return None, None
    si = parts.index("slack")
    if len(parts) < si + 3:
        return None, None
    channel = parts[si + 2]
    thread_ts = next(
        (p for p in parts[si + 3:] if "." in p and p.replace(".", "").isdigit()),
        None,
    )
    return channel, thread_ts


def _button_gate(args: Dict[str, Any], **_: Any) -> str:
    """Post image options as buttons, block until every group is picked.

    Hermes dispatches tool handlers as ``handler(args_dict)`` (a single
    positional dict), and expects a JSON string back — not keyword args and
    not a dict. Mirror the built-in plugin convention.
    """
    if not isinstance(args, dict):
        args = {}
    question = args.get("question") or ""
    groups = args.get("groups")
    timeout_sec = args.get("timeout_sec", 1800)

    if not groups:
        return _j({"error": "no groups provided"})
    keys = [g.get("key") for g in groups]
    if not all(keys) or len(set(keys)) != len(keys):
        return _j({"error": "each group needs a unique non-empty 'key'"})
    for g in groups:
        if not g.get("options"):
            return _j({"error": f"group {g.get('key')!r} has no options"})

    try:
        from tools.approval import get_current_session_key
    except Exception as exc:  # pragma: no cover
        return _j({"error": f"cannot import get_current_session_key: {exc}"})

    session_key = get_current_session_key()
    channel, thread_ts = _channel_and_thread(session_key)
    if not channel:
        # session_key is 'agent:main:<platform>:<chat_type>:...'.
        parts = session_key.split(":")
        platform = parts[2] if len(parts) > 2 else "unknown"
        if platform != "slack":
            return _j({"error": (
                f"button_gate currently supports Slack only; this is a "
                f"{platform!r} session. Receiving button clicks on "
                f"Discord/Telegram needs a Hermes core interactive-handler "
                f"hook that does not exist yet (only register_slack_action_handler "
                f"is exposed to plugins)."
            )})
        return _j({"error": f"could not parse slack channel (session_key={session_key!r})"})

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return _j({"error": "SLACK_BOT_TOKEN not in env"})

    gate_token = uuid.uuid4().hex[:8]
    event = threading.Event()
    expected = {g["key"] for g in groups}
    entry = {
        "event": event,
        "results": {},
        "expected": expected,
        "spec": {"question": question, "groups": groups},
        "channel": channel,
        "message_ts": None,
    }
    with _lock:
        _PENDING[gate_token] = entry
        blocks = _render_blocks(gate_token)

    if len(blocks) > _MAX_BLOCKS:
        with _lock:
            _PENDING.pop(gate_token, None)
        return _j({
            "error": f"too many blocks ({len(blocks)} > {_MAX_BLOCKS}); "
            "split into fewer cuts per gate call"
        })

    try:
        from slack_sdk import WebClient

        resp = WebClient(token=token).chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=question or "게이트",
            blocks=blocks,
        )
    except Exception as exc:
        with _lock:
            _PENDING.pop(gate_token, None)
        logger.exception("button_gate: post failed")
        return _j({"error": f"post failed: {exc}"})

    with _lock:
        entry["message_ts"] = resp.get("ts")

    resolved = _wait_with_heartbeat(event, timeout_sec)
    with _lock:
        entry = _PENDING.pop(gate_token, {})
    picks = entry.get("results", {})
    complete = resolved and set(picks) >= expected
    out: Dict[str, Any] = {"picks": picks, "complete": complete}
    if not complete:
        out["missing"] = sorted(expected - set(picks))
        out["note"] = f"timeout after {timeout_sec}s — not all groups picked"
    return _j(out)


async def _on_pick(ack: Any, _body: Any, action: Any) -> None:
    """Slack action handler — resolves a group's pick and locks its buttons.

    Ignores ``_body`` (the click snapshot) on purpose: channel/ts/blocks are
    rebuilt from the authoritative ``_PENDING`` state so concurrent clicks
    don't clobber each other with stale message snapshots.
    """
    await ack()
    raw = (action or {}).get("value", "")
    try:
        gate_token, group_key, option_value = raw.split("|", 2)
    except ValueError:
        return

    with _lock:
        entry = _PENDING.get(gate_token)
        if entry is None:          # stale: from an expired/earlier gate
            return
        if group_key in entry["results"]:   # idempotent: already picked
            return
        if group_key not in entry["expected"]:
            return
        entry["results"][group_key] = option_value
        done = set(entry["results"]) >= entry["expected"]
        channel = entry["channel"]
        message_ts = entry["message_ts"]
        blocks = _render_blocks(gate_token)
        if done:
            entry["event"].set()

    logger.info("button_gate pick: token=%s group=%s value=%s done=%s",
                gate_token, group_key, option_value, done)

    if not message_ts:
        return  # post hadn't recorded ts yet; pick is still captured
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return
    try:
        from slack_sdk.web.async_client import AsyncWebClient

        await AsyncWebClient(token=token).chat_update(
            channel=channel, ts=message_ts, text="게이트", blocks=blocks,
        )
    except Exception:
        logger.exception("button_gate: chat_update failed")


_DESCRIPTION = (
    "Show choice options as Slack image buttons and block until the user "
    "picks. Pass 'groups': ONE group for a single-select gate (pick one of "
    "N, or approve/revise), MANY groups for one pick each (e.g. a/b per "
    "row). Each option may carry an image_url (Higgsfield CDN ok). Returns "
    "{picks:{group_key:value}, complete:bool}. Prefer this over the "
    "numbered-text 'clarify' tool when options are visual, exceed 4, or "
    "need several independent picks in one message."
)


def register(ctx: Any) -> None:
    import re

    # Hermes' registry treats ``schema`` as the full OpenAI function spec
    # ({name, description, parameters}), not the bare parameter object: it
    # builds the model-facing tool as {"function": {**schema, "name": ...}}.
    # Passing only the param body leaves no "parameters" key, so the schema
    # sanitizer collapses it to empty {} and the model can't fill 'groups'.
    ctx.register_tool(
        name="button_gate",
        toolset="gate",
        schema={
            "name": "button_gate",
            "description": _DESCRIPTION,
            "parameters": _SCHEMA,
        },
        handler=_button_gate,
        is_async=False,
        description=_DESCRIPTION,
        emoji="🗳️",
    )
    ctx.register_slack_action_handler(re.compile(r"^gate_pick:"), _on_pick)
    logger.info("button-gate registered (tool button_gate + gate_pick:* handler)")
