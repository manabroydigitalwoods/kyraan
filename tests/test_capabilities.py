"""The capability brief is generated from config + environment — Kyraan's
self-knowledge can't drift from what's actually built and connected."""
from kyraan.agents import capabilities


def test_full_setup_lists_all_live_capabilities(monkeypatch):
    for k in ("GOOGLE_CALENDAR_ICS_URL", "GOOGLE_OAUTH_CLIENT_ID",
              "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REFRESH_TOKEN",
              "HASS_URL", "HASS_TOKEN"):
        monkeypatch.setenv(k, "x")
    brief = capabilities.capability_brief()
    assert "Read the Google Calendar" in brief
    assert "Create calendar events" in brief
    assert "senders and subjects ONLY" in brief
    assert "smart plugs" in brief and "ac" in brief
    assert "NOT CONNECTED" not in brief
    assert "you can NOT do yet" in brief  # the honest everything-else line


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
    assert "Reminders: create, list, cancel" in brief
    assert "Remember stated personal facts" in brief


def test_brief_denies_internet_and_answers_privacy():
    brief = capabilities.capability_brief()
    assert "NO INTERNET ACCESS" in brief
    assert "Never claim to look anything up online" in brief
    assert "nothing is ever used to train models" in brief
