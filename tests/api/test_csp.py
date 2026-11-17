"""4.2 review: every response carries a Content-Security-Policy that stops the page
loading anything from another site (an answer's image, a stray script), and the
pages need no inline script for it (script-src 'self')."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app

REQUIRED = ["default-src 'self'", "img-src 'self' data:", "script-src 'self'", "connect-src 'self'",
            "frame-ancestors 'none'"]


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", ["/health", "/models", "/ui/ask.html", "/ui/assets/app.js", "/nope-404", "/"])
def test_every_response_carries_the_policy(client, path):
    resp = client.get(path, follow_redirects=False)
    csp = resp.headers.get("content-security-policy")
    assert csp, f"{path} ({resp.status_code}) has no Content-Security-Policy"
    for part in REQUIRED:
        assert part in csp, (path, part)
    assert "unsafe-eval" not in csp
    script = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
    assert "unsafe-inline" not in script


def test_a_streaming_response_carries_it_too(client):
    resp = client.post("/v1/chat/completions", json={"model": "borg_route", "messages": []})
    assert resp.headers.get("content-security-policy")


def test_no_page_has_an_inline_script_or_handler():
    """script-src 'self' blocks inline code, so a page may only link its scripts."""
    for page in Path("ui").glob("*.html"):
        html = page.read_text(encoding="utf-8")
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
            assert "src=" in m.group(1) and not m.group(2).strip(), f"{page.name}: inline script"
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), f"{page.name}: inline event handler"
