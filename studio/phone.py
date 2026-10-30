"""Доступ к студии с телефона по домашней сети (Wi-Fi).

Студия работает на компьютере, телефон открывает её в браузере. Запросы не с
этого компьютера пропускаются только с кодом доступа: он приходит в ссылке из
QR-кода, после чего запоминается в cookie.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import secrets
import socket
import time
from collections import deque
from urllib.parse import urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import config

ENV_FLAG = "TIKTOK_STUDIO_PHONE"
ENV_SERVER = "TIKTOK_STUDIO_SERVER"  # студия на сервере: код нужен для любого запроса
ENV_PORT = "TIKTOK_STUDIO_PORT"
COOKIE = "studio_key"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих 0/O и 1/I
KEY_LENGTH = 8
FAIL_DELAY = 1.0  # пауза после неверного кода — против подбора
FAIL_LIMIT = 20  # неверных кодов с одного адреса за FAIL_WINDOW секунд — и вход с него закрывается
FAIL_LIMIT_TOTAL = 500  # со всех адресов вместе — против подбора с множества адресов
FAIL_WINDOW = 600
# Заголовки, которые добавляет туннель Cloudflare: такой запрос пришёл из интернета,
# хотя сокет у него локальный (127.0.0.1).
PROXY_HEADERS = (b"cf-connecting-ip", b"cf-ray", b"x-forwarded-for")
# Имена, по которым студию открывают на этом компьютере. Другое имя при запросе с 127.0.0.1 —
# признак атаки DNS rebinding: чужой сайт перенаправил свой домен на этот компьютер.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Время неверных попыток входа по адресам клиентов.
failures: dict[str, deque[float]] = {}
_key_cache: tuple | None = None  # (файл, время изменения, размер, код) — чтобы не читать файл на каждый запрос


def enabled() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def server_mode() -> bool:
    return os.environ.get(ENV_SERVER) == "1"


def port() -> int:
    try:
        return int(os.environ.get(ENV_PORT, "8765"))
    except ValueError:
        return 8765


def _settings_stamp() -> tuple | None:
    try:
        st = config.SETTINGS_FILE.stat()
    except OSError:
        return None
    return (str(config.SETTINGS_FILE), st.st_mtime_ns, st.st_size)


def access_key() -> str:
    global _key_cache
    stamp = _settings_stamp()
    if stamp and _key_cache and _key_cache[:3] == stamp:
        return _key_cache[3]
    key = config.load_settings().get("phone_key") or ""
    if len(key) != KEY_LENGTH or any(c not in ALPHABET for c in key):
        key = "".join(secrets.choice(ALPHABET) for _ in range(KEY_LENGTH))
        config.save_settings({"phone_key": key})
        stamp = _settings_stamp()
    _key_cache = (*stamp, key) if stamp else None
    return key


def key_matches(supplied: str, key: str) -> bool:
    supplied = supplied.strip().upper().replace(" ", "").replace("-", "")
    return bool(supplied) and hmac.compare_digest(supplied.encode(), key.encode())


def is_local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def lan_addresses() -> list[str]:
    """IPv4-адреса компьютера в локальных сетях (Wi-Fi, кабель, Tailscale)."""
    found: set[str] = set()
    for probe in ("8.8.8.8", "10.255.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 1))  # UDP connect ничего не отправляет, только выбирает интерфейс
                found.add(s.getsockname()[0])
        except OSError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    addrs = []
    for ip in found:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if not (a.is_loopback or a.is_link_local or a.is_unspecified):
            addrs.append(ip)

    def rank(ip: str) -> tuple:
        # Домашние сети — первыми, затем остальные частные, затем Tailscale (100.64/10).
        return (not ip.startswith("192.168."), not ipaddress.ip_address(ip).is_private, ip)

    return sorted(addrs, key=rank)


def is_proxied(scope) -> bool:
    return any(name in PROXY_HEADERS for name, _ in scope.get("headers") or [])


def _header(scope, name: bytes) -> str:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return ""


def host_is_local(scope) -> bool:
    host = _header(scope, b"host").strip().lower()
    hostname = host.split("]")[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    return hostname in LOCAL_HOSTS


def is_cross_site(scope) -> bool:
    """Запрос, меняющий данные, отправила чужая страница (форма или скрипт другого сайта)."""
    if scope.get("method") not in UNSAFE_METHODS:
        return False
    if _header(scope, b"sec-fetch-site") == "cross-site":
        return True
    origin = _header(scope, b"origin")
    if not origin:
        return False
    return origin == "null" or urlparse(origin).netloc.lower() != _header(scope, b"host").strip().lower()


def client_id(scope, proxied: bool) -> str:
    """Адрес клиента: за туннелем и Caddy — из их заголовков (последний адрес добавил наш прокси)."""
    if proxied:
        forwarded = _header(scope, b"cf-connecting-ip") or _header(scope, b"x-forwarded-for").split(",")[-1]
        if forwarded.strip():
            return forwarded.strip()
    return (scope.get("client") or ("",))[0]


def _too_many_failures(who: str, now: float) -> bool:
    for addr in list(failures):
        attempts = failures[addr]
        while attempts and now - attempts[0] > FAIL_WINDOW:
            attempts.popleft()
        if not attempts:
            del failures[addr]
    total = sum(len(a) for a in failures.values())
    return len(failures.get(who, ())) >= FAIL_LIMIT or total >= FAIL_LIMIT_TOTAL


def phone_urls() -> list[str]:
    query = urlencode({"key": access_key()})
    return [f"http://{ip}:{port()}/?{query}" for ip in lan_addresses()]


def qr_svg(text: str) -> str | None:
    try:
        import segno
    except ImportError:
        return None
    # omitsize: размер задаёт CSS, а viewBox сохраняет пропорции.
    return segno.make(text, error="m").svg_inline(border=2, dark="#000000", light="#ffffff", omitsize=True)


def login_page(wrong: bool) -> str:
    if server_mode():
        where = "Откройте ссылку с кодом доступа или введите код. Узнать его: <code>sudo tiktok-studio info</code> на сервере."
    else:
        where = "Отсканируйте QR-код в студии на компьютере (кнопка «📱 Телефон») или введите код доступа оттуда же."
    error = '<p class="err">Неверный код — проверьте и попробуйте ещё раз.</p>' if wrong else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TikTok Studio — вход</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b10;color:#ececf3;
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px;box-sizing:border-box}}
.card{{background:#14141c;border:1px solid #2a2a38;border-radius:16px;padding:24px;max-width:360px;width:100%}}
h1{{font-size:20px;margin:0 0 8px}} p{{color:#9a9aae;margin:0 0 16px}} .err{{color:#ff8da1}} code{{color:#ececf3}}
input{{width:100%;box-sizing:border-box;font-size:22px;letter-spacing:4px;text-transform:uppercase;text-align:center;
padding:12px;border-radius:10px;border:1px solid #2a2a38;background:#1b1b26;color:#fff;margin-bottom:12px}}
button{{width:100%;padding:13px;border:0;border-radius:10px;font-size:16px;font-weight:600;color:#fff;
background:linear-gradient(135deg,#fe2c55,#ff8a3d)}}
</style></head><body><form class="card" method="get" action="/">
<h1>TikTok Studio</h1><p>{where}</p>
{error}<input name="key" autocomplete="one-time-code" autocapitalize="characters" maxlength="12" placeholder="КОД" required autofocus>
<button type="submit">Войти</button></form></body></html>"""


class PhoneGuard:
    """ASGI-прослойка: с этого компьютера — без ограничений, из сети — только с кодом.
    В режиме сервера код нужен всем: запросы приходят от Caddy с 127.0.0.1."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from .tunnel import tunnel

        client = scope.get("client") or ("", 0)
        proxied = is_proxied(scope)
        server = server_mode()
        if is_local(client[0]) and not proxied and not server:
            # Свой компьютер пускаем без кода — но только по своему имени и не по команде чужого сайта.
            if not host_is_local(scope):
                response = PlainTextResponse(f"Откройте студию по адресу http://127.0.0.1:{port()}", status_code=403)
                return await response(scope, receive, send)
            if is_cross_site(scope):
                response = PlainTextResponse("Запрос с чужого сайта отклонён.", status_code=403)
                return await response(scope, receive, send)
            return await self.app(scope, receive, send)
        if not server and ((proxied and not tunnel.active()) or (not proxied and not enabled())):
            response = PlainTextResponse(
                "Доступ к студии с других устройств выключен. Включите его в студии на компьютере (кнопка 📱).",
                status_code=403,
            )
            return await response(scope, receive, send)

        request = Request(scope)
        key = access_key()
        from_query = request.query_params.get("key", "")
        if key_matches(request.cookies.get(COOKIE, ""), key):
            return await self.app(scope, receive, send)
        now = time.monotonic()
        who = client_id(scope, proxied)
        if from_query and _too_many_failures(who, now):
            response = PlainTextResponse("Слишком много неверных кодов. Подождите 10 минут.", status_code=429)
            return await response(scope, receive, send)
        if key_matches(from_query, key):
            # Запоминаем код в cookie и убираем его из адреса.
            response = RedirectResponse(scope.get("path") or "/", status_code=303)
            response.set_cookie(COOKIE, key, max_age=365 * 24 * 3600, httponly=True, samesite="lax", secure=proxied)
            return await response(scope, receive, send)
        if from_query:
            failures.setdefault(who, deque()).append(now)
            await asyncio.sleep(FAIL_DELAY)
        if (scope.get("path") or "").startswith("/api/"):
            hint = "sudo tiktok-studio info на сервере" if server else "QR-код в студии на компьютере"
            response = JSONResponse({"detail": f"Нужен код доступа — откройте ссылку с кодом ({hint})"}, status_code=401)
        else:
            response = HTMLResponse(login_page(wrong=bool(from_query)), status_code=401)
        await response(scope, receive, send)
