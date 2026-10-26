"""Доступ из интернета: туннель cloudflared (имитация), защита запросов через туннель, фоновые ИИ-задачи."""

import io
import os
import sys
import tarfile
import time

import pytest

from studio import ai, config, phone, tunnel as tunnel_mod

testclient = pytest.importorskip("fastapi.testclient")

FAKE_OK = """#!{python}
import sys, time
print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
print("INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |", flush=True)
print("INF |  https://brave-lucky-test-words.trycloudflare.com                                         |", flush=True)
time.sleep(0.2)
print("INF Registered tunnel connection connIndex=0 location=ams01 protocol=quic", flush=True)
time.sleep(60)
"""

FAKE_FAIL = """#!{python}
print('ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp: i/o timeout', flush=True)
raise SystemExit(1)
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(phone, "FAIL_DELAY", 0)
    phone.failures.clear()
    monkeypatch.setattr(tunnel_mod.shutil, "which", lambda name: None)  # не брать cloudflared из системы
    config.ensure_dirs()
    return tmp_path


def install_fake(script: str) -> None:
    path = tunnel_mod.binary_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script.format(python=sys.executable))
    path.chmod(0o755)


def wait_for(t, states, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if t.state in states:
            return
        time.sleep(0.05)
    raise AssertionError(f"состояние {t.state}, ждали {states}")


@pytest.mark.skipif(os.name == "nt", reason="имитация cloudflared — shell-скрипт")
def test_tunnel_reports_url_and_stops(workspace):
    install_fake(FAKE_OK)
    t = tunnel_mod.Tunnel()
    t.start(8765)
    wait_for(t, {"running"})
    assert t.url == "https://brave-lucky-test-words.trycloudflare.com" and t.active()
    proc = t._proc
    t.stop()
    assert t.state == "off" and not t.active()
    assert proc.poll() is not None  # процесс cloudflared завершён


@pytest.mark.skipif(os.name == "nt", reason="имитация cloudflared — shell-скрипт")
def test_tunnel_failure_shows_cloudflared_log(workspace):
    install_fake(FAKE_FAIL)
    t = tunnel_mod.Tunnel()
    t.start(8765)
    wait_for(t, {"error"})
    assert "i/o timeout" in t.detail and t.url == ""
    assert "trycloudflare.com не ответил" in t.error  # ссылку так и не выдали


FAKE_ONLY_QUIC = """#!{python}
import sys, time
print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
print("INF |  https://quic-only-words.trycloudflare.com  |", flush=True)
if "quic" not in sys.argv:
    print('ERR initial tunnel connection failed error="control stream error: context deadline exceeded"', flush=True)
    raise SystemExit(1)
print("INF Registered tunnel connection connIndex=0 protocol=quic", flush=True)
time.sleep(60)
"""

FAKE_HANG_THEN_OK = """#!{python}
import sys, time
print("INF |  https://slow-then-ok.trycloudflare.com  |", flush=True)
if "http2" in sys.argv and "us" not in sys.argv:
    time.sleep(60)  # TCP повис без подключения
print("INF Registered tunnel connection connIndex=0", flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(os.name == "nt", reason="имитация cloudflared — shell-скрипт")
def test_tunnel_falls_back_to_next_protocol(workspace):
    install_fake(FAKE_ONLY_QUIC)
    t = tunnel_mod.Tunnel()
    t.start(8765)
    wait_for(t, {"running", "error"})
    assert t.state == "running" and t.attempt == 2 and t.method == "QUIC"
    assert t.url == "https://quic-only-words.trycloudflare.com"
    t.stop()


@pytest.mark.skipif(os.name == "nt", reason="имитация cloudflared — shell-скрипт")
def test_tunnel_gives_up_on_hanging_attempt(workspace, monkeypatch):
    monkeypatch.setattr(tunnel_mod, "START_TIMEOUT", 0.5)
    install_fake(FAKE_HANG_THEN_OK)
    t = tunnel_mod.Tunnel()
    t.start(8765)
    wait_for(t, {"running", "error"})
    assert t.state == "running" and t.attempt == 2
    t.stop()


@pytest.mark.skipif(os.name == "nt", reason="имитация cloudflared — shell-скрипт")
def test_tunnel_all_attempts_fail(workspace):
    install_fake(FAKE_FAIL)
    t = tunnel_mod.Tunnel()
    t.start(8765)
    wait_for(t, {"error"})
    assert "провайдер ограничивает Cloudflare" in t.error
    assert sum(1 for line in t.log if line.startswith("--- способ")) == len(tunnel_mod.ATTEMPTS)
    assert t.status()["detail"] == t.detail != ""


FAKE_URL_THEN_SILENCE = """#!{python}
import time
print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
print("INF |  https://got-url-but-stuck.trycloudflare.com  |", flush=True)
print('INF precheck component="TCP Connectivity" details="HTTP/2 connection successful" status=pass', flush=True)
print("INF precheck complete hard_fail=false suggested_protocol=http2", flush=True)
time.sleep(60)  # соединение есть, но Cloudflare так и не ответил
"""


@pytest.mark.skipif(os.name == "nt", reason="имитация cloudflared — shell-скрипт")
def test_tunnel_explains_throttled_cloudflare(workspace, monkeypatch):
    # Так выглядит ограничение Cloudflare провайдером: ссылка выдана, проверки прошли, подключения нет.
    monkeypatch.setattr(tunnel_mod, "START_TIMEOUT", 0.3)
    install_fake(FAKE_URL_THEN_SILENCE)
    t = tunnel_mod.Tunnel()
    t.start(8765)
    wait_for(t, {"error"})
    assert "выдал ссылку" in t.error and "провайдер" in t.error
    assert "precheck component" not in t.detail  # шум проверок убран
    assert "precheck complete" in t.detail and "не подключился" in t.detail


def test_download_extracts_macos_archive(workspace, monkeypatch):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"#!/bin/sh\necho fake\n"
        info = tarfile.TarInfo("cloudflared")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive = buf.getvalue()

    class Resp(io.BytesIO):
        headers = {"Content-Length": str(len(archive))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    requested = []

    class Opener:
        def open(self, req, timeout=0):
            requested.append(req.full_url)
            return Resp(archive)

    monkeypatch.setattr(tunnel_mod, "_asset_name", lambda: "cloudflared-darwin-arm64.tgz")
    monkeypatch.setattr(tunnel_mod, "opener", lambda: Opener())
    t = tunnel_mod.Tunnel()
    path = t._ensure_binary()
    assert requested == [tunnel_mod.DOWNLOAD_BASE + "cloudflared-darwin-arm64.tgz"]
    assert path.read_bytes().startswith(b"#!/bin/sh") and t.progress == 1.0


# ---------------------------------------------------------------- защита


@pytest.fixture
def app(workspace, monkeypatch):
    monkeypatch.delenv(phone.ENV_FLAG, raising=False)
    from studio.server import app

    return app


def local_client(app):
    return testclient.TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False)


CF = {"cf-connecting-ip": "203.0.113.7", "cf-ray": "abc", "x-forwarded-proto": "https"}


def test_tunnel_requests_are_not_treated_as_local(app, monkeypatch):
    c = local_client(app)
    assert c.get("/api/status").status_code == 200  # сам компьютер
    assert c.get("/api/status", headers=CF).status_code == 403  # туннель выключен

    monkeypatch.setattr(tunnel_mod.tunnel, "active", lambda: True)
    assert c.get("/api/status", headers=CF).status_code == 401  # без кода
    key = phone.access_key()
    login = c.get("/", params={"key": key}, headers=CF)
    assert login.status_code == 303 and "secure" in login.headers["set-cookie"].lower()
    c.cookies.set(phone.COOKIE, key)
    assert c.get("/api/status", headers=CF).status_code == 200
    # С телефона через туннель нельзя управлять туннелем и не видно кода.
    assert c.post("/api/tunnel/stop", headers=CF).status_code == 403
    assert c.get("/api/phone", headers=CF).json()["remote"] is True
    monkeypatch.setattr(tunnel_mod.tunnel, "detail", "журнал cloudflared")
    assert c.get("/api/tunnel", headers=CF).json()["detail"] == ""  # журнал — только на компьютере
    assert c.get("/api/tunnel").json()["detail"] == "журнал cloudflared"


def test_too_many_wrong_codes_locks_login(app, monkeypatch):
    monkeypatch.setattr(tunnel_mod.tunnel, "active", lambda: True)
    monkeypatch.setattr(phone, "FAIL_LIMIT", 3)
    c = local_client(app)
    for _ in range(3):
        assert c.get("/", params={"key": "BADBADBA"}, headers=CF).status_code == 401
    assert c.get("/", params={"key": phone.access_key()}, headers=CF).status_code == 429


def test_tunnel_status_for_local_has_qr(app, monkeypatch):
    monkeypatch.setattr(tunnel_mod.tunnel, "state", "running")
    monkeypatch.setattr(tunnel_mod.tunnel, "url", "https://brave-lucky-test-words.trycloudflare.com")
    data = local_client(app).get("/api/tunnel").json()
    assert data["phone_url"] == f"https://brave-lucky-test-words.trycloudflare.com/?key={phone.access_key()}"
    if data["qr"] is not None:
        assert data["qr"].startswith("<svg")


# ---------------------------------------------------------------- ИИ-видео в фоне


def test_ai_video_runs_as_background_task(app, monkeypatch):
    class VideoProvider(ai.Provider):
        name, label, supports_video = "fake", "Fake", True

    monkeypatch.setattr(ai, "get_provider", lambda settings=None: VideoProvider())

    def slow_video(provider, prompt, style, seconds, on_wait=None):
        on_wait and on_wait("видео: pending")
        time.sleep(0.3)
        return {"id": "abc123def456", "kind": "video"}

    monkeypatch.setattr(ai, "generate_video", slow_video)
    c = local_client(app)
    task = c.post("/api/ai/video", json={"prompt": "a cat", "seconds": 4}).json()
    assert task["status"] == "running"  # ответ сразу, не дожидаясь видео
    for _ in range(50):
        task = c.get(f"/api/ai/tasks/{task['id']}").json()
        if task["status"] != "running":
            break
        time.sleep(0.05)
    assert task["status"] == "done" and task["result"]["id"] == "abc123def456"
