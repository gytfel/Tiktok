"""Вход с телефона: код доступа, cookie и закрытый доступ без кода."""

import pytest

from studio import config, phone

testclient = pytest.importorskip("fastapi.testclient")

PHONE_IP = ("192.168.1.50", 50000)
LOCAL_IP = ("127.0.0.1", 50000)


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(phone, "FAIL_DELAY", 0)
    phone.failures.clear()
    monkeypatch.setattr(phone, "lan_addresses", lambda: ["192.168.1.10"])
    config.ensure_dirs()
    from studio.server import app

    return app


def client(app, addr):
    return testclient.TestClient(app, base_url="http://127.0.0.1:8765", client=addr, follow_redirects=False)


def test_network_access_is_closed_without_phone_mode(app, monkeypatch):
    monkeypatch.delenv(phone.ENV_FLAG, raising=False)
    assert client(app, PHONE_IP).get("/").status_code == 403
    assert client(app, LOCAL_IP).get("/api/status").status_code == 200


def test_phone_login_flow(app, monkeypatch):
    monkeypatch.setenv(phone.ENV_FLAG, "1")
    key = phone.access_key()
    assert len(key) == phone.KEY_LENGTH and phone.access_key() == key  # код постоянный

    c = client(app, PHONE_IP)
    page = c.get("/")
    assert page.status_code == 401 and "код доступа" in page.text
    assert c.get("/api/status").status_code == 401
    assert c.get("/", params={"key": "WRONGKEY"}).status_code == 401

    login = c.get("/", params={"key": key.lower()})  # регистр не важен
    assert login.status_code == 303 and login.headers["location"] == "/"
    assert phone.COOKIE in login.headers["set-cookie"]
    assert c.get("/api/status").status_code == 200  # дальше пускает по cookie
    assert c.get("/api/phone").json()["remote"] is True
    assert c.get("/api/phone").json()["key"] == ""  # код с телефона не раскрывается


def test_local_phone_info_has_qr_and_url(app, monkeypatch):
    monkeypatch.setenv(phone.ENV_FLAG, "1")
    data = client(app, LOCAL_IP).get("/api/phone").json()
    assert data["enabled"] and not data["remote"]
    assert data["urls"] == [f"http://192.168.1.10:{phone.port()}/?key={data['key']}"]
    if data["qr"] is not None:
        assert data["qr"].startswith("<svg")


def test_manifest_is_served(app):
    resp = client(app, LOCAL_IP).get("/static/manifest.webmanifest")
    assert resp.status_code == 200 and resp.json()["display"] == "standalone"


def test_lan_addresses_skip_loopback():
    assert all(not ip.startswith("127.") for ip in phone.lan_addresses())


def test_server_mode_requires_key_even_from_localhost(app, monkeypatch):
    # На VPS запросы приходят от Caddy с 127.0.0.1 — без кода пускать нельзя.
    monkeypatch.setenv(phone.ENV_SERVER, "1")
    monkeypatch.delenv(phone.ENV_FLAG, raising=False)
    key = phone.access_key()
    c = client(app, LOCAL_IP)
    page = c.get("/")
    assert page.status_code == 401 and "tiktok-studio info" in page.text
    assert c.get("/api/status").status_code == 401

    login = c.get("/", params={"key": key})
    assert login.status_code == 303 and phone.COOKIE in login.headers["set-cookie"]
    assert c.get("/api/status").json()["server"] is True
    info = c.get("/api/phone").json()
    assert info["remote"] is True and info["key"] == ""  # код через веб не раскрывается
    assert c.post("/api/tunnel/start").status_code == 403  # туннель на сервере не нужен


def test_server_mode_accepts_proxied_requests_with_key(app, monkeypatch):
    monkeypatch.setenv(phone.ENV_SERVER, "1")
    key = phone.access_key()
    c = client(app, LOCAL_IP)
    headers = {"X-Forwarded-For": "203.0.113.9"}  # так приходят запросы через Caddy
    assert c.get("/api/status", headers=headers).status_code == 401
    login = c.get("/", params={"key": key}, headers=headers)
    assert login.status_code == 303 and "secure" in login.headers["set-cookie"].lower()


def test_local_requests_reject_dns_rebinding_and_cross_site(app, monkeypatch):
    monkeypatch.delenv(phone.ENV_FLAG, raising=False)
    c = client(app, LOCAL_IP)
    assert c.get("/api/status").status_code == 200
    # Чужой домен, перенаправленный на 127.0.0.1 (DNS rebinding), не получает доступ «своего компьютера».
    assert c.get("/api/phone", headers={"Host": "evil.example:8765"}).status_code == 403
    # Форма или скрипт чужого сайта не могут ничего менять.
    assert c.post("/api/tunnel/stop", headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/tunnel/stop", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.post("/api/tunnel/stop", headers={"Origin": "null"}).status_code == 403
    assert c.post("/api/tunnel/stop", headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # переход по ссылке — можно


def test_wrong_codes_lock_only_that_address(app, monkeypatch):
    monkeypatch.setenv(phone.ENV_FLAG, "1")
    monkeypatch.setattr(phone, "FAIL_LIMIT", 2)
    key = phone.access_key()
    attacker, owner = client(app, ("192.168.1.66", 1)), client(app, PHONE_IP)
    for _ in range(2):
        assert attacker.get("/", params={"key": "BADBADBA"}).status_code == 401
    assert attacker.get("/", params={"key": key}).status_code == 429
    assert owner.get("/", params={"key": key}).status_code == 303  # владельца это не задевает
    assert owner.get("/api/status").status_code == 200
    monkeypatch.setattr(phone, "FAIL_LIMIT_TOTAL", 2)
    newcomer = client(app, ("192.168.1.77", 1))
    assert newcomer.get("/", params={"key": key}).status_code == 429  # общий предел — против подбора с многих адресов
    assert owner.get("/api/status").status_code == 200  # уже вошедших это не касается


def test_access_code_cannot_be_changed_through_settings(app, monkeypatch):
    monkeypatch.setenv(phone.ENV_SERVER, "1")
    key = phone.access_key()
    c = client(app, LOCAL_IP)
    c.cookies.set(phone.COOKIE, key)
    assert c.post("/api/settings", json={"phone_key": "AAAAAAAA", "proxy": "http://x:1"}).status_code == 200
    assert phone.access_key() == key and config.load_settings()["proxy"] == "http://x:1"
