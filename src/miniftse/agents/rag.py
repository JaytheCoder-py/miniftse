"""Citation-grounded retrieval over methodology documents.

The obvious high-value internal tool at an index provider: hundreds of pages of Ground
Rules, policy documents and market notices that Research, Sales, Product and Client
Services all query constantly, and which almost nobody has read end to end.

Four things make it usable rather than a demo, and they are all about the *document
type* rather than about the model:

* **Section-aware chunking.** Ground Rules are hierarchical legal-ish text where the
  section heading carries essential scope. A chunk saying "the threshold is 5%" is
  worthless without "Section 6.2, Developed Markets" attached, because the emerging
  markets threshold is different and both sentences are true.
* **Every answer carries citations.** An uncited answer about a published rule is
  unusable: the person asking has to verify it, and if they must verify it they may as
  well have looked it up.
* **Superseded documents are marked and down-ranked.** Methodologies change. Answering
  from last year's Ground Rules is worse than not answering.
* **Abstention is a correct outcome.** Measured as its own metric in `evals`. A system
  that always answers is a system that guesses, and on a question about a published
  rule a guess is a liability.

Retrieval is BM25 plus a section-title boost. Deliberately not embeddings: this corpus
is small, highly technical and full of exact terms ("free float", "fast entry",
"buffer"), which lexical search handles well; it needs no API, no key and no index to
rebuild; and it is transparent, so a bad retrieval can be explained. Embeddings would
be the upgrade when the corpus grows or when paraphrase matters.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from miniftse.agents.llm import LlmClient, Message, OfflineLlm

_TOKEN = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset("""
a an and are as at be but by for from has have if in into is it its of on or that the
to was were will with what which when how does do
""".split())


def tokenise(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable passage, carrying everything a citation needs."""

    chunk_id: str
    document: str
    section: str
    text: str
    page: int | None = None
    version: str = "current"
    superseded_by: str | None = None
    effective_from: str | None = None

    @property
    def citation(self) -> str:
        parts = [self.document]
        if self.section:
            parts.append(f"§{self.section}")
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.superseded_by:
            parts.append(f"SUPERSEDED by {self.superseded_by}")
        return ", ".join(parts)

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None

    def for_context(self) -> str:
        return f"[{self.citation}]\n{self.text}"


@dataclass
class Document:
    name: str
    text: str
    version: str = "current"
    superseded_by: str | None = None
    effective_from: str | None = None


_SECTION = re.compile(r"^(#{1,6})\s+(.+)$|^(\d+(?:\.\d+)*)\s+([A-Z].{3,80})$", re.MULTILINE)


def chunk_document(
    document: Document, target_words: int = 220, overlap_words: int = 40
) -> list[Chunk]:
    """Split on section boundaries first, then on size within a section.

    Section-first because the heading is load-bearing context. A fixed-window splitter
    happily cuts "6.2 Developed Markets" from the threshold it governs, and the
    resulting chunk is not merely less useful - it is misleading, because it reads as a
    universal rule.
    """
    lines = document.text.splitlines()
    sections: list[tuple[str, list[str]]] = [("", [])]

    for line in lines:
        match = _SECTION.match(line)
        if match:
            title = (match.group(2) or "").strip()
            number = (match.group(3) or "").strip()
            body = (match.group(4) or "").strip()
            heading = f"{number} {body}".strip() if number else title
            sections.append((heading, []))
        else:
            sections[-1][1].append(line)

    chunks: list[Chunk] = []
    for section_title, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        words = body.split()
        step = max(target_words - overlap_words, 1)
        for start in range(0, len(words), step):
            piece = " ".join(words[start: start + target_words])
            if len(piece.split()) < 20 and chunks and start > 0:
                break
            index = len(chunks)
            chunks.append(Chunk(
                chunk_id=f"{document.name}#{index}",
                document=document.name,
                section=section_title,
                text=piece,
                page=1 + index // 3,
                version=document.version,
                superseded_by=document.superseded_by,
                effective_from=document.effective_from,
            ))
            if start + target_words >= len(words):
                break
    return chunks


@dataclass
class Bm25Index:
    """BM25 with a section-title boost.

    The boost matters because a query like "what is the free float threshold" should
    strongly prefer a chunk under a heading called "Free Float" over one that mentions
    free float in passing while discussing something else. In a document set organised
    by topic, the heading is the strongest signal available.
    """

    k1: float = 1.5
    b: float = 0.75
    section_boost: float = 2.5
    superseded_penalty: float = 0.35

    chunks: list[Chunk] = field(default_factory=list)
    _tokens: list[list[str]] = field(default_factory=list, repr=False)
    _df: Counter[str] = field(default_factory=Counter, repr=False)
    _avg_len: float = 0.0

    def add(self, chunks: list[Chunk]) -> "Bm25Index":
        for chunk in chunks:
            self.chunks.append(chunk)
            tokens = tokenise(chunk.text)
            self._tokens.append(tokens)
            for term in set(tokens):
                self._df[term] += 1
        lengths = [len(t) for t in self._tokens]
        self._avg_len = sum(lengths) / len(lengths) if lengths else 0.0
        return self

    def search(self, query: str, top_k: int = 5, include_superseded: bool = False
               ) -> list[tuple[Chunk, float]]:
        if not self.chunks:
            return []
        terms = tokenise(query)
        n = len(self.chunks)
        scored: list[tuple[Chunk, float]] = []

        for chunk, tokens in zip(self.chunks, self._tokens):
            if chunk.superseded_by and not include_superseded:
                continue
            counts = Counter(tokens)
            length = len(tokens) or 1
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if not tf:
                    continue
                df = self._df.get(term, 0)
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self._avg_len or 1))
                score += idf * tf * (self.k1 + 1) / denom

            section_tokens = set(tokenise(chunk.section))
            overlap = len(section_tokens & set(terms))
            if overlap:
                score += self.section_boost * overlap

            if chunk.superseded_by:
                score *= self.superseded_penalty
            if score > 0:
                scored.append((chunk, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


@dataclass
class RetrievedAnswer:
    question: str
    answer: str
    citations: list[str]
    chunks: list[Chunk]
    scores: list[float]
    abstained: bool
    confidence: float

    def format(self) -> str:
        lines = [self.answer]
        if self.citations:
            lines += ["", "Sources:"] + [f"  - {c}" for c in self.citations]
        if self.abstained:
            lines += ["", "(The assistant abstained: retrieval did not find a passage "
                      "that answers this.)"]
        return "\n".join(lines)


SYSTEM_PROMPT = """You are a methodology assistant for an equity index provider.

Rules you must follow without exception:

1. Answer ONLY from the passages in <context>. If they do not contain the answer, say
   so plainly and stop. Do not use general knowledge about other index providers.
2. Cite the source for every substantive claim, using the bracketed reference given
   with each passage.
3. If passages conflict, say so and prefer the one marked current over one marked
   SUPERSEDED.
4. Never state a numeric threshold that does not appear verbatim in a passage.
5. If the question is about a specific security or a live index level, say that you
   cannot answer it - that requires the calculation systems, not the methodology
   documents.

Abstaining is a correct answer. Guessing about a published rule is not."""


@dataclass
class MethodologyAssistant:
    """Retrieval-augmented question answering over methodology documents."""

    index: Bm25Index = field(default_factory=Bm25Index)
    client: LlmClient = field(default_factory=OfflineLlm)
    top_k: int = 4
    min_score: float = 1.5
    """Below this the retrieval is treated as a miss and the assistant abstains.
    Tuned on the eval set: the alternative is answering from whatever came back, and
    something always comes back."""

    def add_document(self, document: Document) -> "MethodologyAssistant":
        self.index.add(chunk_document(document))
        return self

    def add_directory(self, directory: Path, pattern: str = "*.md"
                      ) -> "MethodologyAssistant":
        for path in sorted(Path(directory).glob(pattern)):
            self.add_document(Document(name=path.name,
                                       text=path.read_text(encoding="utf-8")))
        return self

    def ask(self, question: str, include_superseded: bool = False) -> RetrievedAnswer:
        # Scope check BEFORE retrieval.
        #
        # Retrieval score cannot detect an out-of-scope question, because an
        # out-of-scope question about this domain shares all its vocabulary with the
        # corpus. "What is the index level today" retrieves the calculation section with
        # a high score and produces a confident, sourced, useless answer. The only
        # reliable signal is the *kind* of question being asked, so it is checked first
        # and by rule.
        out_of_scope = self._scope_violation(question)
        if out_of_scope:
            return RetrievedAnswer(
                question=question, answer=out_of_scope, citations=[], chunks=[],
                scores=[], abstained=True, confidence=0.0,
            )

        hits = self.index.search(question, self.top_k, include_superseded)
        strong = [(c, s) for c, s in hits if s >= self.min_score]

        if not strong:
            return RetrievedAnswer(
                question=question,
                answer=(
                    "I cannot answer that from the methodology documents. Retrieval "
                    "found no passage above the relevance threshold, so any answer "
                    "would be a guess. Refer this to the index governance team."
                ),
                citations=[], chunks=[], scores=[], abstained=True, confidence=0.0,
            )

        context = "\n---\n".join(c.for_context() for c, _ in strong)
        prompt = (
            f"<context>\n{context}\n</context>\n\n"
            f"Question: {question}\n\n"
            "Answer using only the context above, citing sources."
        )
        response = self.client.complete(
            [Message("user", prompt)], system=SYSTEM_PROMPT, temperature=0.0
        )

        top = strong[0][1]
        total = sum(s for _, s in strong)
        return RetrievedAnswer(
            question=question, answer=response.text,
            citations=[c.citation for c, _ in strong],
            chunks=[c for c, _ in strong], scores=[s for _, s in strong],
            abstained=False,
            # Concentration of retrieval score in the top hit. A single decisive match
            # is far better evidence than four mediocre ones that happen to sum high.
            confidence=min(1.0, (top / total) * min(1.0, top / 10.0) * 2.0),
        )

    #: Question kinds the methodology corpus structurally cannot answer, with the
    #: reply each gets. Rule-based rather than model-judged so the refusal is
    #: consistent, explainable and cannot be talked out of by rephrasing.
    SCOPE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"\b(today|current|right now|latest|live)\b.*\b(level|value|price|"
                    r"return|performance)\b|\b(level|value|price)\b.*\b(today|right "
                    r"now|currently)\b", re.IGNORECASE),
         "I cannot answer that. A live or current index level comes from the "
         "calculation and distribution systems, not from the methodology documents. "
         "The Ground Rules describe how the level is computed, not what it is. Please "
         "use the index data service."),

        (re.compile(r"\bshould (i|we|you)\b|\bis it a good\b|\brecommend\b|"
                    r"\bworth (buying|investing)\b|\badvice\b", re.IGNORECASE),
         "I cannot answer that. That is a request for investment advice, which is "
         "outside the scope of a methodology assistant and outside what an index "
         "administrator may provide."),

        (re.compile(r"\b(msci|s&p|standard\s*&\s*poor|stoxx|nasdaq|solactive|bloomberg)\b",
                    re.IGNORECASE),
         "I cannot answer that. The question is about another index provider's "
         "methodology, which is not in this document set. Answering from general "
         "knowledge is exactly the failure mode this assistant is built to avoid - "
         "consult that provider's published rules."),

        (re.compile(r"\bexposure to\b|\bdoes (it|the index) hold\b|\bwhich (companies|"
                    r"stocks|securities)\b|\bconstituents? (list|are)\b|"
                    r"\bhow much .* in\b", re.IGNORECASE),
         "I cannot answer that. Holdings and exposures are properties of a particular "
         "index calculation on a particular date, not of the methodology. The Ground "
         "Rules define how constituents are selected; the constituent file says who "
         "they are."),
    )

    def _scope_violation(self, question: str) -> str | None:
        for pattern, reply in self.SCOPE_RULES:
            if pattern.search(question):
                return reply
        return None

    def stats(self) -> dict[str, int]:
        return {
            "documents": len({c.document for c in self.index.chunks}),
            "chunks": len(self.index.chunks),
            "sections": len({(c.document, c.section) for c in self.index.chunks}),
            "superseded_chunks": sum(1 for c in self.index.chunks if c.superseded_by),
            "vocabulary": len(self.index._df),
        }
