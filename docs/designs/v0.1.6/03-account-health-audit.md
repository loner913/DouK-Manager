# 03：账号健康审计

## 目标与非目标

目标是把账号数组位置、历史任务结果、可选原生日志证据和当前 `enable` 状态整理成可复核的健康审计报告，并让用户在确认后以可恢复方式改变主档启用状态。

非目标包括：凭请求失败推断账号被封或注销、把重复身份自动合并、删除账号数组元素、重排 A 编号、让旧模板绕过主档停用、默认深度扫描原生日志，或把内部身份取证值暴露给用户和持久化状态。

## 用户入口与数据流

1. 用户从账号健康审计入口请求只读报告；服务读取 `settings_master.json`、`DownloadTask_*.log` 历史汇总和现有审计旁路状态。
2. 默认只依据任务汇总建立按 A 编号分组的历史；原生日志分析复选框默认关闭，只有用户明确开启且存在精确可靠区间时才读取 `paths.volume / "Log"`。
3. 深度扫描重新验证每个声明的路径、偏移和长度，只把可靠且完整的原生日志结果合并回对应任务；同时使用不可逆身份 token 复核重复和冲突。
4. 服务生成只读 `AccountAuditReport` 和逐账号 `AccountAuditEntry`。用户提交 `AuditDecision` 后先预览变化，再在临界写入中备份、校验主档指纹并原子写入。

## 核心数据结构与状态

- 身份状态为 `CONFIRMED`、`CONFLICT`、`DUPLICATE`、`UNRESOLVED`；可达性状态为 `REACHABLE`、`UNAVAILABLE`、`REQUEST_FAILED`、`UNKNOWN`；隐私状态为 `PUBLIC`、`PRIVATE`、`UNKNOWN`。
- `IdentityObservation` 只保存 A 编号和内部身份 token。`profile_identity_token` 只为符合边界的 Douyin 用户 URL 生成 SHA-256 token；原始账号 ID 不进入条目、原因或 `audit-state.json`。
- `AccountEvidence` 保存运行数量、状态计数、末端错误/私密/无可用作品连续段和安全的 `DownloadTask_...` 标识；默认连续错误阈值为 5，最低证据轮数为 3。
- `AccountAuditReport` 保存主档哈希、账号总数、条目、扫描范围、重复组、警告和原生日志扫描计数。`Disposition` 包括 `ENABLED`、`PERMANENTLY_DISABLED` 和 `PENDING_REVIEW`。
- `AuditApplyPreview` 明确待停用、待启用、待复核、未变化、启用数以及写入前后数组长度；`AuditApplyResult` 记录备份、旁路状态和最终主档哈希。

## 必须保持的安全不变量

- 身份取证只用于相等、重复和冲突判断；同一 A 编号出现多个 token 是冲突，不同 A 编号共享稳定 token 是重复，两者都覆盖自动停用建议。
- 请求失败不等于账号不可用；当前没有获批准的稳定原生日志 unavailable marker，因此错误连续段不能伪装成注销或封禁证据。私密、无可用作品和不充分证据按各自状态复核。
- “墓碑式永久停用”是主档 `accounts_urls` 对应位置保留、只将 `enable` 设为 `false` 的策略：不删除、不重排、不复用位置。`PENDING_REVIEW` 只保留旁路复核状态，不擅自改变主档启用值。
- 新建任务和激活旧模板都以最新主档为权威：活动 `enable` 等于主档 `enable AND` 模板 `enable`；旧模板不能重新启用主档墓碑位置。
- 应用决定必须保持数组长度和每个位置的非 `enable` 字段；至少保留一个启用账号；主档与审计旁路状态都要复读验证。

## 失败、取消、恢复与降级路径

- 未设置证据起点、历史行无法确认 A 编号、缺少原生日志目录或精确区间时，报告保留警告并明确无证据/不可用，不猜测性排除或补齐。
- 原生日志扫描只接受位于 `Volume/Log` 内、后缀和区间合法的文件；路径越界、区间不完整或解析不可靠的轮次不会替换任务汇总结果。
- 报告过期、主档指纹变化、决定重复/越界，或决定会停用全部账号时，预览/应用拒绝且不写旁路状态。
- 写入前创建 `BeforeAccountAudit` 完整备份；临界写入失败或主档/旁路复读失败时恢复主档和之前的审计状态。临界写入前取消保持主档不变。
- 控制器在引擎或采集服务运行时阻止写入；只读报告仍可给出固定警告，不能借审计绕过运行期安全门。

## 兼容性边界

旧任务日志缺少可验证原生日志区间时仍可进入任务汇总，但不能获得深度扫描结论；原生日志深度扫描默认关闭。旧任务模板可以继续使用，但激活时重新读取最新主档，因此不能恢复已经墓碑式停用的账号，也不会重排 A 编号。

## 对应源码与测试

- 源码：[account_audit.py](../../../src/douk_manager/core/account_audit.py)、[account_identity.py](../../../src/douk_manager/core/account_identity.py)、[result_history.py](../../../src/douk_manager/core/result_history.py)、[settings_tasks.py](../../../src/douk_manager/core/settings_tasks.py)、[controller.py](../../../src/douk_manager/controller.py)、[gui.py](../../../src/douk_manager/gui.py)
- 测试：[test_account_audit.py](../../../tests/test_account_audit.py)、[test_settings_tasks.py](../../../tests/test_settings_tasks.py)
- 关键测试包括 `test_identity_duplicate_and_conflict_are_separate_and_deterministic`、`test_native_identity_conflict_never_leaks_raw_id_to_report_or_sidecar`、`test_native_log_analysis_rechecks_exact_historical_segments`、`test_native_log_analysis_never_reads_a_declared_path_outside_volume_log`、`test_five_consecutive_errors_suggest_disable_with_safe_run_ids`、`test_identity_risk_overrides_consecutive_error_disable_suggestion`、`test_apply_changes_only_enable_preserves_positions_and_writes_safe_state`、`test_pending_disposition_round_trips_without_changing_master_enable`、`test_stale_master_fingerprint_rejects_without_writing`、`test_verification_failure_restores_master_from_full_backup`、`test_old_template_cannot_reenable_master_tombstone` 和 `test_master_enable_remains_authoritative_over_sidecar_trace`。

## 已完成的验收证据

阶段 D 的 D-0 至 D-25 验收为 PASS；完整回归保留 `608 total / 606 passed / 2 skipped / 0 failed / 0 errors`。测试覆盖身份取证脱敏、重复/冲突优先级、五次错误阈值、私密和无可用作品复核、原生日志默认关闭与精确边界、墓碑写入、旧模板合并、过期拒绝、取消和失败恢复。
