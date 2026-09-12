from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit
from typing import Any


_ALLOWED_HOSTS = frozenset({"douyin.com", "www.douyin.com"})
_HEX = frozenset("0123456789abcdefABCDEF")
_ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|2F|5c|5C)")


class ProfileUrlError(ValueError):
    """A fixed, non-sensitive URL admission error."""

    code = "INVALID_URL"
    public_message = "主页 URL 不符合观察入口的安全格式。"

    def __init__(self, reason: str = "invalid profile URL") -> None:
        super().__init__(reason)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _legacy_fallback(raw_url: str) -> str:
    # This intentionally mirrors the pre-V0.1.7 vendor comparison fallback.
    # It is a comparison key, not an admission decision.
    return raw_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")


def normalize_url_for_compare(raw_url: Any) -> str:
    """Return the existing loose comparison key byte-for-byte in spirit.

    Formal legacy duplicate checks depend on this permissive behavior.  It
    keeps the vendor's invalid-input fallback, strips ports through the
    historical ``netloc.split(':', 1)`` behavior, and preserves extra path
    segments.  Call :func:`strict_admit_url` for new observation input.
    """

    value = _text(raw_url)
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        host = parsed.netloc.lower().split(":", 1)[0]
        path = parsed.path.rstrip("/")
    except ValueError:
        return _legacy_fallback(value)
    if host in _ALLOWED_HOSTS and path.startswith("/user/"):
        return urlunsplit(("https", "www.douyin.com", path, "", ""))
    return _legacy_fallback(value)


def _reject(reason: str) -> ProfileUrlError:
    return ProfileUrlError(reason)


def strict_admit_url(raw_url: Any) -> str:
    """Validate a newly supplied profile URL and return its canonical form.

    This gate deliberately differs from ``normalize_url_for_compare``.  It
    rejects userinfo, non-default ports, path separators hidden by percent
    encoding, extra path segments, controls, and empty identifiers.  No
    network lookup or file access is performed.
    """

    value = _text(raw_url)
    if not value:
        raise _reject("empty URL")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _reject("control character")
    if "\\" in value or _ENCODED_SEPARATOR_RE.search(value):
        raise _reject("path separator")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _reject("URL cannot be parsed") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise _reject("scheme")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise _reject("userinfo")
    try:
        host = parsed.hostname.casefold() if parsed.hostname else ""
        port = parsed.port
    except ValueError as exc:
        raise _reject("port") from exc
    if host not in _ALLOWED_HOSTS:
        raise _reject("host")
    expected_port = 80 if scheme == "http" else 443
    if port not in {None, expected_port}:
        raise _reject("non-default port")

    path = parsed.path
    if not path or "%" in path:
        index = 0
        while index < len(path):
            if path[index] != "%":
                index += 1
                continue
            if index + 2 >= len(path) or path[index + 1] not in _HEX or path[index + 2] not in _HEX:
                raise _reject("invalid percent escape")
            index += 3
        decoded_path = unquote(path)
        if "/" in decoded_path[6:] or "\\" in decoded_path:
            raise _reject("encoded path separator")

    normalized_path = path.rstrip("/")
    prefix = "/user/"
    if not normalized_path.startswith(prefix):
        raise _reject("profile path")
    identifier = normalized_path[len(prefix) :]
    if not identifier or "/" in identifier or "\\" in identifier:
        raise _reject("extra path segment")
    if any(char.isspace() for char in identifier):
        raise _reject("identifier whitespace")

    return urlunsplit(("https", "www.douyin.com", normalized_path, "", ""))


def normalized_url_digest(normalized_url: str) -> str:
    """Return a digest for an already validated URL without exposing it."""

    import hashlib

    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
