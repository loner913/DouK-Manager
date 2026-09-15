from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from douk_manager.config import ManagedPaths
from douk_manager.core.json_store import JsonFileError, read_json, write_json_atomic
from douk_manager.core.locks import critical_section
from douk_manager.core.profile_url import (
    ProfileUrlError,
    normalize_url_for_compare,
    normalized_url_digest,
    strict_admit_url,
)


WATCHLIST_SCHEMA_VERSION = 1
WATERMARK_SCHEMA_VERSION = 1
CONTROL_SCHEMA_VERSION = 1
VALID_STATES = frozenset({"watching", "promoted", "archived"})
VALID_REASONS = frozenset(
    {"few_works", "unknown_updates", "content_pending", "suspected_private", "other"}
)
VALID_REVIEW_ACTIONS = frozenset(
    {"review", "reschedule", "archive", "restore", "reidentify", "promote"}
)
VALID_RECOVERY_PHASES = frozenset(
    {"formal_pending", "formal_committed_watchlist_pending"}
)
VALID_RECEIPTS = frozenset(
    {"pending", "committed", "exists", "formal_exists", "delete_pending", "deleted"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WatchlistError(RuntimeError):
    """Expected watchlist failure with a fixed public message."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.details = details or {}


@dataclass(frozen=True)
class WatchlistPaths:
    root: Path
    data: Path
    watchlist: Path
    watermark: Path
    control: Path
    lock_file: Path
    master_settings: Path

    @classmethod
    def from_managed_paths(cls, paths: ManagedPaths) -> "WatchlistPaths":
        return cls(
            root=paths.root,
            data=paths.data,
            watchlist=paths.watchlist,
            watermark=paths.watchlist_w_watermark,
            control=paths.watchlist_control,
            lock_file=paths.lock_file,
            master_settings=paths.master_settings,
        )


@dataclass(frozen=True)
class WatchlistSnapshot:
    revision: int
    next_w_id: int
    records: tuple[dict[str, Any], ...]

    def as_document(self) -> dict[str, Any]:
        return {
            "schema_version": WATCHLIST_SCHEMA_VERSION,
            "next_w_id": self.next_w_id,
            "revision": self.revision,
            "records": copy.deepcopy(list(self.records)),
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WatchlistError("SCHEMA_INVALID", "观察数据时间必须使用 UTC RFC 3339 格式。")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WatchlistError("SCHEMA_INVALID", "观察数据时间格式无效。") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise WatchlistError("SCHEMA_INVALID", "观察数据时间必须使用 UTC。")
    if parsed.microsecond:
        raise WatchlistError("SCHEMA_INVALID", "观察数据时间不得包含小数秒。")
    return parsed


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _string(value: Any, *, name: str, max_length: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise WatchlistError("SCHEMA_INVALID", f"观察数据字段 {name} 类型无效。")
    if "\x00" in value or any(ord(char) < 0x20 and char not in "\r\n\t" for char in value):
        raise WatchlistError("SCHEMA_INVALID", f"观察数据字段 {name} 含非法控制字符。")
    if len(value) > max_length:
        raise WatchlistError("SCHEMA_INVALID", f"观察数据字段 {name} 超出长度限制。")
    if not allow_empty and not value.strip():
        raise WatchlistError("SCHEMA_INVALID", f"观察数据字段 {name} 不能为空。")
    return value


def _uuid(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise WatchlistError("REQUEST_INVALID", f"{name} 无效。")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise WatchlistError("REQUEST_INVALID", f"{name} 无效。") from exc
    return str(parsed)


def _require_keys(document: dict[str, Any], expected: set[str], label: str) -> None:
    if set(document) != expected:
        raise WatchlistError("SCHEMA_INVALID", f"{label} 字段集合无效。")


def _validate_recovery(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WatchlistError("SCHEMA_INVALID", "promotion_recovery 类型无效。")
    expected = {"phase", "attempt_id", "normalized_url", "created_at", "updated_at", "a_number"}
    _require_keys(value, expected, "promotion_recovery")
    if value["phase"] not in VALID_RECOVERY_PHASES:
        raise WatchlistError("SCHEMA_INVALID", "promotion_recovery 阶段无效。")
    _uuid(value["attempt_id"], name="attempt_id")
    try:
        normalized = strict_admit_url(value["normalized_url"])
    except ProfileUrlError as exc:
        raise WatchlistError("SCHEMA_INVALID", "promotion_recovery URL 无效。") from exc
    if normalized != value["normalized_url"]:
        raise WatchlistError("SCHEMA_INVALID", "promotion_recovery URL 未规范化。")
    parse_utc(value["created_at"])
    parse_utc(value["updated_at"])
    if value["a_number"] is not None and (
        not _is_int(value["a_number"]) or value["a_number"] < 1
    ):
        raise WatchlistError("SCHEMA_INVALID", "promotion_recovery A 编号无效。")
    return copy.deepcopy(value)


def _validate_history_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchlistError("SCHEMA_INVALID", "review_history 项必须是对象。")
    expected = {"reviewed_at", "action", "reasons", "note", "next_review_at"}
    _require_keys(value, expected, "review_history")
    parse_utc(value["reviewed_at"])
    if value["action"] not in VALID_REVIEW_ACTIONS:
        raise WatchlistError("SCHEMA_INVALID", "review_history action 无效。")
    reasons = _validate_reasons(value["reasons"], value["note"])
    note = _string(value["note"], name="review_history.note", max_length=4000)
    next_review_at = value["next_review_at"]
    if next_review_at is not None:
        parse_utc(next_review_at)
    return {
        "reviewed_at": value["reviewed_at"],
        "action": value["action"],
        "reasons": reasons,
        "note": note,
        "next_review_at": next_review_at,
    }


def _validate_reasons(value: Any, note: str) -> list[str]:
    if not isinstance(value, list) or not value or any(item not in VALID_REASONS for item in value):
        raise WatchlistError("SCHEMA_INVALID", "观察原因无效。")
    if len(set(value)) != len(value):
        raise WatchlistError("SCHEMA_INVALID", "观察原因不得重复。")
    if "other" in value and not str(note).strip():
        raise WatchlistError("SCHEMA_INVALID", "选择其他原因时备注不能为空。")
    return list(value)


def validate_record(value: Any, *, next_w_id: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchlistError("SCHEMA_INVALID", "观察记录必须是对象。")
    expected = {
        "w_id",
        "url",
        "display_name",
        "captured_nickname",
        "douyin_id",
        "nickname_blank",
        "state",
        "reasons",
        "note",
        "created_at",
        "updated_at",
        "next_review_at",
        "review_history",
        "archived_at",
        "promoted_a_number",
        "promotion_recovery",
    }
    _require_keys(value, expected, "观察记录")
    if not _is_int(value["w_id"]) or value["w_id"] < 1 or value["w_id"] >= next_w_id:
        raise WatchlistError("SCHEMA_INVALID", "W 编号不在有效高水位范围内。")
    try:
        normalized_url = strict_admit_url(value["url"])
    except ProfileUrlError as exc:
        raise WatchlistError("SCHEMA_INVALID", "观察记录 URL 无效。") from exc
    if normalized_url != value["url"]:
        raise WatchlistError("SCHEMA_INVALID", "观察记录 URL 未规范化。")
    display_name = _string(value["display_name"], name="display_name", max_length=256)
    captured = _string(
        value["captured_nickname"], name="captured_nickname", max_length=256
    )
    douyin_id = _string(value["douyin_id"], name="douyin_id", max_length=256, allow_empty=False)
    if not isinstance(value["nickname_blank"], bool) or value["nickname_blank"] != (captured == ""):
        raise WatchlistError("SCHEMA_INVALID", "nickname_blank 与原始昵称不一致。")
    if value["state"] not in VALID_STATES:
        raise WatchlistError("SCHEMA_INVALID", "观察记录状态无效。")
    note = _string(value["note"], name="note", max_length=4000)
    reasons = _validate_reasons(value["reasons"], note)
    parse_utc(value["created_at"])
    parse_utc(value["updated_at"])
    next_review_at = value["next_review_at"]
    if value["state"] == "watching":
        if not isinstance(next_review_at, str):
            raise WatchlistError("SCHEMA_INVALID", "观察中的记录必须有下次复查时间。")
        parse_utc(next_review_at)
    elif next_review_at is not None:
        parse_utc(next_review_at)
    history_raw = value["review_history"]
    if not isinstance(history_raw, list):
        raise WatchlistError("SCHEMA_INVALID", "review_history 必须是数组。")
    history = [_validate_history_item(item) for item in history_raw]
    archived_at = value["archived_at"]
    if value["state"] == "archived":
        if not isinstance(archived_at, str):
            raise WatchlistError("SCHEMA_INVALID", "已归档记录必须有 archived_at。")
        parse_utc(archived_at)
    elif archived_at is not None:
        raise WatchlistError("SCHEMA_INVALID", "非归档记录不得有 archived_at。")
    promoted_a_number = value["promoted_a_number"]
    if value["state"] == "promoted":
        if not _is_int(promoted_a_number) or promoted_a_number < 1:
            raise WatchlistError("SCHEMA_INVALID", "已转正式记录必须有 A 编号。")
    elif promoted_a_number is not None:
        raise WatchlistError("SCHEMA_INVALID", "非正式记录不得有 A 编号。")
    recovery = _validate_recovery(value["promotion_recovery"])
    if value["state"] != "watching" and recovery is not None:
        raise WatchlistError("SCHEMA_INVALID", "只有观察中的记录可以保留转正修复意图。")
    return {
        "w_id": value["w_id"],
        "url": normalized_url,
        "display_name": display_name,
        "captured_nickname": captured,
        "douyin_id": douyin_id,
        "nickname_blank": value["nickname_blank"],
        "state": value["state"],
        "reasons": reasons,
        "note": note,
        "created_at": value["created_at"],
        "updated_at": value["updated_at"],
        "next_review_at": next_review_at,
        "review_history": history,
        "archived_at": archived_at,
        "promoted_a_number": promoted_a_number,
        "promotion_recovery": recovery,
    }


def validate_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchlistError("SCHEMA_INVALID", "watchlist.json 顶层必须是对象。")
    expected = {"schema_version", "next_w_id", "revision", "records"}
    _require_keys(value, expected, "watchlist.json")
    if value["schema_version"] != WATCHLIST_SCHEMA_VERSION:
        raise WatchlistError("SCHEMA_INVALID", "watchlist schema 版本不受支持。")
    if not _is_int(value["next_w_id"]) or value["next_w_id"] < 1:
        raise WatchlistError("SCHEMA_INVALID", "next_w_id 无效。")
    if not _is_int(value["revision"]) or value["revision"] < 0:
        raise WatchlistError("SCHEMA_INVALID", "watchlist revision 无效。")
    if not isinstance(value["records"], list):
        raise WatchlistError("SCHEMA_INVALID", "watchlist records 必须是数组。")
    records = [validate_record(item, next_w_id=value["next_w_id"]) for item in value["records"]]
    seen: set[int] = set()
    urls: set[str] = set()
    for record in records:
        if record["w_id"] in seen:
            raise WatchlistError("SCHEMA_INVALID", "观察记录 W 编号重复。")
        seen.add(record["w_id"])
        key = normalize_url_for_compare(record["url"])
        if key in urls:
            raise WatchlistError("SCHEMA_INVALID", "观察记录 URL 重复。")
        urls.add(key)
    return {
        "schema_version": WATCHLIST_SCHEMA_VERSION,
        "next_w_id": value["next_w_id"],
        "revision": value["revision"],
        "records": records,
    }


def validate_watermark(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "next_w_id"}:
        raise WatchlistError("SCHEMA_INVALID", "watchlist 高水位文件字段无效。")
    if value["schema_version"] != WATERMARK_SCHEMA_VERSION:
        raise WatchlistError("SCHEMA_INVALID", "watchlist 高水位版本不受支持。")
    if not _is_int(value["next_w_id"]) or value["next_w_id"] < 1:
        raise WatchlistError("SCHEMA_INVALID", "watchlist 高水位无效。")
    return {"schema_version": WATERMARK_SCHEMA_VERSION, "next_w_id": value["next_w_id"]}


def _validate_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchlistError("SCHEMA_INVALID", "请求收据必须是对象。")
    expected = {
        "request_id",
        "payload_digest",
        "normalized_url_digest",
        "outcome",
        "w_id",
        "a_number",
        "next_review_at",
        "created_at",
    }
    _require_keys(value, expected, "request_receipts")
    _uuid(value["request_id"], name="request_id")
    for name in ("payload_digest", "normalized_url_digest"):
        if not isinstance(value[name], str) or not _SHA256_RE.fullmatch(value[name]):
            raise WatchlistError("SCHEMA_INVALID", f"请求收据 {name} 无效。")
    if value["outcome"] not in VALID_RECEIPTS:
        raise WatchlistError("SCHEMA_INVALID", "请求收据 outcome 无效。")
    for name in ("w_id", "a_number"):
        if value[name] is not None and (not _is_int(value[name]) or value[name] < 1):
            raise WatchlistError("SCHEMA_INVALID", f"请求收据 {name} 无效。")
    if value["next_review_at"] is not None:
        parse_utc(value["next_review_at"])
    parse_utc(value["created_at"])
    return copy.deepcopy(value)


def validate_control(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WatchlistError("SCHEMA_INVALID", "watchlist_control.json 顶层必须是对象。")
    expected = {"schema_version", "installation_id", "initialization_state", "write_gate", "request_receipts"}
    _require_keys(value, expected, "watchlist_control.json")
    if value["schema_version"] != CONTROL_SCHEMA_VERSION:
        raise WatchlistError("SCHEMA_INVALID", "watchlist_control schema 版本不受支持。")
    _uuid(value["installation_id"], name="installation_id")
    if value["initialization_state"] not in {"pending", "complete"}:
        raise WatchlistError("SCHEMA_INVALID", "初始化状态无效。")
    if value["write_gate"] not in {"blocked", "ready"}:
        raise WatchlistError("SCHEMA_INVALID", "观察写入闸门状态无效。")
    if not isinstance(value["request_receipts"], list):
        raise WatchlistError("SCHEMA_INVALID", "请求收据必须是数组。")
    receipts = [_validate_receipt(item) for item in value["request_receipts"]]
    request_ids: set[str] = set()
    for receipt in receipts:
        if receipt["request_id"] in request_ids:
            raise WatchlistError("SCHEMA_INVALID", "请求收据 request_id 重复。")
        request_ids.add(receipt["request_id"])
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "installation_id": value["installation_id"],
        "initialization_state": value["initialization_state"],
        "write_gate": value["write_gate"],
        "request_receipts": receipts,
    }


def _payload_digest(fields: dict[str, Any]) -> str:
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


class WatchlistService:
    """Pure-data observation service shared by manager and collector entrypoints."""

    def __init__(
        self,
        paths: ManagedPaths | WatchlistPaths,
        *,
        lock_timeout: float = 10.0,
    ) -> None:
        self.paths = (
            WatchlistPaths.from_managed_paths(paths)
            if isinstance(paths, ManagedPaths)
            else paths
        )
        self.lock_timeout = lock_timeout

    def _lock(self, locked: bool) -> Iterator[None]:
        return (
            nullcontext()
            if locked
            else critical_section(self.paths.lock_file, timeout=self.lock_timeout)
        )

    def is_initialized(self) -> bool:
        return all(path.is_file() for path in (self.paths.watchlist, self.paths.watermark, self.paths.control))

    def initialize(
        self,
        *,
        initialization_evidence: bool,
        locked: bool = False,
    ) -> None:
        with self._lock(locked):
            if any(path.exists() for path in (self.paths.watchlist, self.paths.watermark, self.paths.control)):
                raise WatchlistError("INITIALIZATION_CONFLICT", "观察数据已存在，禁止重新初始化。")
            if not initialization_evidence:
                raise WatchlistError(
                    "RECOVERY_REQUIRED",
                    "无法证明观察功能是首次启用，已阻止自动初始化。",
                )
            installation_id = str(uuid.uuid4())
            write_json_atomic(
                self.paths.control,
                {
                    "schema_version": CONTROL_SCHEMA_VERSION,
                    "installation_id": installation_id,
                    "initialization_state": "pending",
                    "write_gate": "blocked",
                    "request_receipts": [],
                },
            )
            write_json_atomic(
                self.paths.watermark,
                {"schema_version": WATERMARK_SCHEMA_VERSION, "next_w_id": 1},
            )
            write_json_atomic(self.paths.watchlist, _empty_document())
            write_json_atomic(
                self.paths.control,
                {
                    "schema_version": CONTROL_SCHEMA_VERSION,
                    "installation_id": installation_id,
                    "initialization_state": "complete",
                    "write_gate": "blocked",
                    "request_receipts": [],
                },
            )

    def load_document(self) -> dict[str, Any]:
        return self._read_valid(self.paths.watchlist, validate_document, "观察名单文件不可读。")

    def load_watermark(self) -> dict[str, Any]:
        return self._read_valid(self.paths.watermark, validate_watermark, "观察高水位文件不可读。")

    def load_control(self) -> dict[str, Any]:
        return self._read_valid(self.paths.control, validate_control, "观察控制文件不可读。")

    @staticmethod
    def _read_valid(path: Path, validator: Any, message: str) -> dict[str, Any]:
        try:
            value = validator(read_json(path))
        except FileNotFoundError as exc:
            raise WatchlistError("RECOVERY_REQUIRED", message) from exc
        except (JsonFileError, WatchlistError) as exc:
            if isinstance(exc, WatchlistError):
                raise
            raise WatchlistError("RECOVERY_REQUIRED", message) from exc
        except OSError as exc:
            raise WatchlistError("RECOVERY_REQUIRED", message) from exc
        return value

    def validate_all(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        document = self.load_document()
        watermark = self.load_watermark()
        control = self.load_control()
        if document["next_w_id"] < watermark["next_w_id"]:
            raise WatchlistError("RECOVERY_REQUIRED", "观察名单计数器低于不可回退高水位。")
        return document, watermark, control

    def snapshot(self) -> WatchlistSnapshot:
        document, watermark, _ = self.validate_all()
        if document["next_w_id"] < watermark["next_w_id"]:
            raise WatchlistError("RECOVERY_REQUIRED", "观察名单计数器低于不可回退高水位。")
        return WatchlistSnapshot(
            revision=document["revision"],
            next_w_id=max(document["next_w_id"], watermark["next_w_id"]),
            records=tuple(copy.deepcopy(document["records"])),
        )

    def block_writes(self, *, locked: bool = False) -> None:
        with self._lock(locked):
            control = self.load_control()
            if control["write_gate"] == "blocked":
                return
            control["write_gate"] = "blocked"
            write_json_atomic(self.paths.control, control)

    def mark_ready(self, *, locked: bool = False) -> None:
        with self._lock(locked):
            document, watermark, control = self.validate_all()
            if control["initialization_state"] != "complete":
                raise WatchlistError("READ_ONLY", "观察数据尚未完成初始化。")
            if document["next_w_id"] < watermark["next_w_id"]:
                raise WatchlistError("RECOVERY_REQUIRED", "观察名单计数器低于不可回退高水位。")
            if self._has_unresolved(document, control):
                raise WatchlistError("RECOVERY_REQUIRED", "观察数据仍有未解决恢复状态。")
            if watermark["next_w_id"] < document["next_w_id"]:
                watermark["next_w_id"] = document["next_w_id"]
                write_json_atomic(self.paths.watermark, watermark)
            control["write_gate"] = "ready"
            write_json_atomic(self.paths.control, control)

    def has_unresolved_recovery(self) -> bool:
        try:
            document, _, control = self.validate_all()
        except WatchlistError:
            return True
        return self._has_unresolved(document, control)

    @staticmethod
    def _has_unresolved(document: dict[str, Any], control: dict[str, Any]) -> bool:
        if control["initialization_state"] != "complete":
            return True
        if any(
            item["outcome"] in {"pending", "delete_pending"}
            for item in control["request_receipts"]
        ):
            return True
        if any(item["promotion_recovery"] is not None for item in document["records"]):
            return True
        present_w_ids = {item["w_id"] for item in document["records"]}
        deleted_w_ids = {
            item["w_id"]
            for item in control["request_receipts"]
            if item["outcome"] == "deleted" and item["w_id"] is not None
        }
        for receipt in control["request_receipts"]:
            if receipt["outcome"] in {"committed", "exists"}:
                if (
                    receipt["w_id"] is not None
                    and receipt["w_id"] not in present_w_ids
                    and receipt["w_id"] not in deleted_w_ids
                ):
                    return True
        return False

    def observe(self, payload: dict[str, Any], *, locked: bool = False) -> dict[str, Any]:
        request_fields, request_id = self._validate_request(payload)
        normalized_url = request_fields["url"]
        payload_digest = _payload_digest(request_fields)
        url_digest = normalized_url_digest(normalized_url)
        with self._lock(locked):
            # Inspect the receipt before rejecting a gap caused by its own
            # interrupted body write. All ordinary operations retain the gate.
            document = self.load_document()
            watermark = self.load_watermark()
            control = self.load_control()
            self._require_ready(control)
            receipt = self._receipt(control, request_id)
            if receipt is not None:
                if receipt["payload_digest"] != payload_digest:
                    raise WatchlistError("REQUEST_CONFLICT", "request_id 已对应另一份请求。")
                if receipt["outcome"] == "pending":
                    return self._replay_pending_receipt(
                        receipt, document, control, watermark, request_fields
                    )
                if document["next_w_id"] < watermark["next_w_id"]:
                    raise WatchlistError("RECOVERY_REQUIRED", "观察名单计数器低于不可回退高水位。")
                return self._replay_receipt(receipt, document, control)
            if document["next_w_id"] < watermark["next_w_id"]:
                raise WatchlistError("RECOVERY_REQUIRED", "观察名单计数器低于不可回退高水位。")
            pending = next(
                (
                    item
                    for item in control["request_receipts"]
                    if item["outcome"] == "pending"
                    and item["normalized_url_digest"] == url_digest
                ),
                None,
            )
            if pending is not None:
                raise WatchlistError("RECOVERY_REQUIRED", "相同主页已有未完成观察请求。")
            next_review_at = self._resolve_review_time(payload, first_request=True)

            formal_a = self._find_formal_account(normalized_url)
            if formal_a is not None:
                receipt = self._new_receipt(
                    request_id,
                    payload_digest,
                    url_digest,
                    "formal_exists",
                    None,
                    formal_a,
                )
                control["request_receipts"].append(receipt)
                write_json_atomic(self.paths.control, control)
                return {
                    "status": "FORMAL_EXISTS",
                    "message_code": "FORMAL_EXISTS",
                    "request_id": request_id,
                    "w_id": None,
                    "a_number": formal_a,
                    "state": "promoted",
                }

            existing = next(
                (
                    item
                    for item in document["records"]
                    if normalize_url_for_compare(item["url"]) == normalized_url
                ),
                None,
            )
            if existing is not None:
                if existing["state"] == "archived":
                    outcome = "exists"
                    status = "ARCHIVED"
                    code = "ARCHIVED"
                elif existing["state"] == "promoted":
                    outcome = "formal_exists"
                    status = "FORMAL_EXISTS"
                    code = "FORMAL_EXISTS"
                else:
                    outcome = "exists"
                    status = "EXISTS"
                    code = "EXISTS"
                receipt = self._new_receipt(
                    request_id,
                    payload_digest,
                    url_digest,
                    outcome,
                    existing["w_id"],
                    existing["promoted_a_number"],
                )
                control["request_receipts"].append(receipt)
                write_json_atomic(self.paths.control, control)
                return {
                    "status": status,
                    "message_code": code,
                    "request_id": request_id,
                    "w_id": existing["w_id"],
                    "a_number": existing["promoted_a_number"],
                    "state": existing["state"],
                }

            # A valid document can be ahead of a stale but otherwise valid
            # watermark after an interrupted metadata write.  Never reuse an
            # identifier: advance from the larger durable lower bound.
            w_id = max(watermark["next_w_id"], document["next_w_id"])
            next_w_id = w_id + 1
            watermark["next_w_id"] = next_w_id
            write_json_atomic(self.paths.watermark, watermark)
            pending_receipt = self._new_receipt(
                request_id,
                payload_digest,
                url_digest,
                "pending",
                w_id,
                None,
                next_review_at=next_review_at,
            )
            control["request_receipts"].append(pending_receipt)
            write_json_atomic(self.paths.control, control)

            record = self._observation_record(request_fields, w_id, pending_receipt)
            document["next_w_id"] = next_w_id
            document["revision"] += 1
            document["records"].append(record)
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)

            pending_receipt["outcome"] = "committed"
            control["request_receipts"][-1] = pending_receipt
            write_json_atomic(self.paths.control, control)
            return {
                "status": "CREATED",
                "message_code": "CREATED",
                "request_id": request_id,
                "w_id": w_id,
                "a_number": None,
                "state": "watching",
            }

    def record_review(
        self,
        w_id: int,
        *,
        expected_revision: int,
        reasons: list[str],
        note: str,
        next_review_at: str,
        action: str = "review",
        display_name: str | None = None,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        if action not in {"review", "reschedule", "reidentify"}:
            raise WatchlistError("REQUEST_INVALID", "复查操作无效。")
        parse_utc(next_review_at)
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            if record["state"] != "watching":
                raise WatchlistError("STATE_CONFLICT", "当前观察记录不允许复查。")
            if display_name is not None:
                if record.get("promotion_recovery") is not None:
                    raise WatchlistError("RECOVERY_REQUIRED", "转正待恢复时不能修改观察名称。")
                record["display_name"] = _string(display_name, name="display_name", max_length=256)
            record["reasons"] = _validate_reasons(reasons, note)
            record["note"] = _string(note, name="note", max_length=4000)
            record["next_review_at"] = next_review_at
            record["updated_at"] = utc_now()
            record["review_history"].append(
                {
                    "reviewed_at": record["updated_at"],
                    "action": action,
                    "reasons": list(record["reasons"]),
                    "note": record["note"],
                    "next_review_at": next_review_at,
                }
            )
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def begin_promotion(
        self,
        w_id: int,
        *,
        expected_revision: int,
        attempt_id: str,
        normalized_url: str,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        """Persist the durable intent before touching formal JSON/Excel."""

        attempt_id = _uuid(attempt_id, name="attempt_id")
        try:
            normalized_url = strict_admit_url(normalized_url)
        except ProfileUrlError as exc:
            raise WatchlistError("INVALID", "主页 URL 不符合转正入口的安全格式。") from exc
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            if record["state"] != "watching":
                raise WatchlistError("STATE_CONFLICT", "当前观察记录不允许转正。")
            if record["promotion_recovery"] is not None:
                raise WatchlistError("RECOVERY_REQUIRED", "当前观察记录仍有未完成转正意图，请先处理恢复。")
            if record["url"] != normalized_url:
                raise WatchlistError("STATE_CONFLICT", "观察记录身份已变化，请刷新后重试。")
            now = utc_now()
            record["promotion_recovery"] = {
                "phase": "formal_pending",
                "attempt_id": attempt_id,
                "normalized_url": normalized_url,
                "created_at": now,
                "updated_at": now,
                "a_number": None,
            }
            record["updated_at"] = now
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def mark_promotion_formal_committed(
        self,
        w_id: int,
        *,
        expected_revision: int,
        attempt_id: str,
        a_number: int,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        """Record that both formal files committed before final W promotion."""

        attempt_id = _uuid(attempt_id, name="attempt_id")
        if not _is_int(a_number) or a_number < 1:
            raise WatchlistError("REQUEST_INVALID", "正式 A 编号无效。")
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            recovery = record["promotion_recovery"]
            if record["state"] != "watching" or not isinstance(recovery, dict):
                raise WatchlistError("RECOVERY_REQUIRED", "当前转正意图不存在或状态不一致。")
            if recovery["attempt_id"] != attempt_id:
                raise WatchlistError("RECOVERY_REQUIRED", "转正尝试编号不一致，请先处理恢复。")
            now = utc_now()
            recovery["phase"] = "formal_committed_watchlist_pending"
            recovery["a_number"] = a_number
            recovery["updated_at"] = now
            record["updated_at"] = now
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def complete_promotion(
        self,
        w_id: int,
        *,
        expected_revision: int,
        attempt_id: str,
        a_number: int,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        """Publish promoted only after the formal success boundary is known."""

        attempt_id = _uuid(attempt_id, name="attempt_id")
        if not _is_int(a_number) or a_number < 1:
            raise WatchlistError("REQUEST_INVALID", "正式 A 编号无效。")
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            recovery = record["promotion_recovery"]
            if record["state"] != "watching" or not isinstance(recovery, dict):
                raise WatchlistError("RECOVERY_REQUIRED", "当前转正意图不存在或状态不一致。")
            if recovery["attempt_id"] != attempt_id:
                raise WatchlistError("RECOVERY_REQUIRED", "转正尝试编号不一致，请先处理恢复。")
            if recovery["a_number"] not in (None, a_number):
                raise WatchlistError("RECOVERY_REQUIRED", "转正 A 编号与恢复意图不一致。")
            now = utc_now()
            record["state"] = "promoted"
            record["promoted_a_number"] = a_number
            record["promotion_recovery"] = None
            record["next_review_at"] = None
            record["archived_at"] = None
            record["updated_at"] = now
            record["review_history"].append(
                {
                    "reviewed_at": now,
                    "action": "promote",
                    "reasons": list(record["reasons"]),
                    "note": record["note"],
                    "next_review_at": None,
                }
            )
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def reset_promotion_for_retry(
        self,
        w_id: int,
        *,
        expected_revision: int,
        attempt_id: str,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        """Clear an explicitly resolved, formal-empty intent for a new try."""

        attempt_id = _uuid(attempt_id, name="attempt_id")
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            recovery = record["promotion_recovery"]
            if record["state"] != "watching" or not isinstance(recovery, dict):
                raise WatchlistError("RECOVERY_REQUIRED", "当前转正意图不存在或状态不一致。")
            if recovery["attempt_id"] != attempt_id:
                raise WatchlistError("RECOVERY_REQUIRED", "转正尝试编号不一致，请先处理恢复。")
            record["promotion_recovery"] = None
            record["updated_at"] = utc_now()
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def archive(
        self,
        w_id: int,
        *,
        expected_revision: int,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            if record["state"] != "watching":
                raise WatchlistError("STATE_CONFLICT", "当前观察记录不允许归档。")
            now = utc_now()
            record["state"] = "archived"
            record["archived_at"] = now
            record["next_review_at"] = None
            record["updated_at"] = now
            record["review_history"].append(
                {
                    "reviewed_at": now,
                    "action": "archive",
                    "reasons": list(record["reasons"]),
                    "note": record["note"],
                    "next_review_at": None,
                }
            )
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def restore(
        self,
        w_id: int,
        *,
        expected_revision: int,
        next_review_at: str,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        parse_utc(next_review_at)
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            if record["state"] != "archived":
                raise WatchlistError("STATE_CONFLICT", "当前观察记录不允许恢复。")
            now = utc_now()
            record["state"] = "watching"
            record["archived_at"] = None
            record["next_review_at"] = next_review_at
            record["updated_at"] = now
            record["review_history"].append(
                {
                    "reviewed_at": now,
                    "action": "restore",
                    "reasons": list(record["reasons"]),
                    "note": record["note"],
                    "next_review_at": next_review_at,
                }
            )
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            return self.snapshot_locked(document)

    def delete_archived(
        self,
        w_id: int,
        *,
        request_id: str,
        expected_revision: int,
        locked: bool = False,
    ) -> WatchlistSnapshot:
        request_id = _uuid(request_id, name="request_id")
        with self._lock(locked):
            document, _, control = self.validate_all()
            self._require_ready(control)
            receipt = self._receipt(control, request_id)
            if receipt is not None:
                if receipt["payload_digest"] != _payload_digest({"operation": "delete", "w_id": w_id}):
                    raise WatchlistError("REQUEST_CONFLICT", "request_id 已对应另一份请求。")
                if receipt["outcome"] == "deleted":
                    return self.snapshot_locked(document)
                if receipt["outcome"] == "delete_pending":
                    try:
                        self._find_record(document, receipt["w_id"])
                    except WatchlistError:
                        self._set_deletion_outcome(control, receipt["w_id"], "deleted")
                        write_json_atomic(self.paths.control, control)
                        return self.snapshot_locked(document)
                    raise WatchlistError("RECOVERY_REQUIRED", "原删除请求尚未完成，需先恢复现场。")
                raise WatchlistError("REQUEST_CONFLICT", "request_id 已被其他操作使用。")
            self._check_revision(document, expected_revision)
            record = self._find_record(document, w_id)
            if record["state"] != "archived":
                raise WatchlistError("STATE_CONFLICT", "只有已归档记录可以永久删除。")
            payload_digest = _payload_digest({"operation": "delete", "w_id": w_id})
            pending = self._new_receipt(
                request_id,
                payload_digest,
                normalized_url_digest(record["url"]),
                "delete_pending",
                w_id,
                None,
            )
            control["request_receipts"].append(pending)
            self._set_deletion_outcome(control, w_id, "delete_pending")
            write_json_atomic(self.paths.control, control)
            document["records"] = [item for item in document["records"] if item["w_id"] != w_id]
            document["revision"] += 1
            document = validate_document(document)
            write_json_atomic(self.paths.watchlist, document)
            self._set_deletion_outcome(control, w_id, "deleted")
            write_json_atomic(self.paths.control, control)
            return self.snapshot_locked(document)

    def snapshot_locked(self, document: dict[str, Any]) -> WatchlistSnapshot:
        return WatchlistSnapshot(
            revision=document["revision"],
            next_w_id=document["next_w_id"],
            records=tuple(copy.deepcopy(document["records"])),
        )

    @staticmethod
    def _find_record(document: dict[str, Any], w_id: int) -> dict[str, Any]:
        for item in document["records"]:
            if item["w_id"] == w_id:
                return item
        raise WatchlistError("NOT_FOUND", "观察记录不存在。")

    @staticmethod
    def _check_revision(document: dict[str, Any], expected_revision: int) -> None:
        if not _is_int(expected_revision) or expected_revision != document["revision"]:
            raise WatchlistError("REVISION_CONFLICT", "观察数据已更新，请刷新后重试。")

    @staticmethod
    def _require_ready(control: dict[str, Any]) -> None:
        if control["initialization_state"] != "complete" or control["write_gate"] != "ready":
            raise WatchlistError("READ_ONLY", "观察写入当前未获准。")

    @staticmethod
    def _receipt(control: dict[str, Any], request_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in control["request_receipts"] if item["request_id"] == request_id),
            None,
        )

    @staticmethod
    def _set_deletion_outcome(control: dict[str, Any], w_id: int, outcome: str) -> None:
        for item in control["request_receipts"]:
            if item["w_id"] == w_id:
                item["outcome"] = outcome

    def _replay_receipt(
        self, receipt: dict[str, Any], document: dict[str, Any],
        control: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        outcome = receipt["outcome"]
        # Older deletions only marked the deletion request, not its W aliases.
        if control is not None and receipt["w_id"] is not None:
            related = {item["outcome"] for item in control["request_receipts"]
                       if item["w_id"] == receipt["w_id"]}
            if "delete_pending" in related:
                outcome = "delete_pending"
            elif "deleted" in related:
                outcome = "deleted"
        if outcome == "pending":
            raise WatchlistError("RECOVERY_REQUIRED", "原观察请求尚未完成，需先恢复现场。")
        if outcome == "delete_pending":
            raise WatchlistError("RECOVERY_REQUIRED", "原删除请求尚未完成，需先恢复现场。")
        if outcome == "deleted":
            return {
                "status": "DELETED",
                "message_code": "DELETED",
                "request_id": receipt["request_id"],
                "w_id": receipt["w_id"],
                "a_number": None,
                "state": "archived",
            }
        if outcome == "formal_exists":
            return {
                "status": "FORMAL_EXISTS",
                "message_code": "FORMAL_EXISTS",
                "request_id": receipt["request_id"],
                "w_id": receipt["w_id"],
                "a_number": receipt["a_number"],
                "state": "promoted",
            }
        if receipt["w_id"] is None:
            raise WatchlistError("RECOVERY_REQUIRED", "请求收据缺少观察身份。")
        try:
            record = self._find_record(document, receipt["w_id"])
        except WatchlistError as exc:
            raise WatchlistError("RECOVERY_REQUIRED", "请求收据引用的观察正文缺失。") from exc
        status = "ARCHIVED" if record["state"] == "archived" else "EXISTS"
        code = status
        if record["state"] == "promoted":
            status = code = "FORMAL_EXISTS"
        return {
            "status": status,
            "message_code": code,
            "request_id": receipt["request_id"],
            "w_id": record["w_id"],
            "a_number": record["promoted_a_number"],
            "state": record["state"],
        }

    def _replay_pending_receipt(
        self,
        receipt: dict[str, Any],
        document: dict[str, Any],
        control: dict[str, Any],
        watermark: dict[str, Any],
        request_fields: dict[str, Any],
    ) -> dict[str, Any]:
        """Complete only the original request's proven reservation under lock."""

        w_id = receipt.get("w_id")
        if (
            not _is_int(w_id)
            or w_id >= watermark["next_w_id"]
            or receipt["next_review_at"] is None
            or receipt["a_number"] is not None
            or normalized_url_digest(request_fields["url"]) != receipt["normalized_url_digest"]
            or any(item is not receipt and item["w_id"] == w_id for item in control["request_receipts"])
        ):
            raise WatchlistError("RECOVERY_REQUIRED", "请求收据缺少观察身份。")
        record = next((item for item in document["records"] if item["w_id"] == w_id), None)
        missing = record is None
        if missing:
            if (
                document["next_w_id"] != w_id
                or watermark["next_w_id"] != w_id + 1
                or any(normalize_url_for_compare(item["url"]) == request_fields["url"] for item in document["records"])
                or self._find_formal_account(request_fields["url"]) is not None
            ):
                raise WatchlistError("RECOVERY_REQUIRED", "原请求预留编号或主页归属无法确认。")
            record = self._observation_record(request_fields, w_id, receipt)
            document["records"].append(record)
            document["next_w_id"] = watermark["next_w_id"]
            document["revision"] += 1
            document = validate_document(document)
        else:
            expected = self._observation_record(request_fields, w_id, receipt)
            if (
                document["next_w_id"] < watermark["next_w_id"]
                or any(record[key] != value for key, value in expected.items() if key not in {"created_at", "updated_at"})
            ):
                raise WatchlistError("RECOVERY_REQUIRED", "请求收据与观察正文身份或内容不一致。")
        receipt["outcome"] = "committed"
        if self._has_unresolved(document, control):
            raise WatchlistError("RECOVERY_REQUIRED", "观察数据仍有其他未解决恢复状态。")
        if missing:
            write_json_atomic(self.paths.watchlist, document)
        write_json_atomic(self.paths.control, control)
        return self._replay_receipt(receipt, document)

    @staticmethod
    def _observation_record(
        fields: dict[str, Any], w_id: int, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            **{key: fields[key] for key in (
                "url", "display_name", "captured_nickname", "douyin_id",
                "nickname_blank", "reasons", "note",
            )},
            "w_id": w_id,
            "state": "watching",
            "created_at": receipt["created_at"],
            "updated_at": receipt["created_at"],
            "next_review_at": receipt["next_review_at"],
            "review_history": [],
            "archived_at": None,
            "promoted_a_number": None,
            "promotion_recovery": None,
        }

    def _validate_request(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if not isinstance(payload, dict):
            raise WatchlistError("REQUEST_INVALID", "观察请求必须是 JSON 对象。")
        request_id = _uuid(payload.get("request_id"), name="request_id")
        try:
            normalized_url = strict_admit_url(payload.get("url"))
        except ProfileUrlError as exc:
            raise WatchlistError("INVALID", "主页 URL 不符合观察入口的安全格式。") from exc
        captured = _string(
            payload.get("captured_nickname", ""),
            name="captured_nickname",
            max_length=256,
        )
        nickname_blank = payload.get("nickname_blank") is True
        if nickname_blank != (captured == ""):
            raise WatchlistError("REQUEST_INVALID", "原始昵称与空白昵称标记不一致。")
        douyin_id = _string(
            payload.get("douyin_id", ""),
            name="douyin_id",
            max_length=256,
            allow_empty=False,
        )
        display_name = payload.get("display_name", captured)
        display_name = _string(display_name, name="display_name", max_length=256)
        note = _string(payload.get("note", ""), name="note", max_length=4000)
        reasons = _validate_reasons(payload.get("reasons"), note)
        review_after_days = payload.get("review_after_days")
        next_review_at = payload.get("next_review_at")
        if review_after_days is not None and next_review_at is not None:
            raise WatchlistError("REQUEST_INVALID", "提醒时间字段不能同时提交。")
        if review_after_days is not None and (
            not _is_int(review_after_days) or review_after_days not in {7, 15, 30, 45, 90}
        ):
            raise WatchlistError("REQUEST_INVALID", "提醒天数只能是 7、15、30、45 或 90。")
        if next_review_at is not None:
            if not isinstance(next_review_at, str):
                raise WatchlistError("REQUEST_INVALID", "自定义提醒时间无效。")
            parse_utc(next_review_at)
        return (
            {
                "url": normalized_url,
                "captured_nickname": captured,
                "display_name": display_name,
                "douyin_id": douyin_id,
                "nickname_blank": nickname_blank,
                "reasons": reasons,
                "note": note,
                "review_after_days": review_after_days,
                "next_review_at": next_review_at,
            },
            request_id,
        )

    @staticmethod
    def _resolve_review_time(payload: dict[str, Any], *, first_request: bool) -> str:
        if payload.get("next_review_at") is not None:
            value = payload["next_review_at"]
            parsed = parse_utc(value)
            if first_request and parsed <= datetime.now(timezone.utc):
                raise WatchlistError("REQUEST_INVALID", "自定义提醒时间必须在未来。")
            return value
        days = payload.get("review_after_days") or 30
        return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _find_formal_account(self, normalized_url: str) -> int | None:
        try:
            document = read_json(self.paths.master_settings)
        except (OSError, JsonFileError) as exc:
            raise WatchlistError("READ_ONLY", "正式主档当前不可读取，观察写入已停止。") from exc
        accounts = document.get("accounts_urls")
        if not isinstance(accounts, list):
            raise WatchlistError("READ_ONLY", "正式主档结构不可验证，观察写入已停止。")
        for index, item in enumerate(accounts):
            if not isinstance(item, dict):
                continue
            if normalize_url_for_compare(item.get("url", "")) == normalized_url:
                return index + 1
        return None

    @staticmethod
    def _new_receipt(
        request_id: str,
        payload_digest: str,
        url_digest: str,
        outcome: str,
        w_id: int | None,
        a_number: int | None,
        *,
        next_review_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "payload_digest": payload_digest,
            "normalized_url_digest": url_digest,
            "outcome": outcome,
            "w_id": w_id,
            "a_number": a_number,
            "next_review_at": next_review_at,
            "created_at": utc_now(),
        }


def _empty_document() -> dict[str, Any]:
    return {
        "schema_version": WATCHLIST_SCHEMA_VERSION,
        "next_w_id": 1,
        "revision": 0,
        "records": [],
    }
