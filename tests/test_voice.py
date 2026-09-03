"""Kyraan's voice (2026-09-04)."""
from kyraan.agents.agent_loop import voice


def test_voice_strips_vocatives_emoji_and_eagerness():
    assert voice("Maan, the AC is on.") == "The AC is on."
    assert voice("Yes, Maan — here's what I have saved:") == "Here's what I have saved:"
    assert voice("Of course, Maan 😊 Volume set to 5 on Echo.") == "Volume set to 5 on Echo."
    assert voice("Done, Maan 😊 I started kids Hindi rhymes on Echo!!") == "I started kids Hindi rhymes on Echo!"
    assert voice("Sure thing! The venue is Sharma Garden.") == "The venue is Sharma Garden."


def test_voice_keeps_content_and_receipt_markers():
    assert voice("The bedroom is 26.7°C.") == "The bedroom is 26.7°C."
    assert voice("🏠 House right now:\n• Bedroom 26°C") == "🏠 House right now:\n• Bedroom 26°C"
    assert voice("Maan is your name; kiaan is your son.") == "Maan is your name; kiaan is your son."   # not a vocative
    assert voice("") == ""


def test_persona_config_reads_as_a_person():
    from kyraan.agents import agent_loop
    block = agent_loop._persona_block()
    assert "No emoji" in block and "name rarely" in block and "open with the answer" in block
