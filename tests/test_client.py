"""Tests for lib/client.py — Hindsight REST API client."""

import json
from unittest.mock import patch

from conftest import FakeHTTPResponse

from lib.client import USER_AGENT, HindsightClient


class TestUserAgentHeader:
    """Regression tests for #1041.

    The stdlib default ``Python-urllib/X.Y`` UA is blocked by Cloudflare with
    error 1010, so every request must carry our identifying UA.
    """

    def test_recall_sends_user_agent(self):
        c = HindsightClient("http://localhost:9077")
        captured = {}

        def fake_open(req, timeout=None):
            captured["ua"] = req.get_header("User-agent")
            return FakeHTTPResponse({"results": []})

        with patch("urllib.request.urlopen", side_effect=fake_open):
            c.recall("bank", "query")

        assert captured["ua"] == USER_AGENT
        assert captured["ua"].startswith("hindsight-codex/")


def test_retain_sends_named_strategy():
    c = HindsightClient("http://localhost:9077")
    captured = {}

    def fake_open(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return FakeHTTPResponse({"status": "accepted"})

    with patch("urllib.request.urlopen", side_effect=fake_open):
        c.retain("bank", "content", strategy="codex-hook")

    assert captured["body"]["items"][0]["strategy"] == "codex-hook"
