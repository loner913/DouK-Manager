#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DouK account collector v2.4.9 - strict JSON + Excel consistency mode.

Automatic account screenshots:
- A successful JSON + Excel add triggers one foreground Chrome screenshot.
- The Chrome tab strip and bookmarks bar are excluded.
- The address bar is joined directly to the page; the collector panel remains.
- The image is saved as 账号页面截图/A编号.jpg and is never overwritten.
- Screenshot failure never rolls back a successful JSON + Excel add.

Key v2.4.7 changes:
- Confirmed blank nicknames are allowed and use the Douyin ID as the mark suffix.
- Missing nickname data is still blocked; the browser must explicitly confirm a blank title node.

Key v2.4.6 changes:
- Three mutually exclusive account categories are stored in UTF-8 text files.
- Category ranges are normalized; only runs of 3+ numbers use ``start-end``.
- A timestamped snapshot of all category files is made after the port is acquired.

JSON + Excel backup policy:
- Every successful add overwrites settings_master.json.bak and 录制名单.xlsx.bak.
- Every 20 successful adds creates one timestamped JSON + Excel history pair.
- Timestamped history backups are never deleted automatically.

Key v2.4.1 change:
- New URLs written to Excel column D are plain text only.
- The collector never creates a new Excel hyperlink.
- Existing historical hyperlinks are preserved and are not removed automatically.

The service listens on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
import unicodedata
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from douk_manager.core.locks import LockBusyError, critical_section

try:
    from openpyxl import load_workbook
except ImportError as exc:  # pragma: no cover - handled by launcher too
    print("[ERROR] Missing dependency: openpyxl")
    print("Run: python -m pip install openpyxl")
    raise SystemExit(1) from exc

try:
    from PIL import Image, ImageGrab
except ImportError as exc:  # pragma: no cover - handled by launcher too
    print("[ERROR] Missing dependency: Pillow")
    print("Run: python -m pip install Pillow")
    raise SystemExit(1) from exc


VERSION = "2.4.9"
HOST = "127.0.0.1"
PORT = int(os.environ.get("DOUK_COLLECTOR_PORT", "8765"))
ACCESS_TOKEN = os.environ.get(
    "DOUK_COLLECTOR_TOKEN", "DOUK_COLLECTOR_V248_20260719"
)

BASE_DIR = Path(
    os.environ.get("DOUK_COLLECTOR_DATA_DIR", str(Path(__file__).resolve().parent))
).resolve()
SETTINGS_PATH = Path(
    os.environ.get("DOUK_MASTER_PATH", str(BASE_DIR / "settings_master.json"))
).resolve()
EXCEL_PATH = Path(
    os.environ.get("DOUK_COLLECTOR_EXCEL", str(BASE_DIR / "录制名单.xlsx"))
).resolve()
TRANSACTION_PATH = BASE_DIR / ".douk_collector_transaction.json"
BACKUP_STATE_PATH = BASE_DIR / ".douk_backup_state.json"
GLOBAL_LOCK_PATH = Path(
    os.environ.get("DOUK_GLOBAL_LOCK_PATH", str(BASE_DIR / ".douk_manager.lock"))
).resolve()

SETTINGS_SIMPLE_BACKUP_PATH = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".bak")
EXCEL_SIMPLE_BACKUP_PATH = EXCEL_PATH.with_name(EXCEL_PATH.name + ".bak")
SETTINGS_BACKUP_DIR = BASE_DIR / "settings_backups"
EXCEL_BACKUP_DIR = BASE_DIR / "excel_backups"
HISTORY_BACKUP_INTERVAL = 20
BACKUP_STATE_SCHEMA = 1

CATEGORY_NAMES = ("顶级", "次顶级", "普通")
CATEGORY_PATHS = {name: BASE_DIR / f"{name}.txt" for name in CATEGORY_NAMES}
CATEGORY_BACKUP_DIR = BASE_DIR / "category_backups"
CATEGORY_ITEM_RE = re.compile(r"^(\d+)(?:-(\d+))?$")
CATEGORY_STARTUP_ERROR: "CollectorError | None" = None

SCREENSHOT_DIR = Path(
    os.environ.get("DOUK_SCREENSHOT_DIR", str(BASE_DIR / "账号页面截图"))
).resolve()
# Chrome at 100% UI scale: remove the tab strip while retaining the address bar.
# After capture, remove the bookmarks bar between the address bar and page, then
# join those two retained regions. These calibrated values are DPI-scaled at runtime.
CHROME_TAB_STRIP_DIP = 42
CHROME_ADDRESS_BAR_DIP = 48
CHROME_BOOKMARKS_BAR_DIP = 28
SCREENSHOT_JPEG_QUALITY = 92

ACCOUNTS_KEY = "accounts_urls"
EXCEL_SHEET_NAME = "Sheet1"
EXCEL_FIRST_ACCOUNT_ROW = 2  # A1 is a separate manual row; A1 account is stored on row 2.
EXCEL_LAST_TEMPLATE_ROW = 5001

# This expression is retained only for recognizing the shape of a supplied
# mark.  The authoritative number is always derived from the JSON position;
# never use a greedy digit match to infer a number from ``A987711...``.
A_MARK_RE = re.compile(r"^A(\d+)(.*)$", re.IGNORECASE)
INVALID_FILENAME_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")

FILE_LOCK = threading.RLock()
SCREENSHOT_LOCK = threading.Lock()


class CollectorError(Exception):
    """Expected validation or write error returned to the browser panel."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        stage: str = "precheck",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.stage = stage

    def response(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "details": self.details,
            "version": VERSION,
        }


def foreground_chrome_capture_box() -> tuple[tuple[int, int, int, int], dict[str, Any]]:
    """Return the visible Chrome area below its tab strip in physical pixels."""
    if os.name != "nt":
        raise CollectorError(
            "SCREENSHOT_WINDOWS_ONLY",
            "账号页面截图仅支持 Windows。",
            stage="screenshot",
        )

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise CollectorError(
            "SCREENSHOT_NO_FOREGROUND_WINDOW",
            "没有检测到前台窗口。请先把目标 Chrome 抖音主页切到最前面。",
            stage="screenshot",
        )
    if user32.IsIconic(hwnd):
        raise CollectorError(
            "SCREENSHOT_WINDOW_MINIMIZED",
            "Chrome 当前已最小化，无法截取可见页面。",
            stage="screenshot",
        )

    class_buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
    window_class = class_buffer.value

    title_length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(max(title_length + 1, 2))
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
    window_title = title_buffer.value

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    process_name = ""
    process_handle = kernel32.OpenProcess(0x1000, False, process_id.value)
    if process_handle:
        try:
            path_buffer = ctypes.create_unicode_buffer(32768)
            path_size = wintypes.DWORD(len(path_buffer))
            query_name = kernel32.QueryFullProcessImageNameW
            query_name.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            query_name.restype = wintypes.BOOL
            if query_name(process_handle, 0, path_buffer, ctypes.byref(path_size)):
                process_name = Path(path_buffer.value).name.lower()
        finally:
            kernel32.CloseHandle(process_handle)

    if window_class != "Chrome_WidgetWin_1" or process_name != "chrome.exe":
        shown_name = process_name or "未知程序"
        raise CollectorError(
            "SCREENSHOT_CHROME_NOT_FOREGROUND",
            "当前前台窗口不是 Chrome。账号已添加，但本次截图未保存。",
            details={"foreground_process": shown_name, "window_title": window_title or "（无标题）"},
            stage="screenshot",
        )

    client_rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client_rect)):
        raise CollectorError(
            "SCREENSHOT_WINDOW_RECT_FAILED",
            "无法取得 Chrome 窗口范围。",
            stage="screenshot",
        )
    top_left = wintypes.POINT(client_rect.left, client_rect.top)
    bottom_right = wintypes.POINT(client_rect.right, client_rect.bottom)
    if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)) or not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
        raise CollectorError(
            "SCREENSHOT_WINDOW_RECT_FAILED",
            "无法换算 Chrome 窗口的屏幕坐标。",
            stage="screenshot",
        )

    dpi = 96
    get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
    if get_dpi_for_window is not None:
        get_dpi_for_window.argtypes = [wintypes.HWND]
        get_dpi_for_window.restype = wintypes.UINT
        dpi = get_dpi_for_window(hwnd) or 96
    tab_strip_pixels = max(1, round(CHROME_TAB_STRIP_DIP * dpi / 96))

    box = (
        int(top_left.x),
        int(top_left.y + tab_strip_pixels),
        int(bottom_right.x),
        int(bottom_right.y),
    )
    width = box[2] - box[0]
    height = box[3] - box[1]
    if width < 640 or height < 360:
        raise CollectorError(
            "SCREENSHOT_WINDOW_TOO_SMALL",
            "Chrome 窗口过小，无法可靠校准截图范围。请先最大化窗口。",
            details={"capture_width": width, "capture_height": height},
            stage="screenshot",
        )
    return box, {
        "window_title": window_title,
        "window_class": window_class,
        "process_name": process_name,
        "dpi": dpi,
        "tab_strip_crop_px": tab_strip_pixels,
    }


def remove_chrome_bookmarks_bar(image: Image.Image, dpi: int) -> tuple[Image.Image, dict[str, int]]:
    """Join the retained address-bar and page regions around Chrome's bookmarks bar."""
    address_bar_pixels = max(1, round(CHROME_ADDRESS_BAR_DIP * dpi / 96))
    bookmarks_bar_pixels = max(1, round(CHROME_BOOKMARKS_BAR_DIP * dpi / 96))
    page_start_y = address_bar_pixels + bookmarks_bar_pixels
    if page_start_y >= image.height - 360:
        raise CollectorError(
            "SCREENSHOT_BOOKMARKS_CROP_INVALID",
            "Chrome 可用网页区域过小，无法安全扣除书签栏。请先最大化窗口。",
            details={
                "image_height": image.height,
                "address_bar_keep_px": address_bar_pixels,
                "bookmarks_bar_remove_px": bookmarks_bar_pixels,
            },
            stage="screenshot",
        )

    # A single rectangle cannot retain both the address bar and the page while
    # omitting the bookmarks bar between them, so join both retained regions.
    address_bar_image = image.crop((0, 0, image.width, address_bar_pixels))
    page_image = image.crop((0, page_start_y, image.width, image.height))
    joined_image = Image.new(
        "RGB",
        (image.width, address_bar_image.height + page_image.height),
    )
    joined_image.paste(address_bar_image, (0, 0))
    joined_image.paste(page_image, (0, address_bar_image.height))
    return joined_image, {
        "address_bar_keep_px": address_bar_pixels,
        "bookmarks_bar_remove_px": bookmarks_bar_pixels,
    }


def screenshot_target_from_payload(payload: dict[str, Any]) -> tuple[int, str, str, Path]:
    """Resolve the screenshot number from JSON position and verify JSON+Excel.

    ``number_text`` is optional and is treated only as a consistency claim from
    the browser.  It is never used to discover the number, which prevents a
    numeric suffix in a mark from being mistaken for part of the A number.
    """
    expected_url = clean_profile_url(text(payload.get("expected_url")))
    current_url = clean_profile_url(text(payload.get("current_url")))
    if current_url != expected_url:
        raise CollectorError(
            "SCREENSHOT_PAGE_CHANGED",
            "账号已添加，但截图前页面已经切换，本次截图未保存。",
            details={"added_url": expected_url, "current_url": current_url},
            stage="screenshot",
        )

    with FILE_LOCK:
        document = read_json_document()
        accounts = document.get(ACCOUNTS_KEY)
        if not isinstance(accounts, list):
            raise CollectorError(
                "SCREENSHOT_ACCOUNT_NOT_FOUND",
                "账号已添加，但 settings_master.json 账号数组无效，本次截图未保存。",
                stage="screenshot",
            )
        matches = [
            (index, item)
            for index, item in enumerate(accounts)
            if isinstance(item, dict)
            and normalize_url_for_compare(text(item.get("url"))) == expected_url
        ]
        if len(matches) != 1:
            raise CollectorError(
                "SCREENSHOT_ACCOUNT_NOT_FOUND" if not matches else "SCREENSHOT_ACCOUNT_AMBIGUOUS",
                "当前 URL 在 settings_master.json 中没有且只能有一条对应记录，本次截图未保存。",
                details={"expected_url": expected_url, "match_count": len(matches)},
                stage="screenshot",
            )

        index, record = matches[0]
        number = index + 1
        number_text = f"A{number}"
        record_mark = text(record.get("mark"))
        record_url = clean_profile_url(text(record.get("url")))
        prefix = number_text
        if (
            not record_mark[: len(prefix)].casefold() == prefix.casefold()
            or not record_mark[len(prefix) :]
            or record_url != expected_url
        ):
            raise CollectorError(
                "SCREENSHOT_ACCOUNT_MISMATCH",
                "账号已添加，但 JSON 位置、mark 编号或 URL 不一致，本次截图未保存。",
                details={
                    "number_text": number_text,
                    "record_mark": record_mark or "（空）",
                    "expected_url": expected_url,
                    "record_url": record_url or "（空）",
                },
                stage="screenshot",
            )

        # The portion after the authoritative A number is the mark body.  A
        # single separator space is allowed in JSON, while Excel stores body
        # text without that separator.
        mark_body = record_mark[len(prefix) :]
        expected_excel_body = mark_body[1:] if mark_body.startswith(" ") else mark_body
        workbook = load_excel()
        try:
            worksheet = get_worksheet(workbook)
            excel_row = number + 1
            excel_body = text(worksheet[f"B{excel_row}"].value)
            excel_url = clean_profile_url(text(worksheet[f"D{excel_row}"].value))
            if excel_body != expected_excel_body or excel_url != expected_url:
                raise CollectorError(
                    "SCREENSHOT_JSON_EXCEL_MISMATCH",
                    "JSON、Excel、URL 或编号映射不一致，本次截图未保存。",
                    details={
                        "number_text": number_text,
                        "json_mark": record_mark,
                        "expected_excel_body": expected_excel_body,
                        "excel_b": excel_body or "（空）",
                        "expected_url": expected_url,
                        "excel_url": excel_url,
                        "excel_row": excel_row,
                    },
                    stage="screenshot",
                )
        finally:
            workbook.close()

        claimed_number = text(payload.get("number_text")).upper()
        if claimed_number and claimed_number != number_text:
            raise CollectorError(
                "SCREENSHOT_NUMBER_MISMATCH",
                "浏览器提供的编号与 JSON 实际位置不一致，本次截图未保存。",
                details={"claimed_number": claimed_number, "actual_number": number_text},
                stage="screenshot",
            )

    return number, number_text, expected_url, SCREENSHOT_DIR / f"{number_text}.jpg"


def install_screenshot_without_overwrite(temp_path: Path, target_path: Path) -> None:
    """Publish a verified image while preserving any existing numbered image."""
    try:
        # Same-directory hard-link creation is atomic on NTFS and fails if the
        # numbered target already exists.
        os.link(temp_path, target_path)
        return
    except FileExistsError:
        raise
    except OSError:
        # Some removable/non-NTFS filesystems do not support hard links. An
        # exclusive create keeps the no-overwrite guarantee on those drives.
        descriptor: int | None = None
        target_created = False
        try:
            descriptor = os.open(
                target_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
            target_created = True
            with temp_path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
                descriptor = None
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            if target_created:
                target_path.unlink(missing_ok=True)
            raise


def capture_account_screenshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Create one numbered screenshot without ever replacing an existing image."""
    if not SCREENSHOT_LOCK.acquire(blocking=False):
        raise CollectorError(
            "SCREENSHOT_BUSY",
            "另一项截图操作尚未完成，请稍后再试。",
            stage="screenshot",
        )

    temp_path: Path | None = None
    try:
        number, number_text, expected_url, target_path = screenshot_target_from_payload(payload)
        if target_path.exists():
            raise CollectorError(
                "SCREENSHOT_ALREADY_EXISTS",
                f"账号已添加，但 {number_text}.jpg 已存在，本次未覆盖。",
                details={"relative_path": f"{SCREENSHOT_DIR.name}\\{target_path.name}"},
                stage="screenshot",
            )

        box, window_info = foreground_chrome_capture_box()
        try:
            image = ImageGrab.grab(bbox=box, all_screens=True)
        except TypeError:  # Older Pillow versions on Windows.
            image = ImageGrab.grab(bbox=box)
        except OSError as exc:
            raise CollectorError(
                "SCREENSHOT_CAPTURE_FAILED",
                "Windows 未能截取 Chrome 画面。请确认浏览器未最小化且没有被其他窗口遮挡。",
                details={"error": str(exc)},
                stage="screenshot",
            ) from exc

        if image.mode != "RGB":
            image = image.convert("RGB")
        if image.width != box[2] - box[0] or image.height != box[3] - box[1]:
            raise CollectorError(
                "SCREENSHOT_SIZE_MISMATCH",
                "实际截图尺寸与 Chrome 窗口范围不一致，本次没有保存。",
                details={
                    "expected": f"{box[2] - box[0]}x{box[3] - box[1]}",
                    "actual": f"{image.width}x{image.height}",
                },
                stage="screenshot",
            )

        image, crop_info = remove_chrome_bookmarks_bar(image, int(window_info["dpi"]))

        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{number_text}.", suffix=".tmp.jpg", dir=SCREENSHOT_DIR
        )
        os.close(descriptor)
        temp_path = Path(temp_name)
        image.save(
            temp_path,
            format="JPEG",
            quality=SCREENSHOT_JPEG_QUALITY,
            optimize=True,
            subsampling=0,
        )
        with Image.open(temp_path) as verification_image:
            verification_image.verify()

        try:
            install_screenshot_without_overwrite(temp_path, target_path)
        except FileExistsError as exc:
            raise CollectorError(
                "SCREENSHOT_ALREADY_EXISTS",
                f"账号已添加，但 {number_text}.jpg 已存在，本次未覆盖。",
                details={"relative_path": f"{SCREENSHOT_DIR.name}\\{target_path.name}"},
                stage="screenshot",
            ) from exc
        temp_path.unlink()
        temp_path = None

        relative_path = f"{SCREENSHOT_DIR.name}\\{target_path.name}"
        print(
            f"[SCREENSHOT] {number_text} | {relative_path} | "
            f"{image.width}x{image.height} | "
            f"crop tab strip {window_info['tab_strip_crop_px']}px | "
            f"remove bookmarks bar {crop_info['bookmarks_bar_remove_px']}px"
        )
        return {
            "ok": True,
            "code": "SCREENSHOT_SAVED",
            "message": f"账号截图已保存：{number_text}.jpg",
            "data": {
                "number": number,
                "number_text": number_text,
                "url": expected_url,
                "relative_path": relative_path,
                "width": image.width,
                "height": image.height,
                "tab_strip_crop_px": window_info["tab_strip_crop_px"],
                "address_bar_keep_px": crop_info["address_bar_keep_px"],
                "bookmarks_bar_remove_px": crop_info["bookmarks_bar_remove_px"],
            },
            "version": VERSION,
        }
    except PermissionError as exc:
        raise CollectorError(
            "SCREENSHOT_SAVE_DENIED",
            "账号已添加，但没有权限保存截图，本次截图未保存。",
            details={"error": str(exc)},
            stage="screenshot",
        ) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        SCREENSHOT_LOCK.release()


def check_account_screenshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the exact JSON+Excel screenshot preflight without touching the screen."""
    number, number_text, expected_url, target_path = screenshot_target_from_payload(payload)
    if target_path.exists():
        raise CollectorError(
            "SCREENSHOT_ALREADY_EXISTS",
            f"{number_text}.jpg 已存在，本次不能覆盖。",
            details={"relative_path": f"{SCREENSHOT_DIR.name}\\{target_path.name}"},
            stage="screenshot",
        )
    return {
        "ok": True,
        "code": "SCREENSHOT_READY",
        "message": f"已通过截图核验：{number_text}.jpg 可以补截。",
        "data": {
            "number": number,
            "number_text": number_text,
            "url": expected_url,
            "relative_path": f"{SCREENSHOT_DIR.name}\\{target_path.name}",
        },
        "version": VERSION,
    }


@dataclass(frozen=True)
class JsonState:
    document: dict[str, Any]
    accounts: list[dict[str, Any]]
    numbers: tuple[int, ...]
    max_number: int
    target_index: int


@dataclass(frozen=True)
class ExcelState:
    sheet_name: str
    numbers: tuple[int, ...]
    max_number: int
    target_row: int
    merge_count: int
    conditional_format_count: int
    a_formula_count: int


@dataclass(frozen=True)
class StrictState:
    json_state: JsonState
    excel_state: ExcelState
    next_number: int
    target_json_index: int
    target_excel_row: int


@dataclass(frozen=True)
class BackupState:
    successful_adds_since_history: int
    last_successful_number: int


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def clean_filename_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", text(value))
    value = INVALID_FILENAME_CHARS_RE.sub("_", value)
    # Keep ASCII full stops everywhere, including at either end.  Only actual
    # surrounding whitespace is removed; this is important for marks such as
    # ``A987 7113331313.`` and ``.nickname.``.
    value = WHITESPACE_RE.sub(" ", value).strip()
    return value


def normalize_identity(value: str) -> str:
    value = unicodedata.normalize("NFKC", text(value)).casefold()
    return WHITESPACE_RE.sub("", value)


def strip_mark_number(mark: str, number: int | None = None) -> str:
    """Return mark body using a known position whenever one is available."""
    value = clean_filename_text(mark)
    if number is not None:
        prefix = f"A{number}"
        if value[: len(prefix)].casefold() == prefix.casefold() and value[len(prefix) :]:
            return value[len(prefix) :].lstrip()
        return value
    # Fallback is intentionally non-greedy only for malformed legacy data;
    # strict JSON validation below never uses this path to accept a record.
    match = A_MARK_RE.match(value)
    return match.group(2).lstrip() if match else value


def profile_fields(payload: dict[str, Any]) -> tuple[str, str, str, bool]:
    """Validate browser profile fields and return the raw optional mark text."""
    nickname = clean_filename_text(text(payload.get("nickname")))
    douyin_id = clean_filename_text(text(payload.get("douyin_id")))
    nickname_blank = payload.get("nickname_blank") is True
    if nickname and nickname_blank:
        raise CollectorError(
            "NICKNAME_STATE_INVALID",
            "昵称内容与空白昵称标记冲突，已暂停写入。",
        )
    if not nickname and not nickname_blank:
        raise CollectorError("NICKNAME_MISSING", "未识别到当前主页昵称，已暂停写入。")
    if not douyin_id:
        raise CollectorError("DOUYIN_ID_MISSING", "未识别到当前主页抖音号，已暂停写入。")
    requested_mark = clean_filename_text(text(payload.get("mark")))
    return nickname, douyin_id, requested_mark, nickname_blank


def suffix_from_mark(requested_mark: str, number: int, identity: str) -> str:
    """Resolve editable mark content after the authoritative A number.

    The one separator space after ``A987`` is meaningful and retained in the
    JSON mark.  Excel receives the body without that separator.
    """
    if not requested_mark:
        return identity
    prefix = f"A{number}"
    if requested_mark[: len(prefix)].casefold() != prefix.casefold():
        raise CollectorError(
            "MARK_NUMBER_MISMATCH",
            f"手动 mark 的编号不是当前真实编号 A{number}，请先点击“重新识别”后再添加。",
            details={"requested_mark": requested_mark, "actual_number": prefix},
        )
    remainder = requested_mark[len(prefix) :]
    if not remainder.strip():
        raise CollectorError(
            "MARK_BODY_MISSING",
            "mark 中缺少编号后的正文，已暂停写入。",
            details={"number_text": prefix},
        )
    has_separator = bool(remainder) and remainder[0].isspace()
    body = clean_filename_text(remainder)
    if not body:
        raise CollectorError("MARK_BODY_MISSING", "mark 中缺少编号后的正文，已暂停写入。")
    return f" {body}" if has_separator else body


def excel_body_from_suffix(suffix: str) -> str:
    """Excel B stores mark body without the optional A-number separator."""
    return suffix[1:] if suffix.startswith(" ") else suffix


def clean_profile_url(raw_url: str) -> str:
    raw_url = text(raw_url)
    if not raw_url:
        raise CollectorError("INVALID_URL", "当前主页 URL 为空，已暂停写入。")

    try:
        parsed = urlsplit(raw_url)
    except ValueError as exc:
        raise CollectorError(
            "INVALID_URL", "当前主页 URL 无法解析，已暂停写入。", details={"url": raw_url}
        ) from exc

    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path.rstrip("/")
    if host not in {"www.douyin.com", "douyin.com"} or not path.startswith("/user/"):
        raise CollectorError(
            "INVALID_URL",
            "当前地址不是有效的抖音账号主页，已暂停写入。",
            details={"url": raw_url},
        )

    # Keep only scheme + host + pathname. Query parameters and fragments are discarded.
    return urlunsplit(("https", "www.douyin.com", path, "", ""))


def normalize_url_for_compare(raw_url: str) -> str:
    try:
        return clean_profile_url(raw_url)
    except CollectorError:
        # Existing malformed URLs still participate in duplicate comparison as trimmed text.
        return text(raw_url).split("?", 1)[0].split("#", 1)[0].rstrip("/")


def format_category_numbers(numbers: Iterable[int]) -> str:
    """Return sorted comma text; only runs of three or more use a range."""
    ordered = sorted(set(numbers))
    parts: list[str] = []
    start_index = 0
    while start_index < len(ordered):
        end_index = start_index
        while (
            end_index + 1 < len(ordered)
            and ordered[end_index + 1] == ordered[end_index] + 1
        ):
            end_index += 1

        run = ordered[start_index : end_index + 1]
        if len(run) >= 3:
            parts.append(f"{run[0]}-{run[-1]}")
        else:
            parts.extend(str(number) for number in run)
        start_index = end_index + 1
    return ",".join(parts)


def parse_category_text(raw: str, category: str, path: Path) -> set[int]:
    raw = raw.lstrip("\ufeff")
    nonempty_lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(nonempty_lines) > 1:
        raise CollectorError(
            "CATEGORY_FILE_MULTILINE",
            f"分类文件“{category}”必须只使用一行，已禁止分类修改。",
            details={"path": str(path)},
        )

    content = nonempty_lines[0] if nonempty_lines else ""
    if not content:
        return set()

    numbers: set[int] = set()
    for raw_item in content.split(","):
        item = raw_item.strip()
        match = CATEGORY_ITEM_RE.fullmatch(item)
        if not match:
            raise CollectorError(
                "CATEGORY_FILE_INVALID",
                f"分类文件“{category}”格式错误，已禁止分类修改。",
                details={"path": str(path), "invalid_item": item or "（空项）"},
            )

        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > EXCEL_LAST_TEMPLATE_ROW - 1:
            raise CollectorError(
                "CATEGORY_NUMBER_INVALID",
                f"分类文件“{category}”存在无效编号或范围，已禁止分类修改。",
                details={"path": str(path), "invalid_item": item},
            )

        expanded = set(range(start, end + 1))
        overlap = numbers.intersection(expanded)
        if overlap:
            first = min(overlap)
            raise CollectorError(
                "CATEGORY_NUMBER_DUPLICATE",
                f"分类文件“{category}”内编号 {first} 重复，已禁止分类修改。",
                details={"path": str(path), "number": first},
            )
        numbers.update(expanded)

    canonical = format_category_numbers(numbers)
    if content != canonical:
        raise CollectorError(
            "CATEGORY_FILE_NOT_CANONICAL",
            f"分类文件“{category}”未按约定排序或合并，已禁止分类修改。",
            details={"path": str(path), "expected": canonical},
        )
    return numbers


def validate_category_sets(category_sets: dict[str, set[int]]) -> None:
    owners: dict[int, list[str]] = {}
    for category in CATEGORY_NAMES:
        for number in category_sets[category]:
            owners.setdefault(number, []).append(category)

    conflicts = {number: names for number, names in owners.items() if len(names) > 1}
    if conflicts:
        number = min(conflicts)
        names = conflicts[number]
        raise CollectorError(
            "CATEGORY_DATA_CONFLICT",
            f"分类数据冲突：编号 {number} 同时存在于“{'”和“'.join(names)}”。",
            details={
                "number": number,
                "categories": names,
                "conflicts": {str(key): value for key, value in sorted(conflicts.items())},
            },
        )


def clone_collector_error(error: CollectorError) -> CollectorError:
    return CollectorError(
        error.code,
        error.message,
        details=copy.deepcopy(error.details),
        stage=error.stage,
    )


def read_category_sets(*, ignore_startup_error: bool = False) -> dict[str, set[int]]:
    if CATEGORY_STARTUP_ERROR is not None and not ignore_startup_error:
        raise clone_collector_error(CATEGORY_STARTUP_ERROR)

    existing = {name: CATEGORY_PATHS[name].exists() for name in CATEGORY_NAMES}
    if not all(existing.values()):
        missing = [name for name, exists in existing.items() if not exists]
        raise CollectorError(
            "CATEGORY_FILES_MISSING",
            "分类 TXT 文件缺失，本次禁止分类修改；JSON和 Excel 采集不受影响。",
            details={"missing": [f"{name}.txt" for name in missing]},
        )

    category_sets: dict[str, set[int]] = {}
    for category in CATEGORY_NAMES:
        path = CATEGORY_PATHS[category]
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CollectorError(
                "CATEGORY_FILE_READ_FAILED",
                f"无法读取分类文件“{category}”，本次禁止分类修改。",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        category_sets[category] = parse_category_text(raw, category, path)

    validate_category_sets(category_sets)
    return category_sets


def category_state_from_sets(number: int, category_sets: dict[str, set[int]]) -> dict[str, Any]:
    owners = [name for name in CATEGORY_NAMES if number in category_sets[name]]
    if len(owners) > 1:
        joined = "”和“".join(owners)
        return {
            "available": False,
            "status": "conflict",
            "number": number,
            "number_text": f"A{number}",
            "category": "",
            "categories": owners,
            "message": f"分类数据冲突：该编号同时存在于“{joined}”。",
            "guidance": "本次禁止修改，请先检查分类TXT。",
        }
    if owners:
        category = owners[0]
        return {
            "available": True,
            "status": "classified",
            "number": number,
            "number_text": f"A{number}",
            "category": category,
            "categories": owners,
            "message": f"已分类为“{category}”。",
            "guidance": f"如需更改，请先点击“✓ {category}”取消原分类。",
        }
    return {
        "available": True,
        "status": "unclassified",
        "number": number,
        "number_text": f"A{number}",
        "category": "",
        "categories": [],
        "message": "尚未分类。",
        "guidance": "可以使用昵称后面的分类按钮补充记录。",
    }


def unavailable_category_state(error: CollectorError) -> dict[str, Any]:
    return {
        "available": False,
        "status": "unavailable",
        "number": None,
        "number_text": "",
        "category": "",
        "categories": [],
        "message": error.message,
        "guidance": "请检查管理器采集器日志与分类 TXT，修复后重启采集服务。",
        "error_code": error.code,
    }


def safe_category_state(number: int) -> dict[str, Any]:
    try:
        return category_state_from_sets(number, read_category_sets())
    except CollectorError as exc:
        if exc.code == "CATEGORY_DATA_CONFLICT":
            conflicts = exc.details.get("conflicts", {})
            owners = conflicts.get(str(number), []) if isinstance(conflicts, dict) else []
            if owners:
                joined = "”和“".join(owners)
                return {
                    "available": False,
                    "status": "conflict",
                    "number": number,
                    "number_text": f"A{number}",
                    "category": "",
                    "categories": owners,
                    "message": f"分类数据冲突：该编号同时存在于“{joined}”。",
                    "guidance": "本次禁止修改，请先检查分类TXT。",
                }
        state = unavailable_category_state(exc)
        state["number"] = number
        state["number_text"] = f"A{number}"
        return state


def pending_category_state(number: int) -> dict[str, Any]:
    try:
        read_category_sets()
    except CollectorError as exc:
        state = unavailable_category_state(exc)
        state["number"] = number
        state["number_text"] = f"A{number}"
        return state
    return {
        "available": False,
        "status": "pending_add",
        "number": number,
        "number_text": f"A{number}",
        "category": "",
        "categories": [],
        "message": "该账号尚未写入 JSON 和 Excel。",
        "guidance": "请先点击“同时添加 JSON + Excel”，并等待两份文件写入成功后再分类。",
    }


def category_details(state: dict[str, Any]) -> dict[str, Any]:
    status = state.get("status")
    if status == "classified":
        shown_status = text(state.get("category"))
    elif status == "unclassified":
        shown_status = "尚未分类"
    elif status == "conflict":
        shown_status = "冲突"
    else:
        shown_status = "不可用"

    details: dict[str, Any] = {
        "classification_status": shown_status,
        "category_state": state,
    }
    if status == "conflict":
        details["classification_conflict"] = state.get("message")
    details["classification_guidance"] = state.get("guidance")
    return details


def write_category_file(category: str, numbers: set[int]) -> None:
    if category not in CATEGORY_PATHS:
        raise CollectorError("CATEGORY_INVALID", "未知的分类名称，本次未修改 TXT。")

    path = CATEGORY_PATHS[category]
    content = format_category_numbers(numbers)
    temp_path: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())

        verified = parse_category_text(
            temp_path.read_text(encoding="utf-8-sig"), category, temp_path
        )
        if verified != numbers:
            raise CollectorError(
                "CATEGORY_TEMP_VERIFY_FAILED",
                f"分类文件“{category}”临时写入验证失败，原文件未修改。",
                stage="write",
            )

        os.replace(temp_path, path)
        temp_path = None
    except CollectorError:
        raise
    except PermissionError as exc:
        raise CollectorError(
            "CATEGORY_WRITE_PERMISSION_DENIED",
            f"无法写入“{category}.txt”，可能文件正被其他程序占用。",
            details={"path": str(path), "error": str(exc)},
            stage="write",
        ) from exc
    except OSError as exc:
        raise CollectorError(
            "CATEGORY_WRITE_FAILED",
            f"写入“{category}.txt”失败，原文件未修改。",
            details={"path": str(path), "error": str(exc)},
            stage="write",
        ) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def next_category_backup_targets() -> dict[str, Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = ""
    counter = 1
    while True:
        targets = {
            name: CATEGORY_BACKUP_DIR / f"{name}-{stamp}{suffix}.txt"
            for name in CATEGORY_NAMES
        }
        if not any(path.exists() for path in targets.values()):
            return targets
        counter += 1
        suffix = f"-{counter}"


def initialize_category_files_after_bind() -> None:
    """Initialize or snapshot category files only after the server owns the port."""
    global CATEGORY_STARTUP_ERROR
    CATEGORY_STARTUP_ERROR = None

    existing = {name: CATEGORY_PATHS[name].exists() for name in CATEGORY_NAMES}
    existing_count = sum(existing.values())
    if existing_count == 0:
        created: list[Path] = []
        try:
            for category in CATEGORY_NAMES:
                write_category_file(category, set())
                created.append(CATEGORY_PATHS[category])
            read_category_sets(ignore_startup_error=True)
            print("[CATEGORY] Created 顶级.txt / 次顶级.txt / 普通.txt (first run; no empty backup).")
        except CollectorError as exc:
            for path in created:
                path.unlink(missing_ok=True)
            CATEGORY_STARTUP_ERROR = exc
            format_error_for_console(exc)
        return

    if existing_count != len(CATEGORY_NAMES):
        missing = [f"{name}.txt" for name, exists in existing.items() if not exists]
        CATEGORY_STARTUP_ERROR = CollectorError(
            "CATEGORY_FILES_PARTIAL",
            "三份分类 TXT 只存在一部分，为防止误数据已禁止分类功能。",
            details={"missing": missing},
        )
        format_error_for_console(CATEGORY_STARTUP_ERROR)
        return

    created_backups: list[Path] = []
    try:
        CATEGORY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        targets = next_category_backup_targets()
        for category in CATEGORY_NAMES:
            target = targets[category]
            shutil.copy2(CATEGORY_PATHS[category], target)
            created_backups.append(target)
        print(
            f"[CATEGORY BACKUP] Created startup snapshot: "
            f"{created_backups[0].name}, {created_backups[1].name}, {created_backups[2].name}"
        )
    except OSError as exc:
        for path in created_backups:
            path.unlink(missing_ok=True)
        CATEGORY_STARTUP_ERROR = CollectorError(
            "CATEGORY_BACKUP_FAILED",
            "分类 TXT 启动备份失败，为防止无备份修改，本次已禁止分类功能。",
            details={"path": str(CATEGORY_BACKUP_DIR), "error": str(exc)},
        )
        format_error_for_console(CATEGORY_STARTUP_ERROR)
        return

    try:
        read_category_sets(ignore_startup_error=True)
        print("[CATEGORY] Category files validated; mutual-exclusion check passed.")
    except CollectorError as exc:
        CATEGORY_STARTUP_ERROR = exc
        format_error_for_console(exc)


def read_json_document(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    if not path.exists():
        raise CollectorError(
            "SETTINGS_NOT_FOUND",
            "找不到 settings_master.json，已暂停写入。",
            details={"path": str(path)},
        )
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            document = json.load(fh)
    except json.JSONDecodeError as exc:
        raise CollectorError(
            "SETTINGS_JSON_INVALID",
            "settings_master.json 格式错误，已暂停写入。",
            details={"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from exc
    except OSError as exc:
        raise CollectorError(
            "SETTINGS_READ_FAILED",
            "无法读取 settings_master.json，已暂停写入。",
            details={"error": str(exc)},
        ) from exc

    if not isinstance(document, dict):
        raise CollectorError("SETTINGS_ROOT_INVALID", "settings_master.json 顶层必须是对象，已暂停写入。")
    accounts = document.get(ACCOUNTS_KEY)
    if not isinstance(accounts, list):
        raise CollectorError(
            "ACCOUNTS_LIST_INVALID",
            f"settings_master.json 中的 {ACCOUNTS_KEY} 不是数组，已暂停写入。",
        )
    return document


def find_duplicate_account(
    accounts: Iterable[Any], profile_url: str, identity: str
) -> dict[str, Any] | None:
    wanted_url = normalize_url_for_compare(profile_url)
    wanted_identity = normalize_identity(identity)
    identity_match: dict[str, Any] | None = None

    for index, item in enumerate(accounts):
        if not isinstance(item, dict):
            continue
        existing_mark = text(item.get("mark"))
        existing_url = text(item.get("url"))

        if existing_url and normalize_url_for_compare(existing_url) == wanted_url:
            return {
                "code": "DUPLICATE_JSON_URL",
                "message": "该账号与之前记录重复：主页 URL 已存在。本次未写入 JSON，也未检查 Excel。",
                "duplicate_type": "主页 URL 重复",
                "index": index,
                "mark": existing_mark,
                "url": existing_url,
                "item": item,
            }

        if existing_mark and wanted_identity:
            existing_identity = normalize_identity(
                strip_mark_number(existing_mark, index + 1)
            )
            if existing_identity == wanted_identity and identity_match is None:
                identity_match = {
                    "code": "DUPLICATE_JSON_IDENTITY",
                    "message": "该账号与之前记录重复：昵称＋抖音号已存在。本次未写入 JSON，也未检查 Excel。",
                    "duplicate_type": "昵称＋抖音号重复",
                    "index": index,
                    "mark": existing_mark,
                    "url": existing_url,
                    "item": item,
                }
    return identity_match


def category_state_for_existing_match(match: dict[str, Any]) -> dict[str, Any]:
    mark = text(match.get("mark"))
    index = int(match["index"])
    number = index + 1
    prefix = f"A{number}"
    if (
        mark[: len(prefix)].casefold() != prefix.casefold()
        or not mark[len(prefix) :]
    ):
        return unavailable_category_state(
            CollectorError(
                "CATEGORY_EXISTING_MARK_INVALID",
                "已存在记录的 mark 与 JSON 位置不一致，已禁止分类修改。",
                details={
                    "mark": mark or "（空）",
                    "json_index": index,
                    "expected_number": prefix,
                },
            )
        )
    return safe_category_state(number)


def duplicate_error(match: dict[str, Any]) -> CollectorError:
    category_state = category_state_for_existing_match(match)
    details: dict[str, Any] = {
        "duplicate_type": match["duplicate_type"],
        **category_details(category_state),
        "existing_mark": text(match.get("mark")) or "（mark 为空）",
        "existing_url": text(match.get("url")) or "（url 为空）",
        "json_index": int(match["index"]),
        "json_position": f"{ACCOUNTS_KEY}[{int(match['index'])}]",
    }
    return CollectorError(match["code"], match["message"], details=details)


def duplicate_check_json(
    accounts: Iterable[Any], profile_url: str, identity: str
) -> None:
    """JSON duplicate check has the highest priority and does not touch Excel."""
    match = find_duplicate_account(accounts, profile_url, identity)
    if match is not None:
        raise duplicate_error(match)


def validate_json_structure(document: dict[str, Any]) -> JsonState:
    accounts_raw = document[ACCOUNTS_KEY]
    numbers: list[int] = []
    seen_numbers: dict[int, int] = {}

    for index, item in enumerate(accounts_raw):
        if not isinstance(item, dict):
            raise CollectorError(
                "JSON_RECORD_INVALID",
                "settings_master.json 中存在不是对象的账号记录，已暂停写入。",
                details={"json_index": index, "value_type": type(item).__name__},
            )

        mark = text(item.get("mark"))
        url = text(item.get("url"))

        if bool(mark) != bool(url):
            raise CollectorError(
                "JSON_HALF_RECORD",
                "settings_master.json 存在 mark/url 半条记录，已暂停写入，请手动处理。",
                details={
                    "json_index": index,
                    "json_position": f"{ACCOUNTS_KEY}[{index}]",
                    "mark": mark or "（空）",
                    "url": url or "（空）",
                },
            )

        if not mark and not url:
            continue

        expected_number = index + 1
        expected_prefix = f"A{expected_number}"
        if (
            mark[: len(expected_prefix)].casefold() != expected_prefix.casefold()
            or not mark[len(expected_prefix) :]
        ):
            raise CollectorError(
                "JSON_MARK_INVALID",
                "settings_master.json 中的 mark 必须以该数组位置对应的 A 编号开头，且必须有正文。",
                details={
                    "json_index": index,
                    "expected_number": expected_prefix,
                    "mark": mark,
                    "url": url,
                },
            )

        # The array position, not a greedy digit regex, is authoritative.
        number = expected_number
        if number < 1:
            raise CollectorError(
                "JSON_NUMBER_INVALID",
                "settings_master.json 中存在小于 A1 的编号，已暂停写入。",
                details={"json_index": index, "mark": mark},
            )
        if number in seen_numbers:
            raise CollectorError(
                "JSON_NUMBER_DUPLICATE",
                "settings_master.json 中存在重复 A 编号，已暂停写入。",
                details={
                    "number": f"A{number}",
                    "first_index": seen_numbers[number],
                    "second_index": index,
                },
            )

        seen_numbers[number] = index
        numbers.append(number)

    numbers.sort()
    max_number = numbers[-1] if numbers else 0
    expected = list(range(1, max_number + 1))
    if numbers != expected:
        missing = [n for n in expected if n not in seen_numbers]
        raise CollectorError(
            "JSON_NUMBER_GAP",
            "settings_master.json 的 A 编号存在跳号，已暂停写入。",
            details={
                "max_number": f"A{max_number}",
                "missing_numbers": [f"A{n}" for n in missing[:50]],
                "missing_count": len(missing),
            },
        )

    target_index = max_number
    if target_index < len(accounts_raw):
        target = accounts_raw[target_index]
        target_mark = text(target.get("mark"))
        target_url = text(target.get("url"))
        if target_mark or target_url:
            raise CollectorError(
                "JSON_TARGET_OCCUPIED",
                f"JSON 目标位置 A{max_number + 1} 已被占用或残留，已暂停写入。",
                details={
                    "target_number": f"A{max_number + 1}",
                    "json_index": target_index,
                    "mark": target_mark or "（空）",
                    "url": target_url or "（空）",
                },
            )
    elif target_index > len(accounts_raw):
        # This should be impossible after strict position validation, but keep it defensive.
        raise CollectorError(
            "JSON_TARGET_MISSING",
            "JSON 目标位置不存在且无法安全追加，已暂停写入。",
            details={"target_index": target_index, "array_length": len(accounts_raw)},
        )

    return JsonState(
        document=document,
        accounts=accounts_raw,
        numbers=tuple(numbers),
        max_number=max_number,
        target_index=target_index,
    )


def get_worksheet(workbook: Any) -> Any:
    if EXCEL_SHEET_NAME in workbook.sheetnames:
        return workbook[EXCEL_SHEET_NAME]
    raise CollectorError(
        "EXCEL_SHEET_NOT_FOUND",
        f"Excel 中找不到必需工作表 {EXCEL_SHEET_NAME}，已暂停写入。",
        details={"available_sheets": workbook.sheetnames},
    )


def hyperlink_target(cell: Any) -> str:
    link = cell.hyperlink
    if link is None:
        return ""
    return text(getattr(link, "target", "") or getattr(link, "location", ""))


def load_excel(path: Path = EXCEL_PATH) -> Any:
    if not path.exists():
        raise CollectorError(
            "EXCEL_NOT_FOUND",
            "找不到 录制名单.xlsx，已暂停写入。",
            details={"path": str(path)},
        )
    try:
        return load_workbook(path, data_only=False, read_only=False, keep_links=True)
    except PermissionError as exc:
        raise CollectorError(
            "EXCEL_LOCKED",
            "录制名单.xlsx 正在被 Excel/WPS 占用，请关闭后重试。",
            details={"path": str(path)},
        ) from exc
    except Exception as exc:
        raise CollectorError(
            "EXCEL_OPEN_FAILED",
            "无法打开 录制名单.xlsx，已暂停写入。",
            details={"error": str(exc)},
        ) from exc


def merged_range_exists(ws: Any, coord: str) -> bool:
    return coord in {str(item) for item in ws.merged_cells.ranges}


def count_a_formulas(ws: Any) -> int:
    count = 0
    for row in range(1, EXCEL_LAST_TEMPLATE_ROW + 1):
        value = ws[f"A{row}"].value
        if isinstance(value, str) and value.startswith("="):
            count += 1
    return count


def validate_excel_structure(workbook: Any) -> ExcelState:
    ws = get_worksheet(workbook)
    numbers: list[int] = []

    for row in range(EXCEL_FIRST_ACCOUNT_ROW, EXCEL_LAST_TEMPLATE_ROW + 1):
        b_cell = ws[f"B{row}"]
        d_cell = ws[f"D{row}"]
        b_value = text(b_cell.value)
        d_value = text(d_cell.value)
        d_link = hyperlink_target(d_cell)

        # A hyperlink relationship without visible text is not treated as empty.
        if not d_value and d_link:
            raise CollectorError(
                "EXCEL_GHOST_HYPERLINK",
                "Excel 存在已清空文字但超链接仍残留的 D 单元格，已暂停写入。",
                details={
                    "row": row,
                    "number": f"A{row - 1}",
                    "b_cell": f"B{row}",
                    "b_value": b_value or "（空）",
                    "d_cell": f"D{row}",
                    "d_value": "（空）",
                    "hyperlink": d_link,
                    "action": "请在 Excel/WPS 中删除该单元格的超链接或执行清除全部。",
                },
            )

        if bool(b_value) != bool(d_value):
            raise CollectorError(
                "EXCEL_HALF_ROW",
                "Excel 存在 B/D 半行记录，已暂停写入，请手动处理。",
                details={
                    "row": row,
                    "number": f"A{row - 1}",
                    "b_cell": f"B{row}",
                    "b_value": b_value or "（空）",
                    "d_cell": f"D{row}",
                    "d_value": d_value or "（空）",
                    "hyperlink": d_link or "（无）",
                },
            )

        if b_value and d_value:
            numbers.append(row - 1)

    max_number = numbers[-1] if numbers else 0
    expected = list(range(1, max_number + 1))
    if numbers != expected:
        occupied = set(numbers)
        missing = [n for n in expected if n not in occupied]
        raise CollectorError(
            "EXCEL_NUMBER_GAP",
            "Excel 的账号行存在跳号或中间空行，已暂停写入。",
            details={
                "max_number": f"A{max_number}",
                "missing_numbers": [f"A{n}" for n in missing[:50]],
                "missing_count": len(missing),
            },
        )

    target_row = max_number + 2
    if target_row > EXCEL_LAST_TEMPLATE_ROW:
        raise CollectorError(
            "EXCEL_TEMPLATE_FULL",
            "Excel 模板已写满，已暂停写入。",
            details={"last_template_row": EXCEL_LAST_TEMPLATE_ROW, "next_row": target_row},
        )

    b_target = ws[f"B{target_row}"]
    d_target = ws[f"D{target_row}"]
    b_value = text(b_target.value)
    d_value = text(d_target.value)
    d_link = hyperlink_target(d_target)
    if b_value or d_value or d_link:
        raise CollectorError(
            "EXCEL_TARGET_OCCUPIED",
            f"Excel 目标位置 A{max_number + 1} 已被占用或残留，已暂停写入。",
            details={
                "row": target_row,
                "b_cell": f"B{target_row}",
                "b_value": b_value or "（空）",
                "d_cell": f"D{target_row}",
                "d_value": d_value or "（空）",
                "hyperlink": d_link or "（无）",
            },
        )

    expected_name_merge = f"B{target_row}:C{target_row}"
    expected_url_merge = f"D{target_row}:J{target_row}"
    merged_set = {str(item) for item in ws.merged_cells.ranges}
    missing_merges = [coord for coord in (expected_name_merge, expected_url_merge) if coord not in merged_set]
    if missing_merges:
        raise CollectorError(
            "EXCEL_TARGET_FORMAT_INVALID",
            "Excel 目标行的合并单元格结构不完整，已暂停写入。",
            details={"row": target_row, "missing_merges": missing_merges},
        )

    a_value = ws[f"A{target_row}"].value
    if not (isinstance(a_value, str) and a_value.startswith("=")):
        expected_text = f"{max_number + 1}.A{max_number + 1}"
        if text(a_value) != expected_text:
            raise CollectorError(
                "EXCEL_A_FORMULA_INVALID",
                "Excel 目标行 A 列编号公式或编号文本异常，已暂停写入。",
                details={
                    "cell": f"A{target_row}",
                    "actual": text(a_value) or "（空）",
                    "expected_formula_or_text": expected_text,
                },
            )

    return ExcelState(
        sheet_name=ws.title,
        numbers=tuple(numbers),
        max_number=max_number,
        target_row=target_row,
        merge_count=len(ws.merged_cells.ranges),
        conditional_format_count=len(ws.conditional_formatting),
        a_formula_count=count_a_formulas(ws),
    )


def validate_joint_state(json_state: JsonState, excel_state: ExcelState) -> StrictState:
    if json_state.numbers != excel_state.numbers:
        json_set = set(json_state.numbers)
        excel_set = set(excel_state.numbers)
        raise CollectorError(
            "JSON_EXCEL_NUMBER_MISMATCH",
            "settings_master.json 与 Excel 的已占用编号不一致，已暂停写入。",
            details={
                "json_max": f"A{json_state.max_number}",
                "excel_max": f"A{excel_state.max_number}",
                "only_in_json": [f"A{n}" for n in sorted(json_set - excel_set)[:50]],
                "only_in_excel": [f"A{n}" for n in sorted(excel_set - json_set)[:50]],
            },
        )

    if json_state.max_number != excel_state.max_number:
        raise CollectorError(
            "JSON_EXCEL_MAX_MISMATCH",
            "settings_master.json 与 Excel 的最大编号不同步，已暂停写入。",
            details={
                "json_max": f"A{json_state.max_number}",
                "excel_max": f"A{excel_state.max_number}",
            },
        )

    next_number = json_state.max_number + 1
    if json_state.target_index != next_number - 1 or excel_state.target_row != next_number + 1:
        raise CollectorError(
            "TARGET_MAPPING_INVALID",
            "JSON 与 Excel 的目标位置映射异常，已暂停写入。",
            details={
                "next_number": f"A{next_number}",
                "json_target_index": json_state.target_index,
                "excel_target_row": excel_state.target_row,
            },
        )

    return StrictState(
        json_state=json_state,
        excel_state=excel_state,
        next_number=next_number,
        target_json_index=json_state.target_index,
        target_excel_row=excel_state.target_row,
    )


def check_incomplete_transaction() -> None:
    if TRANSACTION_PATH.exists():
        try:
            content = json.loads(TRANSACTION_PATH.read_text(encoding="utf-8"))
        except Exception:
            content = {"path": str(TRANSACTION_PATH)}
        raise CollectorError(
            "INCOMPLETE_TRANSACTION",
            "检测到上一次双文件写入可能未完整结束，已暂停写入，请手动核对备份。",
            details=content if isinstance(content, dict) else {"value": content},
        )


def strict_precheck(profile_url: str, identity: str) -> tuple[StrictState, Any]:
    check_incomplete_transaction()
    document = read_json_document()

    # Required order: duplicate check first. If duplicate, Excel is never opened.
    duplicate_check_json(document[ACCOUNTS_KEY], profile_url, identity)

    json_state = validate_json_structure(document)
    workbook = load_excel()
    try:
        excel_state = validate_excel_structure(workbook)
        strict_state = validate_joint_state(json_state, excel_state)
        return strict_state, workbook
    except Exception:
        workbook.close()
        raise


def make_ready_data(
    strict_state: StrictState,
    profile_url: str,
    nickname: str,
    douyin_id: str,
    suffix: str,
    nickname_blank: bool,
) -> dict[str, Any]:
    number = strict_state.next_number
    mark = f"A{number}{suffix}"
    return {
        "number": number,
        "number_text": f"A{number}",
        "mark": mark,
        "url": profile_url,
        "nickname": nickname,
        "nickname_blank": nickname_blank,
        "douyin_id": douyin_id,
        "identity": f"{nickname}{douyin_id}",
        "json_max": f"A{strict_state.json_state.max_number}",
        "excel_max": f"A{strict_state.excel_state.max_number}",
        "json_index": strict_state.target_json_index,
        "json_position": f"{ACCOUNTS_KEY}[{strict_state.target_json_index}]",
        "excel_row": strict_state.target_excel_row,
        "excel_name_cell": f"B{strict_state.target_excel_row}",
        "excel_url_cell": f"D{strict_state.target_excel_row}",
        "excel_hyperlink": False,
        "category_state": pending_category_state(number),
    }


def preview(payload: dict[str, Any]) -> dict[str, Any]:
    profile_url = clean_profile_url(text(payload.get("url")))
    nickname, douyin_id, requested_mark, nickname_blank = profile_fields(payload)
    identity = f"{nickname}{douyin_id}"

    with FILE_LOCK:
        strict_state, workbook = strict_precheck(profile_url, identity)
        workbook.close()
        suffix = suffix_from_mark(requested_mark, strict_state.next_number, identity)
        backup_state, _ = load_backup_state(strict_state.json_state.max_number)
        data = make_ready_data(
            strict_state, profile_url, nickname, douyin_id, suffix, nickname_blank
        )
        next_count = backup_state.successful_adds_since_history + 1
        data.update(
            {
                "history_backup_interval": HISTORY_BACKUP_INTERVAL,
                "history_backup_progress": backup_state.successful_adds_since_history,
                "history_backup_due_after_add": next_count >= HISTORY_BACKUP_INTERVAL,
                "history_backup_progress_after_add": 0
                if next_count >= HISTORY_BACKUP_INTERVAL
                else next_count,
            }
        )

    return {
        "ok": True,
        "code": "READY",
        "message": "严格预检通过，可以同时写入 JSON + Excel。",
        "data": data,
        "version": VERSION,
    }


def load_backup_state(current_max: int) -> tuple[BackupState, bytes | None]:
    """Read the 0..19 history counter and reconcile it with manual restores.

    This hidden state file is only a backup cadence counter. It is never used
    for duplicate detection or account data.
    """
    raw_bytes: bytes | None = None
    if not BACKUP_STATE_PATH.exists():
        return BackupState(0, current_max), None

    try:
        raw_bytes = BACKUP_STATE_PATH.read_bytes()
        document = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorError(
            "BACKUP_STATE_INVALID",
            "历史备份计数文件无法读取，已暂停写入。删除 .douk_backup_state.json 可从 0/20 重新计数。",
            details={"path": str(BACKUP_STATE_PATH), "error": str(exc)},
        ) from exc

    if not isinstance(document, dict):
        raise CollectorError(
            "BACKUP_STATE_INVALID",
            "历史备份计数文件格式错误，已暂停写入。",
            details={"path": str(BACKUP_STATE_PATH)},
        )

    try:
        schema = int(document.get("schema", BACKUP_STATE_SCHEMA))
        count = int(document.get("successful_adds_since_history", 0))
        last_number = int(document.get("last_successful_number", current_max))
    except (TypeError, ValueError) as exc:
        raise CollectorError(
            "BACKUP_STATE_INVALID",
            "历史备份计数文件包含无效数字，已暂停写入。",
            details={"path": str(BACKUP_STATE_PATH)},
        ) from exc

    if schema != BACKUP_STATE_SCHEMA or not 0 <= count < HISTORY_BACKUP_INTERVAL or last_number < 0:
        raise CollectorError(
            "BACKUP_STATE_INVALID",
            "历史备份计数文件内容超出允许范围，已暂停写入。",
            details={
                "schema": schema,
                "successful_adds_since_history": count,
                "last_successful_number": last_number,
            },
        )

    if current_max < last_number:
        count = max(0, count - (last_number - current_max))
        last_number = current_max
    elif current_max > last_number:
        count = 0
        last_number = current_max

    return BackupState(count, last_number), raw_bytes


def next_backup_state(state: BackupState, added_number: int) -> tuple[BackupState, bool]:
    next_count = state.successful_adds_since_history + 1
    history_due = next_count >= HISTORY_BACKUP_INTERVAL
    return (
        BackupState(0 if history_due else next_count, added_number),
        history_due,
    )


def write_backup_state(state: BackupState) -> None:
    payload = {
        "schema": BACKUP_STATE_SCHEMA,
        "successful_adds_since_history": state.successful_adds_since_history,
        "last_successful_number": state.last_successful_number,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "history_backup_interval": HISTORY_BACKUP_INTERVAL,
    }
    temp = BACKUP_STATE_PATH.with_suffix(BACKUP_STATE_PATH.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, BACKUP_STATE_PATH)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise CollectorError(
            "BACKUP_STATE_WRITE_FAILED",
            "历史备份计数更新失败，正在撤销本次添加。",
            details={"path": str(BACKUP_STATE_PATH), "error": str(exc)},
            stage="write",
        ) from exc


def restore_backup_state(raw_bytes: bytes | None) -> dict[str, Any]:
    result: dict[str, Any] = {"restored": False}
    temp = BACKUP_STATE_PATH.with_suffix(BACKUP_STATE_PATH.suffix + ".restore.tmp")
    try:
        if raw_bytes is None:
            BACKUP_STATE_PATH.unlink(missing_ok=True)
        else:
            with temp.open("wb") as fh:
                fh.write(raw_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, BACKUP_STATE_PATH)
        result["restored"] = True
    except OSError as exc:
        temp.unlink(missing_ok=True)
        result["error"] = str(exc)
    return result


def _copy_to_temp(source: Path, target: Path, label: str) -> Path:
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.{label}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copy2(source, temp_path)
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def create_rollback_backups() -> tuple[Path, Path]:
    """Atomically overwrite the two fixed .bak files with the pre-add state."""
    settings_new: Path | None = None
    excel_new: Path | None = None
    settings_old: Path | None = None
    excel_old: Path | None = None
    settings_replaced = False
    excel_replaced = False
    try:
        settings_new = _copy_to_temp(SETTINGS_PATH, SETTINGS_SIMPLE_BACKUP_PATH, "new")
        excel_new = _copy_to_temp(EXCEL_PATH, EXCEL_SIMPLE_BACKUP_PATH, "new")
        if SETTINGS_SIMPLE_BACKUP_PATH.exists():
            settings_old = _copy_to_temp(
                SETTINGS_SIMPLE_BACKUP_PATH, SETTINGS_SIMPLE_BACKUP_PATH, "old"
            )
        if EXCEL_SIMPLE_BACKUP_PATH.exists():
            excel_old = _copy_to_temp(
                EXCEL_SIMPLE_BACKUP_PATH, EXCEL_SIMPLE_BACKUP_PATH, "old"
            )

        os.replace(settings_new, SETTINGS_SIMPLE_BACKUP_PATH)
        settings_new = None
        settings_replaced = True
        os.replace(excel_new, EXCEL_SIMPLE_BACKUP_PATH)
        excel_new = None
        excel_replaced = True
    except PermissionError as exc:
        if settings_replaced:
            if settings_old is not None:
                os.replace(settings_old, SETTINGS_SIMPLE_BACKUP_PATH)
                settings_old = None
            else:
                SETTINGS_SIMPLE_BACKUP_PATH.unlink(missing_ok=True)
        if excel_replaced:
            if excel_old is not None:
                os.replace(excel_old, EXCEL_SIMPLE_BACKUP_PATH)
                excel_old = None
            else:
                EXCEL_SIMPLE_BACKUP_PATH.unlink(missing_ok=True)
        raise CollectorError(
            "BACKUP_FAILED",
            "覆盖 .bak 失败，可能是文件正在被占用，已暂停写入。",
            details={"error": str(exc)},
            stage="write",
        ) from exc
    except OSError as exc:
        if settings_replaced:
            if settings_old is not None:
                os.replace(settings_old, SETTINGS_SIMPLE_BACKUP_PATH)
                settings_old = None
            else:
                SETTINGS_SIMPLE_BACKUP_PATH.unlink(missing_ok=True)
        if excel_replaced:
            if excel_old is not None:
                os.replace(excel_old, EXCEL_SIMPLE_BACKUP_PATH)
                excel_old = None
            else:
                EXCEL_SIMPLE_BACKUP_PATH.unlink(missing_ok=True)
        raise CollectorError(
            "BACKUP_FAILED",
            "覆盖 JSON/Excel 的 .bak 失败，已暂停写入。",
            details={"error": str(exc)},
            stage="write",
        ) from exc
    finally:
        for path in (settings_new, excel_new, settings_old, excel_old):
            if path is not None:
                path.unlink(missing_ok=True)

    return SETTINGS_SIMPLE_BACKUP_PATH, EXCEL_SIMPLE_BACKUP_PATH


def create_history_backups() -> tuple[Path, Path]:
    """Create one unique post-commit JSON + Excel history pair; never prune it."""
    SETTINGS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    EXCEL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    settings_final = SETTINGS_BACKUP_DIR / f"settings_master-{stamp}.json"
    excel_final = EXCEL_BACKUP_DIR / f"录制名单-{stamp}.xlsx"
    settings_temp: Path | None = None
    excel_temp: Path | None = None
    settings_published = False
    try:
        settings_temp = _copy_to_temp(SETTINGS_PATH, settings_final, "history")
        excel_temp = _copy_to_temp(EXCEL_PATH, excel_final, "history")
        os.replace(settings_temp, settings_final)
        settings_temp = None
        settings_published = True
        os.replace(excel_temp, excel_final)
        excel_temp = None
    except PermissionError as exc:
        if settings_published:
            settings_final.unlink(missing_ok=True)
        raise CollectorError(
            "HISTORY_BACKUP_FAILED",
            "第 20 次历史备份创建失败，正在撤销本次添加。",
            details={"error": str(exc)},
            stage="write",
        ) from exc
    except OSError as exc:
        if settings_published:
            settings_final.unlink(missing_ok=True)
        raise CollectorError(
            "HISTORY_BACKUP_FAILED",
            "第 20 次 JSON/Excel 历史备份创建失败，正在撤销本次添加。",
            details={"error": str(exc)},
            stage="write",
        ) from exc
    finally:
        if settings_temp is not None:
            settings_temp.unlink(missing_ok=True)
        if excel_temp is not None:
            excel_temp.unlink(missing_ok=True)

    return settings_final, excel_final


def remove_history_backups(settings_backup: Path | None, excel_backup: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {"settings_removed": False, "excel_removed": False}
    errors: list[str] = []
    for key, path in (("settings_removed", settings_backup), ("excel_removed", excel_backup)):
        if path is None:
            result[key] = True
            continue
        try:
            path.unlink(missing_ok=True)
            result[key] = True
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        result["errors"] = errors
    return result


def write_json_temp(document: dict[str, Any], target: Path) -> Path:
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=4)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        return temp_path
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def verify_json_temp(path: Path, number: int, mark: str, profile_url: str) -> None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            document = json.load(fh)
        record = document[ACCOUNTS_KEY][number - 1]
    except Exception as exc:
        raise CollectorError(
            "JSON_TEMP_VERIFY_FAILED",
            "JSON 临时文件验证失败，未替换正式文件。",
            details={"error": str(exc)},
            stage="write",
        ) from exc
    if text(record.get("mark")) != mark or text(record.get("url")) != profile_url:
        raise CollectorError(
            "JSON_TEMP_VERIFY_FAILED",
            "JSON 临时文件中的新记录不正确，未替换正式文件。",
            details={"record": record},
            stage="write",
        )


def verify_excel_temp(
    path: Path,
    *,
    row: int,
    suffix: str,
    profile_url: str,
    original_state: ExcelState,
) -> None:
    try:
        workbook = load_workbook(path, data_only=False, read_only=False, keep_links=True)
        ws = get_worksheet(workbook)
        actual_b = text(ws[f"B{row}"].value)
        actual_d = text(ws[f"D{row}"].value)
        actual_link = hyperlink_target(ws[f"D{row}"])
        merge_count = len(ws.merged_cells.ranges)
        cf_count = len(ws.conditional_formatting)
        formula_count = count_a_formulas(ws)
        workbook.close()
    except Exception as exc:
        raise CollectorError(
            "EXCEL_TEMP_VERIFY_FAILED",
            "Excel 临时文件无法重新打开验证，未替换正式文件。",
            details={"error": str(exc)},
            stage="write",
        ) from exc

    problems: dict[str, Any] = {}
    if actual_b != suffix:
        problems["name_value"] = {"expected": suffix, "actual": actual_b}
    if actual_d != profile_url:
        problems["url_value"] = {"expected": profile_url, "actual": actual_d}
    if actual_link:
        problems["hyperlink"] = {
            "expected": "（无，新记录必须是纯文本 URL）",
            "actual": actual_link,
        }
    if merge_count != original_state.merge_count:
        problems["merge_count"] = {"expected": original_state.merge_count, "actual": merge_count}
    if cf_count != original_state.conditional_format_count:
        problems["conditional_format_count"] = {
            "expected": original_state.conditional_format_count,
            "actual": cf_count,
        }
    if formula_count != original_state.a_formula_count:
        problems["a_formula_count"] = {
            "expected": original_state.a_formula_count,
            "actual": formula_count,
        }

    if problems:
        raise CollectorError(
            "EXCEL_TEMP_VERIFY_FAILED",
            "Excel 临时文件验证未通过，未替换正式文件。",
            details=problems,
            stage="write",
        )


def write_transaction_journal(data: dict[str, Any]) -> None:
    temp = TRANSACTION_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, TRANSACTION_PATH)


def restore_from_backups(settings_backup: Path, excel_backup: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"settings_restored": False, "excel_restored": False}
    errors: list[str] = []
    try:
        shutil.copy2(settings_backup, SETTINGS_PATH)
        result["settings_restored"] = True
    except Exception as exc:  # pragma: no cover - rare OS failure
        errors.append(f"settings restore failed: {exc}")
    try:
        shutil.copy2(excel_backup, EXCEL_PATH)
        result["excel_restored"] = True
    except Exception as exc:  # pragma: no cover - rare OS failure
        errors.append(f"excel restore failed: {exc}")
    if errors:
        result["errors"] = errors
    return result


def add_record(payload: dict[str, Any]) -> dict[str, Any]:
    profile_url = clean_profile_url(text(payload.get("url")))
    nickname, douyin_id, requested_mark, nickname_blank = profile_fields(payload)
    identity = f"{nickname}{douyin_id}"

    with FILE_LOCK:
        # Final check: reload both files at the exact moment the user clicks Add/Alt+A.
        strict_state, workbook = strict_precheck(profile_url, identity)
        number = strict_state.next_number
        try:
            suffix = suffix_from_mark(requested_mark, number, identity)
        except Exception:
            workbook.close()
            raise
        mark = f"A{number}{suffix}"
        target_index = strict_state.target_json_index
        target_row = strict_state.target_excel_row
        backup_state, backup_state_before = load_backup_state(
            strict_state.json_state.max_number
        )
        backup_state_after, history_backup_due = next_backup_state(backup_state, number)

        document = copy.deepcopy(strict_state.json_state.document)
        accounts = document[ACCOUNTS_KEY]
        new_record = {
            "mark": mark,
            "url": profile_url,
            "tab": "post",
            "earliest": "",
            "latest": "",
            "enable": True,
        }
        if target_index == len(accounts):
            accounts.append(new_record)
        else:
            accounts[target_index] = new_record

        ws = get_worksheet(workbook)
        name_cell = ws[f"B{target_row}"]
        url_cell = ws[f"D{target_row}"]
        excel_body = excel_body_from_suffix(suffix)
        name_cell.value = excel_body

        # v2.4.1: new D-column URLs are deliberately plain text.
        # Clear any impossible/stale relation at the target, then write only the value.
        url_cell.hyperlink = None
        url_cell.value = profile_url

        settings_temp: Path | None = None
        excel_temp: Path | None = None
        settings_backup: Path | None = None
        excel_backup: Path | None = None
        history_settings_backup: Path | None = None
        history_excel_backup: Path | None = None
        backup_state_written = False

        try:
            settings_temp = write_json_temp(document, SETTINGS_PATH)
            verify_json_temp(settings_temp, number, mark, profile_url)

            fd, excel_temp_name = tempfile.mkstemp(
                prefix=f".{EXCEL_PATH.stem}.", suffix=".xlsx", dir=EXCEL_PATH.parent
            )
            os.close(fd)
            excel_temp = Path(excel_temp_name)
            workbook.save(excel_temp)
            workbook.close()

            verify_excel_temp(
                excel_temp,
                row=target_row,
                suffix=excel_body,
                profile_url=profile_url,
                original_state=strict_state.excel_state,
            )

            settings_backup, excel_backup = create_rollback_backups()
            write_transaction_journal(
                {
                    "version": VERSION,
                    "state": "replacing",
                    "started_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "number": f"A{number}",
                    "mark": mark,
                    "url": profile_url,
                    "settings_backup": str(settings_backup),
                    "excel_backup": str(excel_backup),
                    "history_backup_due": history_backup_due,
                    "history_backup_interval": HISTORY_BACKUP_INTERVAL,
                    "history_progress_before": backup_state.successful_adds_since_history,
                }
            )

            # Replace the Excel file first because file locking is more common there.
            os.replace(excel_temp, EXCEL_PATH)
            excel_temp = None
            os.replace(settings_temp, SETTINGS_PATH)
            settings_temp = None

            # Post-commit verification. New Excel URL must still have no hyperlink.
            committed_json = read_json_document()
            committed_record = committed_json[ACCOUNTS_KEY][target_index]
            committed_wb = load_excel()
            committed_ws = get_worksheet(committed_wb)
            committed_b = text(committed_ws[f"B{target_row}"].value)
            committed_d = text(committed_ws[f"D{target_row}"].value)
            committed_link = hyperlink_target(committed_ws[f"D{target_row}"])
            committed_wb.close()

            if (
                text(committed_record.get("mark")) != mark
                or text(committed_record.get("url")) != profile_url
                or committed_b != excel_body
                or committed_d != profile_url
                or committed_link
            ):
                raise CollectorError(
                    "POST_COMMIT_VERIFY_FAILED",
                    "双文件替换后验证失败，正在尝试从备份恢复。",
                    details={
                        "json_record": committed_record,
                        "excel_b": committed_b,
                        "excel_d": committed_d,
                        "excel_hyperlink": committed_link or "（无）",
                    },
                    stage="write",
                )

            if history_backup_due:
                history_settings_backup, history_excel_backup = create_history_backups()

            write_backup_state(backup_state_after)
            backup_state_written = True
            TRANSACTION_PATH.unlink(missing_ok=True)

        except CollectorError:
            history_cleanup = remove_history_backups(
                history_settings_backup, history_excel_backup
            )
            state_restore = (
                restore_backup_state(backup_state_before)
                if backup_state_written
                else {"restored": True, "not_changed": True}
            )
            restore_result: dict[str, Any] | None = None
            if settings_backup and excel_backup:
                restore_result = restore_from_backups(settings_backup, excel_backup)
            if (
                (restore_result is None or (
                    restore_result.get("settings_restored")
                    and restore_result.get("excel_restored")
                ))
                and state_restore.get("restored")
                and history_cleanup.get("settings_removed")
                and history_cleanup.get("excel_removed")
            ):
                TRANSACTION_PATH.unlink(missing_ok=True)
            raise
        except PermissionError as exc:
            history_cleanup = remove_history_backups(
                history_settings_backup, history_excel_backup
            )
            state_restore = (
                restore_backup_state(backup_state_before)
                if backup_state_written
                else {"restored": True, "not_changed": True}
            )
            restore_result: dict[str, Any] | None = None
            if settings_backup and excel_backup:
                restore_result = restore_from_backups(settings_backup, excel_backup)
            if (
                (restore_result is None or (
                    restore_result.get("settings_restored")
                    and restore_result.get("excel_restored")
                ))
                and state_restore.get("restored")
                and history_cleanup.get("settings_removed")
                and history_cleanup.get("excel_removed")
            ):
                TRANSACTION_PATH.unlink(missing_ok=True)
            raise CollectorError(
                "WRITE_PERMISSION_DENIED",
                "写入失败，可能是 录制名单.xlsx 正在被 Excel/WPS 占用。两份文件均已停止写入。",
                details={
                    "error": str(exc),
                    "restore": restore_result,
                    "backup_state_restore": state_restore,
                    "history_cleanup": history_cleanup,
                },
                stage="write",
            ) from exc
        except Exception as exc:
            history_cleanup = remove_history_backups(
                history_settings_backup, history_excel_backup
            )
            state_restore = (
                restore_backup_state(backup_state_before)
                if backup_state_written
                else {"restored": True, "not_changed": True}
            )
            restore_result: dict[str, Any] | None = None
            if settings_backup and excel_backup:
                restore_result = restore_from_backups(settings_backup, excel_backup)
            if (
                (restore_result is None or (
                    restore_result.get("settings_restored")
                    and restore_result.get("excel_restored")
                ))
                and state_restore.get("restored")
                and history_cleanup.get("settings_removed")
                and history_cleanup.get("excel_removed")
            ):
                TRANSACTION_PATH.unlink(missing_ok=True)
            raise CollectorError(
                "WRITE_FAILED",
                "同时写入 JSON + Excel 失败，已尝试从 .bak 恢复。",
                details={
                    "error": str(exc),
                    "restore": restore_result,
                    "backup_state_restore": state_restore,
                    "history_cleanup": history_cleanup,
                },
                stage="write",
            ) from exc
        finally:
            try:
                workbook.close()
            except Exception:
                pass
            if settings_temp is not None:
                settings_temp.unlink(missing_ok=True)
            if excel_temp is not None:
                excel_temp.unlink(missing_ok=True)

        data = make_ready_data(
            strict_state, profile_url, nickname, douyin_id, suffix, nickname_blank
        )
        data.update(
            {
                "mark": mark,
                "category_state": safe_category_state(number),
                "backup_settings": str(settings_backup) if settings_backup else "",
                "backup_excel": str(excel_backup) if excel_backup else "",
                "history_backup_interval": HISTORY_BACKUP_INTERVAL,
                "history_backup_created": history_backup_due,
                "history_backup_progress": backup_state_after.successful_adds_since_history,
                "history_backup_settings": str(history_settings_backup)
                if history_settings_backup
                else "",
                "history_backup_excel": str(history_excel_backup)
                if history_excel_backup
                else "",
            }
        )
        backup_note = (
            "history snapshot created"
            if history_backup_due
            else f"history {backup_state_after.successful_adds_since_history}/{HISTORY_BACKUP_INTERVAL}"
        )
        print(
            f"[ADDED] A{number} | JSON {ACCOUNTS_KEY}[{target_index}] | "
            f"Excel B{target_row}/D{target_row} | plain-text URL | {backup_note}"
        )
        message = (
            "已同时写入 settings_master.json 和 Excel；两个 .bak 已覆盖更新；"
            "已生成第 20 次 JSON＋Excel 历史备份。"
            if history_backup_due
            else "已同时写入 settings_master.json 和 Excel；两个 .bak 已覆盖更新。"
        )
        return {
            "ok": True,
            "code": "ADDED",
            "message": message,
            "data": data,
            "version": VERSION,
        }


def classify_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Toggle one category using the account number resolved from settings_master.json."""
    requested_category = text(payload.get("category"))
    if requested_category not in CATEGORY_NAMES:
        raise CollectorError(
            "CATEGORY_INVALID",
            "分类名称无效，本次未修改 TXT。",
            details={"category": requested_category or "（空）"},
        )

    profile_url = clean_profile_url(text(payload.get("url")))
    nickname, douyin_id, _, _ = profile_fields(payload)
    identity = f"{nickname}{douyin_id}"

    with FILE_LOCK:
        document = read_json_document()
        match = find_duplicate_account(document[ACCOUNTS_KEY], profile_url, identity)
        if match is None:
            raise CollectorError(
                "CATEGORY_ACCOUNT_NOT_ADDED",
                "当前账号尚未写入 settings_master.json，请先点击“同时添加 JSON + Excel”。",
            )

        index = int(match["index"])
        number = index + 1
        mark = text(match.get("mark"))
        prefix = f"A{number}"
        if (
            mark[: len(prefix)].casefold() != prefix.casefold()
            or not mark[len(prefix) :]
        ):
            raise CollectorError(
                "CATEGORY_EXISTING_MARK_INVALID",
                "已存在记录的 mark 与 JSON 位置不一致，本次未修改 TXT。",
                details={"mark": mark or "（空）", "json_index": index, "expected_number": prefix},
            )

        category_sets = read_category_sets()
        owners = [name for name in CATEGORY_NAMES if number in category_sets[name]]
        if len(owners) > 1:
            joined = "”和“".join(owners)
            raise CollectorError(
                "CATEGORY_DATA_CONFLICT",
                f"分类数据冲突：该编号同时存在于“{joined}”。本次禁止修改，请先检查分类TXT。",
                details={"number": number, "categories": owners},
            )

        if owners and owners[0] != requested_category:
            current = owners[0]
            raise CollectorError(
                "CATEGORY_CHANGE_REQUIRES_CANCEL",
                f"该编号已分类为“{current}”。请先点击“✓ {current}”取消原分类。",
                details={"number": number, "current_category": current},
            )

        updated = {name: set(values) for name, values in category_sets.items()}
        if owners:
            updated[requested_category].remove(number)
            action = "removed"
            code = "CATEGORY_REMOVED"
            message = f"已取消 A{number} 的“{requested_category}”分类。"
        else:
            updated[requested_category].add(number)
            action = "added"
            code = "CATEGORY_ADDED"
            message = f"已将 A{number} 记录到“{requested_category}.txt”。"

        validate_category_sets(updated)
        write_category_file(requested_category, updated[requested_category])
        verified_sets = read_category_sets()
        state = category_state_from_sets(number, verified_sets)

        print(f"[CATEGORY {action.upper()}] A{number} | {requested_category}.txt")
        return {
            "ok": True,
            "code": code,
            "message": message,
            "data": {
                "number": number,
                "number_text": f"A{number}",
                "category": requested_category,
                "action": action,
                "category_state": state,
            },
            "version": VERSION,
        }


def validate_files_only() -> StrictState:
    check_incomplete_transaction()
    document = read_json_document()
    json_state = validate_json_structure(document)
    workbook = load_excel()
    try:
        excel_state = validate_excel_structure(workbook)
        strict_state = validate_joint_state(json_state, excel_state)
        load_backup_state(strict_state.json_state.max_number)
        return strict_state
    finally:
        workbook.close()


def format_error_for_console(error: CollectorError) -> None:
    print(f"[BLOCKED] {error.code}: {error.message}")
    if error.details:
        print(json.dumps(error.details, ensure_ascii=False, indent=2))


class Handler(BaseHTTPRequestHandler):
    server_version = f"DouKCollector/{VERSION}"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Old/background tabs are rejected silently so dozens of already-open
        # Douyin tabs cannot flood the BAT window.
        if getattr(self, "_suppress_log", False):
            return
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {fmt % args}")

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "https://www.douyin.com")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-DouK-Token")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _authorized(self) -> bool:
        return self.headers.get("X-DouK-Token", "") == ACCESS_TOKEN

    def do_OPTIONS(self) -> None:  # noqa: N802
        # Extension requests do not need noisy preflight logs.
        self._suppress_log = True
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._send_json(
                {
                    "ok": True,
                    "code": "HEALTHY",
                    "message": "DouK collector service is running.",
                    "version": VERSION,
                }
            )
            return
        self._send_json({"ok": False, "code": "NOT_FOUND", "message": "Not found."}, 404)

    def do_POST(self) -> None:  # noqa: N802
        # Requests from already-open tabs with a different client version are
        # rejected silently and never read settings_master.json / Excel.  The token
        # remains compatible with the previous release so users can upgrade
        # the three files together without a separate secret change.
        if not self._authorized():
            self._suppress_log = True
            self._send_json(
                {
                    "ok": False,
                    "code": "CLIENT_OUTDATED",
                    "message": "旧版标签页已被静默停用；切换到该标签页使用时刷新一次即可。",
                    "version": VERSION,
                },
                409,
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256_000:
                raise CollectorError("INVALID_REQUEST", "请求内容为空或过大。")
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise CollectorError("INVALID_REQUEST", "请求内容必须是 JSON 对象。")

            client_version = text(payload.get("client_version"))
            page_visible = payload.get("page_visible") is True
            if client_version != VERSION:
                self._suppress_log = True
                self._send_json(
                    {
                        "ok": False,
                        "code": "CLIENT_OUTDATED",
                        "message": "油猴脚本版本过旧；当前标签页刷新后再使用。",
                        "version": VERSION,
                    },
                    409,
                )
                return
            if not page_visible:
                self._suppress_log = True
                self._send_json(
                    {
                        "ok": False,
                        "code": "BACKGROUND_TAB_IGNORED",
                        "message": "后台标签页请求已忽略。",
                        "version": VERSION,
                    },
                    409,
                )
                return

            with critical_section(GLOBAL_LOCK_PATH, timeout=3.0):
                if self.path.rstrip("/") == "/preview":
                    result = preview(payload)
                elif self.path.rstrip("/") == "/add":
                    result = add_record(payload)
                elif self.path.rstrip("/") == "/classify":
                    result = classify_record(payload)
                elif self.path.rstrip("/") == "/account-screenshot-check":
                    result = check_account_screenshot(payload)
                elif self.path.rstrip("/") == "/account-screenshot":
                    result = capture_account_screenshot(payload)
                else:
                    self._send_json(
                        {"ok": False, "code": "NOT_FOUND", "message": "Not found."}, 404
                    )
                    return
            self._send_json(result)
        except LockBusyError:
            error = CollectorError(
                "MANAGER_BUSY",
                "DouK 管理器正在备份或修改正式配置，请稍后重试。",
                stage="precheck",
            )
            self._send_json(error.response(), 409)
        except CollectorError as exc:
            format_error_for_console(exc)
            self._send_json(exc.response())
        except json.JSONDecodeError as exc:
            error = CollectorError(
                "INVALID_REQUEST_JSON",
                "浏览器请求不是有效 JSON。",
                details={"error": str(exc)},
            )
            format_error_for_console(error)
            self._send_json(error.response())
        except Exception as exc:  # pragma: no cover - last-resort diagnostics
            traceback.print_exc()
            self._send_json(
                {
                    "ok": False,
                    "code": "INTERNAL_ERROR",
                    "message": "采集器出现未处理错误，请查看管理器采集器日志。",
                    "details": {"error": str(exc)},
                    "version": VERSION,
                },
                500,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="DouK collector strict mode")
    parser.add_argument("--check", action="store_true", help="validate files and exit")
    args = parser.parse_args()

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 68)
    print(f"DouK account collector v{VERSION} - strict consistency mode")
    print(f"Multi-tab mode: only the visible v{VERSION} tab is processed; old/background tabs are silent.")
    print(f"Excel template last row: {EXCEL_LAST_TEMPLATE_ROW} (supports A5000).")
    print(f"Folder   : {BASE_DIR}")
    print(f"Settings : {SETTINGS_PATH}")
    print(f"Excel    : {EXCEL_PATH}")
    print("Category : 顶级.txt / 次顶级.txt / 普通.txt")
    print(f"Screenshots: {SCREENSHOT_DIR}\\A-number.jpg (existing images are never overwritten)")
    print("Screenshot range: foreground Chrome below the tab strip; address bar and page are retained; bookmarks bar is removed.")
    print("IMPORTANT: do not let another program save settings_master.json while the collector is adding a record.")
    print("Excel URL policy: NEW records are plain text; no hyperlink is created.")
    print("Backup policy: overwrite two .bak files every success; timestamp history every 20 successes; never auto-delete history.")
    print("=" * 68)

    try:
        state = validate_files_only()
        print(
            f"[READY] JSON A{state.json_state.max_number}; "
            f"Excel A{state.excel_state.max_number}; next A{state.next_number}"
        )
        backup_state, _ = load_backup_state(state.json_state.max_number)
        print(
            f"[BACKUP] fixed .bak pair; history progress "
            f"{backup_state.successful_adds_since_history}/{HISTORY_BACKUP_INTERVAL}"
        )
    except CollectorError as exc:
        format_error_for_console(exc)
        if args.check:
            return 2
        print("[INFO] The service will still start. Fix the files, then click Re-detect in the panel.")

    if args.check:
        return 0

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"[ERROR] Cannot listen on http://{HOST}:{PORT} - {exc}")
        return 3

    with FILE_LOCK:
        initialize_category_files_after_bind()

    print(f"[RUNNING] http://{HOST}:{PORT}")
    print("Keep this window open. Press Ctrl+C to stop.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n[STOPPED]")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
