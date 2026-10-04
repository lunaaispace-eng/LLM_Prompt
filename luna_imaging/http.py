from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, TypeVar

T = TypeVar("T")

# Module-level reference so tests can patch `luna_imaging.http.time.sleep`
# or replace `_sleep` without waiting.
_sleep = time.sleep


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _error_message(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw.strip()[:2000] or "empty error body"
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
        if data.get("message"):
            return str(data["message"])
    return raw.strip()[:2000]


def _send(url: str, headers: dict, data: bytes, content_type: str, timeout: float) -> dict:
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise ProviderError(f"HTTP {e.code}: {_error_message(raw)}", status=e.code) from None
    except urllib.error.URLError as e:
        raise ProviderError(f"network error: {e.reason}", status=None) from None
    except (TimeoutError, OSError) as e:
        raise ProviderError(f"network error: {e}", status=None) from None
    except ValueError as e:
        raise ProviderError(f"invalid JSON in response: {e}", status=None) from None


def post_json(url: str, headers: dict, body: dict, timeout: float) -> dict:
    return _send(url, headers, json.dumps(body).encode("utf-8"), "application/json", timeout)


def _multipart(fields: dict[str, str], files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = "----LunaImaging" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                f"{v}\r\n").encode("utf-8")
    for field_name, fname, data in files:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
                f"filename=\"{fname}\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8")
        out += data
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def post_multipart(url: str, headers: dict, fields: dict[str, str],
                   files: list[tuple[str, str, bytes]], timeout: float) -> dict:
    data, ctype = _multipart(fields, files)
    return _send(url, headers, data, ctype, timeout)


def with_retries(fn: Callable[[], T], max_retries: int) -> T:
    attempt = 0
    while True:
        try:
            return fn()
        except ProviderError as e:
            retryable = e.status is None or e.status >= 500 or e.status == 429
            if not retryable or attempt >= max_retries:
                raise
            _sleep(2 ** attempt)
            attempt += 1
