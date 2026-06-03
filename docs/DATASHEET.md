# openagent-frontend — Datasheet

> Reference document for building on top of, or integrating with, openagent-frontend.
> Intended audience: developers and integrators needing to understand what openagent-frontend
> is, what it owns, and how it talks to the rest of the system.

---

## Quick Reference

| Item | Value |
|---|---|
| Role | User-facing chat UI for OpenAgent |
| Framework | Streamlit |
| Language | Python 3.11 |
| Protocol out | HTTP/1.1 + Server-Sent Events (SSE consumer) |
| Protocol in (user) | HTTPS (Streamlit web UI) |
| Host port | `8000` |
| Container port | `8501` (Streamlit default) |
| Backend | openagent-api (`OPENAGENT_API_URL`, typically `:8001`) |
| Auth out | `X-API-Key: OPENAGENT_API_KEY` on every `/chat` and `/health` call |
| Session store | In-memory `st.session_state`, per browser tab |
| Persistent store | None (stateless across browser refresh) |
| System prompt | Owned by openagent-api — not present in this repo |
| SSE decoder | `src/frontend/sse_decoder.py` |
| Version | 1.0.0 |

---

## Overview

`openagent-frontend` is the **user interface layer** of the OpenAgent system. It is a lean Streamlit web app that renders the chat experience, tracks in-session conversation state, and consumes a streaming response from `openagent-api`. That is the entire job.

It does not own the agent persona. It does not authenticate to the model layer. It does not load a model, run inference, host a database, log events, or assemble the OpenAI messages list with a system prompt. `openagent-api` owns those concerns.

Everything that makes the agent *look and feel* like the agent from the user's perspective — the chat bubbles, the streaming reasoning expander, the error banners, the health gate — happens in this service. Everything that makes the agent *be* the agent on the wire (the persona, the auth chain, the messages-list construction, the SSE relay) happens upstream in `openagent-api`.

This buys clean separation of concerns: the frontend is a UI, `openagent-api` is the gateway, `openagent-infra` is the model proxy, and the compute provider handles the inference layer.

---

## Where This Service Fits

```text
┌──────────────────────────────────────────────────────────────┐
│                    Browser (user)                            │
│                 http://localhost:8000                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS / WebSocket (Streamlit)
                            │ Host 8000 → Container 8501
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-frontend    ←── YOU ARE READING THIS DATASHEET  │
│    Streamlit on :8501 inside container                       │
│                                                              │
│    Owns: chat UI, in-session state, reasoning-format         │
│          display policy, health gate, error display          │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP POST /chat   (SSE response)
                            │ HTTP GET  /health (gated polling)
                            │ X-API-Key: OPENAGENT_API_KEY
                            │ Target: OPENAGENT_API_URL
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-api    (separate repo, separate Docker stack)   │
│    FastAPI gateway on :8001                                  │
│                                                              │
│    Owns: persona, auth chain,                                │
│          OpenAI messages list construction,                  │
│          SSE relay, /health proxy                            │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP POST /chat (SSE response)
                            │ X-API-Key: INFRA_API_KEY
                            │ Target: OPENAGENT_INFRA_URL
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    openagent-infra    (separate repo, separate Docker stack) │
│    FastAPI proxy on :8002 → BYOC Compute Provider            │
│                                                              │
│    Owns: model proxy, reasoning_effort default,              │
│          PROVIDER_API_KEY, "Reasoning: <level>" injection    │
│    Stateless — full messages list sent on every request      │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS POST
                            │ Authorization: Bearer PROVIDER_API_KEY
                            ▼
┌──────────────────────────────────────────────────────────────┐
│    BYOC Compute Provider                                     │
│    Base model                                                │
│    Control layer model                                       │
└──────────────────────────────────────────────────────────────┘
```

**Port topology:**
```text
User → openagent-frontend (:8000) → openagent-api (:8001) → openagent-infra (:8002) → BYOC Provider
```

`openagent-frontend` is the **only** client of `openagent-api` in the basic system. `openagent-api` is the **only** thing `openagent-frontend` talks to over HTTP. The frontend has no knowledge of `openagent-infra` or the compute provider and never sees their auth credentials.

---

## What This Service Owns

A strict list of responsibilities that live inside `openagent-frontend` and nowhere else. Other services should defer to this service for these concerns.

### 1. Chat UI rendering

The visual surface — chat bubbles, the collapsible "Show thinking" expander, the streaming token rendering with the cursor glyph, the input field, the page header, the divider, the reasoning-effort toggle — all live here. Streamlit primitives (`st.chat_message`, `st.expander`, `st.markdown`, `st.empty`, `st.chat_input`, `st.radio`) drive the layout.

### 2. In-session conversation state

Conversation history for the current browser tab lives in `st.session_state.messages`, which is a list of `{"role", "content"}` dicts. The reasoning chain is rendered live during streaming and not persisted back into history. State is:

- **Ephemeral** — destroyed when the tab closes or Streamlit reruns a fresh session
- **Client-side only** — never sent to or stored in any backend service (yet)
- **The source of truth for the UI** during a single session
- **Sent in full** to `openagent-api` on every `/chat` call as user/assistant turns only — `openagent-api` prepends the system message server-side

Closing the tab loses everything.

### 3. Reasoning-format display policy

How the reasoning chain is presented to the user is a UX decision that lives at this layer. `openagent-frontend` chose a collapsible `st.expander("🧠 Show thinking")` rendered above the main chat bubble. The reasoning streams into the expander in real time as `delta.reasoning` tokens arrive; the visible answer streams into the main bubble as `delta.content` tokens arrive.

Note the boundary: `openagent-frontend` chooses the *display policy*. It does NOT do the *parsing* — that is owned by `sse_decoder.py`, which yields typed events with `kind="reasoning"` or `kind="content"` and lets this layer route them.

### 4. Health gate

On startup, `openagent-frontend` polls `GET {OPENAGENT_API_URL}/health` every 3 seconds in a blocking while-loop until the response body's `status` field is `"ok"`. The chat input is not rendered until this gate clears. `openagent-api` translates the upstream's `degraded` (e.g., worker cold-starting) into `loading` for us, so this layer only needs to recognise three values: `ok`, `loading`, `unreachable`.

States the gate handles:

- `{"status": "ok"}` → gate clears, UI unlocks
- `{"status": "loading"}` → live "⏳ The upstream model is starting up" banner
- `{"status": "unreachable"}` → live "🔌 openagent-api is up but cannot reach the upstream model" banner
- Connection error to openagent-api → "🔌 Cannot reach openagent-api" banner
- Any other response → "⚠️ Unknown /health status" banner

This is a UI-lock concern, not a health-checking concern. The actual health logic lives upstream.

### 5. Error display (presentation layer)

`openagent-frontend` owns the user-facing error presentation for any failure on the wire. Emoji prefixes give operators a glanceable signal:

| Prefix | Class | Source |
|---|---|---|
| 🔌 | Connection / network | TCP connect failed, mid-stream disconnect, HTTP 502 |
| ⏳ | Timeout / loading | Connect timeout, HTTP 503, HTTP 504 |
| 🔐 | Auth | HTTP 401 |
| ⚠️ | Request validation | HTTP 400, HTTP 422 |
| ❌ | Unexpected | Anything not matched above |

`openagent-frontend` does not classify upstream errors — `openagent-api` normalises everything to a consistent set of HTTP codes and in-band SSE error events. The frontend just maps each code to a prefix and a user-facing message.

---

## What This Service Does NOT Own

Explicit non-responsibilities.

- **The persona / system prompt** → owned by `openagent-api`. `openagent-api` loads it once at startup and prepends it as the first system message on every `/chat` call.
- **OpenAI messages list construction** → owned by `openagent-api`. `openagent-frontend` sends only user/assistant turns.
- **Byte-level SSE parsing** → owned by `sse_decoder.py`. The `data:` prefix stripping, `[DONE]` sentinel detection, JSON decoding, and routing-by-delta-key all live there.
- **Auth boundary to the model layer** → owned by `openagent-api`. The frontend holds `OPENAGENT_API_KEY` only — the secret for the frontend↔openagent-api boundary. `INFRA_API_KEY` and `PROVIDER_API_KEY` live in their respective services and never reach this repo.
- **Model serving / inference** → owned by the BYOC Provider (proxied by `openagent-infra`).
- **Reasoning effort default** → owned by `openagent-infra`.
- **`Reasoning: <level>` injection into the system message** → owned by `openagent-infra`.

---

## API Reference

`openagent-frontend` does **not** expose an HTTP API to other services. It is a Streamlit web app, reached via browser. There is no `/api`, no REST surface, no callable endpoints.

The only externally-observable surface is the Streamlit app served at host port 8000. Services that need to interact with the system should integrate with `openagent-api` (the gateway).

---

## Outbound HTTP Contracts

`openagent-frontend` is a client of the following endpoints. These contracts are consumed, not provided. Full specs live in `openagent-api`'s datasheet.

### `POST {OPENAGENT_API_URL}/chat` — consumed

**Request:**
```text
POST /chat
Content-Type: application/json
X-API-Key: <OPENAGENT_API_KEY>

{
  "messages": [
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."},
    ...
    {"role": "user",      "content": "..."}
  ],
  "reasoning_effort": "medium"
}
```

The `messages` array contains user/assistant turns ONLY. No system message — `openagent-api` prepends the persona server-side.

The `reasoning_effort` field is optional. The frontend sends it when the user has selected a non-default value via the toggle; when the user keeps the toggle on "Default", the field is omitted so the backend applies its server-side default.

**Response:** `text/event-stream`

Each event is a JSON-encoded OpenAI ChatCompletion chunk. Chain-of-thought tokens stream first inside `choices[0].delta.reasoning`, then visible answer tokens inside `choices[0].delta.content`, then a final empty-delta chunk with `finish_reason: "stop"`, then the `[DONE]` sentinel.

`openagent-frontend` hands the raw line iterator to `sse_decoder.decode_sse_stream()`, which yields typed `SSEEvent` objects. The chat-rendering loop in app.py routes each event by kind to the appropriate UI surface.

**Mid-stream errors** are surfaced by `openagent-api` as in-band SSE events: `data: [ERROR upstream_status=503]

` followed by `data: [DONE]

`. sse_decoder.py recognises these and yields an `SSEEvent(kind="error", error=...)` to the rendering loop, which displays the error banner and stops consumption.

### `GET {OPENAGENT_API_URL}/health` — consumed

**Request:**
```text
GET /health
X-API-Key: <OPENAGENT_API_KEY>
```

**Response:** Always HTTP 200. Body:
```json
{
  "status": "ok" | "loading" | "unreachable",
  "openagent_api": {"version": "...", "identity_loaded": true},
  "openagent_infra": {"url": "...", "status": "...", "raw": {...}}
}
```

The frontend's gate-open loop reads the top-level `status` field only.

---

## State Model

### Per-browser-session state (in `st.session_state`)

| Key | Type | Lifetime | Purpose |
|---|---|---|---|
| `session_id` | `str` | Browser tab | Log correlation only. |
| `messages` | `list[dict]` | Browser tab | Full conversation display + payload source. Shape: `{"role": str, "content": str}`. |
| `model_ready` | `bool` | Browser tab | Health gate flag. Unlocks chat input when `True`. |
| `initialised` | `bool` | Browser tab | One-shot startup logging flag. |
| `reasoning_effort` | `str` | Browser tab | Currently-selected toggle label. |

### Persistent state

**None.** Closing the browser tab or restarting the container loses all state.

---

## Configuration

All runtime configuration is loaded from `.env` at the repository root via `python-dotenv` and `docker-compose`'s `env_file:` directive.

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENAGENT_API_URL` | Yes | `http://localhost:8001` | Base URL of openagent-api. No trailing slash. |
| `OPENAGENT_API_KEY` | Yes | — | Shared secret for `X-API-Key` header on `/chat` and `/health`. Must match `OPENAGENT_API_KEY` in openagent-api's `.env`. |

`OPENAGENT_API_URL` values by deployment topology:

| Scenario | Value |
|---|---|
| Everything on host, no Docker | `http://localhost:8001` |
| Frontend in Docker, openagent-api on host | `http://host.docker.internal:8001` |
| Both in Docker, shared external network | `http://openagent-api:8001` |
| External deployment | `https://api.your-domain.com` |

The default deployment uses `host.docker.internal:8001` because openagent-frontend and openagent-api live in separate Docker Compose stacks.

`OPENAGENT_API_KEY` is the FRONTEND ↔ OPENAGENT-API boundary key only. It is **not** the same as `INFRA_API_KEY` or `PROVIDER_API_KEY`.

---

## Container / Deployment

### Image

- **Base:** `python:3.11-slim`
- **Tag:** `openagent-frontend:1.0.0`
- **Container name:** `openagent-frontend`
- **Size (approximate):** ~450 MB (pure-Python)

### Build

```bash
# From repo root
docker-compose up -d --build
```

### Port mapping

- **Host port 8000 → Container port 8501** (Streamlit default internal port)
- Users open `http://localhost:8000`

### Volumes

None. The container is stateless (state lives in the browser).

### Restart policy

`unless-stopped`

---

## File Structure

```text
openagent-frontend/
├── docker/
│   └── frontend/
│       └── Dockerfile              # Python 3.11 slim + Streamlit
├── src/
│   └── frontend/
│       ├── app.py                  # The Streamlit UI
│       └── sse_decoder.py          # SSE / ChatCompletion chunk decoder
├── docs/
│   └── DATASHEET.md                # This document
├── docker-compose.yml              # Single-service compose
├── requirements.txt                # streamlit, requests, python-dotenv
├── .env                            # secrets — never committed
├── .env.example                    # template for .env
├── .dockerignore
├── .gitignore
└── README.md
```

---

## Integration Notes for Other Services

### For openagent-api (primary upstream)

`openagent-api` is the only service `openagent-frontend` talks to. Touchpoints:

- **`POST /chat`** — frontend sends user/assistant turns plus optional `reasoning_effort`. `openagent-api` prepends the system message and forwards upstream.
- **`GET /health`** — frontend polls during cold start; `openagent-api` proxies health.
- **Auth:** `X-API-Key: OPENAGENT_API_KEY` on every request.

The contract is documented in `openagent-api`'s datasheet. `openagent-frontend` does not need to know what `openagent-api` does internally.

---

## Design Decisions

### Why Streamlit?

Streamlit collapses "build a chat UI, style it, add streaming, manage session state, serve it over HTTP" into a single Python file with no JavaScript. The HTTP/SSE contract with `openagent-api` means the frontend can be swapped wholesale later without touching the backend.

### Why doesn't the frontend own the system prompt?

The persona belongs at the gateway — that's a backend concern, not a UI concern. Moving it upstream means a future mobile app or CLI gets the same identity without re-implementing it.

### Why is the SSE decoder a separate module?

`sse_decoder.py` was split out of app.py for three reasons:
1. It isolates JSON decoding from Streamlit rendering.
2. It's testable in isolation.
3. When the upstream chunk format changes, the change is contained to one file.

### Why is conversation history still in the frontend?

Because `openagent-api` is stateless across requests. The full message list is sent on every `/chat` call, so the frontend has to hold the history to send it.

### Why a blocking health-polling loop?

Streamlit is single-threaded and lacks native auto-refresh. A blocking `while` loop with `st.empty()` status updates is the simplest correct implementation to block until ready.

### Why does the frontend trust openagent-api's error normalisation?

Because `openagent-api` owns the upstream relationship. Re-classifying errors at the frontend would mean duplicating logic that already exists upstream.

### Why pure pass-through on `reasoning_effort`?

One source of truth. `openagent-infra`'s env var holds the default; `openagent-api` passes through; the frontend either sets a value or omits the field. Adding a frontend-side default would create two places to check when debugging.

### Why server-side state in openagent-api instead of here?

A UI layer is not the right place for cross-session state. `openagent-api` is the natural home for orchestration concerns because it already touches every request.

### Why port 8501 internal and 8000 external?

8501 is Streamlit's default — keeping it as the container's internal port means zero Streamlit config overrides. 8000 is the user-facing port in the port convention (`openagent-frontend:8000 → openagent-api:8001 → openagent-infra:8002`).

---

## License

Copyright © 2026 William McKeon.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
