# DouK 全流程一体化管理器 v0.1.6

## 版本定位

v0.1.6 在已验收的 v0.1.5 基础上整合 mark 归类、日志安全统计、下载引擎回退和账号健康审计四项功能。
阶段 D 最终候选已完成 Windows 前台人工验收；本文记录已完成实现和集成边界。
V0.1.6 已通过 `feature/v0.1.6-final` 和 PR #9 进入 `develop` 集成流程。
本流程不创建或移动 Tag、不创建 GitHub Release，`main` 保持不变。

最终发布准备分支：`feature/v0.1.6-final`

功能冻结基点：`c7b90721c3c171193545d80fb9dc6aa4b7305f4d`（历史功能冻结记录，不是当前分支 HEAD）。

设计记录：[总索引](docs/designs/v0.1.6/README.md) ·
[mark 归类](docs/designs/v0.1.6/01-mark-classification.md) ·
[引擎回退](docs/designs/v0.1.6/02-engine-rollback.md) ·
[账号健康审计](docs/designs/v0.1.6/03-account-health-audit.md) ·
[日志统计与诊断](docs/designs/v0.1.6/04-log-statistics-and-diagnostics.md)

## 功能范围

### 01：35 项 mark 归类修复

- 保留原始 mark；
- 只从冻结计划生成经过验证的单向别名；
- 别名与原 mark 或其他别名发生碰撞时拒绝别名；
- 不对原生日志文本做模糊归一化。

### 04：日志安全统计与全量脱敏

- 支持本次会话任务和历史任务的日志统计；
- 统计只保留白名单字段；
- 最终写盘文本执行泄漏自检；
- 诊断导出不重新读取原始日志。

### 02：下载引擎回退

- 回退前执行结构、清单和哈希预检；
- 无清单但结构完整的来源需要明确提示并二次确认；
- 缺失主程序、缺失内部目录、包含 Volume、哈希不符或不可读时禁止回退；
- 失败恢复保留原有安全边界，不自动删除历史副本。

### 03：账号健康审计与生命周期管理

- 身份冲突和重复身份进入人工复核；
- 永久停用采用墓碑模型，不删除、不重排、不复用账号数组位置；
- 任务模板启用状态与主档永久停用状态按 `主档 enable AND 模板 enable` 合并；
- 原生日志深度扫描默认关闭，由用户显式开启。

## 验收与回归

- 阶段 D：D-0 至 D-25 的最终候选记录为 PASS；
- 完整回归：`608 total / 606 passed / 2 skipped / 0 failed / 0 errors`；
- E-0 最小版本测试：PASS；
- `compileall`：PASS；
- `git diff --check`：PASS；
- 敏感数据边界检查：PASS；
- 未使用正式账号、Cookie、Token、数据库、原生日志或正式下载引擎数据。

## 版本身份与 Artifact

- `pyproject.toml` 和 `douk_manager.__version__`：`0.1.6`；
- Windows `BUILD-INFO.txt`：`Version: 0.1.6`；
- Artifact：`DouK-Manager_Windows_X64-v0.1.6-<ref>-run-<number>-<sha>`；
- Artifact 同时包含 ZIP 和 `DouK-Manager_Windows_X64.zip.sha256`；
- SHA-256 文件使用 ZIP 文件的十六进制校验值和文件名生成。

## 构建流程

`.github/workflows/build-windows.yml` 保留现有 `actions/*@v7`，使用 Windows runner 和 Python 3.12。
本次 PR 阶段只运行适用于 pull request 的 `Test` 作业；PR 合并后的 `develop` push 才运行正式 Windows
便携版打包，并生成 ZIP 和 SHA-256 校验文件。

本流程不手动触发 portable package，不创建或移动 Tag，不创建 GitHub Release；`main` 保持不变。
workflow 本身不自动 push、创建 Tag 或发布 Release。

## 安全边界与升级

- 不迁移、不覆盖正式 `_internal\Volume`、数据库、settings、Cookie、账号日志或原始下载器日志；
- 既有 v0.1.5 历史记录和发布对象保持不变；
- V0.1.4 和 V0.1.5 缺少的正式设计文档本次不追溯编造，作为后续独立文档债处理；
- 便携版验收应使用隔离目录，不替换正式运行目录；
- 如需回滚，只恢复管理器程序文件，不回滚或迁移 Volume、数据库和正式业务配置。
