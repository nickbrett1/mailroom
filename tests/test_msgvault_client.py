"""Tests for MsgvaultClient against the real msgvault MCP interface (mocked).

The live msgvault endpoint (verified 2026-08-16) is a streamable-HTTP MCP
server: `http://msgvault:8082/mcp`. These tests exercise the client's MCP
JSON-RPC calls with an httpx MockTransport, covering request shaping, cursor
paging, and full-body paging — without touching the NAS.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mailroom.clients import MsgvaultClient


def _mcp_handler(responses: dict[str, object]):
    """Build a MockTransport handler that answers tools/call per tool name.

    `responses` maps tool name -> either a callable(args)->dict (for dynamic
    responses) or a fixed dict/object.
    """
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["method"] == "tools/call"
        name = payload["params"]["name"]
        args = payload["params"].get("arguments") or {}
        calls.append({"name": name, "args": args})
        spec = responses.get(name)
        result = spec(args) if callable(spec) else spec
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps(result)}]}},
        )

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def _client(responses: dict[str, object]) -> tuple[MsgvaultClient, object]:
    handler = _mcp_handler(responses)
    transport = httpx.MockTransport(handler)
    client = MsgvaultClient("http://msgvault:8082/mcp", client=httpx.Client(base_url="http://msgvault:8082/mcp", transport=transport))
    return client, handler


MSGS = [
    {"id": 30, "subject": "Thank You For Your Purchase", "from_email": "sony@email.sonyentertainmentnetwork.com", "sent_at": "2022-12-14T13:33:42Z"},
    {"id": 20, "subject": "Thank You For Your Purchase", "from_email": "sony@email.sonyentertainmentnetwork.com", "sent_at": "2022-12-01T10:00:00Z"},
    {"id": 10, "subject": "Thank You For Your Purchase", "from_email": "sony@email.sonyentertainmentnetwork.com", "sent_at": "2022-11-01T10:00:00Z"},
]


def test_search_with_subject_uses_metadata_query():
    client, handler = _client({"search_metadata": {"data": MSGS, "total": 3, "has_more": False}})
    out = client.search_messages(
        sender="sony@email.sonyentertainmentnetwork.com",
        subject="Thank You For Your Purchase",
        limit=10,
    )
    call = handler.calls[0]  # type: ignore[attr-defined]
    assert call["name"] == "search_metadata"
    assert call["args"]["query"] == 'from:sony@email.sonyentertainmentnetwork.com subject:"Thank You For Your Purchase"'
    assert [m["id"] for m in out] == [30, 20, 10]


def test_search_sender_only_uses_list_messages():
    client, handler = _client({"list_messages": {"data": MSGS, "has_more": False}})
    out = client.search_messages(sender="sony@email.sonyentertainmentnetwork.com", limit=10)
    call = handler.calls[0]  # type: ignore[attr-defined]
    assert call["name"] == "list_messages"
    assert call["args"]["from"] == "sony@email.sonyentertainmentnetwork.com"
    assert "query" not in call["args"]
    assert len(out) == 3


def test_cursor_stops_at_cursor_id():
    client, _ = _client({"list_messages": {"data": MSGS, "has_more": False}})
    out = client.search_messages(sender="x@y.z", after="20", limit=100)
    assert [m["id"] for m in out] == [30]


def test_cursor_accepts_int():
    client, _ = _client({"list_messages": {"data": MSGS, "has_more": False}})
    out = client.search_messages(sender="x@y.z", after=30, limit=100)
    assert out == []


def test_cursor_pages_across_pages():
    pages = [
        {"data": [MSGS[0], MSGS[1]], "has_more": True},
        {"data": [MSGS[2]], "has_more": False},
    ]

    def list_messages(args):
        return pages[min(args["offset"] // 2, 1)]

    client, handler = _client({"list_messages": list_messages})
    out = client.search_messages(sender="x@y.z", limit=100)
    assert [m["id"] for m in out] == [30, 20, 10]
    assert [c["args"]["offset"] for c in handler.calls] == [0, 2]  # type: ignore[attr-defined]


def test_get_message_pages_full_body():
    body = "".join(str(i % 10) for i in range(250))  # 250 chars

    def get_message(args):
        offset = args["offset"]
        chunk = body[offset : offset + 100]
        return {
            "id": 53008,
            "subject": "Thank You For Your Purchase",
            "from_email": "sony@email.sonyentertainmentnetwork.com",
            "sent_at": "2022-12-14T13:33:42Z",
            "body_format": "text",
            "body_text": chunk,
            "body_html": "",
            "offset": offset,
            "body_returned": len(chunk),
            "has_more": offset + len(chunk) < len(body),
        }

    client, handler = _client({"get_message": get_message})
    msg = client.get_message(53008)
    assert msg["body_text"] == body
    assert msg["body_format"] == "text"
    assert msg["id"] == 53008
    assert [c["args"]["offset"] for c in handler.calls] == [0, 100, 200]  # type: ignore[attr-defined]


def test_get_stats():
    client, _ = _client({"get_stats": {"accounts": [{"Identifier": "nick.brett1@gmail.com"}], "stats": {"MessageCount": 121943}}})
    assert client.get_stats()["stats"]["MessageCount"] == 121943


def test_mcp_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad args"}})

    client = MsgvaultClient("http://msgvault:8082/mcp", client=httpx.Client(base_url="http://msgvault:8082/mcp", transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError, match="bad args"):
        client.get_stats()
