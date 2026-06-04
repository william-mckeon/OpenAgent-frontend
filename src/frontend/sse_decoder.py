#!/usr/bin/env python3
# ============================================================================
# openagent-frontend - SSE Decoder
# Maintainer: William McKeon
# ============================================================================
#
# DESCRIPTION:
#   Standalone Server-Sent Events decoder for the openagent-frontend Streamlit
#   application. This module owns the parsing of the SSE stream that flows
#   from openagent-api back to the frontend on every /chat request.
#
#   The openagent-api gateway forwards openagent-infra's stream byte-for-byte,
#   and openagent-infra in turn forwards the provider's stream byte-for-byte.
#   The net result is that this decoder is parsing OpenAI ChatCompletion
#   chunks (the provider's streaming format) where chain-of-thought tokens
#   arrive in choices[0].delta.reasoning, visible answer tokens arrive in
#   choices[0].delta.content, and the stream terminates with an empty-delta
#   chunk (finish_reason="stop") followed by the [DONE] sentinel.
#
#   Two non-JSON sentinel events also flow through the stream:
#     - "[DONE]"            — clean end of stream, terminate consumption
#     - "[ERROR ...]"       — mid-stream upstream failure surfaced in-band
#
#   This module's job is to consume raw SSE lines from requests.iter_lines()
#   and yield a clean stream of typed SSEEvent dataclasses to app.py. The
#   consumer (app.py) routes each event to the correct UI surface — the
#   reasoning expander, the chat bubble, the error banner, etc.
#
# ARCHITECTURE:
#
#   app.py
#     │
#     │ requests.post(..., stream=True)
#     ▼
#   raw byte stream from openagent-api
#     │
#     │ response.iter_lines(decode_unicode=True)
#     ▼
#   ┌─────────────────────────────────────────────────────────────┐
#   │              sse_decoder.py (THIS FILE)                     │
#   │                                                             │
#   │  decode_sse_stream(iter_lines)                              │
#   │    │                                                        │
#   │    ├─> skip blank lines and SSE comments                    │
#   │    ├─> strip "data: " prefix                                │
#   │    ├─> detect [DONE] sentinel        → SSEEvent("done")     │
#   │    ├─> detect [ERROR ...] sentinel   → SSEEvent("error")    │
#   │    ├─> json.loads(payload) for chunk → routed by delta key  │
#   │    │     ├─> delta.reasoning         → SSEEvent("reasoning")│
#   │    │     ├─> delta.content           → SSEEvent("content")  │
#   │    │     └─> finish_reason set       → SSEEvent("finish")   │
#   │    └─> malformed chunk               → log WARNING and skip │
#   │                                                             │
#   │  yields SSEEvent dataclasses                                │
#   └─────────────────────────────────────────────────────────────┘
#     │
#     │ for event in decode_sse_stream(...):
#     ▼
#   app.py (UI rendering layer)
#     - if event.kind == "reasoning" → append to reasoning expander
#     - if event.kind == "content"   → append to chat bubble
#     - if event.kind == "error"     → render error banner
#     - if event.kind == "finish"    → record finish_reason (usually "stop")
#     - if event.kind == "done"      → break
#
# OWNERSHIP BOUNDARY:
#
#   This module OWNS:
#     - SSE line parsing (data: prefix, blank lines, comments)
#     - Sentinel detection ([DONE], [ERROR ...])
#     - JSON decoding of chunk payloads
#     - Routing chunks by delta key (reasoning/content/finish)
#     - Skipping malformed chunks gracefully with warning logs
#     - Logging stream lifecycle (open, close, totals, errors)
#
#   This module does NOT OWN:
#     - HTTP transport (requests.post, headers, stream=True) → app.py
#     - UI rendering (st.empty, st.expander, st.markdown)    → app.py
#     - Session state (st.session_state.messages)            → app.py
#     - Error display (emoji prefixes, banners)              → app.py
#     - Health gate logic                                    → app.py
#
#   The boundary is: transport in (raw line iterator), structured events
#   out (SSEEvent dataclasses). This module never imports Streamlit.
#   app.py never calls json.loads() on stream content.
# ============================================================================

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

# ============================================================================
# VERSION
# ============================================================================

DECODER_VERSION = "1.0.0"

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================
# Use a child logger of "openagent.frontend" so log output unifies cleanly
# with app.py's logger when both write to stdout. Configured by app.py at
# startup (level, format, handlers) — this module only gets the named logger
# and emits to it. No handler configuration happens here.

logger = logging.getLogger("openagent.frontend.sse_decoder")

# ============================================================================
# SSE PROTOCOL CONSTANTS
# ============================================================================
# Spec reference: https://html.spec.whatwg.org/multipage/server-sent-events.html
#
# An SSE stream is a sequence of lines. Lines starting with "data: " carry
# event payloads. Blank lines separate events. Lines starting with ":" are
# comments (typically used for keep-alive). Other line prefixes (event:, id:,
# retry:) are valid SSE fields but are not used by openagent-api or
# openagent-infra.

SSE_DATA_PREFIX = "data: "
SSE_COMMENT_PREFIX = ":"

# ============================================================================
# UPSTREAM SENTINEL CONSTANTS
# ============================================================================
# Two non-JSON sentinel payloads flow through the stream alongside the
# JSON-encoded ChatCompletion chunks. Both originate at openagent-api and are
# documented in the openagent-api datasheet's "Inbound HTTP Contracts"
# section.

# End-of-stream marker. Forwarded by openagent-api when openagent-infra closes
# the upstream stream cleanly. Always the final event in a successful
# response.
DONE_SENTINEL = "[DONE]"

# Mid-stream error marker. Emitted by openagent-api when the upstream
# connection fails after SSE headers have already been sent (status code 200
# cannot be changed retroactively, so errors must travel in-band). Format
# example:
#   data: [ERROR upstream_status=503]\n\n
# Always followed by a [DONE] sentinel so consumers can clean up.
ERROR_SENTINEL_PREFIX = "[ERROR"

# ============================================================================
# EVENT KIND CONSTANTS
# ============================================================================
# String constants used as the SSEEvent.kind discriminator. Exposed at module
# level so app.py can compare against them by name rather than typing the
# string literals directly — fewer typo opportunities.

KIND_REASONING = "reasoning"
KIND_CONTENT = "content"
KIND_FINISH = "finish"
KIND_ERROR = "error"
KIND_DONE = "done"

# ============================================================================
# SSE EVENT DATACLASS
# ============================================================================

@dataclass(frozen=True)
class SSEEvent:
    """
    A single decoded event from the SSE stream.

    Immutable (frozen=True) so the decoder cannot accidentally mutate an
    event after yielding it — the consumer (app.py) gets a clean snapshot
    of each event at the moment it was decoded.

    The `kind` field discriminates the five event types this decoder emits.
    Each kind populates a different subset of the remaining fields:

        kind="reasoning"  →  text  contains a chain-of-thought token
        kind="content"    →  text  contains a visible answer token
        kind="finish"     →  finish_reason  contains "stop" (typically)
        kind="error"      →  error  contains the upstream error message
        kind="done"       →  no other fields populated; signals end of stream

    Consumers should branch on `kind` and only read the relevant field.

    Attributes:
        kind:           One of "reasoning", "content", "finish", "error", "done".
                        See the KIND_* constants at module level.
        text:           Token text. Populated for "reasoning" and "content".
                        Empty string for other kinds.
        error:          Upstream error message. Populated for "error" only.
                        Empty string for other kinds.
        finish_reason:  Completion reason from the upstream model. Populated
                        for "finish" only. Empty string for other kinds.
                        Typical value is "stop" (model emitted EOS) but may
                        be "length" (token limit hit) or other values per
                        the OpenAI ChatCompletion spec.
    """

    kind: str
    text: str = ""
    error: str = ""
    finish_reason: str = ""


# ============================================================================
# CHUNK PARSING — INTERNAL HELPER
# ============================================================================

def parse_chunk(line: str) -> Optional[SSEEvent]:
    """
    Parse a single raw SSE line into a structured SSEEvent.

    This function is exposed at module level (not underscore-prefixed) so
    unit tests can exercise individual line parsing without setting up a
    full byte stream. In normal operation app.py does not call this
    directly — it consumes decode_sse_stream() instead.

    Returns None for lines that should be skipped silently (blank lines,
    SSE comments, lines without the "data: " prefix). Returns an SSEEvent
    for any line that produces a meaningful event for the consumer.

    Lines that fail JSON parsing are NOT returned as events — instead, a
    WARNING is logged and None is returned, allowing the stream to continue
    without a malformed chunk crashing the consumer's render loop.

    Args:
        line: A single line from response.iter_lines(decode_unicode=True).
              May be empty (SSE event separator), may start with ":" (SSE
              comment), may start with "data: " (event payload), or may
              be something unexpected.

    Returns:
        SSEEvent if the line produced a meaningful event.
        None if the line was a separator, comment, or malformed payload.

    Notes:
        - The "data: " prefix is exactly six characters (d, a, t, a, colon,
          space). The SSE spec allows "data:" without the trailing space,
          but openagent-api / openagent-infra / the provider all emit the
          trailing space consistently. Defensive coding here strips a
          leading space if present, then strips trailing whitespace.
        - Two non-JSON sentinels are checked BEFORE attempting json.loads()
          to avoid generating a WARNING log on every legitimate end-of-
          stream and every legitimate in-band error.
    """
    # Skip empty lines — SSE uses blank lines as event separators.
    if not line:
        return None

    # Skip SSE comments — keep-alive heartbeats and similar.
    if line.startswith(SSE_COMMENT_PREFIX):
        return None

    # Skip lines without the data: prefix. The SSE spec defines other
    # field prefixes (event:, id:, retry:) but openagent-api does not emit
    # them. If one appears, it's safer to skip than to misinterpret it.
    if not line.startswith(SSE_DATA_PREFIX):
        return None

    # Strip the "data: " prefix and any leading/trailing whitespace.
    payload = line[len(SSE_DATA_PREFIX):].strip()

    # An empty payload after the prefix is meaningless — skip it.
    if not payload:
        return None

    # ------------------------------------------------------------------
    # SENTINEL 1: [DONE] — clean end of stream
    # ------------------------------------------------------------------
    # openagent-api forwards openagent-infra's [DONE] byte-for-byte. This is
    # the final event in every successful stream. Yielding KIND_DONE lets the
    # consumer break its loop cleanly.
    if payload == DONE_SENTINEL:
        return SSEEvent(kind=KIND_DONE)

    # ------------------------------------------------------------------
    # SENTINEL 2: [ERROR ...] — mid-stream upstream failure
    # ------------------------------------------------------------------
    # openagent-api emits this when the upstream connection fails after SSE
    # headers have already gone out. Format:  [ERROR upstream_status=503]
    # The full payload (including the brackets) is preserved as the error
    # field so app.py can display it verbatim or parse it further.
    if payload.startswith(ERROR_SENTINEL_PREFIX):
        logger.error(f"In-band SSE error received: {payload}")
        return SSEEvent(kind=KIND_ERROR, error=payload)

    # ------------------------------------------------------------------
    # JSON CHUNK — OpenAI ChatCompletion streaming format
    # ------------------------------------------------------------------
    # Everything else should be a JSON-encoded chunk. Parse it; if parsing
    # fails, log a WARNING and skip — never crash the consumer's loop on
    # a single malformed chunk. The stream may still contain valid chunks
    # after a malformed one (the provider occasionally emits partial chunks
    # under load), and breaking the consumer would lose those.
    try:
        chunk = json.loads(payload)
    except json.JSONDecodeError as err:
        logger.warning(
            f"Failed to JSON-parse SSE chunk (skipping): {err} | "
            f"payload preview: {payload[:120]!r}"
        )
        return None

    # The chunk should be a dict with a "choices" array of length >= 1.
    # If the shape is unexpected, log and skip.
    try:
        choices = chunk.get("choices") or []
        if not choices:
            logger.debug(
                f"Chunk has no choices array (skipping): {payload[:120]!r}"
            )
            return None

        first_choice = choices[0]
        delta = first_choice.get("delta") or {}
        finish_reason = first_choice.get("finish_reason")
    except (AttributeError, TypeError, IndexError) as err:
        logger.warning(
            f"Unexpected chunk shape (skipping): {err} | "
            f"payload preview: {payload[:120]!r}"
        )
        return None

    # ------------------------------------------------------------------
    # ROUTE BY DELTA KEY
    # ------------------------------------------------------------------
    # The streaming format places chain-of-thought tokens in delta.reasoning
    # and visible answer tokens in delta.content. Both are token-by-token
    # strings. The two streams interleave only at chunk boundaries — within
    # a single chunk, only one of the keys is present (per the OpenAI
    # streaming spec).
    #
    # The terminal chunk has an empty delta dict and a non-null
    # finish_reason. Yielding KIND_FINISH lets the consumer record the
    # reason without needing to peek at every chunk's finish_reason.

    reasoning_token = delta.get("reasoning")
    if reasoning_token is not None and reasoning_token != "":
        return SSEEvent(kind=KIND_REASONING, text=reasoning_token)

    content_token = delta.get("content")
    if content_token is not None and content_token != "":
        return SSEEvent(kind=KIND_CONTENT, text=content_token)

    if finish_reason:
        return SSEEvent(kind=KIND_FINISH, finish_reason=str(finish_reason))

    # The chunk had a delta but no reasoning, no content, and no
    # finish_reason. This happens on the very first chunk of some streams
    # (the role-only chunk, e.g. {"delta":{"role":"assistant","content":""}})
    # and is harmless — skip silently.
    return None


# ============================================================================
# STREAM DECODING — PUBLIC API
# ============================================================================

def decode_sse_stream(
    lines: Iterable[Optional[str]],
) -> Iterator[SSEEvent]:
    """
    Decode an iterable of raw SSE lines into a stream of SSEEvent objects.

    This is the primary public entry point of this module. app.py calls
    it with the line iterator returned by requests.iter_lines() and
    consumes the yielded events in a for-loop, routing each one to the
    appropriate UI surface.

    The function is a generator — events are yielded as the upstream
    stream produces them, with no internal buffering. This is deliberate:
    Streamlit's reactive rendering depends on tokens arriving one at a
    time, and buffering would defeat the entire point of streaming.

    Logging:
        - INFO at stream open (one line)
        - INFO at stream close with totals (one line: chunks, reasoning
          chars, content chars, terminal status)
        - WARNING per malformed chunk (skipped, stream continues)
        - ERROR per in-band [ERROR ...] sentinel (event still yielded
          to the consumer; logging is for operator visibility)
        - DEBUG per chunk if debug logging is enabled (off by default)

    Args:
        lines: An iterable of strings, typically from
               response.iter_lines(decode_unicode=True). Items may be
               empty strings (SSE event separators), comments, data
               lines, or — depending on the requests version —
               occasionally None for the final iteration. None values
               are tolerated and skipped.

    Yields:
        SSEEvent objects, one per meaningful event in the stream. Blank
        lines, comments, malformed chunks, and shape-unexpected chunks
        do NOT produce yielded events — they are logged and skipped.

    Termination:
        The generator stops when:
          (a) the input iterable is exhausted (upstream closed the stream),
          (b) a [DONE] sentinel is encountered (KIND_DONE event yielded
              first, then the generator exits on the next iteration),
          (c) the consumer breaks out of its for-loop early.

        It is safe for the consumer to break early — Python's generator
        cleanup will close the underlying iterator. openagent-api's own
        SSE pump detects client disconnects via is_disconnected() and
        abandons the upstream connection, so the provider stops generating
        tokens nobody will read. Cooperative shutdown all the way up.

    Example usage from app.py:

        response = requests.post(
            f"{OPENAGENT_API_URL}/chat",
            json=payload,
            headers={"X-API-Key": OPENAGENT_API_KEY},
            stream=True,
            timeout=(10, None),
        )
        response.raise_for_status()

        for event in decode_sse_stream(response.iter_lines(decode_unicode=True)):
            if event.kind == "reasoning":
                reasoning_buffer += event.text
                reasoning_placeholder.markdown(reasoning_buffer)
            elif event.kind == "content":
                answer_buffer += event.text
                answer_placeholder.markdown(answer_buffer)
            elif event.kind == "error":
                st.error(f"⚠️ Upstream error: {event.error}")
                break
            elif event.kind == "finish":
                logger.info(f"Generation finished: {event.finish_reason}")
            elif event.kind == "done":
                break
    """
    # Stream-level counters for the close-time INFO log line. These give
    # operators a one-line summary of every completed stream when tailing
    # logs, which is useful for spot-checking that reasoning_effort levels
    # are producing the expected token volumes.
    chunk_count = 0
    reasoning_chars = 0
    content_chars = 0
    saw_finish = False
    saw_done = False
    saw_error = False

    logger.info("SSE stream open — beginning decode loop")

    try:
        for line in lines:
            # requests.iter_lines() can yield None on some edge cases
            # (e.g. very last iteration on certain connection terminations).
            # Defensive None-skip — no need to log, just move on.
            if line is None:
                continue

            event = parse_chunk(line)
            if event is None:
                # parse_chunk already logged anything worth logging
                # (warnings on malformed JSON, debug on no-op chunks).
                continue

            chunk_count += 1

            # Tally per-kind statistics for the close-time summary log.
            if event.kind == KIND_REASONING:
                reasoning_chars += len(event.text)
            elif event.kind == KIND_CONTENT:
                content_chars += len(event.text)
            elif event.kind == KIND_FINISH:
                saw_finish = True
            elif event.kind == KIND_ERROR:
                saw_error = True
            elif event.kind == KIND_DONE:
                saw_done = True

            # Per-chunk DEBUG visibility for hairy stream debugging. Kept
            # at DEBUG so it doesn't pollute INFO logs in normal operation.
            logger.debug(
                f"SSE event yielded: kind={event.kind} "
                f"text_len={len(event.text)} "
                f"finish_reason={event.finish_reason or '-'} "
                f"error={event.error or '-'}"
            )

            yield event

            # If we just yielded the [DONE] sentinel, the stream is over.
            # Stop iterating even if the upstream sends more bytes — those
            # bytes shouldn't exist per spec, and consuming them would
            # delay the generator's StopIteration unnecessarily.
            if event.kind == KIND_DONE:
                break

    finally:
        # Always log the close summary, even if the consumer broke early
        # or an exception bubbled up. The finally block runs on normal
        # exit, on consumer break, and on exception unwind.
        terminal_status = (
            "done" if saw_done
            else "error" if saw_error
            else "finish" if saw_finish
            else "incomplete"
        )
        logger.info(
            f"SSE stream closed — events={chunk_count} "
            f"reasoning_chars={reasoning_chars} "
            f"content_chars={content_chars} "
            f"terminal={terminal_status}"
        )


# ============================================================================
# PUBLIC API SURFACE
# ============================================================================
# Symbols that app.py is expected to import. Anything not in this list is
# considered internal and may change without notice. The two-symbol public
# API (decode_sse_stream + SSEEvent) is the entire integration contract
# between this module and app.py.
#
# parse_chunk and the KIND_* constants are also exposed because they are
# useful for unit tests and for app.py code that wants to compare event
# kinds against named constants rather than string literals.

__all__ = [
    # Primary entry points
    "decode_sse_stream",
    "SSEEvent",
    # Secondary helpers (exposed for testability and clarity)
    "parse_chunk",
    # Event kind constants
    "KIND_REASONING",
    "KIND_CONTENT",
    "KIND_FINISH",
    "KIND_ERROR",
    "KIND_DONE",
    # Version
    "DECODER_VERSION",
]


# ============================================================================
# END OF FILE
# ============================================================================
