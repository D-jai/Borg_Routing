"""Work order 5.2: one-command install. What can be proved without Docker."""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
import main
c = TestClient(main.app)
out = {
    "health": c.get("/health").json(),
    "ask": c.get("/ui/ask.html").status_code,
    "ask_has_html": "<html" in c.get("/ui/ask.html").text.lower(),
    "status": c.get("/replay/status").status_code,
    "mode": c.get("/replay/status").json().get("mode"), "prompts": len(c.get("/replay/status").json().get("runs", [])),
    "post": c.post("/models/discover").status_code,
    "spacy": "spacy" in sys.modules,
}
print("PROBE" + json.dumps(out))
"""


def _run_probe(cwd: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BORG_")}
    env["BORG_DEMO_MODE"] = "replay"
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(ROOT)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE")][-1]
    return json.loads(line[5:])


def test_starts_from_any_working_directory(tmp_path):
    out = _run_probe(tmp_path)
    assert out["health"] == {"status": "ok"}
    assert out["ask"] == 200 and out["ask_has_html"]
    assert out["status"] == 200 and out["mode"] == "replay" and out["prompts"] >= 1  # recorded runs found from the package
    assert out["post"] == 403  # replay mode: state-changing route refused


def test_start_up_does_not_import_spacy(tmp_path):
    assert _run_probe(tmp_path)["spacy"] is False


def test_spacy_is_an_optional_extra():
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert not any(d.lower().startswith("spacy") for d in proj["dependencies"])
    assert any(d.lower().startswith("spacy") for d in proj["optional-dependencies"]["ner"])


def _load(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def test_compose_default_is_replay_with_no_env_file():
    svc = _load("compose.yaml")["services"]["borg"]
    assert svc["environment"]["BORG_DEMO_MODE"] == "replay"
    assert "env_file" not in svc
    assert any("8000" in str(p) for p in svc["ports"])
    assert "healthcheck" in svc and "/health" in " ".join(svc["healthcheck"]["test"])


def test_compose_data_is_on_a_named_volume():
    doc = _load("compose.yaml")
    mounts = [v for v in doc["services"]["borg"]["volumes"] if v.endswith(":/app/data")]
    assert len(mounts) == 1
    name = mounts[0].split(":")[0]
    assert name in doc["volumes"]  # a named volume, not a host path
    assert not name.startswith((".", "/"))


def test_live_override_reads_env_and_turns_replay_off():
    svc = _load("compose.live.yaml")["services"]["borg"]
    assert ".env" in svc["env_file"]
    assert svc["environment"]["BORG_DEMO_MODE"] == "off"


def test_dockerfile_serves_on_8000_from_the_app_folder():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 8000" in text and "main:app" in text and "COPY ui" in text and "COPY demo" in text
