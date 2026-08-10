"""The ops desk: a read-mostly web surface over the miniftse library.

`snapshot.py` precomputes everything the application serves; the web layer loads those
files at startup and serves from memory. No index mathematics, no validation logic and
no retrieval logic lives here - every number on every page comes from a library call.
"""

from __future__ import annotations
