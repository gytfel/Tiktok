"""HTTP API: загрузка файлов и понятные сообщения об ошибках."""

import subprocess
import tempfile

import pytest

from studio import config
from studio.ffmpeg_utils import capabilities, ffmpeg_exe

testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("WORKSPACE_DIR", "UPLOADS_DIR", "OUTPUTS_DIR", "JOBS_DIR", "CACHE_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    config.ensure_dirs()
    from studio.server import app

    return testclient.TestClient(app, client=("127.0.0.1", 50000))  # запросы «с этого компьютера»


@pytest.mark.skipif(not capabilities()["found"], reason="нужен ffmpeg")
def test_raw_upload_of_big_track_with_cyrillic_name(client, tmp_path, monkeypatch):
    track = tmp_path / "track.mp3"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=90", "-b:a", "192k", str(track)], check=True)
    assert track.stat().st_size > 1024 * 1024  # больше порога, после которого multipart пишет во временный файл

    # Системная временная папка «сломана», как на Windows с антивирусом, — загрузке она не нужна.
    def broken_rollover(self):
        raise PermissionError(13, "Отказано в доступе")

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", broken_rollover)
    resp = client.post("/api/uploads/raw", params={"name": "Трек — мой (remix).mp3"}, content=track.read_bytes())
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert meta["kind"] == "audio" and meta["name"] == "Трек — мой (remix).mp3" and meta["duration"] > 80
    assert (config.UPLOADS_DIR / f"{meta['id']}.mp3").stat().st_size == track.stat().st_size


def test_raw_upload_rejects_unknown_format(client):
    resp = client.post("/api/uploads/raw", params={"name": "notes.txt"}, content=b"hello")
    assert resp.status_code == 400 and "не поддерживается" in resp.json()["detail"]
    assert not any(config.UPLOADS_DIR.iterdir())


@pytest.mark.skipif(not capabilities()["found"], reason="нужен ffmpeg")
def test_raw_upload_rejects_broken_audio(client):
    resp = client.post("/api/uploads/raw", params={"name": "broken.mp3"}, content=b"\x00" * 5000)
    assert resp.status_code == 400
    assert not any(config.UPLOADS_DIR.glob("*.mp3"))


def test_body_parse_error_shows_real_cause(client, monkeypatch):
    def broken_rollover(self):
        raise PermissionError(13, "Отказано в доступе")

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", broken_rollover)
    resp = client.post("/api/uploads", files={"files": ("big.mp3", b"\x00" * 3_000_000, "audio/mpeg")})
    assert resp.status_code == 400
    assert "PermissionError" in resp.json()["detail"]


def test_disk_full_detection():
    import errno

    assert config.is_disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert config.is_disk_full("ffmpeg: Error writing trailer: No space left on device")
    assert config.is_disk_full("[WinError 112] There is not enough space on the disk")
    assert not config.is_disk_full(OSError(errno.EACCES, "Permission denied"))
    assert "закончилось место" in config.disk_full_message()
