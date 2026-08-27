"""Event-triggered rules (owner: 2026-08-27): "watch the AC; if it's
been on more than 3 hours, tell me." The MODEL translates words into a
rule at creation (confirm-gated — a standing autonomous behavior
deserves a yes); EVALUATION is pure determinism on a poll tick: read
the entity through the kernel, compare, honor the cooldown, notify.

Read-only by doctrine: a rule can check and tell, never act — standing
autonomous writes are a governance conversation, not a feature flag.

Conditions v1 (home entities only — deterministic and pollable):
- is <value> for >= N minutes  (HA's own last_changed makes duration
  stateless: no history tracking, no drift)
- above/below <number>         (sensors: temperature, watts)
"""
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from kyraan.control_plane import kernel
from kyraan.control_plane.dnd import local_now
from kyraan.control_plane.filelock import atomic_write_text, locked
from kyraan.control_plane.logging_setup import log_event

RULES_PATH = Path(__file__).resolve().parents[3] / "data" / "event_rules.json"
# File store only for now (rides the nightly tar like every data/ file);
# a PG mirror joins when rules prove themselves — noted, not forgotten.

_OPS = ("is", "above", "below")
DEFAULT_COOLDOWN_MIN = 120
_send_fn = None


@dataclass
class Rule:
    id: str
    chat_id: int
    description: str
    entity: str
    op: str                 # is | above | below
    value: str              # state string, or number as string
    for_minutes: int = 0    # only for op == "is"
    message: str = ""
    cooldown_minutes: int = DEFAULT_COOLDOWN_MIN
    last_fired_iso: str = ""
    active: bool = True


def _load() -> list:
    try:
        return json.loads(RULES_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(records: list) -> None:
    RULES_PATH.parent.mkdir(exist_ok=True)
    atomic_write_text(RULES_PATH, json.dumps(records, indent=1))


def _known_entities() -> set:
    server = (kernel.config.load().get("tool_servers") or {}).get(
        "home_assistant") or {}
    return set((server.get("read_entities") or [])
               + (server.get("write_entities") or []))


def create(chat_id: int, description: str, entity: str, op: str, value: str,
           for_minutes: int = 0, message: str = "",
           cooldown_minutes: int = DEFAULT_COOLDOWN_MIN) -> Rule:
    if op not in _OPS:
        raise ValueError(f"op must be one of {_OPS}")
    if entity not in _known_entities():
        raise ValueError(f"entity {entity!r} is not in the Home Assistant "
                         "allowlist — name one of the configured entities")
    if op in ("above", "below"):
        float(value)  # must be numeric
    for_minutes = max(0, int(for_minutes))
    cooldown_minutes = max(15, int(cooldown_minutes))
    if not 2 <= len(description) <= 200:
        raise ValueError("give the rule a short description")
    rule = Rule(id=uuid.uuid4().hex[:8], chat_id=chat_id,
                description=description.strip(), entity=entity, op=op,
                value=str(value), for_minutes=for_minutes,
                message=message.strip(), cooldown_minutes=cooldown_minutes)
    with locked(RULES_PATH):
        records = _load()
        records.append(asdict(rule))
        _save(records)
    log_event("event_rule_created", rule_id=rule.id, entity=entity,
              op=op, value=value, for_minutes=for_minutes)
    return rule


def list_active(chat_id: int | None = None) -> list:
    rules = [Rule(**r) for r in _load() if r.get("active")]
    if chat_id is not None:
        rules = [r for r in rules if r.chat_id == chat_id]
    return rules


def cancel(chat_id: int, rule_id_prefix: str) -> Rule:
    wanted = rule_id_prefix.strip().lower()
    with locked(RULES_PATH):
        records = _load()
        matches = [r for r in records
                   if r.get("active") and r["chat_id"] == chat_id
                   and r["id"].startswith(wanted)]
        if not matches:
            raise ValueError(f"no active watch rule with id {wanted!r} — list first")
        if len(matches) > 1:
            raise ValueError(f"{len(matches)} rules match {wanted!r} — use the full id")
        matches[0]["active"] = False
        _save(records)
    log_event("event_rule_cancelled", rule_id=matches[0]["id"])
    return Rule(**matches[0])


def condition_met(rule: Rule, state: dict, now=None) -> bool:
    """Pure: does this observed entity state satisfy the rule?"""
    now = now or datetime.now(timezone.utc)
    raw = str(state.get("state", ""))
    if rule.op == "is":
        if raw.lower() != rule.value.lower():
            return False
        if rule.for_minutes <= 0:
            return True
        changed = state.get("last_changed")
        if not changed:
            return False  # cannot prove the duration — don't fire
        try:
            held = (now - datetime.fromisoformat(changed)).total_seconds() / 60
        except ValueError:
            return False
        return held >= rule.for_minutes
    try:
        number = float(raw)
    except ValueError:
        return False
    return number > float(rule.value) if rule.op == "above" \
        else number < float(rule.value)


def _in_cooldown(rule: Rule, now=None) -> bool:
    if not rule.last_fired_iso:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        fired = datetime.fromisoformat(rule.last_fired_iso)
    except ValueError:
        return False
    return (now - fired).total_seconds() < rule.cooldown_minutes * 60


def _mark_fired(rule_id: str) -> None:
    with locked(RULES_PATH):
        records = _load()
        for record in records:
            if record["id"] == rule_id:
                record["last_fired_iso"] = datetime.now(timezone.utc).isoformat()
        _save(records)


def init(send_fn) -> None:
    global _send_fn
    _send_fn = send_fn


async def tick(send=None) -> int:
    """One evaluation pass over every active rule. Fired count returned.
    A DND-blocked notification does NOT mark the rule fired — it re-fires
    on the first tick after quiet hours, still true, still unheard.
    `send` (job-context-bound in the bot) overrides the init() fallback."""
    send = send or _send_fn
    fired = 0
    for rule in list_active():
        try:
            state = await kernel.run_tool(kernel.ToolCall(
                "home.get_state", {"entity": rule.entity}))
            if not isinstance(state, dict) or not condition_met(rule, state):
                continue
            if _in_cooldown(rule):
                continue
            if not kernel.can_send_proactively(chat_id=rule.chat_id):
                log_event("event_rule_held_dnd", rule_id=rule.id)
                continue
            # The custom message carries the live reading too — the owner
            # got "above 27°C" with the actual 27.7 nowhere in sight.
            reading = str(state.get("state"))
            text = (f"{rule.message} (now: {reading})" if rule.message
                    else f"👁 Watch rule: {rule.description} — "
                         f"{rule.entity} is {reading}.")
            if send is not None:
                await send(rule.chat_id, text)
            _mark_fired(rule.id)
            fired += 1
            log_event("event_rule_fired", rule_id=rule.id,
                      entity=rule.entity, state=str(state.get("state"))[:40])
        except Exception as exc:
            log_event("event_rule_error", rule_id=rule.id,
                      error=str(exc)[:150])
    return fired
