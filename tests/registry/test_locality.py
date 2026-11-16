"""TC-LANE-001..: what counts as "this machine" (src/registry/locality.py).

Design note section 3 and the URL cases of section 12.
"""
import pytest

from src.registry.locality import host_port, is_local_endpoint, is_local_model
from src.registry.models import ModelInfo


# Happy path ---------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://LOCALHOST.",
    "http://localhost:11434",
    "http://127.0.0.1:1234/v1",
    "http://127.9.9.9",
    "http://[::1]:8080",
    "http://[::ffff:127.0.0.1]",
    "https://localhost",
])
def test_loopback_addresses_are_local(url, monkeypatch):
    monkeypatch.delenv("BORG_LOCAL_HOSTS", raising=False)
    assert is_local_endpoint(url)


def test_listed_hosts_names_ips_and_ranges_are_local(monkeypatch):
    monkeypatch.setenv("BORG_LOCAL_HOSTS", "ollama, 192.168.1.20, 10.8.0.0/16")
    assert is_local_endpoint("http://ollama:11434")
    assert is_local_endpoint("http://192.168.1.20:1234")
    assert is_local_endpoint("http://10.8.3.4:8000")
    assert not is_local_endpoint("http://10.9.0.1:8000")


# Unhappy path -------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost.evil.com",
    "http://127.0.0.1.nip.io",
    "http://127.0.0.1@evil.com",
    "http://evil.com#@127.0.0.1",
    "http://2130706433",
    "http://0x7f.1",
    "http://127.1",
    "http://0.0.0.0",
    "ftp://127.0.0.1",
    "https://openrouter.ai/api",
    "http://192.168.1.20:1234",     # private ranges are not local unless listed
    "",
    None,
    "not a url",
])
def test_tricks_and_remote_addresses_are_not_local(url, monkeypatch):
    monkeypatch.delenv("BORG_LOCAL_HOSTS", raising=False)
    assert not is_local_endpoint(url)


def _row(model_id, endpoint, source="ollama", remote=False):
    return ModelInfo(model_id=model_id, source=source, endpoint=endpoint,
                     context_length=8000, remote=remote)


def test_an_ollama_cloud_tag_or_remote_row_is_not_local():
    assert not is_local_model(_row("ollama/gpt-oss:120b-cloud", "http://127.0.0.1:11434"))
    assert not is_local_model(_row("ollama/qwen3:8b", "http://127.0.0.1:11434", remote=True))
    assert is_local_model(_row("ollama/qwen3:8b", "http://127.0.0.1:11434"))


def test_the_source_label_is_not_trusted():
    # Labelled lmstudio, but it lives on another machine.
    assert not is_local_model(_row("lmstudio/x", "http://10.0.0.5:1234", source="lmstudio"))
    # Labelled openrouter, but its address is this machine: the address decides.
    assert is_local_model(_row("openrouter/x", "http://127.0.0.1:9000", source="openrouter"))


def test_the_allow_list_accepts_ids_or_host_port():
    row = _row("ollama/qwen3:8b", "http://127.0.0.1:11434")
    assert not is_local_model(row, allowlist=["ollama/other"])
    assert is_local_model(row, allowlist=["ollama/qwen3:8b"])
    assert is_local_model(row, allowlist=["127.0.0.1:11434"])
    assert host_port("https://localhost") == "localhost:443"


async def test_discovery_marks_ollama_cloud_models_remote():
    from unittest.mock import AsyncMock, MagicMock, patch

    from src.registry.discovery import discover_ollama

    resp = MagicMock()
    resp.json.return_value = {"models": [
        {"name": "qwen3:8b", "details": {}},
        {"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com:443", "details": {}},
        {"name": "deepseek:671b", "remote_model": "deepseek", "details": {}},
    ]}
    resp.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        cls.return_value = client
        rows = await discover_ollama("http://127.0.0.1:11434")
    flags = {r.model_id: r.remote for r in rows}
    assert flags == {"ollama/qwen3:8b": False, "ollama/gpt-oss:120b-cloud": True,
                     "ollama/deepseek:671b": True}
    assert [r.model_id for r in rows if is_local_model(r)] == ["ollama/qwen3:8b"]


def test_the_url_of_the_call_must_be_local_too():
    row = _row("ollama/qwen3:8b", "http://127.0.0.1:11434")
    assert not is_local_model(row, url="https://api.example.com/v1/chat/completions")
