#!/usr/bin/env python3
# ============================================================================
# openagent-frontend - User Interface (Streamlit)
# Maintainer: William McKeon
# ============================================================================
#
# ROLE:
#   Lean Streamlit chat UI for OpenAgent. Talks to the openagent-api gateway
#   over HTTP/SSE. Renders the chat experience and tracks in-session state —
#   nothing else. Does not own the persona, does not assemble the OpenAI
#   messages list with a system prompt, does not authenticate to the model
#   layer, does not parse SSE bytes by hand.
#
# ARCHITECTURE:
#
#   Browser
#     │
#     │ HTTPS (Streamlit, port 8501 internal → 8000 host via compose)
#     ▼
#   openagent-frontend  ←── THIS FILE
#     │
#     │ POST /chat   (SSE stream, X-API-Key: OPENAGENT_API_KEY)
#     │ GET  /health (readiness poll, X-API-Key: OPENAGENT_API_KEY)
#     ▼
#   openagent-api (FastAPI gateway, port 8001)
#     │
#     │ POST /chat   (SSE stream, X-API-Key: INFRA_API_KEY)
#     │ GET  /health (operational state)
#     ▼
#   openagent-infra (FastAPI proxy, port 8002)
#     │
#     │ HTTPS POST  (Authorization: Bearer PROVIDER_API_KEY)
#     ▼
#   BYOC compute provider — base reasoning model
#
# OWNERSHIP BOUNDARY:
#
#   This file OWNS:
#     - Chat UI rendering (chat_message bubbles, expanders, placeholders)
#     - In-session conversation state (st.session_state.messages)
#     - Health gate (UI-lock concern based on openagent-api's /health)
#     - Error display (emoji-prefixed banners, presentation only)
#     - Reasoning-format display policy (collapsible "Show thinking" expander)
#     - Optional reasoning_effort UI toggle (Quick / Standard / Deep)
#
#   This file does NOT OWN (these are owned by openagent-api):
#     - The persona / system prompt                → openagent-api owns bio.txt
#     - OpenAI messages list construction          → openagent-api prepends system
#     - SSE byte-level parsing and JSON decoding   → sse_decoder.py owns it
#     - Auth boundary to the model layer           → openagent-api holds INFRA_API_KEY
#     - Upstream error classification              → openagent-api normalises HTTP codes
#
# CONVERSATION HISTORY OWNERSHIP:
#   openagent-api is stateless across requests. openagent-frontend holds the
#   full conversation in st.session_state.messages and sends the entire
#   history on every request as user/assistant turns only — NO system message.
#   The persona is prepended server-side by openagent-api; sending one from
#   the frontend would be dropped with a warning anyway, so we just don't.
#
#   Schema:
#     [
#       {"role": "user",      "content": <first user turn>},
#       {"role": "assistant", "content": <first model answer>},
#       ...
#       {"role": "user",      "content": <latest user turn>},
#     ]
#
#   The reasoning chain is rendered live during streaming but not stored back
#   into messages — it does not need to be replayed on subsequent reruns and
#   it is not part of the OpenAI schema.
#
#   TODO (deferred): the model has a finite context window. For long
#   conversations we will eventually need to truncate or summarise older
#   turns at this layer (or upstream).
#
# REASONING / ANSWER SPLIT:
#   The upstream model emits OpenAI ChatCompletion chunks with two distinct
#   delta channels:
#     - choices[0].delta.reasoning   → chain-of-thought tokens
#     - choices[0].delta.content     → visible answer tokens
#   sse_decoder.py decodes the JSON and yields typed SSEEvent objects. This
#   file routes "reasoning" events into a collapsible expander and "content"
#   events into the main chat bubble.
#
# HEALTH GATE:
#   Cold start (provider worker spin-up after scale-to-zero) can take minutes.
#   Warm path responds in seconds. The UI polls GET /health on openagent-api
#   and blocks the chat input until the endpoint returns {"status": "ok"}.
#   openagent-api translates upstream "degraded" → "loading" so this file
#   never has to know about the provider.
#
# STYLED ERROR HANDLING:
#   Emoji prefixes — the taxonomy is consistent because openagent-api
#   normalises upstream error codes into the same shape:
#     🔌  Connection / network errors  (502 Cannot reach openagent-api or upstream)
#     ⏳  Timeout / loading             (503 model loading, 504 timeout)
#     🔐  401                            (X-API-Key mismatch)
#     ⚠️  400 / 422                      (request validation)
#     ❌  Unexpected exceptions
# ============================================================================

import logging
import os
import time
import uuid
from typing import Dict, List, Optional

import requests
import streamlit as st
from dotenv import load_dotenv

# sse_decoder lives next to this file in src/frontend/. The decoder owns all
# the byte-level SSE protocol handling and JSON ChatCompletion chunk parsing —
# this file just consumes its yielded SSEEvent objects.
from sse_decoder import (
    decode_sse_stream,
    SSEEvent,
    KIND_REASONING,
    KIND_CONTENT,
    KIND_FINISH,
    KIND_ERROR,
    KIND_DONE,
    DECODER_VERSION,
)

# ============================================================================
# ENVIRONMENT
# ============================================================================

load_dotenv()

# ============================================================================
# PAGE CONFIGURATION
# ============================================================================
# Must be the FIRST Streamlit call in the script. Any st.* call before this
# will raise a StreamlitAPIException.

st.set_page_config(
    page_title="OpenAgent",
    page_icon="⚡",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
# Format deliberately matches openagent-api and openagent-infra so frontend
# and backend lines line up when tailing docker-compose logs across all three
# services. The named logger ("openagent.frontend") is the parent of
# sse_decoder's logger ("openagent.frontend.sse_decoder") so a single logging
# config covers both modules.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("openagent.frontend")

# ============================================================================
# CONFIGURATION
# ============================================================================
# All environment is read once at import time. See .env.example for the
# canonical list of supported variables.
#
# OPENAGENT_API_URL:
#   Base URL of the openagent-api service. No trailing slash — rstrip() guards
#   against a stray one from a misconfigured .env. openagent-api in turn
#   handles all communication with openagent-infra; this file never talks to
#   openagent-infra directly.
#
# OPENAGENT_API_KEY:
#   Shared secret sent on every /chat and /health call via the X-API-Key
#   header. Must match OPENAGENT_API_KEY in openagent-api's .env exactly. A
#   mismatch returns HTTP 401 which surfaces as a 🔐 banner in the UI.
#
#   This is the FRONTEND ↔ OPENAGENT-API boundary key. It is NOT the same as
#   INFRA_API_KEY (which lives in openagent-api and authenticates to
#   openagent-infra) or PROVIDER_API_KEY (which lives in openagent-infra and
#   authenticates to the compute provider). Defense in depth — see
#   openagent-api's security model documentation.

OPENAGENT_API_URL: str = os.getenv(
    "OPENAGENT_API_URL",
    "http://localhost:8001",
).rstrip("/")

OPENAGENT_API_KEY: str = os.getenv("OPENAGENT_API_KEY", "")

# Connect timeout (seconds) for both /chat and /health. The chat READ timeout
# is intentionally None — generation can legitimately take several minutes
# (provider cold start, high reasoning effort 1-3 min) and we do not want
# requests to time out mid-stream.
CONNECT_TIMEOUT_SECONDS: int = 10
HEALTH_TIMEOUT_SECONDS: int = 5
HEALTH_POLL_INTERVAL_SECONDS: int = 3

# ============================================================================
# REASONING EFFORT TOGGLE
# ============================================================================
# Optional UI toggle that maps user-friendly labels onto the three accepted
# reasoning_effort values. Selecting "Default" omits the field from the
# request entirely, letting openagent-api / openagent-infra apply the
# server-side default (currently "medium").
#
# Per openagent-infra's datasheet and openagent-api's pass-through design:
#   low    →  fastest, simple lookups, routing decisions     (5-15s)
#   medium →  balanced, standard chat, general questions     (15-45s)
#   high   →  slowest, complex analysis, multi-step reasoning (1-3 min)

REASONING_EFFORT_OPTIONS: Dict[str, Optional[str]] = {
    "Default": None,    # Omit the field — server-side default applies
    "Quick":    "low",
    "Standard": "medium",
    "Deep":     "high",
}


# ============================================================================
# SESSION STATE INITIALISATION
# ============================================================================

def init_session_state() -> None:
    """
    Initialise all st.session_state keys with their default values.

    Called once at the top of every Streamlit script run. Streamlit only
    assigns the value when the key does not already exist, so calling this on
    every rerun is safe — existing values from the previous run are preserved
    across reruns within the same browser session.

    Keys:
        session_id        — 8-char UUID fragment for log correlation. Not sent
                            to openagent-api today (the gateway is stateless
                            per-request) but makes frontend logs easy to follow
                            per-tab.
        messages          — list of {"role", "content"} dicts, the source of
                            truth for what is rendered in the chat area. No
                            "think" field — reasoning is rendered live during
                            streaming and not persisted back into history.
        model_ready       — bool, flipped to True once openagent-api's /health
                            returns "ok". Gates the chat input.
        initialised       — bool, one-shot flag to ensure startup logging runs
                            once per browser session, not once per rerun.
        reasoning_effort  — str, the currently-selected dropdown label
                            (Default / Quick / Standard / Deep). Maps via
                            REASONING_EFFORT_OPTIONS to the value sent upstream
                            (or None to omit the field).
    """
    defaults: Dict = {
        "session_id":       str(uuid.uuid4())[:8],
        "messages":         [],
        "model_ready":      False,
        "initialised":      False,
        "reasoning_effort": "Default",
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    if not st.session_state.initialised:
        logger.info("=" * 60)
        logger.info("openagent-frontend initialised")
        logger.info(f"Session ID         : {st.session_state.session_id}")
        logger.info(f"openagent-api URL  : {OPENAGENT_API_URL}")
        logger.info(
            f"openagent-api Key  : "
            f"{'[set]' if OPENAGENT_API_KEY else '[MISSING — will 401]'}"
        )
        logger.info(f"SSE decoder        : v{DECODER_VERSION}")
        logger.info("=" * 60)
        st.session_state.initialised = True


init_session_state()


# ============================================================================
# HEALTH CHECK
# ============================================================================

def check_health() -> Optional[str]:
    """
    Call openagent-api's GET /health endpoint once.

    openagent-api always returns HTTP 200 on /health. The readiness signal is
    in the JSON body's top-level "status" field, not the status code. Per
    openagent-api's datasheet:

        {"status": "ok",          ...}  → upstream warm, ready for /chat
        {"status": "loading",     ...}  → provider worker cold-starting
        {"status": "unreachable", ...}  → openagent-api cannot reach openagent-infra

    openagent-api translates upstream "degraded" (the infra layer's term for a
    cold-starting provider worker) into "loading" for us, so this file only
    needs to recognise three values.

    The /health endpoint is authenticated — same X-API-Key as /chat. A 401
    from this endpoint means openagent-api is up but the key is wrong; we
    surface that as None (treated like a connection failure for gate purposes)
    and let the user fix the key and retry.

    Returns:
        The top-level status string ("ok" / "loading" / "unreachable" /
        other) on success, or None on connection error / non-200 / parse
        failure / auth failure.
    """
    try:
        response = requests.get(
            f"{OPENAGENT_API_URL}/health",
            headers={"X-API-Key": OPENAGENT_API_KEY},
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return response.json().get("status", "unknown")
        if response.status_code == 401:
            logger.error(
                "/health returned 401 — OPENAGENT_API_KEY is missing or wrong"
            )
            return None
        logger.warning(
            f"/health returned unexpected status {response.status_code}"
        )
        return None
    except Exception as err:
        logger.error(f"Health check failed: {err}")
        return None


# ============================================================================
# CHAT STREAMING
# ============================================================================

def stream_chat(
    messages: List[Dict[str, str]],
    reasoning_effort: Optional[str] = None,
):
    """
    POST to openagent-api /chat and yield decoded SSEEvent objects.

    This function owns the HTTP transport: building the request, sending it
    with streaming enabled, handling pre-stream HTTP errors with user-friendly
    emoji-prefixed messages. The actual SSE byte parsing and JSON
    ChatCompletion chunk decoding lives in sse_decoder.py — this function just
    hands the raw line iterator to decode_sse_stream() and re-yields its
    events.

    Raises RuntimeError with a pre-formatted user-facing message on any
    pre-stream failure. The caller displays the message directly via
    st.error(). Mid-stream errors arrive as SSEEvent objects with kind="error"
    and are handled by the caller's render loop.

    All error strings are prefixed with a consistent emoji per the house
    style:
        🔌 connection errors / 502 Bad Gateway / mid-stream disconnect
        ⏳ timeouts / 503 loading / 504 upstream timeout
        🔐 401 API key mismatch
        ⚠️  400 / 422 request validation
        ❌ unexpected errors

    Args:
        messages: The list of user/assistant turns to send. Must contain at
                  least one user message — openagent-api returns 400 otherwise.
                  NO system message — openagent-api prepends the persona
                  server-side. If a system message is included it will be
                  silently dropped by openagent-api.
        reasoning_effort: Optional string, one of "low" / "medium" / "high".
                          When None (the default), the field is omitted from
                          the upstream payload entirely so openagent-api /
                          openagent-infra apply their server-side default.
                          openagent-api validates the value and returns 422 for
                          anything else.

    Yields:
        SSEEvent objects from sse_decoder.decode_sse_stream(). The caller
        branches on event.kind to route reasoning vs content vs error vs done
        into the appropriate UI surface.
    """
    headers = {
        "Content-Type": "application/json",
        "X-API-Key":    OPENAGENT_API_KEY,
    }

    payload: Dict = {"messages": messages}
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort

    logger.info(
        f"POST {OPENAGENT_API_URL}/chat "
        f"| Session: {st.session_state.session_id} "
        f"| Messages: {len(messages)} "
        f"| reasoning_effort={reasoning_effort or 'unset'} "
        f"| Last user: "
        f"{messages[-1]['content'][:60] if messages else '<empty>'}"
    )

    # --- Network-level errors (before HTTP response arrives) --------------

    try:
        response = requests.post(
            f"{OPENAGENT_API_URL}/chat",
            headers=headers,
            json=payload,
            stream=True,
            # (connect_timeout, read_timeout). read_timeout=None because
            # generation can legitimately run for several minutes — provider
            # cold start plus high-effort reasoning (1-3 min) means a finite
            # timeout would cut off legitimate work.
            timeout=(CONNECT_TIMEOUT_SECONDS, None),
        )
    except requests.exceptions.ConnectionError as err:
        msg = (
            f"Cannot connect to openagent-api at {OPENAGENT_API_URL}. "
            "Is the gateway running and reachable?"
        )
        logger.error(f"{msg} | {err}")
        raise RuntimeError(f"🔌 {msg}")
    except requests.exceptions.Timeout:
        msg = (
            f"Connection to openagent-api timed out after "
            f"{CONNECT_TIMEOUT_SECONDS}s while opening the request."
        )
        logger.error(msg)
        raise RuntimeError(f"⏳ {msg}")
    except Exception as err:
        msg = f"Unexpected error opening /chat request: {err}"
        logger.exception(msg)
        raise RuntimeError(f"❌ {msg}")

    # --- HTTP-level errors (non-200 response, before stream open) ---------
    # Once openagent-api opens the SSE stream it has already committed to
    # HTTP 200 — any mid-stream upstream failure arrives as an in-band
    # [ERROR ...] sentinel which sse_decoder yields as an SSEEvent with
    # kind="error". The cases below cover only PRE-stream failures where
    # openagent-api decided not to open the stream at all.

    if response.status_code != 200:
        try:
            err_detail = response.json().get("detail", response.text)
        except Exception:
            err_detail = response.text or "<no body>"

        if response.status_code == 400:
            msg = f"Bad request: {err_detail}"
            prefix = "⚠️"
        elif response.status_code == 401:
            msg = (
                "API key missing or invalid. "
                "OPENAGENT_API_KEY in openagent-frontend's .env must match "
                "OPENAGENT_API_KEY in openagent-api's .env exactly."
            )
            prefix = "🔐"
        elif response.status_code == 422:
            msg = f"Request validation failed: {err_detail}"
            prefix = "⚠️"
        elif response.status_code == 502:
            msg = (
                "openagent-api cannot reach the upstream model. "
                "Check that openagent-infra is running."
            )
            prefix = "🔌"
        elif response.status_code == 503:
            msg = (
                "Upstream model is loading. "
                "Please wait for the health gate to clear and try again."
            )
            prefix = "⏳"
        elif response.status_code == 504:
            msg = "Upstream timed out while generating. Please try again."
            prefix = "⏳"
        else:
            msg = f"Backend error {response.status_code}: {err_detail}"
            prefix = "❌"

        logger.error(f"HTTP {response.status_code} | {msg}")
        raise RuntimeError(f"{prefix} {msg}")

    # --- SSE stream — delegate to sse_decoder -----------------------------
    # All byte-level parsing, [DONE] / [ERROR] sentinel detection, and JSON
    # ChatCompletion chunk decoding lives in sse_decoder.py. This function
    # just hands the line iterator over and re-yields events. Mid-stream
    # connection drops are caught here and surfaced as a synthetic error event
    # so the caller's loop has a single error path (kind="error") to handle.

    try:
        for event in decode_sse_stream(
            response.iter_lines(decode_unicode=True)
        ):
            yield event
    except requests.exceptions.ChunkedEncodingError as err:
        msg = f"Connection to openagent-api dropped mid-stream: {err}"
        logger.error(msg)
        # Surface as a synthetic error event so the caller's render loop has
        # one consistent error path. Caller will st.error() it and break out
        # of the loop.
        yield SSEEvent(kind=KIND_ERROR, error=f"🔌 {msg}")
    except Exception as err:
        msg = f"Error reading SSE stream: {err}"
        logger.exception(msg)
        yield SSEEvent(kind=KIND_ERROR, error=f"❌ {msg}")


# ============================================================================
# HEADER
# ============================================================================
# Lean header. Product name only. No sidebar, no mode pills. The reasoning
# effort toggle lives next to the chat input below, not in a sidebar.

st.markdown(
    """
    <div style="text-align:center; padding: 1rem 0 0.5rem 0;">
        <h1 style="margin:0;">⚡ OpenAgent</h1>
    </div>
    """,
    unsafe_allow_html=True,
)

st.divider()


# ============================================================================
# HEALTH GATE
# ============================================================================
# Block the rest of the script until openagent-api reports the upstream is
# ready. Polls GET /health every HEALTH_POLL_INTERVAL_SECONDS seconds and
# updates an st.empty() slot with a status message so the user is not staring
# at a frozen screen.
#
# Once the model reports "ok" we flip the flag, rerun the script, and fall
# through to the chat UI on the next pass with a clean layout.
#
# This is a blocking while-loop. Streamlit is single-threaded so the page is
# frozen for the duration — that is exactly the desired behaviour here: the
# user cannot send messages that would just 503.

if not st.session_state.model_ready:
    status_box = st.empty()
    attempt = 0

    while not st.session_state.model_ready:
        attempt += 1
        health_status = check_health()

        if health_status == "ok":
            status_box.success("🟢 openagent-api ready — starting chat")
            logger.info(f"Model ready after {attempt} health poll(s)")
            st.session_state.model_ready = True
            time.sleep(0.5)  # brief pause so the user sees the success
            st.rerun()

        elif health_status == "loading":
            status_box.info(
                f"⏳ The upstream model is starting up. "
                f"Cold-start can take a few minutes while the provider "
                f"worker spins up; warm-path requests respond in "
                f"seconds. (Poll attempt #{attempt})"
            )

        elif health_status == "unreachable":
            status_box.error(
                f"🔌 openagent-api is up but cannot reach the upstream "
                f"model. Check that openagent-infra is running. "
                f"Retrying every {HEALTH_POLL_INTERVAL_SECONDS}s. "
                f"(Attempt #{attempt})"
            )

        elif health_status is None:
            status_box.error(
                f"🔌 Cannot reach openagent-api at `{OPENAGENT_API_URL}`. "
                f"Retrying every {HEALTH_POLL_INTERVAL_SECONDS}s. "
                f"(Attempt #{attempt})"
            )

        else:
            status_box.warning(
                f"⚠️ Unknown /health status: `{health_status}`. "
                f"Retrying. (Attempt #{attempt})"
            )

        time.sleep(HEALTH_POLL_INTERVAL_SECONDS)

    # Unreachable — the st.rerun() above exits the script. Kept for clarity.
    st.stop()


# ============================================================================
# CHAT HISTORY DISPLAY
# ============================================================================
# Renders everything in st.session_state.messages. Each message is just
# {"role", "content"} — no "think" field. The reasoning chain is rendered live
# during streaming but not persisted, so prior turns just show their final
# answer.

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


# ============================================================================
# REASONING EFFORT TOGGLE
# ============================================================================
# Small dropdown above the chat input. Updates session_state.reasoning_effort
# which is read on submit. "Default" maps to None which omits the field from
# the request — letting openagent-api / openagent-infra apply the server-side
# default (currently "medium"). Operators who want a specific level on every
# request select Quick / Standard / Deep here.

st.session_state.reasoning_effort = st.selectbox(
    "Reasoning effort",
    options=list(REASONING_EFFORT_OPTIONS.keys()),
    index=list(REASONING_EFFORT_OPTIONS.keys()).index(
        st.session_state.reasoning_effort
    ),
    label_visibility="collapsed",
    help=(
        "Default — server decides (typically Standard). "
        "Quick — fastest, simple lookups. "
        "Standard — balanced, general chat. "
        "Deep — slowest, complex analysis."
    ),
)


# ============================================================================
# CHAT INPUT
# ============================================================================
# The single input path. Streamlit disables this automatically while a script
# run is in progress, so users cannot double-submit during a generation. On
# submit we:
#
#   1. Append the user turn to session_state.messages and render it.
#   2. Build the OpenAI messages list — user/assistant turns ONLY. No system
#      message; openagent-api prepends the persona server-side.
#   3. Create the assistant chat bubble with two containers:
#        - a collapsible expander for the reasoning chain
#        - a placeholder for the final answer
#   4. Stream events from /chat. Each SSEEvent yielded by stream_chat() is
#      routed by event.kind to the appropriate container.
#   5. On success, append the assistant turn (content only, no think) to
#      session_state.messages.
#   6. On any RuntimeError from stream_chat OR an in-band error event, show
#      the pre-formatted error via st.error() and do NOT append to history.
#      State stays clean so the user can retry without a phantom half-answer
#      in the transcript.

if prompt := st.chat_input("Message OpenAgent..."):

    logger.info(
        f"User input | Session: {st.session_state.session_id} "
        f"| Length: {len(prompt)} chars "
        f"| Preview: {prompt[:60]}{'...' if len(prompt) > 60 else ''}"
    )

    # --- 1. Append and render the user turn -------------------------------

    st.session_state.messages.append({
        "role":    "user",
        "content": prompt,
    })

    with st.chat_message("user"):
        st.markdown(prompt)

    # --- 2. Build the OpenAI messages list (user/assistant only) ----------
    # NO system message. openagent-api prepends the persona server-side on
    # every request. If a system message is sent here openagent-api drops it
    # with a warning, so we just don't send one.
    #
    # TODO (deferred): truncate history when approaching the model's context
    # window. This is a low-priority concern for typical conversation lengths.

    messages_payload: List[Dict[str, str]] = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.messages
    ]

    # Resolve the dropdown label to the wire value (or None to omit).
    selected_effort: Optional[str] = REASONING_EFFORT_OPTIONS[
        st.session_state.reasoning_effort
    ]

    # --- 3 / 4. Assistant bubble with streaming split ---------------------

    with st.chat_message("assistant"):
        thinking_expander    = st.expander("🧠 Show thinking", expanded=False)
        thinking_placeholder = thinking_expander.empty()
        answer_placeholder   = st.empty()

        # Show a gentle placeholder while waiting for the first token.
        # Reasoning models can take a few seconds to start producing text,
        # especially on cold path — a silent bubble feels broken without this
        # hint.
        answer_placeholder.markdown("_OpenAgent is thinking…_")

        reasoning_text: str = ""
        answer_text:    str = ""
        finish_reason:  str = ""
        error_msg:      Optional[str] = None

        try:
            for event in stream_chat(messages_payload, selected_effort):

                if event.kind == KIND_REASONING:
                    reasoning_text += event.text
                    thinking_placeholder.markdown(reasoning_text + " ▌")

                elif event.kind == KIND_CONTENT:
                    answer_text += event.text
                    answer_placeholder.markdown(answer_text + " ▌")

                elif event.kind == KIND_FINISH:
                    # Generation finished cleanly — record the reason for the
                    # close-time log line. Stream will end with a KIND_DONE
                    # event right after.
                    finish_reason = event.finish_reason

                elif event.kind == KIND_ERROR:
                    # In-band error from openagent-api (mid-stream upstream
                    # failure) or a synthetic error event from stream_chat() on
                    # a connection drop. Either way, display it and stop
                    # consuming.
                    error_msg = event.error
                    break

                elif event.kind == KIND_DONE:
                    # Clean end-of-stream sentinel. Exit the loop.
                    break

        except RuntimeError as err:
            # Pre-formatted, user-facing message from stream_chat().
            error_msg = str(err)

        except Exception as err:
            logger.exception("Unexpected error during generation")
            error_msg = f"❌ Unexpected error during generation: {err}"

        # --- 5. Finalise the UI -------------------------------------------
        # Strip the cursor glyph for the final render. Both placeholders may
        # be empty if the stream errored before producing anything; handle
        # each independently.

        if reasoning_text:
            thinking_placeholder.markdown(reasoning_text)
        # If reasoning was empty, leave the expander as-is — Streamlit renders
        # an empty expander gracefully and it gives the user a consistent UI
        # shape across turns.

        if answer_text:
            answer_placeholder.markdown(answer_text)
        elif not error_msg:
            # Stream ended cleanly but produced no answer content. Rare but
            # possible — show a neutral marker so the bubble is not blank or
            # stuck on the "thinking" hint.
            answer_placeholder.markdown("_(no answer produced)_")

        # --- 6. Append or error-out ---------------------------------------

        if error_msg:
            st.error(error_msg)
            # Do NOT append to history — keep state clean so the user can
            # retry without a phantom half-answer in the transcript.
        else:
            st.session_state.messages.append({
                "role":    "assistant",
                "content": answer_text,
            })
            logger.info(
                f"Response complete "
                f"| Session: {st.session_state.session_id} "
                f"| Reasoning chars: {len(reasoning_text)} "
                f"| Answer chars: {len(answer_text)} "
                f"| finish_reason: {finish_reason or '-'}"
            )


# ============================================================================
# END OF FILE
# ============================================================================