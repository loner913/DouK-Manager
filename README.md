# DouK 全流程一体化管理器

面向 Windows X64 的便携式自用管理程序，统一管理：

- DouK 账号采集器本机服务；
- `settings_master.json` 与任务版 `settings.json`；
- `A1,A10-A99,A879` 形式的账号选择；
- 固定数量或自定义范围的批次任务；
- DouK-Downloader 下载引擎调用；
- 账号截图安全归档；
- 视频账号文件夹快捷方式索引；
- 启动前、修改前和下载前只备份 3 个关键文件并自动限制保留数量；下载引擎更新前完整备份 Volume；
- 管理器日志和下载任务日志。
- 下载引擎 ZIP 只读预检、安全更新和旧引擎程序永久留存；更新时移动保留唯一正式 Volume。

下载器退出码 `0` 只代表进程正常结束。账号可能因接口错误、私密状态、没有新作品
或防重复记录而没有产生下载结果，管理器不会把进程正常退出误报为账号下载成功。

## 任务模板怎样复用

- `F:\DouK-Manager\Data\Tasks\*.json` 是永久保留、可反复使用的完整任务模板；
- 下载器不会直接读取 `Data\Tasks`，它始终只读取唯一正式
  `_internal\Volume\settings.json`；
- “将勾选任务设为正式 settings.json”会先备份 3 个关键文件，再把一个模板复制为正式文件；
- “按顺序运行勾选任务”会逐个执行“备份 → 激活为正式 settings.json → 启动下载”；
- “运行当前正式 settings.json”不使用任务列表，直接运行当时已经激活的正式文件；
- 下载结束后的黑框可以选择保留以查看统计，也可以在无人值守队列中选择自动关闭。

## 重要安全原则

1. 唯一正式数据始终位于下载引擎的 `_internal\Volume`。
2. `settings_master.json` 的 `enable` 不会因任务选择而改变。
3. 任务选择只在任务版 `settings.json` 中设置 `enable`。
4. 只有用户明确勾选时，才会把所选账号的 `earliest` 写回主档。
5. 所有正式 JSON 使用“临时文件 + 校验 + 原子替换”。
6. 所有写入和下载启动前必须先完成关键文件备份；完整 Volume 备份只在手动确认或更新引擎前执行。
7. 更新下载引擎时禁止覆盖 `_internal\Volume`。
8. 本仓库禁止提交数据库、主档、任务配置、Cookie、截图、日志和备份。
9. 旧采集器 Excel 只补齐尾部缺失账号；既有行即使链接写法不同也原样保留。
10. 同名任务模板自动递增编号，不覆盖旧模板。

## 默认正式路径

```text
下载引擎：F:\DouK-Downloader_Custom_50_150\DouK-Downloader_Windows_X64_20260626\main.exe
视频目录：F:\DouK-Downloader
索引目录：F:\Douk videos
运行目录：F:\DouK-Manager
```

所有路径都可以在管理器设置页面修改。正式 `Volume` 路径由下载引擎路径推导，不能单独指向另一份主档。

## 账号表达式示例

```text
A1,A10-A99,A879
1,10-99,879
A1，A10-A99，A879
A1-A250
```

编号按 `accounts_urls` 的数组位置固定映射，不会因为空 URL 而重新编号。

## 本地源码运行

需要 Python 3.12：

```powershell
python -m pip install -e .
python main.py
```

运行自动测试：

```powershell
python -m unittest discover -s tests -v
```

## Windows 便携版构建

仓库包含 `.github/workflows/build-windows.yml`。上传到 GitHub 后：

1. 打开仓库 `Actions`；
2. 选择“构建 Windows 便携版”；
3. 点击 `Run workflow`；
4. 构建完成后下载 `DouK-Manager_Windows_X64` Artifact；
5. 解压到 `F:\DouK-Manager`；
6. 双击 `DouKManager.exe`。

编译产物、运行数据和正式账号资料都不会写入源码仓库。

## 当前阶段

首个工程版本包含安全底座、配置任务、批次生成、下载调用、采集服务入口、截图归档和索引脚本入口。兼容版下载引擎可读取管理器设置的“每批账号数/暂停秒数”；旧版 EXE 仍按其自身内置值执行。
