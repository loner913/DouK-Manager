param(
    [string]$SourceRoot = 'F:\DouK-Downloader',
    [string]$IndexRoot = 'F:\Douk videos',
    [Parameter(Mandatory)]
    [string]$LogRoot,
    [switch]$OpenIndexFolderAfterRun
)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

function Get-NormalizedFullPath {
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "路径不能为空。"
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath.Length -gt 3) {
        return $fullPath.TrimEnd('\')
    }
    return $fullPath
}

function Test-PathIsStrictChildOfRoot {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )

    $fullPath = Get-NormalizedFullPath -Path $Path
    $fullRoot = Get-NormalizedFullPath -Path $Root
    $rootPrefix = if ($fullRoot.EndsWith('\')) { $fullRoot } else { $fullRoot + '\' }
    return $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

$ManagedTag = '[DoukIndex]'
$CleanupReportFilePrefix = 'Cleanup-Broken-Shortcut-Report'

$srcFull = Get-NormalizedFullPath -Path $SourceRoot
$idxFull = Get-NormalizedFullPath -Path $IndexRoot
$logFull = Get-NormalizedFullPath -Path $LogRoot

if (-not (Test-Path -LiteralPath $srcFull -PathType Container)) {
    throw "源目录不存在：$srcFull"
}
if (-not (Test-Path -LiteralPath $idxFull -PathType Container)) {
    throw "快捷方式索引目录不存在：$idxFull"
}
if ($srcFull.Equals($idxFull, [System.StringComparison]::OrdinalIgnoreCase) -or
    (Test-PathIsStrictChildOfRoot -Path $idxFull -Root $srcFull) -or
    (Test-PathIsStrictChildOfRoot -Path $srcFull -Root $idxFull)) {
    throw "源目录和快捷方式索引目录必须相互独立，不能相同或互相包含。"
}

if (-not (Test-Path -LiteralPath $logFull -PathType Container)) {
    New-Item -ItemType Directory -Path $logFull -ErrorAction Stop | Out-Null
}

$runTimestamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff'
$reportPath = Join-Path $logFull ("{0}_{1}.txt" -f $CleanupReportFilePrefix, $runTimestamp)
$script:shell = New-Object -ComObject WScript.Shell
$script:shortcutReadFailures = @()

function Test-SourceFolderIsEmptyByOriginalRule {
    param([Parameter(Mandatory)][string]$FolderPath)

    try {
        # 沿用原脚本规则：只检查顶层可见子项，不使用 -Force。
        $firstVisibleItem = Get-ChildItem -LiteralPath $FolderPath -ErrorAction SilentlyContinue | Select-Object -First 1
        return ($null -eq $firstVisibleItem)
    } catch {
        return $true
    }
}

function Get-ManagedShortcutRecords {
    param([Parameter(Mandatory)][string]$FolderPath)

    $records = @()
    Get-ChildItem -LiteralPath $FolderPath -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $script:shell.CreateShortcut($_.FullName)
            if ($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' ') -and $sc.TargetPath) {
                $fullTarget = Get-NormalizedFullPath -Path $sc.TargetPath
                if (Test-PathIsStrictChildOfRoot -Path $fullTarget -Root $srcFull) {
                    $records += [PSCustomObject]@{
                        ShortcutName = $_.Name
                        ShortcutPath = $_.FullName
                        TargetPath = $fullTarget
                        Category = ''
                        Action = 'Pending'
                    }
                }
            }
        } catch {
            $script:shortcutReadFailures += [PSCustomObject]@{
                ShortcutName = $_.Name
                ShortcutPath = $_.FullName
                ErrorMessage = $_.Exception.Message
            }
        }
    }
    return @($records)
}

function Remove-ManagedIndexShortcutSafely {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$ExpectedTargetPath,
        [Parameter(Mandatory)][ValidateSet('EmptySource', 'MissingTarget')][string]$Reason
    )

    $fullShortcutPath = Get-NormalizedFullPath -Path $ShortcutPath
    $fullExpectedTarget = Get-NormalizedFullPath -Path $ExpectedTargetPath
    $shortcutParent = Get-NormalizedFullPath -Path ([System.IO.Path]::GetDirectoryName($fullShortcutPath))

    if (-not $shortcutParent.Equals($idxFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "安全检查阻止删除：文件不在快捷方式索引目录顶层：$fullShortcutPath"
    }
    if (-not (Test-Path -LiteralPath $fullShortcutPath -PathType Leaf)) {
        throw "待删除快捷方式不存在：$fullShortcutPath"
    }

    $shortcutItem = Get-Item -LiteralPath $fullShortcutPath -ErrorAction Stop
    if ($shortcutItem.PSIsContainer -or
        -not $shortcutItem.Extension.Equals('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "安全检查阻止删除：只允许删除 .lnk 文件：$fullShortcutPath"
    }

    $sc = $script:shell.CreateShortcut($fullShortcutPath)
    if (-not ($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' ') -and $sc.TargetPath)) {
        throw "安全检查阻止删除：该快捷方式不带 [DoukIndex] 管理标记：$fullShortcutPath"
    }

    $actualTarget = Get-NormalizedFullPath -Path $sc.TargetPath
    if (-not $actualTarget.Equals($fullExpectedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "扫描后快捷方式目标发生变化。原目标：$fullExpectedTarget；现目标：$actualTarget"
    }
    if (-not (Test-PathIsStrictChildOfRoot -Path $actualTarget -Root $srcFull)) {
        throw "安全检查阻止删除：快捷方式目标不在源目录下：$actualTarget"
    }

    if ($Reason -eq 'EmptySource') {
        if (-not (Test-Path -LiteralPath $actualTarget -PathType Container)) {
            throw "空源目录目标已不存在，请重新扫描后再清理：$actualTarget"
        }
        if (-not (Test-SourceFolderIsEmptyByOriginalRule -FolderPath $actualTarget)) {
            throw "源目录已不再为空，已保留快捷方式：$actualTarget"
        }
    } else {
        if (Test-Path -LiteralPath $actualTarget -PathType Container) {
            throw "原失效目标现在已经存在，已保留快捷方式：$actualTarget"
        }
    }

    # 本脚本唯一的删除语句；变量已经逐项复核，只能指向 IndexRoot 顶层受管 .lnk。
    Remove-Item -LiteralPath $fullShortcutPath -Force -ErrorAction Stop
}

$sourceFolderCandidates = @(Get-ChildItem -LiteralPath $srcFull -Directory -ErrorAction SilentlyContinue)
$sourceFolders = @(
    $sourceFolderCandidates |
        Where-Object { $_.Name -match '^UID[0-9]+_A[1-9][0-9]*([^0-9]|$)' } |
        Sort-Object Name
)
$ignoredSourceFolderCount = $sourceFolderCandidates.Count - $sourceFolders.Count
$emptySourceFolders = @()
$emptyTargetSet = @{}
foreach ($folder in $sourceFolders) {
    $targetPath = Get-NormalizedFullPath -Path $folder.FullName
    if (Test-SourceFolderIsEmptyByOriginalRule -FolderPath $targetPath) {
        $emptySourceFolders += [PSCustomObject]@{ FolderName = $folder.Name; TargetPath = $targetPath }
        $emptyTargetSet[$targetPath] = $true
    }
}

$managedBefore = @(Get-ManagedShortcutRecords -FolderPath $idxFull)
$emptyShortcutCandidates = @()
$missingShortcutCandidates = @()

foreach ($record in $managedBefore) {
    if ($emptyTargetSet.ContainsKey($record.TargetPath)) {
        $record.Category = 'EmptySource'
        $emptyShortcutCandidates += $record
    } elseif (-not (Test-Path -LiteralPath $record.TargetPath -PathType Container)) {
        $record.Category = 'MissingTarget'
        $missingShortcutCandidates += $record
    }
}

$movedOrDeletedFolderCount = @($missingShortcutCandidates | Select-Object -ExpandProperty TargetPath -Unique).Count
$plannedDeletionCount = $emptyShortcutCandidates.Count + $missingShortcutCandidates.Count

Write-Host ""
Write-Host "手动清理计划" -ForegroundColor Cyan
Write-Host "空源文件夹：$($emptySourceFolders.Count) 个（仅提示，源目录需由用户手动处理）"
Write-Host "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个"
Write-Host "计划从 $idxFull 删除空源目录对应快捷方式：$($emptyShortcutCandidates.Count) 个"
Write-Host "计划从 $idxFull 删除目标不存在的快捷方式：$($missingShortcutCandidates.Count) 个"
Write-Host "计划删除快捷方式合计：$plannedDeletionCount 个"

$deletedEmptyShortcuts = @()
$deletedMissingShortcuts = @()
$deleteFailures = @()

foreach ($item in @($emptyShortcutCandidates + $missingShortcutCandidates)) {
    try {
        Remove-ManagedIndexShortcutSafely -ShortcutPath $item.ShortcutPath -ExpectedTargetPath $item.TargetPath -Reason $item.Category
        $item.Action = 'Deleted'
        if ($item.Category -eq 'EmptySource') {
            $deletedEmptyShortcuts += $item
        } else {
            $deletedMissingShortcuts += $item
        }
    } catch {
        $item.Action = 'DeleteFailed'
        $deleteFailures += [PSCustomObject]@{
            Category = $item.Category
            ShortcutName = $item.ShortcutName
            ShortcutPath = $item.ShortcutPath
            TargetPath = $item.TargetPath
            ErrorMessage = $_.Exception.Message
        }
    }
}

$remainingManaged = @(Get-ManagedShortcutRecords -FolderPath $idxFull)
$remainingEmptyShortcuts = @($remainingManaged | Where-Object { $emptyTargetSet.ContainsKey($_.TargetPath) })
$remainingMissingShortcuts = @($remainingManaged | Where-Object { -not (Test-Path -LiteralPath $_.TargetPath -PathType Container) })
$actualDeletedCount = $deletedEmptyShortcuts.Count + $deletedMissingShortcuts.Count
$shortcutReadFailureItems = @($script:shortcutReadFailures | Sort-Object ShortcutPath -Unique)

$summary = [ordered]@{
    Mode = 'ManualCleanup'
    SourceRoot = $srcFull
    IndexRoot = $idxFull
    SourceFoldersScanned = $sourceFolders.Count
    IgnoredSourceFolders = $ignoredSourceFolderCount
    EmptySourceFolders = $emptySourceFolders.Count
    MovedOrDeletedTargetFolders = $movedOrDeletedFolderCount
    EmptySourceShortcutsDetected = $emptyShortcutCandidates.Count
    MissingTargetShortcutsDetected = $missingShortcutCandidates.Count
    PlannedShortcutDeletions = $plannedDeletionCount
    DeletedEmptySourceShortcuts = $deletedEmptyShortcuts.Count
    DeletedMissingTargetShortcuts = $deletedMissingShortcuts.Count
    DeletedShortcutsTotal = $actualDeletedCount
    ShortcutDeleteFailures = $deleteFailures.Count
    RemainingEmptySourceShortcuts = $remainingEmptyShortcuts.Count
    RemainingMissingTargetShortcuts = $remainingMissingShortcuts.Count
    ShortcutReadFailures = $shortcutReadFailureItems.Count
}

$reportLines = @(
    "生成时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "运行方式：手动清理（不创建、不更新快捷方式）"
    "源目录（仅扫描和提示）：$srcFull"
    "快捷方式索引目录（唯一允许删除的位置）：$idxFull"
    "扫描账号源文件夹：$($sourceFolders.Count) 个"
    "忽略非账号文件夹：$ignoredSourceFolderCount 个"
    "空源文件夹：$($emptySourceFolders.Count) 个（仅提示，需手动处理）"
    "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个"
    "发现空源目录对应快捷方式：$($emptyShortcutCandidates.Count) 个"
    "发现目标不存在的快捷方式：$($missingShortcutCandidates.Count) 个"
    "计划从快捷方式索引目录删除：$plannedDeletionCount 个"
    "已删除空源目录对应快捷方式：$($deletedEmptyShortcuts.Count) 个"
    "已删除目标不存在的快捷方式：$($deletedMissingShortcuts.Count) 个"
    "已删除快捷方式合计：$actualDeletedCount 个"
    "删除失败：$($deleteFailures.Count) 个"
    "剩余空源目录对应快捷方式：$($remainingEmptyShortcuts.Count) 个"
    "剩余目标不存在的快捷方式：$($remainingMissingShortcuts.Count) 个"
    "快捷方式读取失败：$($shortcutReadFailureItems.Count) 个"
    ""
    "【空源文件夹（仅提示，源目录需手动处理）】"
    ""
)

if ($emptySourceFolders.Count -eq 0) {
    $reportLines += "数量：0"
    $reportLines += ""
} else {
    foreach ($item in $emptySourceFolders | Sort-Object FolderName) {
        $reportLines += "文件夹：$($item.FolderName)"
        $reportLines += "路径：$($item.TargetPath)"
        $reportLines += "处理：源文件夹保持不变；对应受管快捷方式单独处理"
        $reportLines += ""
    }
}

$reportLines += "【空源目录对应快捷方式】"
$reportLines += ""
if ($emptyShortcutCandidates.Count -eq 0) {
    $reportLines += "数量：0"
    $reportLines += ""
} else {
    foreach ($item in $emptyShortcutCandidates | Sort-Object ShortcutName) {
        $actionText = if ($item.Action -eq 'Deleted') { '已删除' } elseif ($item.Action -eq 'DeleteFailed') { '删除失败' } else { '待处理' }
        $reportLines += "快捷方式：$($item.ShortcutName)"
        $reportLines += "目标：$($item.TargetPath)"
        $reportLines += "处理：$actionText"
        $reportLines += ""
    }
}

$reportLines += "【目标已移动或删除的失效快捷方式】"
$reportLines += ""
if ($missingShortcutCandidates.Count -eq 0) {
    $reportLines += "数量：0"
    $reportLines += ""
} else {
    foreach ($item in $missingShortcutCandidates | Sort-Object ShortcutName) {
        $actionText = if ($item.Action -eq 'Deleted') { '已删除' } elseif ($item.Action -eq 'DeleteFailed') { '删除失败' } else { '待处理' }
        $reportLines += "快捷方式：$($item.ShortcutName)"
        $reportLines += "原目标：$($item.TargetPath)"
        $reportLines += "处理：$actionText"
        $reportLines += ""
    }
}

if ($deleteFailures.Count -gt 0) {
    $reportLines += "【删除失败】"
    $reportLines += ""
    foreach ($item in $deleteFailures | Sort-Object ShortcutName) {
        $categoryText = if ($item.Category -eq 'EmptySource') { '空源目录对应快捷方式' } else { '目标不存在的快捷方式' }
        $reportLines += "类别：$categoryText"
        $reportLines += "快捷方式：$($item.ShortcutName)"
        $reportLines += "目标：$($item.TargetPath)"
        $reportLines += "错误：$($item.ErrorMessage)"
        $reportLines += ""
    }
}

if ($shortcutReadFailureItems.Count -gt 0) {
    $reportLines += "【快捷方式读取失败】"
    $reportLines += ""
    foreach ($item in $shortcutReadFailureItems | Sort-Object ShortcutName) {
        $reportLines += "快捷方式：$($item.ShortcutName)"
        $reportLines += "路径：$($item.ShortcutPath)"
        $reportLines += "错误：$($item.ErrorMessage)"
        $reportLines += ""
    }
}

Set-Content -LiteralPath $reportPath -Value $reportLines -Encoding UTF8

Write-Host ""
Write-Host "手动清理汇总" -ForegroundColor Green
Write-Host "空源文件夹：$($emptySourceFolders.Count) 个（仅提示，需手动处理）"
Write-Host "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个"
Write-Host "计划删除快捷方式：空源目录对应 $($emptyShortcutCandidates.Count) 个 + 目标不存在 $($missingShortcutCandidates.Count) 个 = $plannedDeletionCount 个"
Write-Host "实际删除快捷方式：空源目录对应 $($deletedEmptyShortcuts.Count) 个 + 目标不存在 $($deletedMissingShortcuts.Count) 个 = $actualDeletedCount 个；失败 $($deleteFailures.Count) 个"
Write-Host "剩余快捷方式：空源目录对应 $($remainingEmptyShortcuts.Count) 个；目标不存在 $($remainingMissingShortcuts.Count) 个"
Write-Host "日志：$reportPath"

# 供管理器解析的稳定机器数据；字段名仅内部使用，不直接显示给用户。
Write-Output ("DOUK_INDEX_SUMMARY_JSON=" + ($summary | ConvertTo-Json -Compress))

if ($OpenIndexFolderAfterRun) {
    Invoke-Item $idxFull
}
