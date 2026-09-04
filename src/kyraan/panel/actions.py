"""The panel's first write path (Phase C, 2026-09-04): a review queue.

The brain surfaces what a list cannot — a contact that MAYBE is a
person, a fact wired to nothing — and until now the panel could only
point at it. Two actions, and only two, each the owner's own decision
taken on one item:

  forget_fact      deactivate one fact by id (memory.engine.forget: kept
                   as history, out of all retrieval, audited by the
                   engine itself as memory_forgotten)
  confirm_contact  give a person the contact's full name as an alias
                   (store.persons.add_alias), which turns the dashed
                   `maybe` wire into an exact `is` on the next build

Both refuse while the kill switch is engaged, both are logged as a
panel_review event with who-by (the panel token's owner) and what, and
neither takes a free-text argument: an id and a name the graph itself
handed out. The rest of the panel stays read-only; the never-writes test
still holds for every GET.
"""
from __future__ import annotations

from kyraan.control_plane import kill_switch
from kyraan.control_plane.logging_setup import log_event


class ReviewRefused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _gate():
    if kill_switch.is_engaged():
        raise ReviewRefused(423, "kill switch engaged — nothing changes until it is released")


def forget_fact(fact_id: str) -> dict:
    fact_id = str(fact_id or "").strip()
    if not fact_id or len(fact_id) > 80:
        raise ReviewRefused(400, "a fact id is needed")
    _gate()
    from kyraan.memory import engine
    forgotten = engine.forget([fact_id])
    log_event("panel_review", op="forget_fact", fact=fact_id, forgotten=len(forgotten))
    if not forgotten:
        raise ReviewRefused(404, "no active fact with that id")
    return {"forgotten": forgotten}


def confirm_contact(person: str, name: str) -> dict:
    person = str(person or "").strip().lower()
    name = str(name or "").strip()
    if not person or not name or len(name) > 120:
        raise ReviewRefused(400, "a person and the contact's name are needed")
    _gate()
    from kyraan.store import persons
    persons.add_alias(person, name)
    log_event("panel_review", op="confirm_contact", person=person, alias=name.lower())
    return {"person": person, "alias": name.lower()}
