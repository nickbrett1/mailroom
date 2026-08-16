"""Tests for MsgvaultClient against the real msgvault REST API (mocked).

The client hits the REST API (`/api/v1/messages/filter`, `/messages/{id}`,
`/stats`) — NOT the MCP path, whose daemon-client adapter drops body_html
(memos/ZgBU8cXUQ7PkyRFKHGrZ8e).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mailroom.clients import MsgvaultClient, recover_webview_html
from mailroom.verticals.game_catalog.classifier import classify_item
from mailroom.verticals.game_catalog.parsers.bestbuy import parse_bestbuy_receipt

MSGS = [
    {"id": 30, "subject": "Thank You For Your Purchase", "from_email": "sony@email.sonyentertainmentnetwork.com", "sent_at": "2022-12-14T13:33:42Z"},
    {"id": 20, "subject": "Thank You For Your Purchase", "from_email": "sony@email.sonyentertainmentnetwork.com", "sent_at": "2022-12-01T10:00:00Z"},
    {"id": 10, "subject": "Some Other Mail", "from_email": "sony@email.sonyentertainmentnetwork.com", "sent_at": "2022-11-01T10:00:00Z"},
]


def _client(handler) -> MsgvaultClient:
    transport = httpx.MockTransport(handler)
    return MsgvaultClient("http://msgvault:8080", retries=1, client=httpx.Client(base_url="http://msgvault:8080", transport=transport))


def _filter_handler(messages: list[dict], page_size: int = 2):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/messages/filter"
        offset = int(request.url.params.get("offset", "0"))
        chunk = messages[offset : offset + page_size]
        return httpx.Response(
            200,
            json={
                "count": len(messages),
                "has_more": offset + len(chunk) < len(messages),
                "offset": offset,
                "limit": page_size,
                "messages": chunk,
            },
        )

    return handler


def test_search_subject_filter_client_side():
    client = _client(_filter_handler(MSGS))
    out = client.search_messages(sender="sony@email.sonyentertainmentnetwork.com", subject="Thank You For Your Purchase", limit=10)
    assert [m["id"] for m in out] == [30, 20]


def test_cursor_stops_at_cursor_id():
    client = _client(_filter_handler(MSGS))
    out = client.search_messages(sender="sony@email.sonyentertainmentnetwork.com", after="20", limit=10)
    assert [m["id"] for m in out] == [30]


def test_cursor_accepts_int():
    client = _client(_filter_handler(MSGS))
    assert client.search_messages(sender="x@y.z", after=30, limit=10) == []


def test_paging_across_pages():
    client = _client(_filter_handler(MSGS, page_size=2))
    out = client.search_messages(sender="sony@email.sonyentertainmentnetwork.com", limit=10)
    assert [m["id"] for m in out] == [30, 20, 10]


def test_get_message_returns_body_and_body_html():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/messages/53008"
        return httpx.Response(
            200,
            json={
                "id": 53008,
                "subject": "Thank You For Your Purchase",
                "from_email": "sony@email.sonyentertainmentnetwork.com",
                "sent_at": "2022-12-14T13:33:42Z",
                "has_attachments": False,
                "body": "plain text receipt",
                "body_html": "<html>receipt</html>",
            },
        )

    msg = _client(handler).get_message(53008)
    assert msg["id"] == 53008
    assert msg["body_text"] == "plain text receipt"
    assert msg["body_html"] == "<html>receipt</html>"
    assert msg["from_email"].startswith("sony@")


def test_get_stats():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_messages": 121875, "active_messages": 121875})

    stats = _client(handler).get_stats()
    assert stats["total_messages"] == 121875


def test_5xx_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"total_messages": 1})

    # retries=1 -> no retry: the 500 surfaces immediately.
    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.get_stats()

    # retries=3 -> the transient 500 is retried and the call succeeds.
    calls["n"] = 0
    client2 = MsgvaultClient("http://msgvault:8080", retries=3, client=httpx.Client(base_url="http://msgvault:8080", transport=httpx.MockTransport(handler)))
    assert client2.get_stats()["total_messages"] == 1
    assert calls["n"] == 2


def test_429_rate_limit_retries():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"total_messages": 1})

    client = MsgvaultClient("http://msgvault:8080", retries=3, client=httpx.Client(base_url="http://msgvault:8080", transport=httpx.MockTransport(handler)))
    assert client.get_stats()["total_messages"] == 1
    assert calls["n"] == 2


def test_recover_webview_html_fetches_bestbuy_link():
    html = "<html>DRAGON QUEST III HD-2D Remake - PlayStation 5</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html)

    client = _client(handler)
    stub = "View as a Web page:\r\nhttps://click.emailinfo2.bestbuy.com/?qs=ABC123\r\n"
    assert recover_webview_html(stub, client=client) == html


def test_recover_webview_html_none_when_no_link():
    assert recover_webview_html("no link here") is None


def test_bestbuy_25977_parses_from_real_body_html():
    """Definition of done (memos/kVHa96tZPovZwscG3CgNik): Best Buy parses a
    confirmation natively from body_html with a real sample (msg 25977)."""
    html = Path(__file__).parent.joinpath("fixtures", "bestbuy_25977.html").read_text()
    assert len(html) > 10_000  # real full HTML, not a stub
    p = parse_bestbuy_receipt(body_html=html, message_id="25977")
    assert p is not None
    assert p.order_number == "BBY01-807003276801"
    assert len(p.items) == 1
    item = p.items[0]
    assert item.title == "God of War III Remastered Standard Edition - PlayStation 4"
    assert item.price == "$9.99"
    assert item.qty == 1
    assert p.subtotal == "$9.99"
    assert p.total == "$5.43"
    assert classify_item(item.title, platform_hint=item.platform_hint).classification == "playstation_game"
