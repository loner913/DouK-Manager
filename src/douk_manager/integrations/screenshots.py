from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from douk_manager.vendor import screenshot_organizer as organizer

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


@dataclass(frozen=True)
class ScreenshotPreview:
    recognized_folders: int
    recognized_images: int
    movable: int
    missing_account_folder: int
    already_existing: int
    unmatched_folders: int


@dataclass(frozen=True)
class ScreenshotResult:
    moved: int
    messages: tuple[str, ...]
    preview: ScreenshotPreview


class ScreenshotService:
    def preview(
        self,
        inbox: Path,
        account_root: Path,
        *,
        context: OperationContext | None = None,
    ) -> ScreenshotPreview:
        if context is not None:
            context.raise_if_cancelled()
        scan = organizer.scan_all(inbox, account_root)
        if context is not None:
            context.raise_if_cancelled()
        plans, missing, existing = organizer.make_plans(scan)
        return ScreenshotPreview(
            recognized_folders=len(scan.folder_map),
            recognized_images=len(scan.images),
            movable=len(plans),
            missing_account_folder=len(missing),
            already_existing=len(existing),
            unmatched_folders=scan.unmatched_folder_count,
        )

    def execute(
        self,
        inbox: Path,
        account_root: Path,
        *,
        context: OperationContext | None = None,
    ) -> ScreenshotResult:
        if context is not None:
            context.raise_if_cancelled()
        first_scan = organizer.scan_all(inbox, account_root)
        first_plans, first_missing, first_existing = organizer.make_plans(first_scan)
        preview = ScreenshotPreview(
            recognized_folders=len(first_scan.folder_map),
            recognized_images=len(first_scan.images),
            movable=len(first_plans),
            missing_account_folder=len(first_missing),
            already_existing=len(first_existing),
            unmatched_folders=first_scan.unmatched_folder_count,
        )
        second_scan = organizer.scan_all(inbox, account_root)
        second_plans, second_missing, second_existing = organizer.make_plans(second_scan)
        if (
            organizer.scan_signature(first_scan) != organizer.scan_signature(second_scan)
            or tuple((p.image.path.name, p.destination_dir.name) for p in first_plans)
            != tuple((p.image.path.name, p.destination_dir.name) for p in second_plans)
            or tuple(i.path.name for i in first_missing)
            != tuple(i.path.name for i in second_missing)
            or tuple(i.path.name for i in first_existing)
            != tuple(i.path.name for i in second_existing)
        ):
            raise organizer.OrganizerError(
                "两次预检期间截图或账号文件夹发生变化，本次没有移动图片。"
            )
        if context is not None:
            context.raise_if_cancelled()
        messages: list[str] = []
        for index, plan in enumerate(second_plans):
            if context is not None and index == 0:
                context.enter_critical_phase()
            messages.append(organizer.safe_move(plan))
        return ScreenshotResult(len(messages), tuple(messages), preview)

