"""Gmail adapter — metadata-only reads, error classification. HTTP mocked."""
import io
import json
import urllib.request

import pytest

from kyraan.tools import gmail, registry


@pytest.fixture
def fake_gmail(monkeypatch):
    monkeypatch.setattr(gmail, "access_token", lambda: "tok")
    responses = {
        "/messages?q=is:unread&maxResults=2": {
            "resultSizeEstimate": 7,
            "messages": [{"id": "m1"}, {"id": "m2"}],
        },
        "/messages/m1?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date": {
            "payload": {"headers": [
                {"name": "From", "value": '"Suman Das" <suman@x.com>'},
                {"name": "Subject", "value": "Invoice pending"},
                {"name": "Date", "value": "Mon, 25 Aug 2026 18:00:00 +0530"},
            ]}
        },
        "/messages/m2?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date": {
            "payload": {"headers": [{"name": "From", "value": "noreply@bank.com"}]}
        },
    }

    def fake_urlopen(request, timeout=None):
        path = request.full_url.split("/gmail/v1/users/me", 1)[1]
        return io.BytesIO(json.dumps(responses[path]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


async def test_unread_returns_metadata_only(fake_gmail):
    result = await gmail.call("email.unread", {"limit": 2})
    assert result["unread_estimate"] == 7
    assert result["messages"][0] == {
        "from": '"Suman Das" <suman@x.com>', "subject": "Invoice pending",
        "date": "Mon, 25 Aug 2026 18:00:00 +0530",
    }
    assert result["messages"][1]["subject"] == "(no subject)"


async def test_unauthorized_scope_gives_rerun_instruction(monkeypatch):
    monkeypatch.setattr(gmail, "access_token", lambda: "tok")

    import urllib.error

    def deny(request, timeout=None):
        body = b'{"error": {"code": 403, "message": "Gmail API has not been used in project 123 before or it is disabled."}}'
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", deny)
    with pytest.raises(registry.ToolError, match="Gmail API has not been used"):
        await gmail.call("email.unread", {"limit": 1})


def test_access_token_is_cached_until_near_expiry(monkeypatch):
    """External review P2: one inbox check performed six OAuth refresh
    round-trips. The token now caches until 60s before expiry."""
    import io
    import json as j
    import urllib.request
    from kyraan.tools import google_auth

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "sec")
    monkeypatch.setenv("GOOGLE_OAUTH_REFRESH_TOKEN", "ref")
    google_auth._cached_token = None
    google_auth._cached_until = 0.0
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        return io.BytesIO(j.dumps({"access_token": "tok", "expires_in": 3600}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    for _ in range(6):
        assert google_auth.access_token() == "tok"
    assert len(calls) == 1
    google_auth._cached_token = None
    google_auth._cached_until = 0.0
