"""Evaluation harness for the AI layer.

The part everyone skips, and the part that makes the rest credible. Without it, "we
built a methodology assistant" is an anecdote. With it, the claim becomes: *"it answers
87% of eligibility questions correctly with correct citations, and on the remaining 13%
it abstains rather than guessing."* Only the second version survives a governance
review.

Four metrics, and the last two matter more than the first two:

* **answer accuracy** - does the answer contain the required facts
* **citation precision** - do the cited sources actually support it
* **abstention correctness** - does it decline when the answer is not in the corpus.
  A system that never abstains scores well on questions it can answer and is dangerous
  on the rest.
* **hallucinated numbers** - any figure not present in the retrieved text. Should be
  zero, and any non-zero value is disqualifying regardless of the other three.

The eval set deliberately includes unanswerable questions. Measuring only on questions
with answers rewards confident guessing, which is the failure mode that actually causes
harm.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from miniftse.agents.rag import MethodologyAssistant

_NUMBER = re.compile(r"\d+\.?\d*%?")


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One graded question."""

    case_id: str
    question: str
    category: str
    must_contain: tuple[str, ...] = ()
    """Substrings that must appear, case-insensitively. Kept to key facts rather than
    exact phrasing - grading on wording measures the model's style, not its
    correctness."""

    must_not_contain: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    should_abstain: bool = False
    difficulty: str = "standard"
    note: str = ""


@dataclass
class CaseResult:
    case: EvalCase
    answer: str
    citations: list[str]
    abstained: bool
    contains_required: bool
    avoids_forbidden: bool
    citation_precision: float
    abstention_correct: bool
    hallucinated_numbers: list[str]
    passed: bool

    def explain(self) -> str:
        marks = []
        if not self.contains_required:
            missing = [t for t in self.case.must_contain if t.lower() not in self.answer.lower()]
            marks.append(f"missing required content: {missing}")
        if not self.avoids_forbidden:
            present = [t for t in self.case.must_not_contain if t.lower() in self.answer.lower()]
            marks.append(f"contains forbidden content: {present}")
        if not self.abstention_correct:
            marks.append(
                "abstained when it should have answered"
                if self.abstained
                else "answered when it should have abstained"
            )
        if self.hallucinated_numbers:
            marks.append(f"numbers not in the retrieved text: {self.hallucinated_numbers}")
        if self.citation_precision < 1.0 and self.case.expected_sources:
            marks.append(f"citation precision {self.citation_precision:.0%}")
        return "; ".join(marks) or "passed"


@dataclass
class EvalReport:
    results: list[CaseResult]
    assistant_stats: dict[str, int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    @property
    def citation_precision(self) -> float:
        scored = [r for r in self.results if r.case.expected_sources and not r.abstained]
        return (sum(r.citation_precision for r in scored) / len(scored)) if scored else 1.0

    @property
    def abstention_accuracy(self) -> float:
        return (
            sum(r.abstention_correct for r in self.results) / len(self.results)
            if self.results
            else 0.0
        )

    @property
    def hallucination_rate(self) -> float:
        return (
            sum(bool(r.hallucinated_numbers) for r in self.results) / len(self.results)
            if self.results
            else 0.0
        )

    def by_category(self) -> pd.DataFrame:
        rows = [
            {"category": r.case.category, "passed": r.passed, "abstained": r.abstained}
            for r in self.results
        ]
        return (
            pd.DataFrame(rows)
            .groupby("category", as_index=False)
            .agg(n=("passed", "size"), passed=("passed", "sum"), abstained=("abstained", "sum"))
            .assign(accuracy=lambda d: d["passed"] / d["n"])
        )

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    def headline(self) -> str:
        """The sentence to put in the interview and the governance paper."""
        return (
            f"The methodology assistant answers {self.accuracy:.0%} of "
            f"{len(self.results)} evaluation questions correctly, with "
            f"{self.citation_precision:.0%} citation precision. It correctly decides "
            f"whether to answer or abstain {self.abstention_accuracy:.0%} of the time, "
            f"and its hallucinated-number rate is {self.hallucination_rate:.0%}."
        )

    def summary(self) -> str:
        lines = [self.headline(), "", "By category:", self.by_category().to_string(index=False)]
        failures = self.failures()
        if failures:
            lines += ["", f"Failures ({len(failures)}):"]
            lines += [f"  {r.case.case_id} [{r.case.category}]: {r.explain()}" for r in failures]
        return "\n".join(lines)

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "case_id": r.case.case_id,
                    "category": r.case.category,
                    "difficulty": r.case.difficulty,
                    "passed": r.passed,
                    "abstained": r.abstained,
                    "should_abstain": r.case.should_abstain,
                    "citation_precision": r.citation_precision,
                    "hallucinated": len(r.hallucinated_numbers),
                    "explanation": r.explain(),
                }
                for r in self.results
            ]
        )


def grade(
    case: EvalCase, answer: str, citations: list[str], abstained: bool, retrieved_text: str
) -> CaseResult:
    lowered = answer.lower()
    contains = all(t.lower() in lowered for t in case.must_contain) if case.must_contain else True
    avoids = not any(t.lower() in lowered for t in case.must_not_contain)

    if case.expected_sources and citations:
        matched = sum(
            1 for c in citations if any(e.lower() in c.lower() for e in case.expected_sources)
        )
        precision = matched / len(citations)
    else:
        precision = 1.0

    abstention_correct = abstained == case.should_abstain

    # A number in the answer that is nowhere in the retrieved text is invented,
    # whatever else the answer got right.
    #
    # The citations count as source text. They contain section and page references -
    # "§5.1", "p.7" - and an early version of this grader scored every correctly cited
    # answer as hallucinating, which made the metric worse than useless: it was
    # anti-correlated with the behaviour it was supposed to reward.
    in_answer = set(_NUMBER.findall(answer))
    in_context = set(_NUMBER.findall(retrieved_text + " " + " ".join(citations)))
    hallucinated = sorted(
        n for n in in_answer - in_context if n.rstrip("%") not in {"", "0", "1", "2", "3", "4", "5"}
    )

    passed = bool(
        abstention_correct
        and avoids
        and not hallucinated
        and (case.should_abstain or (contains and precision >= 0.5))
    )
    return CaseResult(
        case=case,
        answer=answer,
        citations=citations,
        abstained=abstained,
        contains_required=contains,
        avoids_forbidden=avoids,
        citation_precision=precision,
        abstention_correct=abstention_correct,
        hallucinated_numbers=hallucinated,
        passed=passed,
    )


def run_evals(assistant: MethodologyAssistant, cases: list[EvalCase]) -> EvalReport:
    results = []
    for case in cases:
        answer = assistant.ask(case.question)
        retrieved = "\n".join(c.text for c in answer.chunks)
        results.append(grade(case, answer.answer, answer.citations, answer.abstained, retrieved))
    return EvalReport(results=results, assistant_stats=assistant.stats())


# --------------------------------------------------------------------------------------
# The evaluation set
# --------------------------------------------------------------------------------------


def default_eval_set() -> list[EvalCase]:
    """Forty graded questions over the miniFTSE Ground Rules.

    Composition is deliberate:

    * **eligibility, weighting, review, corporate actions** - the routine questions the
      tool exists to answer
    * **regional variation** - rules that differ between developed and emerging markets,
      the case where a chunk without its section heading gives a confidently wrong
      answer
    * **out of scope** - live index levels and individual securities, which the
      methodology documents cannot answer and the assistant must decline
    * **not in the corpus** - plausible questions about rules that do not exist, which
      is where a system that always answers gets caught
    """
    return [
        # --- eligibility ------------------------------------------------------
        EvalCase(
            "E01",
            "What is the minimum free float for a developed market security?",
            "eligibility",
            must_contain=("5%",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "E02",
            "What is the minimum free float in an emerging market?",
            "eligibility",
            must_contain=("15%",),
            expected_sources=("ground_rules",),
            note="Differs from developed. Tests whether the section heading survived chunking.",
        ),
        EvalCase(
            "E03",
            "How is the liquidity screen defined?",
            "eligibility",
            must_contain=("median",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "E04",
            "Are depositary receipts eligible for the index?",
            "eligibility",
            must_contain=("local line",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "E05",
            "Are preferred shares included?",
            "eligibility",
            must_contain=("preferred",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "E06",
            "How many trading days of price history are required?",
            "eligibility",
            must_contain=("200",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "E07",
            "What is the minimum free-float market capitalisation?",
            "eligibility",
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "E08",
            "How does a foreign ownership limit affect eligibility?",
            "eligibility",
            must_contain=("foreign ownership",),
            expected_sources=("ground_rules",),
        ),
        # --- weighting and capping -------------------------------------------
        EvalCase(
            "W01",
            "How are constituents weighted?",
            "weighting",
            must_contain=("free float",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "W02",
            "What is the maximum weight of a single constituent?",
            "weighting",
            must_contain=("10%",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "W03",
            "Explain the 5/10/40 rule.",
            "weighting",
            must_contain=("40%",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "W04",
            "How is the capping factor applied?",
            "weighting",
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "W05",
            "Is the index capped between reviews?",
            "weighting",
            expected_sources=("ground_rules",),
            note="Tests the review-versus-daily distinction.",
        ),
        # --- review and banding ----------------------------------------------
        EvalCase(
            "R01",
            "How often is the index reviewed?",
            "review",
            must_contain=("quarter",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "R02",
            "How many days notice is given before review changes take effect?",
            "review",
            must_contain=("14",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "R03",
            "What is a buffer and why does the index use one?",
            "review",
            must_contain=("turnover",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "R04",
            "How wide is the size band buffer?",
            "review",
            must_contain=("2 percentage points",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "R05",
            "What is fast entry?",
            "review",
            must_contain=("IPO",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "R06",
            "Where are the boundaries between the size bands?",
            "review",
            must_contain=("70%",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "R07",
            "When are share count changes implemented?",
            "review",
            expected_sources=("ground_rules",),
        ),
        # --- corporate actions -------------------------------------------------
        EvalCase(
            "C01",
            "Does a cash dividend change the divisor?",
            "corporate_actions",
            must_contain=("not",),
            expected_sources=("ground_rules",),
            note="The answer is no, and it is the most common misunderstanding.",
        ),
        EvalCase(
            "C02",
            "What happens to the divisor on a share split?",
            "corporate_actions",
            must_contain=("unchanged",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "C03",
            "How is a rights issue treated?",
            "corporate_actions",
            must_contain=("TERP",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "C04",
            "What is TERP?",
            "corporate_actions",
            must_contain=("theoretical",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "C05",
            "How is a spin-off handled?",
            "corporate_actions",
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "C06",
            "What happens when a constituent is acquired for cash?",
            "corporate_actions",
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "C07",
            "How is a suspended security valued?",
            "corporate_actions",
            must_contain=("last",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "C08",
            "When is a special dividend treated as a return of capital?",
            "corporate_actions",
            expected_sources=("ground_rules",),
        ),
        # --- calculation --------------------------------------------------------
        EvalCase(
            "K01",
            "What is the index divisor for?",
            "calculation",
            must_contain=("continuous",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "K02",
            "What is the difference between gross and net total return?",
            "calculation",
            must_contain=("withholding",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "K03",
            "On what date are dividends reinvested?",
            "calculation",
            must_contain=("ex-date",),
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "K04",
            "Whose tax position does the net return index represent?",
            "calculation",
            must_contain=("non-resident",),
            expected_sources=("ground_rules",),
            difficulty="hard",
        ),
        EvalCase(
            "K05",
            "What is the index base date and base level?",
            "calculation",
            expected_sources=("ground_rules",),
        ),
        # --- governance -----------------------------------------------------------
        EvalCase(
            "G01",
            "Who can approve an exception to the rules?",
            "governance",
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "G02",
            "When is an index recalculated after an error?",
            "governance",
            expected_sources=("ground_rules",),
        ),
        EvalCase(
            "G03",
            "How is a methodology change consulted on?",
            "governance",
            expected_sources=("ground_rules",),
        ),
        # --- must abstain ----------------------------------------------------------
        EvalCase(
            "A01",
            "What is the index level today?",
            "out_of_scope",
            should_abstain=True,
            note="A live level comes from the calculation systems, not the methodology documents.",
        ),
        EvalCase(
            "A02",
            "Should I buy this index?",
            "out_of_scope",
            should_abstain=True,
            note="Investment advice. Must decline.",
        ),
        EvalCase(
            "A03",
            "What is the minimum free float under the MSCI methodology?",
            "out_of_scope",
            should_abstain=True,
            note="Another provider's rules. Answering from general knowledge is "
            "exactly the failure this guards against.",
        ),
        EvalCase(
            "A04",
            "What is the index's exposure to lithium mining?",
            "out_of_scope",
            should_abstain=True,
            note="A holdings question, not a methodology question.",
        ),
        EvalCase(
            "A05",
            "What is the carbon reduction requirement for this index?",
            "not_in_corpus",
            should_abstain=True,
            note="Plausible, and not a rule this index has. The parent index has "
            "no climate constraint.",
        ),
    ]


def save_eval_set(cases: list[EvalCase], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "case_id": c.case_id,
                    "question": c.question,
                    "category": c.category,
                    "must_contain": list(c.must_contain),
                    "must_not_contain": list(c.must_not_contain),
                    "expected_sources": list(c.expected_sources),
                    "should_abstain": c.should_abstain,
                    "difficulty": c.difficulty,
                    "note": c.note,
                }
                for c in cases
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load_eval_set(path: Path) -> list[EvalCase]:
    return [
        EvalCase(
            case_id=r["case_id"],
            question=r["question"],
            category=r["category"],
            must_contain=tuple(r.get("must_contain", [])),
            must_not_contain=tuple(r.get("must_not_contain", [])),
            expected_sources=tuple(r.get("expected_sources", [])),
            should_abstain=r.get("should_abstain", False),
            difficulty=r.get("difficulty", "standard"),
            note=r.get("note", ""),
        )
        for r in json.loads(path.read_text(encoding="utf-8"))
    ]


def triage_eval(drill_frame: pd.DataFrame, notes: list[Any]) -> dict[str, float]:
    """Score the triage agent against the chaos drill.

    The measurable claim: of the faults the validation layer detects, what fraction does
    the agent diagnose correctly? That is the number to put in the proposal, and it is
    also the number that decides whether the tool saves an analyst any time.
    """
    if not notes:
        return {"n_notes": 0.0}
    with_hypothesis = sum(
        1 for n in notes if n.hypotheses and n.hypotheses[0].likelihood in {"HIGH", "MEDIUM"}
    )
    material = sum(1 for n in notes if abs(n.materiality_bps) > 1.0)
    return {
        "n_notes": float(len(notes)),
        "with_confident_hypothesis": float(with_hypothesis),
        "confident_rate": with_hypothesis / len(notes),
        "material_findings": float(material),
        "n_faults_detected": float(drill_frame["detected"].sum()) if not drill_frame.empty else 0.0,
    }
