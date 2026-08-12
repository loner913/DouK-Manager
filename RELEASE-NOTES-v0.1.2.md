# DouK 全流程一体化管理器 v0.1.2 开发构建

## 版本定位

v0.1.2 是 `develop` 上的日志整理开发构建，不创建 Tag 或 Release，也不进入 `main`。`v0.1.0` Tag 和 Release 保持不变。

## 日志目录

修改后产生的新日志统一写入管理器便携目录的五个英文子目录：

- `Logs\Manager`：`DouKManager_*.log`；
- `Logs\Collector`：`Collector_*.log`；
- `Logs\DownloadTasks`：`DownloadTask_*.log`；
- `Logs\IndexRefresh`：`Refresh-DoukIndex-RunLog_*.txt`；
- `Logs\IndexCleanup`：`Cleanup-Broken-Shortcut-Report_*.txt`。

索引脚本通过显式日志目录写入管理器日志区，不再向索引目录的 `Logs` 写入新日志。文件名前缀和时间排序方式保持原有风格。

## 旧日志

本版本不扫描、迁移、复制、移动、重命名或删除任何旧日志。旧日志由用户手动复制到对应子目录。

## 安全边界

本次修改不改变索引快捷方式的创建、更新和删除范围，也不修改数据库、主档、当前任务配置、采集锁、下载引擎单实例或更新保护逻辑。自动测试继续使用临时目录和合成数据。

## 回滚

停止管理器、采集器和下载引擎后，恢复此前保留的便携文件即可。新版本已经生成的五个日志子目录可以保留，不影响旧版本运行。
