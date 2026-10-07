"""Aus offenen rids und der Queue-Laenge folgen Halter und Altbestand.

Rein: keine Uhr, kein I/O. Die Zeit kommt von aussen herein.
"""

from dataclasses import dataclass


@dataclass
class SlotState:
    holder_rid: str | None
    holder_since: float | None
    open_count: int
    legacy: int | None
    complete: bool


def evaluate(open_rids, waiting):
    open_count = len(open_rids)
    if waiting is None:
        return SlotState(None, None, open_count, None, False)

    legacy = max(0, waiting - open_count)
    complete = legacy == 0
    if not complete or not open_rids:
        return SlotState(None, None, open_count, legacy, complete)

    holder_rid = min(open_rids, key=lambda rid: open_rids[rid])
    return SlotState(holder_rid, open_rids[holder_rid], open_count, legacy, True)