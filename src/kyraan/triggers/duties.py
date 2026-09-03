"""The duties, in one place (owner 2026-09-04: "find gaps and fill
them"). Four duties speak on their own; nothing told the owner when
each last spoke or when it will next look. "duties status" does."""
import json
from pathlib import Path

from kyraan.control_plane.dnd import local_now


def _state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _latest(values) -> str:
    vals = [v for v in values if isinstance(v, str) and v]
    return max(vals)[:10] if vals else "never"


def status_text() -> str:
    from kyraan.triggers import chief_of_staff, house_steward, kiaan_keeper, whereabouts
    from kyraan.channels import voice_echo
    lines = ["🗓 Duties:"]
    # Kiaan's keeper
    ks = _state(kiaan_keeper.STATE_PATH)
    kt = kiaan_keeper.check_time()
    said = _latest(list((ks.get("nudged") or {}).values()) + list((ks.get("asked") or {}).values()))
    lines.append(f"• Kiaan's keeper — daily at {kt.strftime('%H:%M') if kt else 'off'}; last spoke {said}; "
                 f"{len(ks.get('done') or {})} dose(s) recorded by you. \"kiaan status\"")
    # chief of staff
    cs = _state(chief_of_staff.STATE_PATH)
    ct = chief_of_staff.still_open_time()
    lines.append(f"• Chief of staff — in the morning brief; still-open at {ct.strftime('%H:%M') if ct else 'off'} on weekdays; "
                 f"last still-open {_latest((cs.get('said') or {}).keys())}. \"what's open\"")
    # house steward
    hs = _state(house_steward.STATE_PATH)
    ht = house_steward.settle_time()
    energy = hs.get("energy") or {}
    lines.append(f"• House steward — settle check at {ht.strftime('%H:%M') if ht else 'off'}; "
                 f"energy ledger {len(energy)} night(s); filters {'flagged' if any(k.startswith('filter:') for k in (hs.get('markers') or {})) else 'fine'}. \"house status\"")
    # whereabouts
    ws = _state(whereabouts.STATE_PATH)
    last = ws.get("last")
    when = "never"
    if last:
        age = int((local_now().timestamp() - float(last.get("at", 0))) / 60)
        when = f"{age} min ago"
    lines.append(f"• Whereabouts — last fix {when}; {len(ws.get('places') or {})} saved place(s); "
                 f"homeward nudge {'armed' if (ws.get('armed') or {}).get('homeward', True) else 'used this trip'}. \"where am I\"")
    # voice
    lines.append(f"• Voice — {'listening on ' + ', '.join(voice_echo.devices()) + f' every {voice_echo.poll_seconds()}s' if voice_echo.enabled() and voice_echo.devices() else 'off'}. "
                 "\"Alexa, house status\"")
    return "\n".join(lines)
