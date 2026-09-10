from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit


_STABLE_ID_TEXT = r"(?:[A-Za-z0-9_-]{55}|[A-Za-z0-9_-]{76})"
_PROFILE_PATH = re.compile(rf"/user/({_STABLE_ID_TEXT})/?")
_LOG_FIELD_ID = re.compile(
    r"(?<![A-Za-z0-9_])['\"]?(?:sec_user_id|sec_uid|secuid)['\"]?"
    r"\s*(?:=|:)\s*['\"]?"
    rf"({_STABLE_ID_TEXT})(?![A-Za-z0-9._~-])",
    re.IGNORECASE,
)
_ENGINE_INFO_FAILURE_ID = re.compile(
    rf"(?<![A-Za-z0-9._~-])({_STABLE_ID_TEXT})(?![A-Za-z0-9._~-])"
    r"\s+获取账号信息失败(?:[，,!！。. ]|$)"
)
_TOKEN_DOMAIN = b"douk-account-identity\0"


def profile_identity_token(raw_url: str) -> str | None:
    """Return a non-reversible token only for a canonical Douyin profile ID."""

    try:
        parsed = urlsplit(raw_url.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"}:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if port is not None:
        return None
    if (hostname or "").casefold() not in {"douyin.com", "www.douyin.com"}:
        return None
    matched = _PROFILE_PATH.fullmatch(parsed.path)
    if matched is None:
        return None
    return identity_token(matched.group(1))


def log_identity_tokens(line: str) -> tuple[str, ...]:
    """Extract stable IDs from one log line and discard their raw values."""

    stable_ids = {
        match.group(1)
        for pattern in (_LOG_FIELD_ID, _ENGINE_INFO_FAILURE_ID)
        for match in pattern.finditer(line)
    }
    return tuple(sorted(identity_token(stable_id) for stable_id in stable_ids))


def identity_token(stable_id: str) -> str:
    return hashlib.sha256(_TOKEN_DOMAIN + stable_id.encode("utf-8")).hexdigest()
