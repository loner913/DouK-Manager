#!/usr/bin/env python3
"""DouK account screenshot organizer.

The authoritative account number is the A-number field immediately following
the leading UID field in an account folder name:

    UID1234567890123456_A115display-name_发布作品

Any later ``A123``-like text belongs to the display-name/account text and is
ignored.  A fatal ambiguity exists only when two different sibling account
folders claim the same authoritative A-number.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


VERSION = "1.1"
CONFIG_NAME = ".douk_screenshot_organizer_config.json"

# Only the canonical field directly after the leading UID is an account number.
# A later A-number in a nickname/account ID is deliberately ignored.
ACCOUNT_FOLDER_PATTERN = re.compile(
    r"^UID[0-9]+_A([1-9][0-9]*)(?=[^0-9]|$)",
    re.IGNORECASE,
)
IMAGE_PATTERN = re.compile(r"^A([1-9][0-9]*)\.(jpg|png)$", re.IGNORECASE)


class OrganizerError(RuntimeError):
    """A safety condition that must stop the current run."""


@dataclass(frozen=True)
class ImageSnapshot:
    path: Path
    account_id: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class ScanResult:
    folder_map: dict[str, Path]
    images: tuple[ImageSnapshot, ...]
    unmatched_folder_count: int


@dataclass(frozen=True)
class MovePlan:
    image: ImageSnapshot
    destination_dir: Path
    destination_path: Path


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def canonical_account_id(number_text: str) -> str:
    return f"A{int(number_text)}"


def extract_account_folder_id(folder_name: str) -> str | None:
    """Return the one authoritative A-number, or None for unrelated folders."""
    match = ACCOUNT_FOLDER_PATTERN.match(folder_name)
    if match is None:
        return None
    return canonical_account_id(match.group(1))


def extract_image_id(file_name: str) -> str | None:
    match = IMAGE_PATTERN.fullmatch(file_name)
    if match is None:
        return None
    return canonical_account_id(match.group(1))


def format_paths(paths: Iterable[Path]) -> str:
    return "\n".join(f"  - {path.name}" for path in paths)


def scan_account_folders(account_root: Path) -> tuple[dict[str, Path], int]:
    claims: dict[str, list[Path]] = {}
    unmatched_count = 0

    try:
        entries = sorted(account_root.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise OrganizerError(f"无法扫描账号目录：{exc}") from exc

    for entry in entries:
        if not entry.is_dir():
            continue
        account_id = extract_account_folder_id(entry.name)
        if account_id is None:
            unmatched_count += 1
            continue
        claims.setdefault(account_id, []).append(entry)

    duplicates = {
        account_id: paths
        for account_id, paths in claims.items()
        if len(paths) > 1
    }
    if duplicates:
        lines = [
            "发现同一个 A编号被多个账号文件夹重复占用，已停止整个任务；本次移动 0 张。",
            "只有这种“不同文件夹重复使用同一规范 A编号”的情况才会停止：",
        ]
        for account_id in sorted(
            duplicates,
            key=lambda value: int(value[1:]),
        ):
            lines.append(f"\n重复编号：{account_id}")
            lines.append(format_paths(duplicates[account_id]))
        raise OrganizerError("\n".join(lines))

    return {account_id: paths[0] for account_id, paths in claims.items()}, unmatched_count


def scan_images(screenshot_dir: Path) -> tuple[ImageSnapshot, ...]:
    images: list[ImageSnapshot] = []
    seen_names: dict[str, Path] = {}

    try:
        entries = sorted(screenshot_dir.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise OrganizerError(f"无法扫描截图目录：{exc}") from exc

    for entry in entries:
        if not entry.is_file():
            continue
        account_id = extract_image_id(entry.name)
        if account_id is None:
            continue

        folded_name = entry.name.casefold()
        prior = seen_names.get(folded_name)
        if prior is not None:
            raise OrganizerError(
                "截图目录中存在仅大小写不同的同名图片，Windows 下无法安全区分，"
                f"已停止：\n  - {prior.name}\n  - {entry.name}"
            )
        seen_names[folded_name] = entry

        try:
            stat = entry.stat()
        except OSError as exc:
            raise OrganizerError(f"无法读取图片信息：{entry.name}：{exc}") from exc
        images.append(
            ImageSnapshot(
                path=entry,
                account_id=account_id,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
            )
        )

    return tuple(images)


def validate_directories(screenshot_dir: Path, account_root: Path) -> None:
    if not screenshot_dir.is_dir():
        raise OrganizerError(f"截图目录不存在或不是文件夹：{screenshot_dir}")
    if not account_root.is_dir():
        raise OrganizerError(f"账号目录不存在或不是文件夹：{account_root}")

    try:
        screenshot_real = screenshot_dir.resolve(strict=True)
        account_real = account_root.resolve(strict=True)
    except OSError as exc:
        raise OrganizerError(f"无法解析所选目录：{exc}") from exc

    if screenshot_real == account_real:
        raise OrganizerError("截图目录和账号目录不能是同一个文件夹。")


def scan_all(screenshot_dir: Path, account_root: Path) -> ScanResult:
    validate_directories(screenshot_dir, account_root)
    folder_map, unmatched_count = scan_account_folders(account_root)
    images = scan_images(screenshot_dir)
    return ScanResult(
        folder_map=folder_map,
        images=images,
        unmatched_folder_count=unmatched_count,
    )


def find_case_insensitive_child(directory: Path, name: str) -> Path | None:
    folded = name.casefold()
    try:
        for child in directory.iterdir():
            if child.name.casefold() == folded:
                return child
    except OSError as exc:
        raise OrganizerError(f"无法检查目标目录 {directory.name}：{exc}") from exc
    return None


def make_plans(scan: ScanResult) -> tuple[tuple[MovePlan, ...], tuple[ImageSnapshot, ...], tuple[ImageSnapshot, ...]]:
    plans: list[MovePlan] = []
    missing: list[ImageSnapshot] = []
    existing: list[ImageSnapshot] = []

    for image in scan.images:
        destination_dir = scan.folder_map.get(image.account_id)
        if destination_dir is None:
            missing.append(image)
            continue
        conflict = find_case_insensitive_child(destination_dir, image.path.name)
        if conflict is not None:
            existing.append(image)
            continue
        plans.append(
            MovePlan(
                image=image,
                destination_dir=destination_dir,
                destination_path=destination_dir / image.path.name,
            )
        )

    return tuple(plans), tuple(missing), tuple(existing)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise OrganizerError(f"无法读取文件 {path.name}：{exc}") from exc
    return digest.hexdigest()


def ensure_snapshot_unchanged(snapshot: ImageSnapshot) -> None:
    try:
        stat = snapshot.path.stat()
    except OSError as exc:
        raise OrganizerError(f"源图片已消失或无法读取：{snapshot.path.name}：{exc}") from exc
    if stat.st_size != snapshot.size or stat.st_mtime_ns != snapshot.mtime_ns:
        raise OrganizerError(
            f"源图片在预检后发生变化，已停止：{snapshot.path.name}"
        )


def copy_exclusive(source: Path, destination: Path) -> None:
    with source.open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def publish_temp_exclusively(temp_path: Path, destination: Path) -> None:
    """Publish without ever replacing an existing destination."""
    try:
        os.link(temp_path, destination)
    except FileExistsError as exc:
        raise OrganizerError(
            f"目标中已出现同名图片，未覆盖：{destination}"
        ) from exc
    except (AttributeError, NotImplementedError, OSError):
        # Exclusive creation is the no-hardlink fallback.  It still cannot
        # overwrite an existing destination.
        created_destination = False
        try:
            with temp_path.open("rb") as src, destination.open("xb") as dst:
                created_destination = True
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
        except FileExistsError as exc:
            raise OrganizerError(
                f"目标中已出现同名图片，未覆盖：{destination}"
            ) from exc
        except Exception:
            try:
                if created_destination and destination.exists():
                    destination.unlink()
            except OSError:
                pass
            raise
    else:
        temp_path.unlink()
        return

    temp_path.unlink()


def safe_move(plan: MovePlan) -> str:
    ensure_snapshot_unchanged(plan.image)

    # Re-scan sibling account folders immediately before every move.  This
    # catches a newly created duplicate A-number even after the second preflight.
    current_folder_map, _ = scan_account_folders(plan.destination_dir.parent)
    current_destination = current_folder_map.get(plan.image.account_id)
    if (
        current_destination is None
        or current_destination.resolve() != plan.destination_dir.resolve()
    ):
        raise OrganizerError(
            f"目标账号文件夹映射在预检后发生变化，已停止："
            f"{plan.destination_dir.name}"
        )

    conflict = find_case_insensitive_child(
        plan.destination_dir,
        plan.destination_path.name,
    )
    if conflict is not None:
        raise OrganizerError(
            f"目标中已出现同名图片，未覆盖并已停止：{conflict}"
        )

    source_hash = sha256_file(plan.image.path)
    temp_path = plan.destination_dir / (
        f".{plan.destination_path.name}.douk-moving-{uuid.uuid4().hex}.tmp"
    )

    try:
        copy_exclusive(plan.image.path, temp_path)
        if temp_path.stat().st_size != plan.image.size:
            raise OrganizerError(f"临时副本大小校验失败：{plan.image.path.name}")
        if sha256_file(temp_path) != source_hash:
            raise OrganizerError(f"临时副本 SHA-256 校验失败：{plan.image.path.name}")

        ensure_snapshot_unchanged(plan.image)
        if sha256_file(plan.image.path) != source_hash:
            raise OrganizerError(f"源图片在复制期间发生变化：{plan.image.path.name}")

        conflict = find_case_insensitive_child(
            plan.destination_dir,
            plan.destination_path.name,
        )
        if conflict is not None:
            raise OrganizerError(
                f"目标中已出现同名图片，未覆盖并已停止：{conflict}"
            )

        publish_temp_exclusively(temp_path, plan.destination_path)

        if plan.destination_path.stat().st_size != plan.image.size:
            raise OrganizerError(
                f"正式目标大小校验失败；源图仍保留：{plan.image.path.name}"
            )
        if sha256_file(plan.destination_path) != source_hash:
            raise OrganizerError(
                f"正式目标 SHA-256 校验失败；源图仍保留：{plan.image.path.name}"
            )

        try:
            plan.image.path.unlink()
        except OSError as exc:
            return (
                f"已复制但源图删除失败，两处均保留：{plan.image.path.name}（{exc}）"
            )
        return f"已归档：{plan.image.path.name} -> {plan.destination_dir.name}"
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        raise


def scan_signature(scan: ScanResult) -> tuple:
    folder_items = tuple(
        sorted(
            (account_id, str(path.resolve()))
            for account_id, path in scan.folder_map.items()
        )
    )
    image_items = tuple(
        (
            image.path.name.casefold(),
            image.account_id,
            image.size,
            image.mtime_ns,
        )
        for image in scan.images
    )
    return folder_items, image_items


def print_preview(
    scan: ScanResult,
    plans: tuple[MovePlan, ...],
    missing: tuple[ImageSnapshot, ...],
    existing: tuple[ImageSnapshot, ...],
) -> None:
    print("\n预检完成，尚未移动任何图片。")
    print(f"识别账号文件夹：{len(scan.folder_map)} 个")
    print(f"识别截图：{len(scan.images)} 张")
    print(f"可归档：{len(plans)} 张")
    print(f"暂未找到账号文件夹：{len(missing)} 张（保留原图）")
    print(f"目标已有同名图片：{len(existing)} 张（保留原图）")
    if scan.unmatched_folder_count:
        print(
            f"忽略不符合 UID数字_A编号... 规范的文件夹："
            f"{scan.unmatched_folder_count} 个"
        )

    if plans:
        print("\n准备归档：")
        for plan in plans:
            print(f"  {plan.image.path.name} -> {plan.destination_dir.name}")
    if missing:
        print("\n未找到对应账号文件夹：")
        for image in missing:
            print(f"  {image.path.name}")
    if existing:
        print("\n目标已有同名图片，绝不覆盖：")
        for image in existing:
            print(f"  {image.path.name}")


def run_organizer(screenshot_dir: Path, account_root: Path) -> None:
    first_scan = scan_all(screenshot_dir, account_root)
    plans, missing, existing = make_plans(first_scan)
    print_preview(first_scan, plans, missing, existing)

    if not plans:
        print("\n没有需要移动的图片。")
        return

    answer = input("\n确认开始安全移动？输入 YES 继续，其他输入取消：").strip()
    if answer.upper() != "YES":
        print("已取消；本次移动 0 张。")
        return

    # Full second preflight immediately before the first move.
    second_scan = scan_all(screenshot_dir, account_root)
    if scan_signature(first_scan) != scan_signature(second_scan):
        raise OrganizerError(
            "确认期间截图或账号文件夹发生变化，二次预检失败；本次移动 0 张。"
        )
    second_plans, second_missing, second_existing = make_plans(second_scan)
    if (
        tuple((p.image.path.name, p.destination_dir.name) for p in plans)
        != tuple((p.image.path.name, p.destination_dir.name) for p in second_plans)
        or tuple(i.path.name for i in missing)
        != tuple(i.path.name for i in second_missing)
        or tuple(i.path.name for i in existing)
        != tuple(i.path.name for i in second_existing)
    ):
        raise OrganizerError(
            "确认期间目标状态发生变化，二次预检失败；本次移动 0 张。"
        )

    moved = 0
    warnings = 0
    print("\n开始安全归档……")
    for plan in second_plans:
        try:
            result = safe_move(plan)
        except Exception as exc:
            print(f"\n【已停止】{exc}")
            print(f"停止前已成功归档 {moved} 张；其余图片未移动。")
            return
        print(result)
        if result.startswith("已复制但"):
            warnings += 1
        else:
            moved += 1

    print(
        f"\n完成：成功归档 {moved} 张，"
        f"两处均保留 {warnings} 张，未覆盖任何已有图片。"
    )


def config_path() -> Path:
    return Path(__file__).resolve().parent / CONFIG_NAME


def load_config() -> tuple[Path, Path] | None:
    path = config_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        screenshot_dir = Path(data["screenshot_dir"])
        account_root = Path(data["account_root"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not screenshot_dir.is_dir() or not account_root.is_dir():
        return None
    return screenshot_dir, account_root


def save_config(screenshot_dir: Path, account_root: Path) -> None:
    data = {
        "screenshot_dir": str(screenshot_dir.resolve()),
        "account_root": str(account_root.resolve()),
    }
    path = config_path()
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp, path)
    except OSError as exc:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
        raise OrganizerError(f"无法保存路径配置：{exc}") from exc


def choose_directory(title: str) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title=title, mustexist=True)
        root.destroy()
        if selected:
            return Path(selected)
        return None
    except Exception:
        print(f"\n{title}")
        entered = input("请输入完整路径（直接回车取消）：").strip().strip('"')
        return Path(entered) if entered else None


def choose_and_save_config() -> tuple[Path, Path] | None:
    print("\n请选择“账号页面截图”所在文件夹。")
    screenshot_dir = choose_directory("选择账号截图所在文件夹")
    if screenshot_dir is None:
        print("已取消选择。")
        return None

    print("\n请选择“直接包含各 UID..._A编号... 账号文件夹”的上级目录。")
    account_root = choose_directory("选择账号文件夹的上级目录")
    if account_root is None:
        print("已取消选择。")
        return None

    validate_directories(screenshot_dir, account_root)
    save_config(screenshot_dir, account_root)
    return screenshot_dir.resolve(), account_root.resolve()


def print_header() -> None:
    print("=" * 58)
    print(f"DouK 账号截图安全归档工具 v{VERSION}")
    print("先完整预检，后安全移动；任何已有文件都不会被覆盖。")
    print("=" * 58)


def main() -> int:
    configure_console()
    print_header()

    try:
        selected = load_config()
        if selected is None:
            selected = choose_and_save_config()
            if selected is None:
                return 0

        while True:
            screenshot_dir, account_root = selected
            print("\n当前配置：")
            print(f"截图目录：{screenshot_dir}")
            print(f"账号目录：{account_root}")
            choice = input("按回车继续，输入 R 重选路径，输入 Q 退出：").strip().upper()
            if choice == "Q":
                return 0
            if choice == "R":
                replacement = choose_and_save_config()
                if replacement is not None:
                    selected = replacement
                continue
            if choice:
                print("输入无效，请按回车、R 或 Q。")
                continue
            break

        run_organizer(screenshot_dir, account_root)
        return 0
    except OrganizerError as exc:
        print(f"\n【已停止】{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:
        print(f"\n【异常停止】{type(exc).__name__}: {exc}")
        print("为保护原图，任务已停止。")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
