"""The capability brief is generated from config + environment — Kyraan's
self-knowledge can't drift from what's actually built and connected."""
from kyraan.agents import capabilities


def test_full_setup_lists_all_live_capabilities(monkeypatch):
    for k in ("GOOGLE_CALENDAR_ICS_URL", "GOOGLE_OAUTH_CLIENT_ID",
              "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN",
              "HASS_URL", "HASS_TOKEN", "SEARXNG_URL", "GOOGLE_MAPS_API_KEY"):
        monkeypatch.setenv(k, "x")
    brief = capabilities.capability_brief()
    # abilities live in the tool menu now (token audit 2026-09-03); the
    # brief carries the truths the menu cannot: what is NOT connected,
    # what the internet really is, the privacy answer, the can't-do line
    assert "senders and subjects ONLY" in brief
    assert "Switchable home devices" in brief and "ac" in brief
    assert "EXACTLY the web.search tool" in brief
    assert "NOT CONNECTED" not in brief
    assert "you can NOT do yet" in brief  # the honest everything-else line
    from kyraan.agents import agent_loop
    menu = agent_loop._tools_block()
    assert "- calendar.list_events {" in menu and "- calendar.create_event {" in menu
    assert "- web.search {" in menu


def test_missing_setup_moves_capabilities_to_not_connected(monkeypatch):
    for k in ("GOOGLE_CALENDAR_ICS_URL", "GOOGLE_OAUTH_CLIENT_ID",
              "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN",
              "HASS_URL", "HASS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    brief = capabilities.capability_brief()
    assert "NOT CONNECTED" in brief
    assert "Calendar reading" in brief and "secret ICS URL" in brief
    assert "Home Assistant URL + token" in brief
    assert "Read the Google Calendar" not in brief  # not claimed as live


def test_reminders_and_memory_are_always_live(monkeypatch):
    for k in ("HASS_URL", "HASS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    brief = capabilities.capability_brief()
    assert "Remember stated personal facts" in brief
    from kyraan.agents import agent_loop
    assert "- reminders.create {" in agent_loop._tools_block()


def test_brief_denies_internet_and_answers_privacy(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    brief = capabilities.capability_brief()
    assert "NO INTERNET ACCESS" in brief
    assert "Never claim to look anything up online" in brief
    assert "nothing is ever used to train models" in brief


def test_privacy_answer_tracks_tier_providers(monkeypatch):
    """The privacy truths must follow the ACTUAL model config — after the
    local-only switch, claiming Groq processes conversations would be a
    false statement in the other direction."""
    from kyraan.control_plane import config

    base = config.load()
    monkeypatch.setattr(capabilities.config, "load", lambda: {
        **base, "model_tiers": {"cheap": {"provider": "ollama"}, "frontier": {"provider": "ollama"}}})
    assert "no conversation ever leaves the machine" in capabilities.capability_brief()

    monkeypatch.setattr(capabilities.config, "load", lambda: {
        **base, "model_tiers": {"cheap": {"provider": "ollama"}, "frontier": {"provider": "groq"}}})
    assert "groq cloud API" in capabilities.capability_brief()
