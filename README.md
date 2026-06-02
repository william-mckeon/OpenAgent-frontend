# openagent-frontend

> **The OpenAgent user interface** — the lean Streamlit chat UI that sits in front of `openagent-api`.

---

## Overview

`openagent-frontend` is the **user interface layer** of the OpenAgent system. It is a lean Streamlit web app that renders the chat experience, tracks in-session conversation state, and consumes a streaming response from [`openagent-api`](../openagent-api). That is the entire job.

This repo is scoped to the UI only. It has no model, no persona, no inference code, no database, no auth backend. It does not own the agent identity—the persona is owned upstream by `openagent-api`. What lives here:

- The **Streamlit chat UI** that users talk to
- The **HTTP/SSE client** that streams responses from `openagent-api`
- The **JSON ChatCompletion chunk decoder** (`sse_decoder.py`) that turns the byte stream into typed events
- The **conversation state** held per browser session
- The **health gate** that locks the UI until the upstream model is ready
- The **error display** with the emoji prefixes (🔌 ⏳ 🔐 ⚠️ ❌)

The boundary with `openagent-api` is deliberately sharp: the frontend owns *how the agent looks and feels to the user*, the gateway owns *who the agent is, how requests are authenticated, and how the upstream stream is relayed*. The two communicate over a stable HTTP/SSE contract.

---

## Where This Fits

```text
openagent-os
│
├── openagent-infra      ← separate repo
│   └── Model proxy → BYOC Provider (port 8002)
│       Stateless proxy that forwards to compute providers
│
├── openagent-api        ← separate repo
│   └── FastAPI gateway (port 8001)
│       Owns the persona, auth chain, and SSE relay
│
├── openagent-frontend   ← YOU ARE HERE
│   └── Streamlit chat UI (port 8000)
│       Pure UI layer. Talks only to openagent-api.
│
└── openagent-logger     ← separate repo
    └── Structured event log (called by openagent-api)
