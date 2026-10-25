"""Доступ к студии из интернета через бесплатный туннель Cloudflare (trycloudflare.com).

Студия по-прежнему работает на компьютере и слушает только 127.0.0.1, а
cloudflared пробрасывает к ней защищённую ссылку https://….trycloudflare.com.
Аккаунт Cloudflare не нужен; программа cloudflared скачивается при первом
включении. Ссылка меняется при каждом запуске туннеля.
"""

from __future__ import annotations

import atexit
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
from collections import deque
from pathlib import Path

from . import config
from .net import USER_AGENT, opener

DOWNLOAD_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
READY_MARK = "Registered tunnel connection"


def _asset_name() -> str:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "cloudflared-windows-amd64.exe"  # на Windows ARM работает через эмуляцию
    if sys.platform == "darwin":
        return f"cloudflared-darwin-{'arm64' if arm else 'amd64'}.tgz"
    if arm:
        return "cloudflared-linux-arm64"
    if machine.startswith("arm"):
        return "cloudflared-linux-arm"
    return "cloudflared-linux-amd64"


def binary_path() -> Path:
    return config.CACHE_DIR / "bin" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")


class Tunnel:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self.state = "off"  # off | downloading | starting | running | error
        self.url = ""
        self.error = ""
        self.progress = 0.0
        self.log: deque[str] = deque(maxlen=40)

    # ------------------------------------------------------------ состояние

    def active(self) -> bool:
        return self.state in ("starting", "running") and self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        return {"state": self.state, "url": self.url, "error": self.error, "progress": round(self.progress * 100)}

    # ------------------------------------------------------------ запуск

    def start(self, port: int) -> None:
        with self._lock:
            if self.state in ("downloading", "starting", "running") and (self._proc is None or self._proc.poll() is None):
                return
            self.state, self.url, self.error, self.progress = "starting", "", "", 0.0
            self.log.clear()
        threading.Thread(target=self._run, args=(port,), daemon=True, name="tunnel").start()

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self.state, self.url, self.error = "off", "", ""
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run(self, port: int) -> None:
        try:
            exe = self._ensure_binary()
        except Exception as exc:  # сеть, диск, распаковка
            self._fail(f"Не удалось скачать cloudflared: {exc}")
            return
        with self._lock:
            if self.state == "off":  # выключили, пока скачивали
                return
            self.state = "starting"
        try:
            proc = subprocess.Popen(
                [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._fail(f"Не удалось запустить cloudflared: {exc}")
            return
        with self._lock:
            if self.state == "off":  # выключили, пока процесс запускался
                proc.terminate()
                return
            self._proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            self.log.append(line)
            if not self.url:
                m = URL_RE.search(line)
                if m:
                    self.url = m.group(0)
            if self.url and READY_MARK in line and self._proc is proc:
                self.state = "running"
        proc.wait()
        if self._proc is proc:  # процесс завершился сам, а не по кнопке
            tail = "\n".join(list(self.log)[-6:])
            self._fail("Туннель остановился. Проверьте интернет и попробуйте ещё раз.\n" + tail)

    def _fail(self, message: str) -> None:
        with self._lock:
            self._proc = None
            self.state, self.error, self.url = "error", message, ""

    def _ensure_binary(self) -> Path:
        found = shutil.which("cloudflared")
        if found:
            return Path(found)
        target = binary_path()
        if target.exists():
            return target
        if self.state != "off":
            self.state = "downloading"
        target.parent.mkdir(parents=True, exist_ok=True)
        asset = _asset_name()
        tmp = target.parent / (asset + ".part")
        import urllib.request

        req = urllib.request.Request(DOWNLOAD_BASE + asset, headers={"User-Agent": USER_AGENT})
        with opener().open(req, timeout=60) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    self.progress = done / total
        if asset.endswith(".tgz"):
            with tarfile.open(tmp) as tar:
                member = next(m for m in tar.getmembers() if Path(m.name).name == "cloudflared")
                with tar.extractfile(member) as src, open(target, "wb") as dst:  # type: ignore[union-attr]
                    shutil.copyfileobj(src, dst)
            tmp.unlink()
        else:
            tmp.replace(target)
        if os.name != "nt":
            target.chmod(0o755)
        return target


tunnel = Tunnel()
atexit.register(tunnel.stop)
