"""Corporate action triage: read an announcement, produce the structured event.

Graded by index impact in basis points rather than classification accuracy. A misread
dividend amount is nearly free; a spin-off booked as a special dividend breaks divisor
continuity and moves the published level. Accuracy scores those the same. Basis points
do not.

Nothing here is imported by the calculation path.
"""
