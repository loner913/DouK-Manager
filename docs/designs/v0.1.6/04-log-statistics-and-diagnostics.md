# 04：日志统计与诊断

## 目标与非目标

目标是为当前任务和历史 `DownloadTask_*.log` 提供可靠日志区间上的只读统计，并从缓存的统计结果生成可安全分享的中英文诊断文本。

非目标包括：读取任意用户指定的第二份日志、修改任务日志或下载器原生日志、把统计推断为账号封禁/注销结论、把完整 URL 查询参数或敏感字段写入报告，以及在日志区间缺失时猜测偏移和长度。

## 用户入口与数据流

1. 当前任务入口在汇总完成后可按界面设置排队自动分析；历史任务由结果看板选择单个已完成任务后手动分析。自动分析默认关闭，并且不会在关闭流程中提交。
2. `download_summary` 只把本次原生日志定位得到的精确 `path/offset/length` 传给统计层；结果看板从 `DownloadTask_*.log` 建立只读索引和选择快照，不在索引阶段读取原生日志内容。
3. 统计层按声明的偏移和长度读取区间，只丢弃区间开头的半行，严格处理 EOF、UTF-8 和未结束行，聚合固定白名单字段，返回不可变的 `LogStats`。
4. 控制器把成功统计保存在进程内缓存；导出从缓存结果渲染中文或英文报告，先对最终文本执行泄漏自检，只有自检 clean 才原子写出诊断文件。

## 核心数据结构与状态

- `LogStats` 保存 schema、行数、已分析字节、时间窗口、活跃分钟数、日志级别、HTTP 状态、异常路径状态、白名单端点、签名字段存在计数、失败分类、业务关键词及截断状态。
- 统计层只保留固定端点、三位 HTTP 状态码、固定日志级别、固定签名/参数字段存在情况和固定业务关键词；查询参数被丢弃，签名只计数不保存值。
- `SegmentSelectionStatus` 明确 `READY`、`PARTIAL`、`MISSING_RANGE` 和 `MISSING_FILE`。有可用片段但另有缺失时返回 partial；全部缺少可靠区间或文件时明确不可用。
- `ReportSelfCheck` 记录固定高危字段计数和 `clean`；`LogStatsLeakError` 在命中时拒绝导出，错误信息也不回显日志中的实际值。

## 必须保持的安全不变量

- 只有同时具备合法偏移/长度且文件存在的片段才能被选择；选择结果必须来自受管理的任务日志定位范围，不能借任意路径读取第二来源。
- 读操作不修改任务日志、原生日志或结果看板源目录；统计缓存不成为第二业务结果源。
- 报告只输出固定白名单字段。`sessionid`、`sid_tt`、`sid_guard`、Cookie、Authorization 等敏感字段以及原始字段值不能进入最终文本。
- 写盘前必须对最终英文/中文文本各执行一次自检；任一报告失败，双语导出都不得留下最终或部分文件。导出不重新读取原生日志。
- `request_failed` 只表示统计到的请求/解析/中断失败；`unavailable` 与 `unknown` 不被伪造为账号健康结论，系统明确不从日志推断封禁或注销。

## 失败、取消、恢复与降级路径

- 旧日志没有偏移/长度时返回 `MISSING_RANGE`；新旧片段混合时保留可用片段并标记 `PARTIAL`，不补猜缺失区间。文件不存在时返回 `MISSING_FILE` 或 partial。
- 声明区间超出文件、读取失败、非法 UTF-8 或区间末尾没有完整行时抛出固定读取错误；行数/字节上限达到时显式标记 `truncated` 与原因。
- 分析或导出取消会停在协作检查点，删除临时文件且不写最终诊断；自动分析失败/取消只更新界面状态，不自动导出。
- 报告自检命中任一高危字段时，拒绝写入并只报告字段类别/计数；不会在异常消息中回显敏感内容。
- 结果看板对运行中、部分汇总或指纹变化的任务保持不可用/需刷新，过期后台结果不会覆盖新的选择。

## 兼容性边界

当前任务和历史任务均以 `DownloadTask_*.log` 为业务结果来源；历史日志可继续查看已有汇总，但缺少可靠原生日志区间时，深度原生日志统计明确不可用。报告支持固定的 `en` 和 `zh-CN` 渲染，不扩展到未经白名单验证的字段或语言。旧结果不会被回填成未知细节，也不会因正常退出码单独变成账号成功。

## 对应源码与测试

- 源码：[log_stats.py](../../../src/douk_manager/core/log_stats.py)、[download_summary.py](../../../src/douk_manager/core/download_summary.py)、[result_dashboard.py](../../../src/douk_manager/core/result_dashboard.py)、[controller.py](../../../src/douk_manager/controller.py)、[gui.py](../../../src/douk_manager/gui.py)
- 测试：[test_log_stats.py](../../../tests/test_log_stats.py)、[test_result_dashboard.py](../../../tests/test_result_dashboard.py)
- 关键测试包括 `test_nonzero_offset_and_multiple_segments_only_count_selected_bytes`、`test_declared_segment_beyond_file_raises_fixed_read_error`、`test_invalid_utf8_and_incomplete_last_line_raise_fixed_read_error`、`test_sensitive_input_is_accepted_but_values_cannot_reach_report`、`test_all_eleven_forbidden_output_categories_are_individually_absent`、`test_product_self_check_detects_bad_output_without_echoing_values`、`test_current_and_dashboard_sources_use_the_same_analysis`、`test_analyse_current_run_then_export_without_rereading_native_log`、`test_dual_export_blocks_both_files_when_either_report_fails_self_check`、`test_history_analysis_is_manual_single_selection_and_never_auto_submitted`、`test_auto_analysis_submits_once_after_summary_removal_but_not_during_closing` 和 `test_old_count_only_log_keeps_missing_fields_unknown`。

## 已完成的验收证据

阶段 D 的 D-0 至 D-25 验收为 PASS；完整回归保留 `608 total / 606 passed / 2 skipped / 0 failed / 0 errors`。测试覆盖精确区间、多片段顺序、缺失区间/文件、行字节上限、取消、固定白名单、全量脱敏、写盘前自检、缓存导出、双语原子边界、当前/历史任务入口和旧日志未知边界。
