# 纯网页上传与构建指南

本项目不要求安装 GitHub Desktop、Git 或 Python。

## 一、上传源码

1. 下载并解压 `DouK-Manager-Source-v0.1.0.zip`。
2. 打开仓库：`https://github.com/loner913/DouK-Manager`。
3. 在空仓库蓝色区域点击 `uploading an existing file`；仓库已有文件时使用 `Add file` → `Upload files`。
4. 打开解压后的 `DouK-Manager-Source-v0.1.0` 文件夹。
5. 选择文件夹里面的全部内容，而不是外层文件夹本身。
6. 将全部内容拖到 GitHub 上传页面。
7. 确认上传列表中包含：

```text
.github/workflows/build-windows.yml
src/douk_manager/...
resources/...
tests/...
.gitignore
DouKManager.spec
main.py
pyproject.toml
README.md
```

8. 页面底部 Commit message 填写：

```text
初始化 DouK 全流程一体化管理器 v0.1.0
```

9. 选择 `Commit directly to the main branch`。
10. 点击 `Commit changes`。

严禁上传数据库、主档、任务设置、Cookie、日志、截图、Excel或备份。

## 二、构建 Windows 便携版

1. 打开仓库顶部 `Actions`。
2. 第一次使用时，如果看到启用提示，点击 `I understand my workflows, go ahead and enable them`。
3. 左侧选择 `构建 Windows 便携版`。
4. 点击右侧 `Run workflow`。
5. Branch 选择 `main`。
6. 再点击绿色 `Run workflow`。
7. 等待任务由黄色变为绿色。
8. 打开该次运行记录。
9. 页面底部 `Artifacts` 下载：

```text
DouK-Manager_Windows_X64
```

10. 下载得到的 Artifact ZIP 解压一次，里面还有 `DouK-Manager_Windows_X64.zip`，再解压一次。
11. 将便携版内容放到：

```text
F:\DouK-Manager
```

12. 双击：

```text
F:\DouK-Manager\DouKManager.exe
```

## 三、首次运行顺序

1. 程序使用预设路径寻找当前 `main.exe`。
2. 检查 `_internal\Volume`、数据库、主档和任务设置。
3. 检查成功后自动创建 `Backups\Startup` 轻量快照，只包含三个关键文件，并按保留上限轮换。
4. 先进入“账号采集”页面，点击“迁移旧Excel/分类/截图”。
5. 确认迁移日志明确显示“settings_master.json 未复制”。
6. 使用“账号任务”页面预览一个小范围，例如 `A1`。
7. 第一轮只创建任务模板，不激活、不下载。
8. 确认预览与任务JSON正确后，再使用正式激活与下载。

## 四、Actions 失败时

不要反复修改正式数据。打开失败的 Actions 记录，把红色步骤名称和错误日志截图保存。源码构建失败不会接触本机的 `Volume`。
