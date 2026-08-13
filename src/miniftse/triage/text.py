"""Filing text, and joining it to vendor labels.

The join is the honest problem in this stage. Vendor data says *a dividend of 0.24 went
ex on 2024-06-10*. A filing says *the Board declared a quarterly dividend*. Nothing
links them but the issuer and a date, so the join is a heuristic and is treated as one:
nearest filing within a window, same security, and **anything unjoined is dropped and
counted** rather than paired with a plausible neighbour.

A wrongly joined pair is worse than a dropped one. It puts the right label on the wrong
text, and every metric computed downstream inherits the error silently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from dataclasses import dataclass

from miniftse.triage.corpus import Announcement, LabelSource, Provenance
from miniftse.triage.labels import LabelledEvent


@dataclass(frozen=True, slots=True)
class FilingDocument:
    accession: str
    filed: dt.date
    url: str
    text: str
    security_id: str


def join_labels_to_text(
    labels: list[LabelledEvent],
    documents: list[FilingDocument],
    *,
    window_days: int = 5,
    retrieved: dt.date | None = None,
) -> tuple[list[Announcement], list[LabelledEvent]]:
    """Pair each label with the nearest filing for the same security.

    Returns `(joined, unjoined)`. The second element is not a failure to hide - the
    unjoined count is a reported property of the corpus, because a corpus that silently
    kept only the easy-to-join announcements would be biased in a way no metric reveals.
    """
    retrieved = retrieved or dt.date.today()
    by_security: dict[str, list[FilingDocument]] = {}
    for document in documents:
        by_security.setdefault(document.security_id, []).append(document)

    joined: list[Announcement] = []
    unjoined: list[LabelledEvent] = []

    for label in labels:
        target = label.event.announcement_date
        candidates = [
            document
            for document in by_security.get(label.security_id, [])
            if abs((document.filed - target).days) <= window_days
        ]
        if not candidates:
            unjoined.append(label)
            continue

        best = min(candidates, key=lambda d: (abs((d.filed - target).days), d.accession))
        joined.append(
            Announcement(
                announcement_id=f"{label.raw_event_id}::{best.accession}",
                security_id=label.security_id,
                text=best.text,
                provenance=Provenance(
                    source="sec-edgar",
                    url=best.url,
                    retrieved=retrieved,
                    sha256=hashlib.sha256(best.text.encode("utf-8")).hexdigest(),
                ),
                label=label.event,
                label_source=LabelSource.AUTO,
            )
        )

    return joined, unjoined


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(raw: str) -> str:
    """Reduce a filing's raw HTML body to plain text.

    Minimal and dependency-free by design: no HTML parser, just enough regex work to
    turn an 8-K body into readable prose worth hashing for `Provenance.sha256` and
    worth feeding to an extraction model. In order: drop `<script>`/`<style>` elements
    and their contents, strip every remaining tag, unescape entities (`&amp;`,
    `&nbsp;`, ...), then collapse whitespace runs - including the non-breaking spaces
    entity-decoding produces - to single spaces.

    What this does NOT handle, deliberately: a real HTML parser would be needed for
    any of it. Tags whose attributes contain a literal `>` (e.g. inside a quoted
    attribute value) truncate the match early. `<!-- comments -->` are stripped only
    incidentally, as ordinary "tags", and break on an embedded `>`. CDATA sections are
    not recognised. Self-closing `<script/>`/`<style/>` tags with no closing tag are
    stripped as plain tags rather than matched as a script/style block (they have no
    inline content to remove, so this is harmless). Block-level boundaries (`</p>`,
    `<br>`, `<td>`) do not insert semantic paragraph or line breaks - they become a
    single collapsed space like any other tag, so adjacent block text runs together
    with no punctuation.
    """
    without_scripts = _SCRIPT_STYLE_RE.sub(" ", raw)
    without_tags = _TAG_RE.sub(" ", without_scripts)
    unescaped = html.unescape(without_tags)
    return _WHITESPACE_RE.sub(" ", unescaped).strip()


SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
"""Filing index per issuer. The SEC requires a declared contact in the User-Agent and
rate-limits to 10 requests/second; ignoring either gets the IP blocked."""


def fetch_filings(
    cik: int,
    security_id: str,
    contact: str,
    *,
    forms: tuple[str, ...] = ("8-K",),
    since: dt.date | None = None,
    limit: int = 50,
) -> list[FilingDocument]:
    """Recent filings for one issuer. Requires network.

    Kept deliberately thin: it turns SEC JSON into `FilingDocument`, stripping the
    fetched body down to plain text on the way in, and nothing else. Every judgement
    about which filing matches which event lives in `join_labels_to_text`, which is
    pure and tested.
    """
    import requests

    headers = {"User-Agent": f"miniftse-research/0.1 (contact: {contact})"}
    response = requests.get(SUBMISSIONS_URL.format(cik=cik), headers=headers, timeout=30)
    response.raise_for_status()
    recent = response.json()["filings"]["recent"]

    documents: list[FilingDocument] = []
    for form, accession, filing_date, primary in zip(
        recent["form"],
        recent["accessionNumber"],
        recent["filingDate"],
        recent["primaryDocument"],
        strict=False,
    ):
        if form not in forms:
            continue
        filed = dt.date.fromisoformat(filing_date)
        if since and filed < since:
            continue

        bare = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{bare}/{primary}"
        body = requests.get(url, headers=headers, timeout=30)
        body.raise_for_status()
        documents.append(
            FilingDocument(
                accession=accession,
                filed=filed,
                url=url,
                text=_strip_html(body.text),
                security_id=security_id,
            )
        )
        if len(documents) >= limit:
            break
    return documents
