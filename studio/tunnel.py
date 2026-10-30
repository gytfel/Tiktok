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
URL_RE = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")  # api.… — адрес сервиса, а не ссылка
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
# Строки журнала cloudflared, которые только мешают найти причину ошибки.
LOG_NOISE = ("precheck component=", "Thank you for trying Cloudflare Tunnel", "+----")
# С лета 2025 года многие провайдеры в России ограничивают Cloudflare: соединение
# устанавливается (проверки cloudflared проходят), но обмен данными обрывается.
BLOCKED_HINT = (
    "Обычно так бывает, когда интернет-провайдер ограничивает Cloudflare — в России это происходит "
    "у многих операторов с лета 2025 года."
)


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
        self.detail = ""  # хвост журнала cloudflared для «Технических подробностей»
        self.progress = 0.0
        self.attempt = 0
        self.method = ""
        self.got_url = False  # Cloudflare выдал ссылку хотя бы в одной попытке
        # Номер запуска: «Выключить» и новый «Включить» делают прежний поток недействительным,
        # чтобы он не запустил второй cloudflared, который потом уже не остановить.
        self._run_id = 0
        self.log: deque[str] = deque(maxlen=200)

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
            "detail": self.detail,
        }

    def log_tail(self, lines: int = 25) -> str:
        useful = [line for line in self.log if line.strip() and not any(n in line for n in LOG_NOISE)]
        return "\n".join(useful[-lines:])

    # ------------------------------------------------------------ запуск

    def start(self, port: int) -> None:
        with self._lock:
            if self.state in ("downloading", "starting", "running") and (self._proc is None or self._proc.poll() is None):
                return
            self.state, self.url, self.error, self.detail, self.progress = "starting", "", "", "", 0.0
            self.got_url = False
            self.log.clear()
            self._run_id += 1
            run_id = self._run_id
        threading.Thread(target=self._run, args=(port, run_id), daemon=True, name="tunnel").start()

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            self.state, self.url, self.error, self.detail = "off", "", "", ""
            self._run_id += 1
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run(self, port: int, run_id: int = 0) -> None:
        try:
            exe = self._ensure_binary()
        except Exception as exc:  # сеть, диск, распаковка
            self._fail(f"Не удалось скачать cloudflared: {exc}", run_id=run_id)
            return
        for n, (method, extra) in enumerate(ATTEMPTS, 1):
            with self._lock:
                if self._run_id != run_id:  # выключили (или запустили заново), пока скачивали или подключались
                    return
                self.state, self.url, self.attempt, self.method = "starting", "", n, method
            self.log.append(f"--- способ {n} из {len(ATTEMPTS)}: {method}")
            outcome = self._launch(exe, port, extra, run_id)
            if outcome in ("stopped", "error"):
                return
            if outcome == "died":
                self._fail("Туннель оборвался. Проверьте интернет и включите доступ ещё раз.", self.log_tail(), run_id)
                return
        if self.got_url:
            reason = (
                f"Cloudflare выдал ссылку, но подключиться к его серверам не удалось ни одним из способов "
                f"({len(ATTEMPTS)} попытки): соединение устанавливается, но передача данных обрывается. "
            )
        else:
            reason = "Не удалось получить ссылку от Cloudflare: сервис trycloudflare.com не ответил. "
        self._fail(reason + BLOCKED_HINT, self.log_tail(), run_id)

    def _launch(self, exe: Path, port: int, extra: list[str], run_id: int = 0) -> str:
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
            self._fail(f"Не удалось запустить cloudflared: {exc}", run_id=run_id)
            return "error"
        with self._lock:
            if self._run_id != run_id:  # выключили, пока процесс запускался
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
                    self.got_url = True
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

    def _fail(self, message: str, detail: str = "", run_id: int | None = None) -> None:
        with self._lock:
            if run_id is not None and run_id != self._run_id:
                return  # этот запуск уже отменён — его ошибка никому не нужна
            self._proc = None
            self.state, self.error, self.detail, self.url = "error", message, detail, ""

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
        tmp = target.parent / f"{asset}.{os.getpid()}.{threading.get_ident()}.part"  # у каждого потока свой
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
