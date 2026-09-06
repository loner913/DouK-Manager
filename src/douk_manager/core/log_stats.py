from __future__ import annotations

from codecs import getincrementaldecoder
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from re import IGNORECASE, compile as re_compile
from types import MappingProxyType
from typing import Iterable, Mapping

from douk_manager.core.download_summary import NativeLogSegment


LOG_STATS_SCHEMA = 1
_READ_CHUNK_SIZE = 64 * 1024
_CANCEL_LINE_INTERVAL = 256
_MAX_BUFFERED_LINE_SIZE = 1024 * 1024
_UTF8_BOM = b"\xef\xbb\xbf"

TS = re_compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
LEVEL = re_compile(r"\[(INFO|WARNING|ERROR|DEBUG|CRITICAL)\]")
ENDPOINTS = (
    "aweme/v1/web/aweme/post/",
    "aweme/v1/web/aweme/detail/",
    "aweme/v1/web/im/user/info/",
    "aweme/v1/web/mix/aweme/",
    "aweme/v1/web/aweme/favorite/",
)
RESP_CODE = re_compile(r"Response Code:\s*(\d{3})")
FIELD_PRESENCE_LINES = ("INFO]:  Params", "INFO]:  URL: ")
FIELD_PRESENCE = (
    "a_bogus",
    "x-secsdk-web-signature",
    "msToken",
    "uifid",
    "webid",
    "screen_width",
    "screen_height",
    "device_platform",
    "version_code",
)
MUST_BE_ABSENT = (
    "sessionid",
    "sid_tt",
    "sid_guard",
    "passport_csrf_token",
    "passport_auth_token",
    "sso_uid_tt",
    "uid_tt",
    "s_v_web_id",
    "verifyFp",
    "Authorization",
)
LEAK_PATTERNS = {
    field: re_compile(r"(?<![A-Za-z0-9_])" + field, IGNORECASE)
    for field in MUST_BE_ABSENT
}
KEYWORDS = {
    "account_start": "开始处理账号",
    "extract_start": "开始提取作品",
    "filtered": "筛选处理后作品",
    "cache_update": "更新缓存数据",
    "download_start": "开始下载作品",
    "skip_video": "跳过视频作品",
    "skip_image": "跳过图集作品",
    "skip_live": "跳过实况作品",
    "retry": "重试",
    "failed_extract": "链接提取失败",
    "batch_fallback": "回退单账号路径",
    "batch_uncertain": "批量账号信息存在不确定返回",
    "cookie_invalid": "Cookie",
    "resp_code_abnormal": "响应码异常",
    "download_interrupted": "下载中断",
    "url_parse_failed": "视频下载地址解析失败",
    "private_account": "私密账号",
}
LOG_STATS_LABELS_ZH: Mapping[str, str] = MappingProxyType(
    {
        "schema": "统计格式版本",
        "lines": "已分析行数",
        "bytes_analysed": "已分析字节数",
        "window": "日志时间窗口",
        "active_minutes": "活跃分钟数",
        "log_location_reason": "日志定位说明",
        "truncated": "是否截断",
        "truncate_reason": "截断原因",
        "INFO": "信息",
        "WARNING": "警告",
        "ERROR": "错误",
        "DEBUG": "调试",
        "CRITICAL": "严重错误",
        "request_failed": "请求失败",
        "private_account": "私密账号",
        "resp_code_abnormal": "响应码异常",
        "download_interrupted": "下载中断",
        "url_parse_failed": "视频下载地址解析失败",
        "unavailable": "不可用",
        "unknown": "未知",
        "request_lines": "请求行数",
        "signature_coverage": "签名字段覆盖率",
        "account_start": "开始处理账号",
        "extract_start": "开始提取作品",
        "filtered": "筛选处理后作品",
        "cache_update": "更新缓存数据",
        "download_start": "开始下载作品",
        "skip_video": "跳过视频作品",
        "skip_image": "跳过图集作品",
        "skip_live": "跳过实况作品",
        "retry": "重试",
        "failed_extract": "链接提取失败",
        "batch_fallback": "回退单账号路径",
        "batch_uncertain": "批量账号信息不确定",
        "cookie_invalid": "Cookie 无效",
    }
)
ABNORMAL_CODE = re_compile(r"Client error '(\d{3})|Server error '(\d{3})")

# No stable, explicitly verified native-log wording exists for these states.
UNAVAILABLE_MARKERS: tuple[str, ...] = ()

_LOCATED_REASONS = frozenset(
    {
        "",
        "未找到本次新增或增长的原生日志。",
        "本次原生日志缺少运行锚点。",
        "存在多个候选原生日志，无法唯一确定本次日志。",
        "仅分析了可用的部分原生日志片段。",
    }
)
_TRUNCATE_REASONS = frozenset({"", "line_limit", "byte_limit"})
_LOCATED_REASONS_EN: Mapping[str, str] = MappingProxyType(
    {
        "": "none",
        "未找到本次新增或增长的原生日志。": "no new or expanded native log was found",
        "本次原生日志缺少运行锚点。": "the native log lacks a run anchor",
        "存在多个候选原生日志，无法唯一确定本次日志。": (
            "multiple candidate native logs prevented unique selection"
        ),
        "仅分析了可用的部分原生日志片段。": (
            "only the available native-log segments were analysed"
        ),
        "unknown": "unknown",
    }
)
_TRUNCATE_REASONS_ZH: Mapping[str, str] = MappingProxyType(
    {
        "": "无",
        "line_limit": "达到分析行数上限",
        "byte_limit": "达到分析字节数上限",
        "unknown": "未知",
    }
)
_TRUNCATE_REASONS_EN: Mapping[str, str] = MappingProxyType(
    {
        "": "none",
        "line_limit": "line limit",
        "byte_limit": "byte limit",
        "unknown": "unknown",
    }
)


def normalise_located_reason(reason: str) -> str:
    return reason if reason in _LOCATED_REASONS else "unknown"


def normalise_truncate_reason(reason: str) -> str:
    return reason if reason in _TRUNCATE_REASONS else "unknown"


def log_stats_label_zh(name: str) -> str:
    return LOG_STATS_LABELS_ZH.get(name, name)


def located_reason_zh(reason: str) -> str:
    normalised = normalise_located_reason(reason)
    if not normalised:
        return "无"
    return "未知" if normalised == "unknown" else normalised


def truncate_reason_zh(reason: str) -> str:
    return _TRUNCATE_REASONS_ZH[normalise_truncate_reason(reason)]


class LogStatsReadError(RuntimeError):
    """A native-log segment could not be read exactly as declared."""


@dataclass(frozen=True)
class HttpStatusStats:
    counts: Mapping[str, int]
    total: int


@dataclass(frozen=True)
class AbnormalStatusStats:
    counts: Mapping[str, int]
    total: int


@dataclass(frozen=True)
class SignatureStats:
    presence: Mapping[str, int]
    request_lines: int


@dataclass(frozen=True)
class FailureStats:
    resp_code_abnormal: int
    download_interrupted: int
    url_parse_failed: int
    private_account: int


@dataclass(frozen=True)
class ReportSelfCheck:
    counts: Mapping[str, int]
    clean: bool
    schema: int = LOG_STATS_SCHEMA


@dataclass(frozen=True)
class LogStats:
    schema: int
    lines: int
    bytes_analysed: int
    window_start: str | None
    window_end: str | None
    active_minutes: int
    levels: Mapping[str, int]
    http: HttpStatusStats
    abnormal: AbnormalStatusStats
    endpoints: Mapping[str, int]
    signatures: SignatureStats
    failures: FailureStats
    keywords: Mapping[str, int]
    truncated: bool
    truncate_reason: str = ""

    @property
    def http_403_rate(self) -> float:
        total = self.http.total + self.abnormal.total
        if not total:
            return 0.0
        count = self.http.counts.get("403", 0) + self.abnormal.counts.get("403", 0)
        return count / total

    @property
    def signature_coverage(self) -> float:
        if not self.signatures.request_lines:
            return 0.0
        return (
            self.signatures.presence.get("x-secsdk-web-signature", 0)
            / self.signatures.request_lines
        )

    @property
    def request_failed(self) -> int:
        abnormal_responses = sum(
            count
            for code, count in self.http.counts.items()
            if code not in {"200", "206"}
        )
        abnormal_path_failures = max(
            self.abnormal.total, self.failures.resp_code_abnormal
        )
        return (
            abnormal_responses
            + abnormal_path_failures
            + self.failures.download_interrupted
            + self.failures.url_parse_failed
        )

    @property
    def unavailable(self) -> int:
        return 0

    @property
    def unknown(self) -> int:
        return 0


class SegmentSelectionStatus(Enum):
    READY = "ready"
    PARTIAL = "partial"
    MISSING_RANGE = "missing_range"
    MISSING_FILE = "missing_file"


class LogStatsLeakError(RuntimeError):
    def __init__(self, check: ReportSelfCheck) -> None:
        hits = [(name, count) for name, count in check.counts.items() if count]
        detail = "、".join(f"{name}×{count}" for name, count in hits)
        super().__init__(
            f"诊断报告导出被拒绝：检测到 {sum(count for _, count in hits)} 个高危字段。"
            f"命中：{detail}。\n（为避免二次泄漏，不显示命中的日志内容。）"
        )
        self.check = check


def analyse_segments(
    segments: tuple[NativeLogSegment, ...],
    *,
    context=None,
    max_lines: int = 5_000_000,
    max_bytes: int = 512 * 1024 * 1024,
) -> LogStats:
    levels: Counter[str] = Counter()
    http_codes: Counter[str] = Counter()
    abnormal_codes: Counter[str] = Counter()
    endpoints: Counter[str] = Counter()
    presence: Counter[str] = Counter()
    keywords: Counter[str] = Counter()
    minutes: set[str] = set()
    lines = 0
    bytes_analysed = 0
    request_lines = 0
    first_ts: str | None = None
    last_ts: str | None = None
    truncated = False
    truncate_reason = ""

    _raise_if_cancelled(context)
    for segment in segments:
        try:
            size = segment.path.stat().st_size
        except OSError as exc:
            raise LogStatsReadError("原生日志片段不可读取。") from exc
        start = max(int(segment.offset), 0)
        length = max(int(segment.length), 0)
        if start + length > size:
            raise LogStatsReadError("原生日志在声明片段结束前提前结束。")
        if not length:
            continue
        if bytes_analysed >= max_bytes:
            truncated = True
            truncate_reason = "byte_limit"
            break

        try:
            handle = segment.path.open("rb")
        except OSError as exc:
            raise LogStatsReadError("原生日志片段不可读取。") from exc
        with handle:
            discard_partial = False
            if start > 0:
                if start == len(_UTF8_BOM):
                    handle.seek(0)
                    starts_after_bom = handle.read(len(_UTF8_BOM)) == _UTF8_BOM
                    if not starts_after_bom:
                        handle.seek(start - 1)
                        discard_partial = handle.read(1) not in (b"\n", b"\r")
                else:
                    handle.seek(start - 1)
                    discard_partial = handle.read(1) not in (b"\n", b"\r")
            handle.seek(start)
            remaining = length
            while discard_partial and remaining > 0:
                _raise_if_cancelled(context)
                budget = max_bytes - bytes_analysed
                if budget <= 0:
                    truncated = True
                    truncate_reason = "byte_limit"
                    break
                chunk = handle.read(min(_READ_CHUNK_SIZE, remaining, budget))
                if not chunk:
                    raise LogStatsReadError("原生日志在声明片段结束前提前结束。")
                newline_index = chunk.find(b"\n")
                consumed = len(chunk) if newline_index < 0 else newline_index + 1
                bytes_analysed += consumed
                remaining -= consumed
                if consumed < len(chunk):
                    handle.seek(consumed - len(chunk), 1)
                if newline_index >= 0:
                    discard_partial = False
            if truncated:
                break
            decoder = getincrementaldecoder("utf-8-sig")(errors="replace")
            buffer = ""
            while remaining > 0:
                _raise_if_cancelled(context)
                budget = max_bytes - bytes_analysed
                if budget <= 0:
                    truncated = True
                    truncate_reason = "byte_limit"
                    break
                chunk = handle.read(min(_READ_CHUNK_SIZE, remaining, budget))
                if not chunk:
                    raise LogStatsReadError("原生日志在声明片段结束前提前结束。")
                remaining -= len(chunk)
                bytes_analysed += len(chunk)
                decoded = decoder.decode(chunk, final=False)
                buffer += decoded
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if lines >= max_lines:
                        truncated = True
                        truncate_reason = "line_limit"
                        break
                    line = line.rstrip("\r")
                    if "\ufffd" in line:
                        raise LogStatsReadError("原生日志包含无法解码的 UTF-8 编码。")
                    if len(line.encode("utf-8")) > _MAX_BUFFERED_LINE_SIZE:
                        raise LogStatsReadError("原生日志单行长度超过解析上限。")
                    (
                        first_ts,
                        last_ts,
                        request_line,
                    ) = _accumulate_line(
                        line,
                        levels=levels,
                        http_codes=http_codes,
                        abnormal_codes=abnormal_codes,
                        endpoints=endpoints,
                        presence=presence,
                        keywords=keywords,
                        minutes=minutes,
                        first_ts=first_ts,
                        last_ts=last_ts,
                    )
                    request_lines += int(request_line)
                    lines += 1
                    if lines % _CANCEL_LINE_INTERVAL == 0:
                        _raise_if_cancelled(context)
                if "\ufffd" in buffer:
                    raise LogStatsReadError("原生日志包含无法解码的 UTF-8 编码。")
                if len(buffer.encode("utf-8")) > _MAX_BUFFERED_LINE_SIZE:
                    raise LogStatsReadError("原生日志单行长度超过解析上限。")
                if truncated:
                    break
            if not truncated and remaining == 0:
                buffer += decoder.decode(b"", final=True)
                if "\ufffd" in buffer:
                    raise LogStatsReadError("原生日志包含无法解码的 UTF-8 编码。")
                if buffer:
                    raise LogStatsReadError("原生日志最后一行未换行，记录不完整。")
        if truncated:
            break

    _raise_if_cancelled(context)
    failures = FailureStats(
        resp_code_abnormal=keywords.get("resp_code_abnormal", 0),
        download_interrupted=keywords.get("download_interrupted", 0),
        url_parse_failed=keywords.get("url_parse_failed", 0),
        private_account=keywords.get("private_account", 0),
    )
    return LogStats(
        schema=LOG_STATS_SCHEMA,
        lines=lines,
        bytes_analysed=bytes_analysed,
        window_start=first_ts,
        window_end=last_ts,
        active_minutes=len(minutes),
        levels=_frozen_counts(levels),
        http=HttpStatusStats(_frozen_counts(http_codes), sum(http_codes.values())),
        abnormal=AbnormalStatusStats(
            _frozen_counts(abnormal_codes), sum(abnormal_codes.values())
        ),
        endpoints=_frozen_counts(endpoints),
        signatures=SignatureStats(_frozen_counts(presence), request_lines),
        failures=failures,
        keywords=_frozen_counts(keywords),
        truncated=truncated,
        truncate_reason=truncate_reason if truncate_reason in _TRUNCATE_REASONS else "",
    )


def select_dashboard_segments(
    segments: Iterable[object],
) -> tuple[tuple[NativeLogSegment, ...], SegmentSelectionStatus]:
    usable: list[NativeLogSegment] = []
    missing_range = False
    missing_file = False
    saw_any = False
    for segment in segments:
        saw_any = True
        offset = getattr(segment, "offset", None)
        length = getattr(segment, "length", None)
        path = Path(getattr(segment, "path"))
        if offset is None or length is None:
            missing_range = True
            continue
        if not path.is_file():
            missing_file = True
            continue
        usable.append(NativeLogSegment(path, int(offset), int(length)))
    if usable:
        status = (
            SegmentSelectionStatus.PARTIAL
            if missing_range or missing_file
            else SegmentSelectionStatus.READY
        )
        return tuple(usable), status
    if missing_range or not saw_any:
        return (), SegmentSelectionStatus.MISSING_RANGE
    return (), SegmentSelectionStatus.MISSING_FILE


def render_report(
    stats: LogStats, *, located_reason: str = "", language: str = "en"
) -> str:
    if language == "en":
        return _render_report_en(stats, located_reason=located_reason)
    if language == "zh-CN":
        return _render_report_zh_cn(stats, located_reason=located_reason)
    raise ValueError("Unsupported diagnostic report language.")


def _render_report_en(stats: LogStats, *, located_reason: str) -> str:
    reason = _LOCATED_REASONS_EN[normalise_located_reason(located_reason)]
    truncate_reason = _TRUNCATE_REASONS_EN[
        normalise_truncate_reason(stats.truncate_reason)
    ]
    metadata_rows = (
        ("schema", stats.schema),
        ("lines", stats.lines),
        ("bytes_analysed", stats.bytes_analysed),
        ("window", f"{stats.window_start or 'none'} -> {stats.window_end or 'none'}"),
        ("active_minutes", stats.active_minutes),
        ("log_location_reason", reason),
        ("truncated", "yes" if stats.truncated else "no"),
        ("truncate_reason", truncate_reason),
    )
    metadata_width = max(len(label) for label, _value in metadata_rows)
    lines = [
        *(f"{label:<{metadata_width}}  {value}" for label, value in metadata_rows),
        "",
        "[log levels]",
    ]
    for level in ("INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"):
        lines.append(f"  {level:<10} {stats.levels.get(level, 0)}")
    lines.extend(("", "[http status]"))
    http_width = len("403 rate")
    for code, count in sorted(stats.http.counts.items()):
        lines.append(f"  {code:<{http_width}}  {count}")
    lines.extend(
        (
            f"  {'TOTAL':<{http_width}}  {stats.http.total}",
            f"  {'403 rate':<{http_width}}  {stats.http_403_rate:.4%}",
            "",
            "[abnormal-path status]",
        )
    )
    abnormal_width = 4
    if stats.abnormal.counts:
        for code, count in sorted(stats.abnormal.counts.items()):
            lines.append(f"  {code:<{abnormal_width}}  {count}")
    else:
        lines.append(f"  {'none':<{abnormal_width}}  0")
    lines.extend(("", "[endpoints seen] (path only, query discarded)"))
    endpoint_width = max(len(endpoint) for endpoint in ENDPOINTS)
    for endpoint in ENDPOINTS:
        lines.append(
            f"  {endpoint:<{endpoint_width}}  {stats.endpoints.get(endpoint, 0)}"
        )
    lines.extend(("", "[signature / param field PRESENCE] (counts only)"))
    for field in FIELD_PRESENCE:
        lines.append(f"  {field:<26} {stats.signatures.presence.get(field, 0)}")
    lines.extend(
        (
            f"  request_lines              {stats.signatures.request_lines}",
            f"  signature_coverage         {stats.signature_coverage:.4%}",
            "",
            "[failure classification]",
            f"  request_failed             {stats.request_failed}",
            f"  private_account            {stats.failures.private_account}",
            "  unavailable                0",
            "  unknown                    0",
            "  This version does not infer whether accounts are deactivated or banned.",
            "",
            "[business keywords]",
        )
    )
    for name in KEYWORDS:
        lines.append(f"  {name:<26} {stats.keywords.get(name, 0)}")
    lines.extend(("", "[LEAK SELF-CHECK] all values below MUST be 0"))
    for field in MUST_BE_ABSENT:
        lines.append(f"  {field:<24} 0")
    lines.extend(("", "RESULT: CLEAN", ""))
    return "\n".join(lines)


def _render_report_zh_cn(stats: LogStats, *, located_reason: str) -> str:
    metadata_rows = (
        (log_stats_label_zh("schema"), stats.schema),
        (log_stats_label_zh("lines"), stats.lines),
        (log_stats_label_zh("bytes_analysed"), stats.bytes_analysed),
        (
            log_stats_label_zh("window"),
            f"{stats.window_start or '无'} -> {stats.window_end or '无'}",
        ),
        (log_stats_label_zh("active_minutes"), stats.active_minutes),
        (log_stats_label_zh("log_location_reason"), located_reason_zh(located_reason)),
        (log_stats_label_zh("truncated"), "是" if stats.truncated else "否"),
        (
            log_stats_label_zh("truncate_reason"),
            truncate_reason_zh(stats.truncate_reason),
        ),
    )
    lines = [
        *(f"{label}：{value}" for label, value in metadata_rows),
        "",
        "[日志级别]",
    ]
    for level in ("INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL"):
        lines.append(f"  {log_stats_label_zh(level)}：{stats.levels.get(level, 0)}")
    lines.append("")
    lines.append("[HTTP 响应状态]")
    for code, count in sorted(stats.http.counts.items()):
        lines.append(f"  {code}：{count}")
    lines.extend(
        (
            f"  总计：{stats.http.total}",
            f"  403 比例：{stats.http_403_rate:.4%}",
            "",
            "[异常路径状态]",
        )
    )
    if stats.abnormal.counts:
        for code, count in sorted(stats.abnormal.counts.items()):
            lines.append(f"  {code}：{count}")
    else:
        lines.append("  无：0")
    lines.extend(("", "[已出现的请求端点]（仅路径，查询参数已丢弃）"))
    for endpoint in ENDPOINTS:
        lines.append(f"  {endpoint}：{stats.endpoints.get(endpoint, 0)}")
    lines.extend(("", "[签名和参数字段存在情况]（仅计数）"))
    for field in FIELD_PRESENCE:
        lines.append(f"  {field}  {stats.signatures.presence.get(field, 0)}")
    lines.extend(
        (
            f"  {log_stats_label_zh('request_lines')}：{stats.signatures.request_lines}",
            f"  {log_stats_label_zh('signature_coverage')}：{stats.signature_coverage:.4%}",
            "",
            "[失败分类]",
            f"  {log_stats_label_zh('request_failed')}：{stats.request_failed}",
            (
                f"  {log_stats_label_zh('private_account')}："
                f"{stats.failures.private_account}"
            ),
            f"  {log_stats_label_zh('unavailable')}：0",
            f"  {log_stats_label_zh('unknown')}：0",
            "  本版本不从日志推断账号是否已注销或被封。",
            "",
            "[业务事件]",
        )
    )
    for name in KEYWORDS:
        lines.append(f"  {log_stats_label_zh(name)}：{stats.keywords.get(name, 0)}")
    lines.extend(("", "[泄漏自检] 以下值必须全部为 0"))
    for field in MUST_BE_ABSENT:
        lines.append(f"  {field:<24} 0")
    lines.extend(("", "RESULT: CLEAN", ""))
    return "\n".join(lines)


def self_check_text(text: str) -> ReportSelfCheck:
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        if _is_safe_structural_field_line(line):
            continue
        for field, pattern in LEAK_PATTERNS.items():
            counts[field] += sum(1 for _ in pattern.finditer(line))
    complete = {field: counts.get(field, 0) for field in MUST_BE_ABSENT}
    return ReportSelfCheck(
        counts=MappingProxyType(complete),
        clean=not any(complete.values()),
    )


def _is_safe_structural_field_line(line: str) -> bool:
    stripped = line.strip()
    for field in MUST_BE_ABSENT:
        if re_compile(rf"^{field}\s+0$", IGNORECASE).fullmatch(stripped) or (
            field in FIELD_PRESENCE
            and re_compile(rf"^{field}\s+\d+$", IGNORECASE).fullmatch(stripped)
        ):
            return True
    return False


def _accumulate_line(
    line: str,
    *,
    levels: Counter[str],
    http_codes: Counter[str],
    abnormal_codes: Counter[str],
    endpoints: Counter[str],
    presence: Counter[str],
    keywords: Counter[str],
    minutes: set[str],
    first_ts: str | None,
    last_ts: str | None,
) -> tuple[str | None, str | None, bool]:
    matched = TS.match(line)
    if matched:
        minute = f"{matched.group(1)} {matched.group(2)}:{matched.group(3)}"
        first_ts = first_ts or minute
        last_ts = minute
        minutes.add(minute)
    matched = LEVEL.search(line)
    if matched:
        levels[matched.group(1)] += 1
    matched = RESP_CODE.search(line)
    if matched and len(matched.group(1)) == 3 and matched.group(1).isdigit():
        http_codes[matched.group(1)] += 1
    matched = ABNORMAL_CODE.search(line)
    if matched:
        code = matched.group(1) or matched.group(2)
        if len(code) == 3 and code.isdigit():
            abnormal_codes[code] += 1
    request_line = any(marker in line for marker in FIELD_PRESENCE_LINES)
    if "INFO]:  URL: " in line:
        for endpoint in ENDPOINTS:
            if endpoint in line:
                endpoints[endpoint] += 1
                break
    if request_line:
        for field in FIELD_PRESENCE:
            if field in line:
                presence[field] += 1
    for name, needle in KEYWORDS.items():
        if needle in line:
            keywords[name] += 1
    return first_ts, last_ts, request_line


def _raise_if_cancelled(context) -> None:
    if context is not None:
        context.raise_if_cancelled()


def _frozen_counts(counts: Mapping[str, int]) -> Mapping[str, int]:
    return MappingProxyType(dict(counts))
