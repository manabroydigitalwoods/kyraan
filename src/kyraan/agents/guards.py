"""Deterministic guards — pure text heuristics, no model, no state.

Each of these exists because of a specific live failure (the comments
carry the incidents). They run in front of and underneath both brains:
the dispatch pre-guards, the burst gatherer, and the reminder/home
executors all draw from here. Extracted from orchestrator.py (G-04);
content unchanged."""


# Words that make up bare time-phrases ("tomorrow morning", "at 9",
# "tonight after dinner"). A message consisting ONLY of these is a
# fragment starting a thought — detected deterministically, because the
# classifier was seen turning "tomorrow morning" into a literal reminder
# named "tomorrow morning" at 6 AM.
_TIME_WORDS = {
    "tomorrow", "today", "tonight", "yesterday", "morning", "evening",
    "afternoon", "noon", "night", "midnight", "next", "this", "week",
    "month", "at", "on", "in", "am", "pm", "o'clock", "oclock", "after",
    "before", "around", "lunch", "dinner", "breakfast", "early", "late",
    "the", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
}


def is_time_fragment(text: str) -> bool:
    words = [w.strip(".,!?…") for w in text.lower().split()]
    words = [w for w in words if w]
    if not words:
        return False
    return all(w in _TIME_WORDS or w.replace(":", "").replace("am", "").replace("pm", "").isdigit()
               for w in words)


# Words that cannot END a finished English thought (prepositions,
# conjunctions, articles, bare auxiliaries) and words that START a message
# which is really the continuation of the previous one ("to buy something"
# after "I have to go to siliguri", seen live misread as a standalone
# request and turned into a junk reminder).
_TRAILING_OPEN = {
    "to", "and", "or", "but", "the", "a", "an", "my", "your", "our", "his",
    "her", "their", "for", "with", "of", "in", "at", "on", "so", "then",
    "about", "because", "if", "when", "while", "than", "into", "from",
    "by", "as", "will", "would", "can", "could", "should", "must", "have",
    "has", "had", "am", "are", "was", "were", "be", "been", "very",
    "really", "just", "also", "plus", "i", "we", "they", "he", "she",
}
_LEADING_OPEN = {
    "to", "and", "but", "or", "also", "then", "because", "so", "plus",
    "with", "for", "very", "really", "on", "in", "at", "about", "of",
    "from", "after", "before",
}

# A genuine reminder request contains remind-ish wording somewhere in the
# raw or normalized text ("remind", "reminder", "wake me", "don't
# forget", "tell me at 9", ...). A reminders.create classification
# without ANY of these is the classifier inventing intent.
_REMIND_WORDS = (
    "remind", "remember", "alarm", "alert", "wake me", "notify",
    "forget", "ping me", "timer", "tell me", "let me know",
)



def _is_review_request(text: str) -> bool:
    t = text.lower()
    return "review" in t and any(w in t for w in ("memory", "fact", "pending"))


# "are these latest emails", "is that all?" — a question ABOUT the reply
# Kyraan just sent. Re-running the tool and reprinting the same text (seen
# live 2026-08-26, twice in one session) reads as a broken record; a human
# answers the question. Shape: interrogative opener + a demonstrative
# pointing back at the previous reply.
_META_STARTERS = ("are", "is", "was", "were", "do", "does", "did", "really", "so")
_META_DEMONSTRATIVES = {"these", "this", "that", "those", "it", "they", "them"}
# "these emails are already shared by u" — a repetition COMPLAINT, same
# family: the user is talking about the previous reply, not requesting it.
_META_COMPLAINT_MARKERS = {"already", "again", "repeating", "repeated"}
_META_YOU = {"you", "u", "shared", "showed", "said", "told", "sent", "gave"}


def _is_meta_question(text: str) -> bool:
    words = [w.strip(".,!?\"'").lower() for w in text.split()]
    words = [w for w in words if w]
    if not words:
        return False
    if words[0] in _META_STARTERS and set(words) & _META_DEMONSTRATIVES:
        return True
    return bool(set(words) & _META_COMPLAINT_MARKERS) and bool(set(words) & _META_YOU)


_GREETING_WORDS = {
    "hi", "hii", "hiii", "hello", "helo", "hallo", "hey", "heya", "yo",
    "namaste", "good", "morning", "evening", "afternoon", "night", "there",
    "kyraan",
}


def _is_greeting(text: str) -> bool:
    words = [w.strip(".,!?…\"'").lower() for w in text.split()]
    words = [w for w in words if w]
    return bool(words) and all(w in _GREETING_WORDS for w in words)


# A genuine home/climate question names something in the home. A
# home.query classification whose text mentions none of these is the
# classifier guessing ("on my smoke havite" got the full AC dump, live).
_HOME_WORDS_EXACT = {"ac", "a/c", "air", "hot", "hub", "fan", "off"}
_HOME_WORD_STEMS = (
    "temp", "humid", "plug", "power", "energy", "watt", "kwh", "room",
    "bedroom", "home", "house", "device", "switch", "vacuum", "geyser",
    "heater", "climate", "degree", "cold", "warm", "sensor", "run",
)


def _mentions_home(text: str) -> bool:
    tokens = {w.strip(".,!?…'\"()") for w in text.lower().split()}
    if tokens & _HOME_WORDS_EXACT:
        return True
    return any(t.startswith(s) for t in tokens for s in _HOME_WORD_STEMS)


def thought_open(text: str) -> bool:
    """Deterministic "is the user still mid-thought?" — the channel's
    substitute for a human watching the typing indicator (Telegram never
    sends bots one). A message that trails off on a connector, ends in a
    comma/ellipsis, opens with a continuation word, or is a bare
    time-phrase means more is coming: wait, like a person would."""
    t = text.strip()
    if not t:
        return False
    if is_time_fragment(t):
        return True
    if t.endswith((",", ";", ":", "-", "—", "...", "…")):
        return True
    if t.endswith(("?", "!", ".")):
        return False
    words = [w.strip(".,!?…\"'") for w in t.lower().split()]
    words = [w for w in words if w]
    if not words:
        return False
    return words[-1] in _TRAILING_OPEN or words[0] in _LEADING_OPEN
