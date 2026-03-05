"""Shared fixtures for martol-agent tests."""

import hashlib
import hmac as hmac_lib
import json
from base64 import b64encode
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def hmac_secret():
    """A test HMAC secret."""
    return "test-secret-key-123"


def sign_message(msg: dict, secret: str) -> str:
    """Sign a message dict with HMAC-SHA256, returning the JSON with _hmac appended."""
    raw = json.dumps(msg)
    sig = hmac_lib.new(secret.encode(), raw.encode(), hashlib.sha256).digest()
    hmac_b64 = b64encode(sig).decode()
    # Append _hmac to the JSON (matching server behavior)
    return raw[:-1] + ',"_hmac":"' + hmac_b64 + '"}'


@pytest.fixture
def mock_ws():
    """A mock WebSocket connection."""
    ws = AsyncMock()
    ws.closed = False
    ws.send = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture
def mock_http_session():
    """A mock aiohttp ClientSession."""
    session = AsyncMock()
    resp = AsyncMock()
    resp.status = 200
    resp.json = AsyncMock(return_value={"ok": True})
    session.post.return_value.__aenter__ = AsyncMock(return_value=resp)
    session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    return session
