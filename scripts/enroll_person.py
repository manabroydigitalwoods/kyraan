"""Owner-run enrollment ceremony (P3.5a, governance §8).

    scripts/enroll_person.py                                   # list
    scripts/enroll_person.py <person_id> <chat_id> <stage> <consent YYYY-MM-DD>
    scripts/enroll_person.py <person_id> none                  # revoke access
    scripts/enroll_person.py --extraction <person_id> on|off   # §4 first-month flag

Stages: none (no access) | read_mostly | full. Enrolling at any stage
above none REFUSES while an active fact has an unreviewed subject.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[1]
load_dotenv(REPO / ".env")

from kyraan.store import persons  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if not args:
        for pid, chat_id, stage, consented in persons.list_persons():
            print(f"  {pid:12s} stage={stage:12s} chat={chat_id} "
                  f"consented={consented}")
        return 0
    if args[0] == "--extraction" and len(args) == 3 and args[2] in ("on", "off"):
        persons.set_extraction(args[1], args[2] == "on")
        print(f"{args[1]}: extraction {'enabled' if args[2] == 'on' else 'disabled'}")
        return 0
    if len(args) == 2 and args[1] == "none":
        row = next((r for r in persons.list_persons() if r[0] == args[0]), None)
        if row is None:
            print(f"no person {args[0]!r}")
            return 1
        persons.enroll(args[0], row[1], "none", str(row[3] or ""))
        print(f"{args[0]}: access revoked (stage=none)")
        return 0
    if len(args) != 4:
        print(__doc__)
        return 2
    person_id, chat_id, stage, consented = args
    try:
        persons.enroll(person_id, int(chat_id), stage, consented)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"enrolled {person_id} at stage={stage} (chat {chat_id}, "
          f"consented {consented})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
