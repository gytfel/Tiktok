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
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from . import config

ENV_FLAG = "TIKTOK_STUDIO_PHONE"
ENV_PORT = "TIKTOK_STUDIO_PORT"
COOKIE = "studio_key"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без похожих 0/O и 1/I
KEY_LENGTH = 8
FAIL_DELAY = 1.0  # пауза после неверного кода — против подбора


def enabled() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def port() -> int:
    try:
        return int(os.environ.get(ENV_PORT, "8765"))
    except ValueError:
        return 8765


def access_key() -> str:
    key = config.load_settings().get("phone_key") or ""
    if len(key) != KEY_LENGTH or any(c not in ALPHABET for c in key):
        key = "".join(secrets.choice(ALPHABET) for _ in range(KEY_LENGTH))
        config.save_settings({"phone_key": key})
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
    error = '<p class="err">Неверный код — проверьте и попробуйте ещё раз.</p>' if wrong else ""
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>TikTok Studio — вход</title>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b10;color:#ececf3;
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px;box-sizing:border-box}}
.card{{background:#14141c;border:1px solid #2a2a38;border-radius:16px;padding:24px;max-width:360px;width:100%}}
h1{{font-size:20px;margin:0 0 8px}} p{{color:#9a9aae;margin:0 0 16px}} .err{{color:#ff8da1}}
input{{width:100%;box-sizing:border-box;font-size:22px;letter-spacing:4px;text-transform:uppercase;text-align:center;
padding:12px;border-radius:10px;border:1px solid #2a2a38;background:#1b1b26;color:#fff;margin-bottom:12px}}
button{{width:100%;padding:13px;border:0;border-radius:10px;font-size:16px;font-weight:600;color:#fff;
background:linear-gradient(135deg,#fe2c55,#ff8a3d)}}
</style></head><body><form class="card" method="get" action="/">
<h1>TikTok Studio</h1><p>Отсканируйте QR-код в студии на компьютере (кнопка «📱 Телефон») или введите код доступа оттуда же.</p>
{error}<input name="key" autocomplete="one-time-code" autocapitalize="characters" maxlength="12" placeholder="КОД" required autofocus>
<button type="submit">Войти</button></form></body></html>"""


class PhoneGuard:
    """ASGI-прослойка: с этого компьютера — без ограничений, из сети — только с кодом."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = scope.get("client") or ("", 0)
        if is_local(client[0]):
            return await self.app(scope, receive, send)
        if not enabled():
            response = PlainTextResponse(
                "Доступ к студии из сети выключен. Запустите её через start-phone.bat.", status_code=403
            )
            return await response(scope, receive, send)

        request = Request(scope)
        key = access_key()
        from_query = request.query_params.get("key", "")
        if key_matches(request.cookies.get(COOKIE, ""), key):
            return await self.app(scope, receive, send)
        if key_matches(from_query, key):
            # Запоминаем код в cookie и убираем его из адреса.
            response = RedirectResponse(scope.get("path") or "/", status_code=303)
            response.set_cookie(COOKIE, key, max_age=365 * 24 * 3600, httponly=True, samesite="lax")
            return await response(scope, receive, send)
        if from_query:
            await asyncio.sleep(FAIL_DELAY)
        if (scope.get("path") or "").startswith("/api/"):
            response = JSONResponse({"detail": "Нужен код доступа — откройте студию по QR-коду с компьютера"}, status_code=401)
        else:
            response = HTMLResponse(login_page(wrong=bool(from_query)), status_code=401)
        await response(scope, receive, send)
