"""Legal endpoints — privacy policy + version metadata ([7.4.4]).

Coverage:
  1. /api/legal/privacy returns the markdown policy with the Italian title
     and the V0-alpha disclaimer header.
  2. The endpoint is public (no Authorization header required) — GDPR
     transparency principle (Art. 12-14).
  3. /api/legal/privacy/version returns the metadata JSON shape.
  4. Version metadata matches the V0 baseline (1.0.0, "it", non-empty
     effective_date).
"""
from __future__ import annotations

import pytest


@pytest.mark.db
async def test_privacy_policy_endpoint_returns_markdown(http_client):
    resp = await http_client.get("/api/legal/privacy")
    assert resp.status_code == 200, resp.text

    content = resp.text
    assert "# Informativa sulla privacy" in content
    assert "Vifaras" in content
    # The disclaimer block flags the V0 alpha state explicitly.
    lower = content.lower()
    assert "draft" in lower or "alpha" in lower


@pytest.mark.db
async def test_privacy_policy_endpoint_no_auth_required(http_client):
    """No Authorization header → still 200. GDPR transparency Art. 12-14."""
    # http_client may have lingering headers from other tests; clear it.
    http_client.headers.pop("Authorization", None)
    resp = await http_client.get("/api/legal/privacy")
    assert resp.status_code == 200


@pytest.mark.db
async def test_privacy_policy_version_endpoint(http_client):
    resp = await http_client.get("/api/legal/privacy/version")
    assert resp.status_code == 200

    body = resp.json()
    assert "version" in body
    assert "effective_date" in body
    assert "language" in body


@pytest.mark.db
async def test_privacy_policy_version_matches_v0_baseline(http_client):
    resp = await http_client.get("/api/legal/privacy/version")
    body = resp.json()

    assert body["version"] == "1.0.0"
    assert body["language"] == "it"
    # OR-pattern stays green when V0.5+ flips effective_date to a real date,
    # but still catches an accidental empty value today.
    assert body["effective_date"]
