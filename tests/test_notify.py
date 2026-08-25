"""ntfy notification tests for new-game catalog pushes (game_catalog/notify.py)."""

from __future__ import annotations

from mailroom.verticals.game_catalog.notify import notify_new_games, ntfy_endpoint


class _FakeResp:
    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, url, content, headers):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return _FakeResp()


def test_notify_new_games_posts_single_message(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://ntfy.test")
    monkeypatch.setenv("NTFY_NEW_GAME_TOPIC", "new-game-nickbrett-9831")
    client = _FakeClient()
    ok = notify_new_games(
        [{"title": "Hades II", "igdb_id": 123}, {"title": "Slay the Spire 2", "igdb_id": 456}],
        had_prior_games=True,
        client=client,
    )
    assert ok is True
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "http://ntfy.test/new-game-nickbrett-9831"
    assert "Hades II" in call["content"] and "Slay the Spire 2" in call["content"]
    assert "2 new games" in call["headers"]["Title"]


def test_notify_skips_initial_backfill():
    """A previously-empty store (initial backfill) must NOT notify."""
    assert notify_new_games([{"title": "Hades II"}], had_prior_games=False) is False


def test_notify_skips_when_no_new_games():
    assert notify_new_games([], had_prior_games=True) is False


def test_notify_respects_disable_env(monkeypatch):
    monkeypatch.setenv("NTFY_NEW_GAME_ENABLED", "0")
    assert notify_new_games([{"title": "Hades II"}], had_prior_games=True) is False


def test_ntfy_endpoint_defaults(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_NEW_GAME_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_NEW_GAME_ENABLED", raising=False)
    assert ntfy_endpoint() == ("https://ntfy.sh", "new-game-nickbrett-9831")
