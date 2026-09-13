"""Explicit-path adapter for the existing formal collector transaction.

The vendor module owns the JSON + Excel consistency algorithm.  This module
does not create a second writer; it supplies the manager's ``ManagedPaths`` to
that implementation and exposes only the small, non-HTTP surface required by
W promotion and recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from douk_manager.config import ManagedPaths
from douk_manager.vendor import collector_server as vendor


class FormalCollectorError(RuntimeError):
    """Expected formal-data failure with a safe public message."""

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
class FormalMatch:
    a_number: int
    url_key: str
    identity_key: str
    matched_by: tuple[str, ...]


@dataclass(frozen=True)
class FormalInspection:
    """Read-only joint status used before and during promotion."""

    status: str
    json_matches: tuple[FormalMatch, ...]
    excel_matches: tuple[FormalMatch, ...]
    existing_a_number: int | None = None


@dataclass(frozen=True)
class FormalPreview:
    status: str
    next_a_number: int | None
    a_number: int | None
    mark: str | None
    message_code: str


@dataclass(frozen=True)
class FormalAddResult:
    a_number: int
    mark: str
    message_code: str


_SAFE_MESSAGES = {
    "DUPLICATE_JSON_URL": "正式主档已存在相同主页，未追加。",
    "DUPLICATE_JSON_IDENTITY": "正式主档已存在相同身份，未追加。",
    "INCOMPLETE_TRANSACTION": "检测到未完成正式双文件事务，已阻止转正。",
    "WRITE_PERMISSION_DENIED": "正式 JSON＋Excel 写入被拒绝，观察记录已保留待恢复。",
    "WRITE_FAILED": "正式 JSON＋Excel 写入未完成，观察记录已保留待恢复。",
    "POST_COMMIT_VERIFY_FAILED": "正式双文件写入复读校验失败，观察记录已保留待恢复。",
    "BACKUP_STATE_WRITE_FAILED": "正式写入的备份状态未完成，观察记录已保留待恢复。",
    "FORMAL_INCONSISTENT": "正式 JSON 与 Excel 的目标记录不一致，已阻止转正。",
}


def _safe_message(code: str) -> str:
    return _SAFE_MESSAGES.get(code, "正式数据联合检查未通过，已阻止本次转正。")


class FormalCollectorService:
    """Reuse the vendor's formal precheck and dual-file transaction."""

    def __init__(self, paths: ManagedPaths) -> None:
        self.paths = paths

    def _runtime(self):
        return vendor.configured_runtime(
            base_dir=self.paths.collector_data,
            settings_path=self.paths.master_settings,
            excel_path=self.paths.collector_excel,
            global_lock_path=self.paths.lock_file,
            watchlist_path=self.paths.watchlist,
            watchlist_watermark_path=self.paths.watchlist_w_watermark,
            watchlist_control_path=self.paths.watchlist_control,
            manager_instance_lock_path=self.paths.instance_lock_file,
            screenshot_dir=self.paths.screenshot_inbox,
        )

    @staticmethod
    def payload_for_record(record: Mapping[str, Any]) -> dict[str, Any]:
        """Build the formal collector payload from immutable W identity fields."""

        return {
            "url": record["url"],
            "nickname": record["captured_nickname"],
            "douyin_id": record["douyin_id"],
            "nickname_blank": record["nickname_blank"],
            # The editable W display name is deliberately not formal identity.
            "mark": "",
        }

    @staticmethod
    def _raise_vendor(error: vendor.CollectorError) -> None:
        raise FormalCollectorError(
            error.code,
            _safe_message(error.code),
            details={"vendor_code": error.code},
        ) from error

    def preview(self, payload: Mapping[str, Any]) -> FormalPreview:
        inspection = self.inspect(payload)
        if inspection.status == "inconsistent":
            raise FormalCollectorError(
                "FORMAL_INCONSISTENT",
                _safe_message("FORMAL_INCONSISTENT"),
                details={
                    "json_match_count": len(inspection.json_matches),
                    "excel_match_count": len(inspection.excel_matches),
                },
            )
        if inspection.status == "consistent":
            number = inspection.existing_a_number
            return FormalPreview(
                status="formal_exists",
                next_a_number=None,
                a_number=number,
                mark=None,
                message_code="FORMAL_EXISTS",
            )

        try:
            with self._runtime():
                result = vendor.preview(dict(payload))
        except vendor.CollectorError as exc:
            self._raise_vendor(exc)
        except Exception as exc:  # pragma: no cover - defensive boundary.
            raise FormalCollectorError("FORMAL_PREVIEW_FAILED", _safe_message("FORMAL_PREVIEW_FAILED")) from exc

        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("number"), int):
            raise FormalCollectorError("FORMAL_PREVIEW_FAILED", _safe_message("FORMAL_PREVIEW_FAILED"))
        return FormalPreview(
            status="available",
            next_a_number=int(data["number"]),
            a_number=None,
            mark=str(data.get("mark") or ""),
            message_code="READY",
        )

    def add(self, payload: Mapping[str, Any]) -> FormalAddResult:
        try:
            with self._runtime():
                result = vendor.add_record(dict(payload))
        except vendor.CollectorError as exc:
            self._raise_vendor(exc)
        except Exception as exc:  # pragma: no cover - defensive boundary.
            raise FormalCollectorError("FORMAL_WRITE_FAILED", _safe_message("FORMAL_WRITE_FAILED")) from exc

        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("number"), int):
            raise FormalCollectorError("FORMAL_WRITE_FAILED", _safe_message("FORMAL_WRITE_FAILED"))
        return FormalAddResult(
            a_number=int(data["number"]),
            mark=str(data.get("mark") or ""),
            message_code=str(result.get("code") or "ADDED"),
        )

    def inspect(self, payload: Mapping[str, Any]) -> FormalInspection:
        """Inspect both formal files without writing or reserving an A number."""

        try:
            with self._runtime():
                vendor.check_incomplete_transaction()
                nickname, douyin_id, _requested_mark, _nickname_blank = vendor.profile_fields(
                    dict(payload)
                )
                wanted_url = vendor.normalize_url_for_compare(str(payload.get("url") or ""))
                wanted_identity = vendor.normalize_identity(f"{nickname}{douyin_id}")
                document = vendor.read_json_document()
                json_state = vendor.validate_json_structure(document)
                workbook = vendor.load_excel()
                try:
                    excel_state = vendor.validate_excel_structure(workbook)
                    worksheet = vendor.get_worksheet(workbook)
                    json_matches = self._json_matches(
                        json_state.accounts,
                        wanted_url=wanted_url,
                        wanted_identity=wanted_identity,
                    )
                    excel_matches = self._excel_matches(
                        worksheet,
                        wanted_url=wanted_url,
                        wanted_identity=wanted_identity,
                        max_row=vendor.EXCEL_LAST_TEMPLATE_ROW,
                    )
                    # Force the structural result to be part of the read-only
                    # contract even though the matching loops above are enough
                    # to locate a target.
                    _ = excel_state
                finally:
                    workbook.close()
        except vendor.CollectorError as exc:
            self._raise_vendor(exc)
        except FormalCollectorError:
            raise
        except Exception as exc:  # pragma: no cover - defensive boundary.
            raise FormalCollectorError("FORMAL_INSPECTION_FAILED", _safe_message("FORMAL_INSPECTION_FAILED")) from exc

        json_tuple = tuple(json_matches)
        excel_tuple = tuple(excel_matches)
        if not json_tuple and not excel_tuple:
            status = "available"
            existing = None
        elif self._is_consistent(json_tuple, excel_tuple, wanted_url, wanted_identity):
            status = "consistent"
            existing = json_tuple[0].a_number
        else:
            status = "inconsistent"
            existing = None
        return FormalInspection(status, json_tuple, excel_tuple, existing)

    @staticmethod
    def _json_matches(
        accounts: Any,
        *,
        wanted_url: str,
        wanted_identity: str,
    ) -> list[FormalMatch]:
        matches: list[FormalMatch] = []
        for index, item in enumerate(accounts):
            if not isinstance(item, dict):
                continue
            number = index + 1
            url_key = vendor.normalize_url_for_compare(item.get("url"))
            suffix = vendor.strip_mark_number(vendor.text(item.get("mark")), number)
            identity_key = vendor.normalize_identity(suffix)
            matched_by: list[str] = []
            if url_key and url_key == wanted_url:
                matched_by.append("url")
            if identity_key and identity_key == wanted_identity:
                matched_by.append("identity")
            if matched_by:
                matches.append(FormalMatch(number, url_key, identity_key, tuple(matched_by)))
        return matches

    @staticmethod
    def _excel_matches(
        worksheet: Any,
        *,
        wanted_url: str,
        wanted_identity: str,
        max_row: int,
    ) -> list[FormalMatch]:
        matches: list[FormalMatch] = []
        for row in range(vendor.EXCEL_FIRST_ACCOUNT_ROW, max_row + 1):
            body = vendor.text(worksheet[f"B{row}"].value)
            raw_url = vendor.text(worksheet[f"D{row}"].value)
            if not body and not raw_url:
                continue
            number = row - 1
            url_key = vendor.normalize_url_for_compare(raw_url)
            identity_key = vendor.normalize_identity(body)
            matched_by: list[str] = []
            if url_key and url_key == wanted_url:
                matched_by.append("url")
            if identity_key and identity_key == wanted_identity:
                matched_by.append("identity")
            if matched_by:
                matches.append(FormalMatch(number, url_key, identity_key, tuple(matched_by)))
        return matches

    @staticmethod
    def _is_consistent(
        json_matches: tuple[FormalMatch, ...],
        excel_matches: tuple[FormalMatch, ...],
        wanted_url: str,
        wanted_identity: str,
    ) -> bool:
        if len(json_matches) != 1 or len(excel_matches) != 1:
            return False
        json_match = json_matches[0]
        excel_match = excel_matches[0]
        return (
            json_match.a_number == excel_match.a_number
            and json_match.url_key == wanted_url
            and excel_match.url_key == wanted_url
            and json_match.identity_key == wanted_identity
            and excel_match.identity_key == wanted_identity
        )


__all__ = [
    "FormalAddResult",
    "FormalCollectorError",
    "FormalCollectorService",
    "FormalInspection",
    "FormalMatch",
    "FormalPreview",
]
