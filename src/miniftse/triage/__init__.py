"""Corporate action triage: read an announcement, produce the structured event.

Graded by index impact in basis points rather than classification accuracy. A misread
dividend amount is nearly free; a spin-off booked as a special dividend breaks divisor
continuity and moves the published level. Accuracy scores those the same. Basis points
do not.

The comparison is a **ratio, not two absolute numbers.** Any bps figure quoted for a
single misclassification is a figure about the constituent's index weight as much as
about the error: the canonical return-of-capital-as-dividend case costs 204 bps at 100%
weight, 67 bps on the three-name test fixture, and 2 bps at a realistic 1% large-cap
weight, where a misread amount costs 0.1 bps. The weight cancels out of the comparison
but not out of either figure, so the claim this package makes is that the
misclassification costs **20x** the misread parameter, at every weight - not that it
costs 67bp. See `verify.py` and D-022.

Not to be confused with `miniftse.agents.triage`, which shares the word and nothing else:
that module triages **data-quality alerts** from the quality layer, this one triages
**corporate action announcements** into structured index events. Neither imports the
other.

Nothing here is imported by the calculation path.
"""
