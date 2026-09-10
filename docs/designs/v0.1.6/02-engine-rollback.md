# 02：下载引擎回退

## 目标与非目标

目标是从受管理的历史引擎回退点中选择一个经过结构和完整性检查的程序版本，替换引擎程序文件，并保留当前正式 `Volume`、数据库、settings 及其业务内容。

非目标包括：把任意路径当作回退来源、覆盖或迁移 `Volume`、用一次确认绕过完整性拒绝、删除其他历史回退点，以及在恢复失败时假装事务成功。

## 用户入口与数据流

1. 用户从引擎更新/回退入口请求预览；控制器和 `EngineUpdateService` 先确认运行目录、正式 `main.exe`、`_internal` 与 `Volume` 的关系。
2. 服务只扫描受管理的 `EngineRollback/<stamp>` 和 `EngineSuperseded/<stamp>` 历史目录，分别识别 `update-manifest.json` 与 `rollback-manifest.json`，并按来源记录回退点。
3. 预览对候选进行结构、清单、关键文件和哈希检查，展示可用、带警告或拒绝的状态；预览本身不移动文件。
4. 应用阶段在临界写入前重新检查进程、路径和哈希，创建 `BeforeEngineRollback` 完整备份点，再移动旧引擎、候选引擎和正式 `Volume`，最后复读并校验结果。

## 核心数据结构与状态

- `EngineRollbackPoint` 保存来源、时间戳、清单名、归档/主程序哈希、主程序大小、内部文件数量、清单存在性、观察到的哈希和 `RollbackIntegrity`。
- 完整性状态包括 `OK`、`NO_MANIFEST`、`MISSING_MAIN`、`MISSING_INTERNAL`、`HASH_MISMATCH`、`CONTAINS_VOLUME` 和 `UNREADABLE`。
- `rollback_can_apply` 是唯一应用门：`OK` 为允许，`NO_MANIFEST` 为带警告且要求第二次确认，其余状态全部拒绝。
- `EngineRollbackPreview` 表达当前程序与候选的比较、关键文件和 `volume_stays=True`；`EngineRollbackResult` 记录备份、被替代目录、恢复哈希和清单验证结果。

## 必须保持的安全不变量

- 候选只能来自受管理的回退或已被替代引擎历史；不接受任意外部路径作为回退点。
- 候选必须具备 `main.exe` 和 `_internal`，结构可读，且不能在候选 `_internal` 中包含 `Volume`；清单存在时哈希必须与清单一致。
- 正式 `Volume` 的位置必须恰好是当前引擎 `_internal/Volume`；回退只替换程序文件，正式 `Volume` 被移动回当前目录，不能被候选数据覆盖。
- `settings_master.json`、`settings.json`、`DouK-Downloader.db` 等关键数据在回退前后必须通过校验；`encipher.py` 等不应被候选覆盖的旁路文件保持原有边界。
- 临界阶段开始后取消请求不能打断已开始的不可分割事务；任何验证失败都必须进入恢复路径并如实报告。

## 失败、取消、恢复与降级路径

- 预览会保留被拒绝的候选及固定原因，但拒绝原因不向界面泄露完整路径；二次确认不能强制通过拒绝状态。
- 无清单但结构完整的候选可以在警告下应用，但必须第二次确认，成功结果只报告实际观察到的哈希和大小，不冒充清单验证。
- 备份完成前取消只完成安全备份，不发生正式引擎移动；临界阶段之后的迟到取消不改变回退结果。
- 应用移动失败、哈希/JSON/SQLite 复核失败时，服务恢复原引擎、正式 `Volume` 和候选来源；恢复失败会同时报告备份点和 `EngineSuperseded` 位置。
- 恢复和清理都受 `Volume` 守卫保护：如果目标 `_internal` 仍含有 `Volume`，禁止直接删除，以免把业务数据当作程序残留清掉。

## 兼容性边界

无清单的旧回退点只获得明确的警告路径；缺少主程序/内部目录、哈希不符、不可读或含 `Volume` 的点不兼容且不可强制。历史回退点会继续留存并可作为后续来源，不因一次成功回退而清空。

## 对应源码与测试

- 源码：[engine_update.py](../../../src/douk_manager/core/engine_update.py)、[controller.py](../../../src/douk_manager/controller.py)、[gui.py](../../../src/douk_manager/gui.py)
- 测试：[test_engine_rollback.py](../../../tests/test_engine_rollback.py)、[test_engine_update.py](../../../tests/test_engine_update.py)
- 关键测试包括 `test_can_apply_is_the_single_gate_for_all_integrity_states`、`test_preview_returns_rejected_point_without_writing`、`test_apply_preserves_volume_and_sidecar_and_creates_superseded`、`test_no_manifest_post_move_hash_mismatch_restores_engine_volume_and_source`、`test_sqlite_verification_failure_restores_engine_and_volume`、`test_volume_guard_refuses_to_delete_internal_containing_volume`、`test_cancel_after_backup_completes_backup_without_formal_moves` 和 `test_late_cancellation_after_critical_phase_does_not_interrupt_rollback`。

## 已完成的验收证据

阶段 D 的 D-0 至 D-25 验收为 PASS；完整回归保留 `608 total / 606 passed / 2 skipped / 0 failed / 0 errors`。回退测试覆盖候选来源排序、所有完整性状态、单/双确认、Volume 与旁路文件保留、备份点、移动失败、复读校验失败、恢复失败报告、取消窗口及历史点留存。
