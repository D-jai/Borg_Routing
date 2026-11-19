"""Work order 4.2: the Ask page is the front door; the old chat page address goes there."""
from fastapi.testclient import TestClient


def test_root_and_old_chat_page_redirect_to_ask():
    from main import app

    client = TestClient(app)
    for path in ("/", "/ui", "/ui/", "/ui/chat.html"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307), path
        assert r.headers["location"] == "/ui/ask.html", path
    assert client.get("/ui/ask.html").status_code == 200
    assert client.get("/ui/runs.html").status_code == 200
    assert client.get("/ui/run.html").status_code == 200
