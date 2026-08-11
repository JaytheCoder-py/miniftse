"""Provider-agnostic LLM interface, with an offline deterministic backend.

Two hard rules govern everything in `agents/`, and they are not stylistic:

1. **No number in a client-facing output may originate from a language model.**
   Numbers come from code. The model arranges prose around values it is handed, and
   `drafter.NumberGuard` enforces this by checking every numeral in the output against
   the supplied facts. A model that produces a plausible-looking index level is worse
   than one that refuses, because plausible-looking is exactly what gets published.

2. **Nothing here touches the index calculation path.** Not as a fallback, not for
   filling a gap, not to "estimate" a missing price. A published index must be
   reproducible from its inputs and its rules, and a sampled model is neither.

The offline backend exists so the whole AI layer is testable, benchmarkable and
CI-runnable without keys, network or spend - and so the eval harness measures the
*scaffolding* (retrieval, citation, guardrails) separately from the model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str


@dataclass
class LlmResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stop_reason: str = "end_turn"
    raw: Any = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LlmClient(ABC):
    """Everything in `agents/` binds to this, never to a vendor SDK."""

    name: str

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse: ...

    def ask(self, prompt: str, system: str | None = None, **kwargs: Any) -> str:
        return self.complete([Message("user", prompt)], system=system, **kwargs).text


@dataclass
class OfflineLlm(LlmClient):
    """Deterministic, template-driven stand-in for a real model.

    Not a mock in the testing sense - it produces genuinely useful output for the
    narrow, highly structured tasks in this module, because those tasks are template
    filling with retrieved evidence rather than open generation.

    Its real purpose is separation of concerns in evaluation. Run the eval suite
    against this and you are measuring retrieval quality, citation correctness and
    guardrail behaviour. Swap in a real model and the delta is the model's contribution.
    Most RAG systems that disappoint do so because retrieval is bad, and without this
    baseline you cannot tell.
    """

    name: str = "offline-deterministic"
    handlers: dict[str, Any] = field(default_factory=dict)

    def complete(
        self,
        messages: list[Message],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        del max_tokens, temperature
        prompt = "\n".join(m.content for m in messages)
        for pattern, handler in self.handlers.items():
            if re.search(pattern, prompt, re.IGNORECASE):
                return LlmResponse(text=handler(prompt, system), model=self.name)
        return LlmResponse(text=self._default(prompt, system), model=self.name)

    @staticmethod
    def _default(prompt: str, system: str | None) -> str:
        """Extract and restate the supplied evidence rather than inventing anything.

        The refusal behaviour is deliberate and is itself measured by the eval suite:
        abstaining when the context does not contain an answer is the correct outcome,
        not a failure, and a system that never abstains is one that guesses.
        """
        del system
        context = re.search(r"<context>(.*?)</context>", prompt, re.DOTALL)
        if not context or not context.group(1).strip():
            return (
                "I cannot answer that from the methodology documents available. "
                "No relevant passage was retrieved, so answering would mean guessing. "
                "Refer this to the index governance team."
            )
        question = re.search(r"Question:\s*(.+)", prompt)
        query_terms = set(
            re.findall(r"[a-z]{4,}", question.group(1).lower())
        ) if question else set()

        passages = [p.strip() for p in context.group(1).split("---") if p.strip()]
        lines = [
            "Based on the supplied evidence:",
            "",
        ]
        for passage in passages[:3]:
            cite = re.search(r"\[(.*?)\]", passage)
            body = re.sub(r"^\[.*?\]\s*", "", passage).strip()
            # Select the sentences that actually address the question rather than the
            # first 400 characters. A section's opening sentence is usually scope-setting
            # prose, and the threshold the question asked about sits three sentences
            # further down - so a fixed prefix reliably cites the right section and
            # quotes the wrong part of it.
            snippet = _most_relevant(body, query_terms, budget=520)
            lines.append(f"- {snippet}")
            if cite:
                lines.append(f"  (source: {cite.group(1)})")
        lines += [
            "",
            "This response is drawn only from the evidence above. Anything not stated "
            "there is not covered and should be escalated rather than inferred.",
        ]
        return "\n".join(lines)


def _most_relevant(body: str, query_terms: set[str], budget: int = 520) -> str:
    """Pick the sentences of a passage that best match the question, in original order.

    Order is preserved rather than sorting by score: methodology text is sequential, and
    a reordered extract can read as saying something the document does not.
    """
    # Split on sentence boundaries AND on markdown table rows. A table has no
    # sentence punctuation, so a naive splitter treats the whole thing as one unit and
    # either blows the budget or truncates mid-row. Thresholds in this corpus live in
    # tables more often than in prose.
    units: list[str] = []
    for block in re.split(r"(?<=[.;])\s+|(?=\|)", body):
        block = block.strip()
        if block:
            units.append(block)
    sentences = units
    if not sentences or not query_terms:
        return body[:budget] + ("..." if len(body) > budget else "")

    scored = []
    for i, sentence in enumerate(sentences):
        words = set(re.findall(r"[a-z]{4,}", sentence.lower()))
        overlap = len(words & query_terms)
        # Sentences carrying a number are worth more: the questions are usually about
        # thresholds, and the threshold lives in the sentence that states it.
        numeric_bonus = 1 if re.search(r"\d", sentence) else 0
        scored.append((overlap + numeric_bonus, i, sentence))

    scored.sort(key=lambda t: -t[0])
    chosen: list[tuple[int, str]] = []
    used = 0
    for score, i, sentence in scored:
        if score == 0 and chosen:
            break
        if used + len(sentence) > budget and chosen:
            break
        chosen.append((i, sentence))
        used += len(sentence)

    chosen.sort(key=lambda t: t[0])
    return " ".join(s for _, s in chosen) or body[:budget]


# --------------------------------------------------------------------------------------
# The fact-pack drafting handler
# --------------------------------------------------------------------------------------

FACT_PACK_PROMPT = r"FACTS \(the only numbers you may use\)"
"""The marker `drafter.FactPack.render()` opens every drafting prompt with. An
`OfflineLlm` that should draft from a fact pack binds `restate_fact_pack` to this
pattern; `_default` only recognises the `<context>...</context>` shape `agents/rag.py`'s
retrieval prompts build, so without the handler the offline backend answers every
drafting prompt with the retrieval refusal - a fact table on screen next to prose that
ignores it entirely."""


def restate_fact_pack(prompt: str, system: str | None) -> str:
    """Draft by restating the prompt's own FACTS block - a template in the same spirit
    as `_default`'s "restate the supplied evidence rather than inventing anything". It
    rearranges values the caller already computed into prose and invents nothing; no
    number is computed or verified here, both stay in `NumberGuard` and the fact-pack
    builders, and `ClientResponseDrafter.draft` runs the guard on this output
    unconditionally, exactly as it would on a real model's.

    Deliberately quotes each fact's `key` and `formatted` value only - never the full
    `Fact.line()` (which carries `description`/`source`) and never the CONTEXT block.
    Two reasons, both the same shape: `FactPack.allowed_numbers()` only ever scans a
    fact's *value* (in several roundings) and the raw context text, never a fact's
    free-text `description` - `performance_facts` writes descriptions like "price
    return over the last 21 sessions", and 21 (`period_days`) is not a fact value
    anywhere in the pack, so echoing descriptions verbatim would have `NumberGuard`
    block its own draft on a number that was only ever English, not a claim. And
    `performance_facts`'s `as_of` context is an ISO-style date string ("2019-12-30
    00:00:00"): `allowed_numbers()`'s naive regex reads "-12-30" as the *negative*
    numbers -12 and -30, while `NumberGuard.check` strips leading signs from the draft
    side - so a draft quoting that string back would compare a stripped "30" against
    an allowed set that only ever recorded "-30", and lose. Both are pre-existing
    properties of `agents/drafter.py`; the fix on this side is simply not to write a
    draft that walks into either edge case.
    """
    del system
    facts_block = re.search(FACT_PACK_PROMPT + r":\n(.*?)(?:\n\n|\Z)", prompt, re.DOTALL)
    fact_lines = facts_block.group(1).splitlines() if facts_block else []
    # Each line is `Fact.line()` behind a two-space indent:
    # `  {key} = {formatted}  ({description}; source: {source})`.
    facts = [
        (match.group("key"), match.group("formatted"))
        for line in fact_lines
        if (match := re.match(r"\s*(?P<key>\S+) = (?P<formatted>.*?)\s{2}\(", line))
    ]

    question_match = re.search(r"CLIENT QUESTION:\n(.*?)(?:\n\n|\Z)", prompt, re.DOTALL)
    question = question_match.group(1).strip() if question_match else ""

    lines = [f'In response to your question, "{question}":', ""]
    lines += [f"- {key.replace('_', ' ')}: {formatted}" for key, formatted in facts]
    lines += [
        "",
        "Every figure above comes from the index's computed history; nothing has been "
        "estimated, projected, or supplied by a model.",
    ]
    return "\n".join(lines)


def offline_drafting_client() -> OfflineLlm:
    """An `OfflineLlm` that can actually draft from a fact-pack prompt.

    `OfflineLlm.handlers` is itself the extension point for exactly this kind of
    composition - the handler is registered on the fact-pack prompt shape, not on
    `.*`, so a client built here still answers retrieval prompts through `_default`
    and its abstention behaviour, unchanged. A bare `OfflineLlm()` keeps refusing
    drafting prompts; opting into drafting is this one function call.
    """
    return OfflineLlm(handlers={FACT_PACK_PROMPT: restate_fact_pack})


@dataclass
class AnthropicLlm(LlmClient):
    """Anthropic Claude via the Messages API.

    Requires `anthropic` and `ANTHROPIC_API_KEY`. Temperature defaults to zero: for
    methodology questions and client drafts, reproducibility matters more than variety,
    and a methodology assistant that answers differently on Tuesday is not usable.
    """

    model: str = "claude-sonnet-5"
    name: str = "anthropic"
    api_key: str | None = None
    max_retries: int = 3

    def __post_init__(self) -> None:
        self.api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")

    def complete(
        self,
        messages: list[Message],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Use OfflineLlm for offline runs - the "
                "eval suite is designed to work without a key."
            )
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("pip install anthropic to use this client") from exc

        client = anthropic.Anthropic(api_key=self.api_key, max_retries=self.max_retries)
        response = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "",
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return LlmResponse(
            text=text, model=self.model,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            stop_reason=str(response.stop_reason), raw=response,
        )


@dataclass
class CachingLlm(LlmClient):
    """Wraps any client with an on-disk cache keyed on the exact prompt.

    Two reasons, both practical. Evals get re-run constantly while the *scaffolding*
    changes, and paying for identical completions is waste. And a cached eval is
    reproducible, so a change in the score is attributable to a change you made rather
    than to sampling.
    """

    inner: LlmClient
    cache_dir: Any = None
    name: str = "cached"

    def __post_init__(self) -> None:
        from pathlib import Path

        self.cache_dir = Path(self.cache_dir or "artefacts/llm_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.name = f"cached({self.inner.name})"

    def complete(
        self,
        messages: list[Message],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LlmResponse:
        key = hashlib.sha256(json.dumps({
            "model": self.inner.name,
            "system": system,
            "messages": [(m.role, m.content) for m in messages],
            "max_tokens": max_tokens, "temperature": temperature,
        }, sort_keys=True).encode()).hexdigest()[:32]

        path = self.cache_dir / f"{key}.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return LlmResponse(**payload)

        response = self.inner.complete(messages, system, max_tokens, temperature)
        path.write_text(json.dumps({
            "text": response.text, "model": response.model,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "stop_reason": response.stop_reason,
        }), encoding="utf-8")
        return response


def default_client(prefer_real: bool = False) -> LlmClient:
    """Real model when a key is present and asked for, offline otherwise.

    CI always takes the offline path, so the AI layer is exercised on every commit
    rather than being the part of the repo nobody can run.
    """
    if prefer_real and os.environ.get("ANTHROPIC_API_KEY"):
        return CachingLlm(inner=AnthropicLlm())
    return OfflineLlm()
