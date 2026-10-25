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
# Способы подключения к Cloudflare по очереди. По умолчанию cloudflared начинает
# с QUIC (UDP), который многие провайдеры режут, и после неудачи просто
# завершается — поэтому первым идёт обычный TCP (http2), затем QUIC, затем
# запасной набор серверов Cloudflare.
ATTEMPTS: list[tuple[str, list[str]]] = [
    ("TCP (http2)", ["--protocol", "http2"]),
    ("QUIC", ["--protocol", "quic"]),
    ("TCP, серверы региона US", ["--protocol", "http2", "--region", "us"]),
]
START_TIMEOUT = 45.0  # столько ждём подключения, прежде чем пробовать следующий способ


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
        self.attempt = 0
        self.method = ""
        self.log: deque[str] = deque(maxlen=40)

    # ------------------------------------------------------------ состояние

    def active(self) -> bool:
        return self.state in ("starting", "running") and self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        return {
            "state": self.state,
            "url": self.url,
            "error": self.error,
            "progress": round(self.progress * 100),
            "attempt": self.attempt,
            "attempts": len(ATTEMPTS),
            "method": self.method,
        }

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
        for n, (method, extra) in enumerate(ATTEMPTS, 1):
            with self._lock:
                if self.state == "off":  # выключили, пока скачивали или подключались
                    return
                self.state, self.url, self.attempt, self.method = "starting", "", n, method
            self.log.append(f"--- способ {n} из {len(ATTEMPTS)}: {method}")
            outcome = self._launch(exe, port, extra)
            if outcome in ("stopped", "error"):
                return
            if outcome == "died":
                tail = "\n".join(list(self.log)[-6:])
                self._fail("Туннель оборвался. Проверьте интернет и включите доступ ещё раз.\n" + tail)
                return
        tail = "\n".join(line for line in list(self.log)[-8:] if not line.startswith("---"))
        self._fail(
            "Не удалось подключиться к Cloudflare ни одним из способов — похоже, провайдер блокирует "
            "туннели Cloudflare. Включите VPN на компьютере и нажмите «Попробовать снова», "
            "или подключите телефон через Tailscale (см. README).\n" + tail
        )

    def _launch(self, exe: Path, port: int, extra: list[str]) -> str:
        """Запускает cloudflared. Возвращает stopped (выключили кнопкой), failed (не подключился —
        можно пробовать следующий способ), died (работал и оборвался) или error."""
        try:
            proc = subprocess.Popen(
                [str(exe), "tunnel", "--no-autoupdate", *extra, "--url", f"http://127.0.0.1:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._fail(f"Не удалось запустить cloudflared: {exc}")
            return "error"
        with self._lock:
            if self.state == "off":  # выключили, пока процесс запускался
                proc.terminate()
                return "stopped"
            self._proc = proc

        def give_up() -> None:  # повисло без подключения — пробуем следующий способ
            if self._proc is proc and self.state != "running" and proc.poll() is None:
                self.log.append(f"не подключился за {int(START_TIMEOUT)} с")
                proc.kill()

        watchdog = threading.Timer(START_TIMEOUT, give_up)
        watchdog.daemon = True
        watchdog.start()
        connected = False
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            self.log.append(line)
            if not self.url:
                m = URL_RE.search(line)
                if m:
                    self.url = m.group(0)
            if self.url and READY_MARK in line and self._proc is proc:
                connected = True
                self.state = "running"
        proc.wait()
        watchdog.cancel()
        if self._proc is not proc:  # выключили кнопкой
            return "stopped"
        with self._lock:
            self._proc = None
        return "died" if connected else "failed"

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
