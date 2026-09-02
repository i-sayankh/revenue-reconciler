"""LLM-generated discrepancy explanations via Groq.

This module only *explains* a discrepancy the deterministic engine
(`app.engine.reconcile`) already classified -- it never re-derives or
overrides the `type`/field values a discrepancy carries. Everything here is
backend-only: `GROQ_API_KEY` is read from `Settings` (`app.config`) and is
never sent to the client.

`explain_discrepancy` builds a prompt from the discrepancy's already-fixed
fields, calls Groq, and validates the response against `Explanation`. On a
malformed/invalid response it retries once with a stricter "JSON only"
instruction; if that also fails (malformed JSON, schema mismatch, or any
other call failure -- e.g. a network/API error), it returns a fixed
`FALLBACK_EXPLANATION` object instead of raising, so the route never turns
an LLM hiccup into a 500 -- the frontend renders the fallback gracefully.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal, Mapping

from groq import AsyncGroq
from pydantic import BaseModel, ValidationError

from app.config import Settings

# gpt-oss-120b: confirmed active/production on Groq, fast/cheap, and a good
# fit for structured JSON output (see console.groq.com/docs/models).
MODEL = "openai/gpt-oss-120b"

# Temperature 0.2: this is a factual/explanatory task (summarizing a
# discrepancy a deterministic system already found), not creative writing --
# a low temperature keeps the explanation grounded in the given fields
# instead of speculating. See the README (Step 17) for the full rationale.
TEMPERATURE = 0.2

SYSTEM_PROMPT = (
    "You are a financial reconciliation assistant. You explain discrepancies "
    "that a deterministic system already found. You never decide whether "
    "records match -- that decision is already made."
)

_JSON_SCHEMA_HINT = (
    '{"summary": "string", "likely_cause": "string", '
    '"recommended_action": "string", "confidence": "low|medium|high"}'
)

_RETRY_INSTRUCTION = "Return ONLY valid JSON, no prose, no markdown fences."


class Explanation(BaseModel):
    """Validated shape of an LLM-generated discrepancy explanation."""

    summary: str
    likely_cause: str
    recommended_action: str
    confidence: Literal["low", "medium", "high"]


FALLBACK_EXPLANATION = Explanation(
    summary="Automated explanation unavailable for this discrepancy.",
    likely_cause="unknown",
    recommended_action="Review this discrepancy manually.",
    confidence="low",
)


@lru_cache(maxsize=1)
def _default_client() -> AsyncGroq:
    """Build (and cache) the default AsyncGroq client from `Settings`.

    Only constructed lazily, on first real use -- unit tests always pass an
    explicit `client` into `explain_discrepancy` and never touch this, so
    they don't need a real `GROQ_API_KEY`/`.env`.
    """
    settings = Settings()
    return AsyncGroq(api_key=settings.groq_api_key)


def _build_user_prompt(discrepancy: Mapping[str, Any]) -> str:
    """Build the user message from a discrepancy's already-fixed fields.

    `discrepancy` is expected to look like the dict shape returned by
    `app.routers.reconcile._discrepancy_to_dict` (or any mapping with the
    same keys): `type`, `order_id`, `payment_ref`, `order_amount`,
    `payment_amount`, `currency_order`, `currency_payment`, `difference`,
    and optionally `detail` (whose `reason` key, if present, is the
    engine's own one-line explanation of the classification -- included
    here as extra grounding context, not re-derived).
    """
    detail = discrepancy.get("detail") or {}
    reason = detail.get("reason")

    lines = [
        "A deterministic reconciliation engine already classified this "
        "discrepancy. Do not re-decide or second-guess the classification "
        "-- only explain it in plain language for a store operator.",
        "",
        f"Discrepancy type: {discrepancy.get('type')}",
        f"Order id: {discrepancy.get('order_id')}",
        f"Order amount: {discrepancy.get('order_amount')} {discrepancy.get('currency_order') or ''}".strip(),
        f"Payment reference: {discrepancy.get('payment_ref')}",
        f"Payment amount: {discrepancy.get('payment_amount')} {discrepancy.get('currency_payment') or ''}".strip(),
        f"Computed difference (payment amount minus order amount): {discrepancy.get('difference')}",
    ]
    if reason:
        lines.append(f"Engine's stated reason: {reason}")
    lines += [
        "",
        "Respond with STRICT JSON only, matching exactly this schema "
        "(no other keys, no prose outside the JSON object, no markdown "
        "code fences):",
        _JSON_SCHEMA_HINT,
    ]
    return "\n".join(lines)


def _strip_markdown_fence(raw: str) -> str:
    """Defensively strip a ```/```json fence the model may have added
    despite being asked not to, so a well-formed-but-fenced JSON body still
    parses instead of being treated as malformed.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
        elif "```" in text:
            text = text.rsplit("```", 1)[0]
    return text.strip()


def _parse_explanation(raw: str) -> Explanation:
    """Parse+validate raw model output. Raises on malformed JSON or a
    schema mismatch -- the caller decides what to do about that."""
    cleaned = _strip_markdown_fence(raw)
    data = json.loads(cleaned)
    return Explanation.model_validate(data)


async def _call_groq(client: AsyncGroq, user_prompt: str) -> str:
    response = await client.chat.completions.create(
        model=MODEL,
        temperature=TEMPERATURE,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("Groq response had no content")
    return content


async def explain_discrepancy(discrepancy: Mapping[str, Any], *, client: AsyncGroq | None = None) -> Explanation:
    """Explain one already-classified discrepancy via Groq.

    Builds the prompt, calls Groq, and validates the response against
    `Explanation`. On any failure (malformed/non-JSON response, a schema
    mismatch, or a call failure such as a network/API error) it retries
    once with an added "JSON only" instruction; a second failure returns
    the fixed `FALLBACK_EXPLANATION` instead of raising, so this function
    never surfaces as a 500 to the route handler.

    `client` is injectable for tests (mock the Groq call itself, not
    `httpx`, so the test stays stable against SDK internals); production
    callers omit it and get the lazily-built default client.
    """
    active_client = client if client is not None else _default_client()
    prompt = _build_user_prompt(discrepancy)

    for attempt_prompt in (prompt, f"{prompt}\n\n{_RETRY_INSTRUCTION}"):
        try:
            raw = await _call_groq(active_client, attempt_prompt)
            return _parse_explanation(raw)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError):
            continue
        except Exception:
            # Any other call-time failure (network error, API error, rate
            # limit, etc.) -- still worth one retry, then fall back rather
            # than ever raising out of this function.
            continue

    return FALLBACK_EXPLANATION
