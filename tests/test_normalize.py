"""Regression tests for normalize()'s handling of malformed model output.

A small local model won't always follow the requested JSON schema exactly.
These are the concrete shapes observed live from llama3.2 that used to
crash or misroute — mocking router.call keeps the tests fast/deterministic
instead of depending on a live model's sampling variance.
"""
from kyraan.intent import normalize as normalize_module
from kyraan.intent.normalize import normalize


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


def _mock_call(monkeypatch, response_text: str) -> None:
    monkeypatch.setattr(normalize_module.router, "call", lambda **kwargs: _FakeResponse(response_text))


def test_null_intent_falls_back_to_unknown(monkeypatch):
    _mock_call(monkeypatch, '{"intent": null, "confidence": 0.8, "normalized_text": "hello"}')
    result = normalize("hello")
    assert result.intent == "unknown"


def test_intent_outside_known_set_falls_back_to_unknown(monkeypatch):
    """The model must return exactly one of KNOWN_INTENTS — anything else
    (e.g. "greeting") used to pass straight through to the orchestrator's
    exact-match dispatch and produce "I didn't recognize a supported
    command" instead of being treated as low-confidence/unknown."""
    _mock_call(monkeypatch, '{"intent": "greeting", "confidence": 0.9, "normalized_text": "hello"}')
    result = normalize("hello")
    assert result.intent == "unknown"


def test_null_confidence_does_not_crash(monkeypatch):
    """dict.get(key, default) only applies the default for a *missing* key
    — a key present with value null returns None, and float(None) used to
    raise an uncaught TypeError."""
    _mock_call(monkeypatch, '{"intent": "qa.answer", "confidence": null, "normalized_text": "hello"}')
    result = normalize("hello")
    assert result.intent == "qa.answer"
    assert result.confidence == 0.0


def test_null_normalized_text_falls_back_to_raw_text(monkeypatch):
    _mock_call(monkeypatch, '{"intent": "qa.answer", "confidence": 0.9, "normalized_text": null}')
    result = normalize("hello")
    assert result.normalized_text == "hello"


def test_well_formed_response_still_works(monkeypatch):
    _mock_call(monkeypatch, '{"intent": "qa.answer", "confidence": 1.0, "normalized_text": "hello"}')
    result = normalize("hello")
    assert result.intent == "qa.answer"
    assert result.confidence == 1.0
    assert result.normalized_text == "hello"
