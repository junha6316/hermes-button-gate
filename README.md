# hermes-button-gate

A visual, multi-pick **human-in-the-loop (HITL) gate** for [Hermes Agent](https://github.com/NousResearch/hermes-agent), delivered as Slack Block Kit image buttons.

It adds one tool — `button_gate` — that posts choices as clickable buttons (with optional images) and **blocks the agent until the user picks**. It's a drop-in upgrade over the built-in `clarify` tool for Slack/gateway sessions.

## Why

On Slack, the core `clarify` tool degrades to a numbered-text prompt ("type `2`"). It can't:

- show **images** alongside options,
- offer **more than 4** options,
- ask for **several independent picks** in one message.

`button_gate` does all three. One call can be a single-select gate, an approve/revise gate, or a per-row A/B gate.

```
single group, N options        → single-select gate
single group, approve/revise   → approval gate
many groups, 2 options each     → one pick per row (e.g. A/B per cut)
```

Each option can carry a public `image_url`, so it's well suited to picking between generated images (the gate this was originally built for: an image-content pipeline's candidate-pick / approve / A-B-per-cut steps).

## Scope (read this)

**Slack only, gateway mode.** The handler posts via the Slack Web API and resolves picks through Hermes' Slack action-handler hook. It does **not** support Discord/Telegram or CLI sessions — in a non-Slack session the tool returns a clear error instead of blocking.

Generalizing to Discord/Telegram is **blocked on Hermes core**, not on this plugin: `PluginContext` only exposes `register_slack_action_handler`. The Telegram and Discord adapters route interaction callbacks through closed dispatchers, so a plugin can send buttons but can't receive the clicks. A cross-platform `register_interactive_handler(platform, pattern, callback)` extension point in Hermes core would unblock it.

The bridge uses a `threading.Event` keyed by a per-call token, so it works in gateway mode (unlike the CLI-only `inject_message`). Buttons lock after a pick (`chat_update` removes them), and a stale-token guard prevents a leftover button from an earlier gate resolving a later one.

## Install

```bash
pip install hermes-button-gate          # once published
hermes plugins enable button-gate
```

Or for local development, drop the package into `~/.hermes/plugins/` (directory discovery) or `pip install -e .` from a clone (entry-point discovery via the `hermes_agent.plugins` group).

### Requirements

- Hermes Agent with the **Slack platform** enabled and running in gateway mode.
- `SLACK_BOT_TOKEN` in the environment (Hermes' Slack adapter already sets this).
- The Slack app must have **interactivity enabled** so button clicks reach the gateway.

## Usage

The agent calls the tool; you don't call it by hand. Schema:

```jsonc
{
  "question": "인물 골라줘",          // optional header
  "groups": [                          // required: one entry per independent pick
    {
      "key": "choice",                 // unique id, returned as the result key
      "label": "후보",                  // optional row label
      "options": [
        { "label": "A", "value": "cand_a", "image_url": "https://.../a.png" },
        { "label": "B", "value": "cand_b", "image_url": "https://.../b.png" }
      ]
    }
  ],
  "timeout_sec": 1800                   // optional, default 1800
}
```

Returns:

```json
{ "picks": { "choice": "cand_a" }, "complete": true }
```

On timeout, `complete` is `false` and the response includes `missing` (unpicked group keys).

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

Tests are stdlib + `pytest` only and make no network calls (Slack and Hermes internals are stubbed / avoided), so they run in a bare environment.

## License

MIT — see [LICENSE](LICENSE).
