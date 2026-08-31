"""Synthetic brain for design work — never touches a real store.

Why this exists rather than a script that seeds the live memory tree: an
ACTIVE fact enters the model's memory block, so a dummy "favourite colour
is blue" is not decoration, it is something Kyraan will recall as true and
act on. Two eval fixtures had already leaked in by this route (found and
retired 2026-08-31). Demo data therefore lives only in this process, is
generated on demand, and is labelled as demo in the payload so the page
can say so out loud.

    KYRAAN_PANEL_DEMO=1 .venv/bin/python scripts/panel.py

Deterministic: one seed, so the same brain comes back every run and a
screenshot taken today matches one taken next week.
"""
import os
import random
from datetime import datetime, timedelta, timezone

SEED = 20260831

# Clusters of a plausible household brain. Each is a topic with a handful
# of templates; the generator fills them out. The point is not realism for
# its own sake — it is that the CLUSTERS be genuinely separable, so the
# projection, the k-means grouping and the synapse mesh are exercised the
# way real facts exercise them.
_TOPICS = {
    "family": [
        "{who}'s birthday is on {day} {month}",
        "{who} prefers {food} over anything else",
        "{who} is allergic to {allergen}",
        "{who}'s school term starts in {month}",
        "{who} calls every {day_name} evening",
        "{who} has a vaccination due in {month}",
    ],
    "work": [
        "the {project} project ships in {month}",
        "{who} is the reviewer for {project}",
        "standup for {project} moved to {time}",
        "{project} uses {tech} for its data layer",
        "the {project} retro is every second {day_name}",
    ],
    "health": [
        "blood test results are due in {month}",
        "{who} takes medication at {time} daily",
        "the clinic only answers before {time}",
        "physio appointments run every {day_name}",
        "{who} is tracking blood pressure weekly",
    ],
    "home": [
        "the {appliance} filter needs changing every {n} months",
        "the {appliance} is on the {room} circuit",
        "{room} temperature sits around {n} degrees",
        "the plumber's number is saved under {who}",
        "bin collection is {day_name} morning",
    ],
    "travel": [
        "the {city} trip is booked for {month}",
        "passport for {who} expires in {month}",
        "the {city} hotel allows late checkout",
        "{who} prefers an aisle seat",
        "the drive to {city} takes about {n} hours",
    ],
    "food": [
        "{who} does not eat {food}",
        "the {food} recipe needs {n} hours to rest",
        "the good {food} place closes on {day_name}",
        "we buy {food} from the market on {day_name}",
    ],
    "routine": [
        "water reminder every hour from {time}",
        "gym on {day_name} and {day_name}",
        "the weekly review happens {day_name} at {time}",
        "lights go off automatically at {time}",
    ],
}

_PEOPLE = ["owner", "ruma", "kiaan", "ganak_roy", "suman_ghosh", "titu_roy",
           "kamal", "avik"]
_FILL = {
    "who": _PEOPLE + ["the neighbour", "the landlord"],
    "food": ["dal", "biryani", "momos", "fish curry", "paneer", "idli"],
    "allergen": ["peanuts", "shellfish", "dust", "pollen"],
    "month": ["January", "March", "May", "July", "September", "November"],
    "day": ["3rd", "9th", "12th", "21st", "27th"],
    "day_name": ["Monday", "Tuesday", "Thursday", "Friday", "Sunday"],
    "time": ["7:00 AM", "9:30 AM", "1:00 PM", "6:45 PM", "9:00 PM"],
    "project": ["Woodsportal", "Atlas", "Harbour", "Kestrel"],
    "tech": ["Postgres", "Redis", "SQLite", "pgvector"],
    "appliance": ["AC", "water purifier", "geyser", "fridge"],
    "room": ["bedroom", "kitchen", "living room", "study"],
    "city": ["Siliguri", "Kolkata", "Darjeeling", "Gangtok"],
    "n": ["2", "3", "4", "6", "24", "26"],
}


def enabled() -> bool:
    return os.environ.get("KYRAAN_PANEL_DEMO", "").strip() not in ("", "0")


def _fill(rng, template: str) -> str:
    text = template
    while "{" in text:
        start = text.index("{")
        end = text.index("}", start)
        key = text[start + 1:end]
        text = text[:start] + rng.choice(_FILL[key]) + text[end + 1:]
    return text


def facts(count: int = 240) -> tuple:
    """(facts, vectors). Vectors are synthetic but STRUCTURED: one centre
    per topic plus jitter, so neighbours are genuinely near each other and
    the projection, clustering and mesh behave as they do on real data.
    Random noise would have made every layout look the same — a blob."""
    rng = random.Random(SEED)
    dims = 24
    centres = {topic: [rng.uniform(-1, 1) for _ in range(dims)]
               for topic in _TOPICS}

    rows, vectors = [], []
    start = datetime.now(timezone.utc) - timedelta(days=180)
    topics = list(_TOPICS)
    for index in range(count):
        topic = topics[index % len(topics)]
        content = _fill(rng, rng.choice(_TOPICS[topic]))
        subject = next((p for p in _PEOPLE if p in content), "owner")
        rows.append({
            "id": f"d{index:04x}",
            "content": content,
            "subject": subject,
            "kind": {"family": "relationship", "work": "work",
                     "health": "other", "home": "preference",
                     "travel": "other", "food": "preference",
                     "routine": "routine"}[topic],
            "sphere": "work" if topic == "work" else "personal",
            "era": "current" if rng.random() > 0.12 else "past",
            "term": "long" if rng.random() > 0.3 else "short",
            "importance": "high" if rng.random() > 0.82 else "normal",
            "active": rng.random() > 0.14,
            "target": f"{topic}/{index:04x}.md",
            "created": (start + timedelta(hours=index * 17)).isoformat(),
            "topic": topic,
        })
        centre = centres[topic]
        vectors.append([centre[d] + rng.gauss(0, 0.22) for d in range(dims)])
    return rows, vectors


def triples(rows: list) -> list:
    """A few relations between the people, plus one deliberate
    contradiction so the Findings console has something true to show."""
    rng = random.Random(SEED + 1)
    out = [
        ("kiaan", "son_of", "owner"), ("ruma", "wife_of", "owner"),
        ("ganak_roy", "father_of", "owner"), ("avik", "nephew_of", "owner"),
        ("kamal", "friend_of", "owner"), ("titu_roy", "brother_of", "owner"),
        ("suman_ghosh", "colleague_of", "owner"),
        # Same head+relation, two tails, from two different facts: a real
        # contested pair, which is what the console is for.
        ("kiaan", "born_on", "12_october_2025"),
        ("kiaan", "born_on", "14_october_2025"),
    ]
    ids = [r["id"] for r in rows if r["active"]]
    return [{"fact": rng.choice(ids), "from": head, "rel": rel, "to": tail}
            for head, rel, tail in out]


def tasks() -> list:
    rng = random.Random(SEED + 2)
    now = datetime.now(timezone.utc)
    plan = [
        ("reminder", "Drink water", 35 * 60, "interval"),
        ("reminder", "Call the clinic about the blood test", 3 * 3600, ""),
        ("reminder", "Kiaan's vaccination review", 26 * 3600, ""),
        ("reminder", "Pay the electricity bill", -2 * 3600, ""),
        ("agent_task", "Every evening, check tomorrow's calendar", 4 * 3600, "daily"),
        ("agent_task", "Weekly spend summary", 3 * 86400, "weekly"),
        ("goal", "Plan Kiaan's birthday", 20 * 3600, "every 24h"),
        ("goal", "Find a physio near Siliguri", 30 * 3600, "every 24h"),
    ]
    out = []
    for index, (kind, text, offset, repeat) in enumerate(plan):
        out.append({
            "type": kind, "id": f"demo{index:02d}", "text": text,
            "repeat": repeat, "chat_id": 1, "person": "owner",
            "steps_done": rng.randint(0, 2) if kind == "goal" else 0,
            "steps_total": 3 if kind == "goal" else 0,
            "fire": {"iso": (now + timedelta(seconds=offset)).isoformat(),
                     "in_seconds": offset, "overdue": offset < -60},
        })
    return out
