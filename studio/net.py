"""HTTP-запросы к внешним сервисам (с учётом прокси из настроек)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config

USER_AGENT = "TikTokStudio/1.0"


class NetError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: bytes = b""):
        super().__init__(message)
        self.status = status
        self.body = body


def opener() -> urllib.request.OpenerDirector:
    proxy = config.get_proxy()
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    json_body: object | None = None,
    timeout: float = 60,
) -> tuple[int, str, bytes]:
    """Выполняет запрос и возвращает (код, content-type, тело). Ошибки HTTP -> NetError."""
    data = None
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener().open(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        raise NetError(error_message(body) or f"HTTP {exc.code}", exc.code, body) from exc
    except OSError as exc:  # нет сети, таймаут, DNS
        raise NetError(f"нет связи с сервисом ({exc})") from exc


def error_message(body: bytes) -> str:
    """Достаёт текст ошибки из типичных JSON-ответов API."""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        text = body.decode("utf-8", "replace").strip()
        return text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            return str(first.get("message") if isinstance(first, dict) else first)
        if data.get("message"):
            return str(data["message"])
    return ""


def parse_json(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise NetError("сервис вернул не JSON: " + body[:200].decode("utf-8", "replace")) from exc
