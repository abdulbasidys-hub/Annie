"""OpenAI adapter — reasoning and image analysis.

Two distinct jobs behind one credential:

* :class:`OpenAIImageAnalyst` implements ``ImageAnalysisProvider`` (§15).
* :class:`OpenAIReasoner` is the language surface Annie and the research engine
  use. It is deliberately *not* a general chat wrapper: every call is structured
  and every call is metered.

§48 is the governing constraint here — "do not send unnecessary data to the
LLM". Nothing in this module accepts raw record sets. Callers pass compact
evidence that SQL and the statistical engine already reduced, which is why the
methods take small typed payloads rather than free-form context blobs.

§15 also warns against building an elaborate computer-vision system in version
one. Image analysis is therefore a single structured call per token with a
fixed schema, plus an open ``other_categories`` field so new visual patterns can
surface without a code change.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from openai import APIError, AsyncOpenAI, RateLimitError

from app.providers.interfaces import ProviderError, ProviderRateLimited
from app.providers.types import ImageAnalysis, Provenance

log = structlog.get_logger(__name__)


# -----------------------------------------------------------------------------
# Cost table
# -----------------------------------------------------------------------------
# USD per 1M tokens. Used only for the running cost estimates §48 and §63
# require; it is not a billing record and will drift as pricing changes.
# Unknown models fall back to zero and are reported as "not estimated" rather
# than silently costed at another model's rate.

PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    # $0.20/$1.20 per 1M input/output tokens — current price as of the 80%
    # cut OpenAI applied 2026-07-30. Verified against OpenAI/OpenRouter/AWS
    # Bedrock listings on 2026-08-21; re-check if cost estimates look off.
    "gpt-5.6-luna": (0.20, 1.20),
}


@dataclass(slots=True)
class Usage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    latency_ms: int


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    rates = PRICING_PER_MTOK.get(model)
    if rates is None:
        return None
    in_rate, out_rate = rates
    total = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
    return Decimal(str(round(total, 6)))


# -----------------------------------------------------------------------------
# Image analysis
# -----------------------------------------------------------------------------

#: The seed vocabulary from §15. `other_categories` is what keeps it open —
#: the spec explicitly wants new categories to emerge, so the model is asked to
#: name anything the fixed list cannot express rather than forcing a bad fit.
IMAGE_CATEGORIES = [
    "animal",
    "human",
    "celebrity",
    "political_figure",
    "cartoon",
    "existing_meme",
    "internet_culture",
    "ai_generated_style",
    "character_based",
    "text_heavy",
    "brand_parody",
    "simple_graphic",
    "abstract",
    "absurd",
]

IMAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "categories",
        "other_categories",
        "subjects",
        "style",
        "has_text",
        "text_content",
        "is_ai_generated_style",
        "references_existing_meme",
        "confidence",
    ],
    "properties": {
        "categories": {
            "type": "array",
            "items": {"type": "string", "enum": IMAGE_CATEGORIES},
        },
        "other_categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Visual characteristics the fixed list cannot express.",
        },
        "subjects": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete depicted subjects, e.g. 'shiba inu', 'pepe'.",
        },
        "style": {"type": ["string", "null"]},
        "has_text": {"type": "boolean"},
        "text_content": {"type": ["string", "null"]},
        "is_ai_generated_style": {"type": "boolean"},
        "references_existing_meme": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

IMAGE_PROMPT = (
    "You are cataloguing the visual characteristics of a cryptocurrency token "
    "image for statistical research. Describe only what is visible.\n\n"
    "Do not speculate about the token's quality, prospects, or whether it will "
    "succeed. You are producing features for a frequency analysis, not an "
    "opinion. If the image is unclear or fails to load, report low confidence "
    "rather than guessing plausible categories."
)


class OpenAIImageAnalyst:
    """Implements ``ImageAnalysisProvider``."""

    name = "openai"
    cost_per_request_usd = 0.0  # metered per token, see Usage

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = (api_key or "").strip()
        self._model = model
        self._client = AsyncOpenAI(api_key=self._api_key) if self._api_key else None
        self.last_usage: Usage | None = None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def healthcheck(self) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.models.retrieve(self._model)
            return True
        except Exception as exc:
            log.info("openai_healthcheck_failed", error=str(exc))
            return False

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def analyze(self, mint: str, image_url: str) -> ImageAnalysis | None:
        if self._client is None:
            return None

        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": IMAGE_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                # "low" detail is a deliberate cost choice: token
                                # avatars are small and the features we extract
                                # are coarse. High detail would multiply cost for
                                # no gain in category accuracy.
                                "image_url": {"url": image_url, "detail": "low"},
                            }
                        ],
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "token_image_features",
                        "strict": True,
                        "schema": IMAGE_SCHEMA,
                    },
                },
                max_completion_tokens=500,
                reasoning_effort="none",  # see openai_provider's model note; matches agent.py
            )
        except RateLimitError as exc:
            raise ProviderRateLimited(self.name, "analyze_image") from exc
        except APIError as exc:
            raise ProviderError(
                self.name, "analyze_image", str(exc), retryable=True
            ) from exc

        latency = int((time.perf_counter() - started) * 1000)
        self.last_usage = _usage_from(response, self._model, latency)

        content = response.choices[0].message.content
        if not content:
            return None
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            log.warning("image_analysis_unparseable", mint=mint)
            return None

        categories = list(payload.get("categories") or [])
        categories.extend(payload.get("other_categories") or [])

        return ImageAnalysis(
            mint=mint,
            provenance=Provenance(
                provider=self.name,
                operation="analyze_image",
                observed_at=datetime.now(timezone.utc),
                raw_reference=image_url,
                confidence=Decimal(str(payload.get("confidence", 0))),
            ),
            categories=categories,
            subjects=list(payload.get("subjects") or []),
            style=payload.get("style"),
            has_text=payload.get("has_text"),
            text_content=payload.get("text_content"),
            is_ai_generated_style=payload.get("is_ai_generated_style"),
            references_existing_meme=payload.get("references_existing_meme"),
            model=self._model,
        )


# -----------------------------------------------------------------------------
# Reasoning
# -----------------------------------------------------------------------------


class OpenAIReasoner:
    """Structured language calls for Annie and the research engine.

    Exposes no free-form completion method. Every entry point takes a schema,
    so a caller cannot accidentally ask for prose that then gets parsed with a
    regex — and so every response Annie produces has a shape the UI can render
    with its claim type and confidence intact (§33).
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        reasoning_model: str,
        cheap_model: str,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._reasoning_model = reasoning_model
        self._cheap_model = cheap_model
        self._client = AsyncOpenAI(api_key=self._api_key) if self._api_key else None

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    def model_for(self, task: str) -> str:
        """Route a task to a model.

        Classification and labelling go to the cheap model; anything that has to
        weigh evidence goes to the reasoning model. §48 wants the LLM used
        "where reasoning adds value" — this is where that decision is made once
        rather than at every call site.
        """
        cheap_tasks = {"categorise", "label", "summarise_row", "slugify", "title"}
        return self._cheap_model if task in cheap_tasks else self._reasoning_model

    async def structured(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int = 1500,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], Usage]:
        """One structured call. Returns the parsed payload and its usage."""
        if self._client is None:
            raise ProviderError(
                self.name,
                task,
                "OPENAI_API_KEY is not configured; AI reasoning is unavailable.",
                retryable=False,
            )

        model = self.model_for(task)
        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
                max_completion_tokens=max_tokens,
                temperature=0.2,  # research output; consistency over variety
                reasoning_effort="none",  # required alongside tools — see agent.py's note
                **({"tools": tools} if tools else {}),
            )
        except RateLimitError as exc:
            raise ProviderRateLimited(self.name, task) from exc
        except APIError as exc:
            raise ProviderError(self.name, task, str(exc), retryable=True) from exc

        latency = int((time.perf_counter() - started) * 1000)
        usage = _usage_from(response, model, latency)

        content = response.choices[0].message.content
        if not content:
            raise ProviderError(self.name, task, "empty response", retryable=True)
        try:
            return json.loads(content), usage
        except json.JSONDecodeError as exc:
            raise ProviderError(
                self.name, task, f"response was not valid JSON: {exc}", retryable=True
            ) from exc

    async def raw_client(self) -> AsyncOpenAI:
        """Escape hatch for the agent loop, which needs multi-turn tool calling.

        Kept explicit so that ad-hoc use is visible in review rather than
        arriving through a general-purpose ``complete()`` method.
        """
        if self._client is None:
            raise ProviderError(
                self.name, "raw_client", "OPENAI_API_KEY is not configured", False
            )
        return self._client


def _usage_from(response: Any, model: str, latency_ms: int) -> Usage:
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    return Usage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=estimate_cost(model, input_tokens, output_tokens),
        latency_ms=latency_ms,
    )
