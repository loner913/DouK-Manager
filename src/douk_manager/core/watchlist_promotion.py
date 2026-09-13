"""W-to-A promotion and recovery orchestration."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from douk_manager.config import ManagedPaths
from douk_manager.core.collector_service import (
    FormalCollectorError,
    FormalCollectorService,
    FormalInspection,
    FormalPreview,
)
from douk_manager.core.locks import critical_section
from douk_manager.core.profile_url import ProfileUrlError, strict_admit_url
from douk_manager.core.watchlist import WatchlistError, WatchlistService

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


class WatchlistPromotionError(RuntimeError):
    """Expected promotion failure with a fixed, non-sensitive message."""

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


@contextmanager
def _watchlist_error_boundary():
    """Expose watchlist conflicts through the promotion API's safe error type."""

    try:
        yield
    except WatchlistError as exc:
        raise WatchlistPromotionError(
            exc.code,
            exc.public_message,
            details=exc.details,
        ) from exc


@dataclass(frozen=True)
class PromotionPreview:
    w_id: int
    revision: int
    display_name: str
    captured_nickname: str
    douyin_id: str
    nickname_blank: bool
    formal_status: str
    message_code: str
    next_a_number: int | None
    existing_a_number: int | None
    mark: str | None
    recovery_phase: str | None


@dataclass(frozen=True)
class PromotionResult:
    w_id: int
    a_number: int
    revision: int
    formal_status: str
    recovered_existing: bool


class WatchlistPromotionService:
    """Coordinate W metadata and the existing formal dual-file writer."""

    def __init__(
        self,
        paths: ManagedPaths,
        *,
        watchlist: WatchlistService | None = None,
        formal: FormalCollectorService | None = None,
    ) -> None:
        self.paths = paths
        self.watchlist = watchlist or WatchlistService(paths)
        self.formal = formal or FormalCollectorService(paths)

    def preview(
        self,
        w_id: int,
        *,
        expected_revision: int | None = None,
    ) -> PromotionPreview:
        with critical_section(self.paths.lock_file):
            with _watchlist_error_boundary():
                document, _watermark, control = self.watchlist.validate_all()
                self.watchlist._require_ready(control)
                if expected_revision is not None:
                    self.watchlist._check_revision(document, expected_revision)
                record = self.watchlist._find_record(document, w_id)
                if record["state"] != "watching":
                    raise WatchlistPromotionError("STATE_CONFLICT", "当前观察记录不允许转正。")
                try:
                    normalized_url = strict_admit_url(record["url"])
                except ProfileUrlError as exc:
                    raise WatchlistPromotionError(
                        "INVALID",
                        "观察记录主页 URL 无法通过转正安全校验。",
                    ) from exc
                if normalized_url != record["url"]:
                    raise WatchlistPromotionError("INVALID", "观察记录主页 URL 未规范化。")

                payload = self.formal.payload_for_record(record)
                recovery = record["promotion_recovery"]
                if recovery is not None:
                    inspection = self._formal_inspection(payload)
                    if inspection.status == "inconsistent":
                        self._raise_inconsistent(inspection)
                    if inspection.status == "consistent":
                        return self._preview_from_formal(
                            document["revision"], record, recovery, inspection=inspection
                        )
                    # A pending intent with an empty formal target is never
                    # retried implicitly.  Show the next read-only A preview only
                    # as guidance for an explicit retry action.
                    formal_preview = self._formal_preview(payload)
                    return self._preview_from_formal(
                        document["revision"],
                        record,
                        recovery,
                        formal_preview=formal_preview,
                    )

                formal_preview = self._formal_preview(payload)
                return self._preview_from_formal(
                    document["revision"], record, None, formal_preview=formal_preview
                )

    def promote(
        self,
        w_id: int,
        *,
        expected_revision: int,
        context: OperationContext | None = None,
    ) -> PromotionResult:
        if context is not None:
            context.raise_if_cancelled()
        with critical_section(self.paths.lock_file):
            with _watchlist_error_boundary():
                if context is not None:
                    context.raise_if_cancelled()
                document, _watermark, control = self.watchlist.validate_all()
                self.watchlist._require_ready(control)
                self.watchlist._check_revision(document, expected_revision)
                record = self.watchlist._find_record(document, w_id)
                if record["state"] != "watching":
                    raise WatchlistPromotionError("STATE_CONFLICT", "当前观察记录不允许转正。")
                self._validate_record_url(record)
                payload = self.formal.payload_for_record(record)
                inspection = self._formal_inspection(payload)
                recovery = record["promotion_recovery"]

                if recovery is not None:
                    if inspection.status == "consistent":
                        attempt_id = str(recovery["attempt_id"])
                        self._enter_critical(context)
                        snapshot = self.watchlist.complete_promotion(
                            w_id,
                            expected_revision=document["revision"],
                            attempt_id=attempt_id,
                            a_number=int(inspection.existing_a_number),
                            locked=True,
                        )
                        return PromotionResult(
                            w_id=w_id,
                            a_number=int(inspection.existing_a_number),
                            revision=snapshot.revision,
                            formal_status="recovered_existing",
                            recovered_existing=True,
                        )
                    self._raise_retry_required()

                self._enter_critical(context)
                attempt_id = str(uuid.uuid4())
                intent_snapshot = self.watchlist.begin_promotion(
                    w_id,
                    expected_revision=document["revision"],
                    attempt_id=attempt_id,
                    normalized_url=record["url"],
                    locked=True,
                )
                if inspection.status == "consistent":
                    snapshot = self.watchlist.complete_promotion(
                        w_id,
                        expected_revision=intent_snapshot.revision,
                        attempt_id=attempt_id,
                        a_number=int(inspection.existing_a_number),
                        locked=True,
                    )
                    return PromotionResult(
                        w_id=w_id,
                        a_number=int(inspection.existing_a_number),
                        revision=snapshot.revision,
                        formal_status="formal_exists",
                        recovered_existing=True,
                    )
                if inspection.status != "available":
                    self._raise_inconsistent(inspection)

                try:
                    added = self.formal.add(payload)
                except FormalCollectorError as exc:
                    raise WatchlistPromotionError(
                        exc.code,
                        exc.public_message,
                        details=exc.details,
                    ) from exc

                try:
                    committed_snapshot = self.watchlist.mark_promotion_formal_committed(
                        w_id,
                        expected_revision=intent_snapshot.revision,
                        attempt_id=attempt_id,
                        a_number=added.a_number,
                        locked=True,
                    )
                except WatchlistError as exc:
                    raise WatchlistPromotionError(
                        exc.code,
                        "正式数据已完成，但观察记录仍处于转正修复中。",
                        details={"watchlist_code": exc.code},
                    ) from exc

                try:
                    snapshot = self.watchlist.complete_promotion(
                        w_id,
                        expected_revision=committed_snapshot.revision,
                        attempt_id=attempt_id,
                        a_number=added.a_number,
                        locked=True,
                    )
                except WatchlistError as exc:
                    raise WatchlistPromotionError(
                        exc.code,
                        "正式数据已完成，但观察记录仍处于转正修复中。",
                        details={"watchlist_code": exc.code},
                    ) from exc
                return PromotionResult(
                    w_id=w_id,
                    a_number=added.a_number,
                    revision=snapshot.revision,
                    formal_status="promoted",
                    recovered_existing=False,
                )

    def retry(
        self,
        w_id: int,
        *,
        expected_revision: int,
        attempt_id: str,
        context: OperationContext | None = None,
    ) -> PromotionResult:
        """Explicitly retry only after read-only reconciliation proves both files empty."""

        if context is not None:
            context.raise_if_cancelled()
        with critical_section(self.paths.lock_file):
            with _watchlist_error_boundary():
                if context is not None:
                    context.raise_if_cancelled()
                document, _watermark, control = self.watchlist.validate_all()
                self.watchlist._require_ready(control)
                self.watchlist._check_revision(document, expected_revision)
                record = self.watchlist._find_record(document, w_id)
                recovery = record["promotion_recovery"]
                if record["state"] != "watching" or not isinstance(recovery, dict):
                    raise WatchlistPromotionError("RECOVERY_REQUIRED", "当前观察记录没有可重试的转正意图。")
                if str(recovery["attempt_id"]) != str(attempt_id):
                    raise WatchlistPromotionError("RECOVERY_REQUIRED", "转正尝试编号不一致，请先刷新。")
                payload = self.formal.payload_for_record(record)
                inspection = self._formal_inspection(payload)
                if inspection.status == "inconsistent":
                    self._raise_inconsistent(inspection)
                if inspection.status == "consistent":
                    self._enter_critical(context)
                    snapshot = self.watchlist.complete_promotion(
                        w_id,
                        expected_revision=document["revision"],
                        attempt_id=str(attempt_id),
                        a_number=int(inspection.existing_a_number),
                        locked=True,
                    )
                    return PromotionResult(
                        w_id=w_id,
                        a_number=int(inspection.existing_a_number),
                        revision=snapshot.revision,
                        formal_status="recovered_existing",
                        recovered_existing=True,
                    )
                if inspection.status != "available":
                    self._raise_retry_required()
                self._enter_critical(context)
                cleared = self.watchlist.reset_promotion_for_retry(
                    w_id,
                    expected_revision=document["revision"],
                    attempt_id=str(attempt_id),
                    locked=True,
                )
                # Re-enter the same locked orchestration with a new revision.  No
                # public call is made, so there is no lock gap or automatic loop.
                return self._promote_after_retry(
                    w_id,
                    expected_revision=cleared.revision,
                    context=context,
                )

    def _promote_after_retry(
        self,
        w_id: int,
        *,
        expected_revision: int,
        context: OperationContext | None,
    ) -> PromotionResult:
        with _watchlist_error_boundary():
            document, _watermark, control = self.watchlist.validate_all()
            self.watchlist._require_ready(control)
            self.watchlist._check_revision(document, expected_revision)
            record = self.watchlist._find_record(document, w_id)
            self._validate_record_url(record)
            payload = self.formal.payload_for_record(record)
            inspection = self._formal_inspection(payload)
            if inspection.status != "available":
                if inspection.status == "consistent":
                    raise WatchlistPromotionError(
                        "STATE_CONFLICT",
                        "正式目标已出现，请刷新后使用恢复入口。",
                    )
                self._raise_inconsistent(inspection)
            attempt_id = str(uuid.uuid4())
            intent_snapshot = self.watchlist.begin_promotion(
                w_id,
                expected_revision=document["revision"],
                attempt_id=attempt_id,
                normalized_url=record["url"],
                locked=True,
            )
            try:
                added = self.formal.add(payload)
            except FormalCollectorError as exc:
                raise WatchlistPromotionError(exc.code, exc.public_message, details=exc.details) from exc
            committed_snapshot = self.watchlist.mark_promotion_formal_committed(
                w_id,
                expected_revision=intent_snapshot.revision,
                attempt_id=attempt_id,
                a_number=added.a_number,
                locked=True,
            )
            snapshot = self.watchlist.complete_promotion(
                w_id,
                expected_revision=committed_snapshot.revision,
                attempt_id=attempt_id,
                a_number=added.a_number,
                locked=True,
            )
            return PromotionResult(
                w_id=w_id,
                a_number=added.a_number,
                revision=snapshot.revision,
                formal_status="promoted",
                recovered_existing=False,
            )

    def _formal_inspection(self, payload: Mapping[str, Any]) -> FormalInspection:
        try:
            return self.formal.inspect(payload)
        except FormalCollectorError as exc:
            raise WatchlistPromotionError(exc.code, exc.public_message, details=exc.details) from exc

    def _formal_preview(self, payload: Mapping[str, Any]) -> FormalPreview:
        try:
            return self.formal.preview(payload)
        except FormalCollectorError as exc:
            raise WatchlistPromotionError(exc.code, exc.public_message, details=exc.details) from exc

    @staticmethod
    def _preview_from_formal(
        revision: int,
        record: Mapping[str, Any],
        recovery: Mapping[str, Any] | None,
        *,
        inspection: FormalInspection | None = None,
        formal_preview: FormalPreview | None = None,
    ) -> PromotionPreview:
        if inspection is not None and inspection.status == "consistent":
            status = "formal_exists" if recovery is None else "recovery_ready"
            return PromotionPreview(
                w_id=int(record["w_id"]),
                revision=revision,
                display_name=str(record["display_name"]),
                captured_nickname=str(record["captured_nickname"]),
                douyin_id=str(record["douyin_id"]),
                nickname_blank=bool(record["nickname_blank"]),
                formal_status=status,
                message_code="FORMAL_EXISTS",
                next_a_number=None,
                existing_a_number=inspection.existing_a_number,
                mark=None,
                recovery_phase=(str(recovery["phase"]) if recovery else None),
            )
        if formal_preview is None:
            raise WatchlistPromotionError("FORMAL_PREVIEW_FAILED", "无法生成正式转正预览。")
        status = formal_preview.status
        if recovery is not None:
            status = "retry_required"
        return PromotionPreview(
            w_id=int(record["w_id"]),
            revision=revision,
            display_name=str(record["display_name"]),
            captured_nickname=str(record["captured_nickname"]),
            douyin_id=str(record["douyin_id"]),
            nickname_blank=bool(record["nickname_blank"]),
            formal_status=status,
            message_code=formal_preview.message_code,
            next_a_number=formal_preview.next_a_number,
            existing_a_number=formal_preview.a_number,
            mark=formal_preview.mark,
            recovery_phase=(str(recovery["phase"]) if recovery else None),
        )

    @staticmethod
    def _validate_record_url(record: Mapping[str, Any]) -> None:
        try:
            normalized = strict_admit_url(record["url"])
        except (ProfileUrlError, TypeError) as exc:
            raise WatchlistPromotionError(
                "INVALID",
                "观察记录主页 URL 无法通过转正安全校验。",
            ) from exc
        if normalized != record["url"]:
            raise WatchlistPromotionError("INVALID", "观察记录主页 URL 未规范化。")

    @staticmethod
    def _raise_inconsistent(inspection: FormalInspection) -> None:
        raise WatchlistPromotionError(
            "FORMAL_INCONSISTENT",
            "正式 JSON 与 Excel 的目标记录不一致，已阻止转正。",
            details={
                "json_match_count": len(inspection.json_matches),
                "excel_match_count": len(inspection.excel_matches),
            },
        )

    @staticmethod
    def _raise_retry_required() -> None:
        raise WatchlistPromotionError(
            "RECOVERY_REQUIRED",
            "此前转正尚未完成；请先确认正式数据状态，再明确重试。",
        )

    @staticmethod
    def _enter_critical(context: OperationContext | None) -> None:
        if context is not None:
            context.enter_critical_phase()


__all__ = [
    "PromotionPreview",
    "PromotionResult",
    "WatchlistPromotionError",
    "WatchlistPromotionService",
]
