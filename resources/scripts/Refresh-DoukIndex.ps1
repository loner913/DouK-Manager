param(
    [string]$SourceRoot = 'F:\DouK-Downloader',
    [string]$IndexRoot = 'F:\Douk videos',
    [switch]$OpenIndexFolderAfterRun,
    [switch]$PromptDeleteBrokenShortcuts
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
$RunLogFilePrefix = 'Refresh-DoukIndex-RunLog'

# 最终规则：刷新会自动删除两类受管快捷方式，但绝不删除任何源文件夹。
$DeleteShortcutsForEmptySourceFolders = $true
$DeleteBrokenShortcutsDuringRefresh = $true

$srcFull = Get-NormalizedFullPath -Path $SourceRoot
$idxFull = Get-NormalizedFullPath -Path $IndexRoot

if (-not (Test-Path -LiteralPath $srcFull -PathType Container)) {
    throw "源目录不存在：$srcFull"
}

if ($srcFull.Equals($idxFull, [System.StringComparison]::OrdinalIgnoreCase) -or
    (Test-PathIsStrictChildOfRoot -Path $idxFull -Root $srcFull) -or
    (Test-PathIsStrictChildOfRoot -Path $srcFull -Root $idxFull)) {
    throw "源目录和快捷方式索引目录必须相互独立，不能相同或互相包含。"
}

if (-not (Test-Path -LiteralPath $idxFull -PathType Container)) {
    New-Item -ItemType Directory -Path $idxFull -ErrorAction Stop | Out-Null
}

$logRoot = Join-Path $idxFull 'Logs'
if (-not (Test-Path -LiteralPath $logRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $logRoot -ErrorAction Stop | Out-Null
}

$runTimestamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff'
$runLogPath = Join-Path $logRoot ("{0}_{1}.txt" -f $RunLogFilePrefix, $runTimestamp)
$script:shell = New-Object -ComObject WScript.Shell
$script:shortcutReadFailures = @()

function Get-ShortcutDisplayName {
    param([Parameter(Mandatory)][string]$FolderName)

    if ($FolderName -match '^UID\d+_(.+)$') {
        return $matches[1]
    }
    return $FolderName
}

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
            $isManaged = [bool]($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' '))
            $fullTarget = $null
            if ($sc.TargetPath) {
                $candidateTarget = Get-NormalizedFullPath -Path $sc.TargetPath
                if (Test-PathIsStrictChildOfRoot -Path $candidateTarget -Root $srcFull) {
                    $fullTarget = $candidateTarget
                }
            }
            if (-not $fullTarget -and $isManaged) {
                $storedTarget = $sc.Description.Substring(($ManagedTag + ' ').Length).Trim()
                if ($storedTarget) {
                    $candidateTarget = Get-NormalizedFullPath -Path $storedTarget
                    if (Test-PathIsStrictChildOfRoot -Path $candidateTarget -Root $srcFull) {
                        $fullTarget = $candidateTarget
                    }
                }
            }
            if ($fullTarget) {
                $records += [PSCustomObject]@{
                    ShortcutName = $_.Name
                    ShortcutPath = $_.FullName
                    TargetPath = $fullTarget
                    IsManaged = $isManaged
                    Action = 'Pending'
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

function Get-UniqueShortcutPath {
    param(
        [Parameter(Mandatory)][string]$FolderPath,
        [Parameter(Mandatory)][string]$BaseName
    )

    $safeBaseName = $BaseName
    foreach ($invalidCharacter in [System.IO.Path]::GetInvalidFileNameChars()) {
        $safeBaseName = $safeBaseName.Replace([string]$invalidCharacter, '_')
    }
    $safeBaseName = $safeBaseName.Trim().TrimEnd('.')
    if ([string]::IsNullOrWhiteSpace($safeBaseName)) {
        $safeBaseName = 'DouK'
    }

    $maxBaseLength = [Math]::Min(120, 235 - $FolderPath.Length - 5)
    if ($maxBaseLength -lt 16) {
        throw "快捷方式索引目录路径过长，无法安全创建快捷方式：$FolderPath"
    }
    if ($safeBaseName.Length -gt $maxBaseLength) {
        $safeBaseName = $safeBaseName.Substring(0, $maxBaseLength).Trim().TrimEnd('.')
    }

    $candidate = Join-Path $FolderPath ($safeBaseName + '.lnk')
    $number = 2
    while (Test-Path -LiteralPath $candidate) {
        $suffix = " ($number)"
        $availableBaseLength = $maxBaseLength - $suffix.Length
        $numberedBaseName = $safeBaseName
        if ($numberedBaseName.Length -gt $availableBaseLength) {
            $numberedBaseName = $numberedBaseName.Substring(0, $availableBaseLength).Trim().TrimEnd('.')
        }
        $candidate = Join-Path $FolderPath ($numberedBaseName + $suffix + '.lnk')
        $number++
    }
    return $candidate
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
$indexableSourceFolders = @()
$emptyTargetSet = @{}

foreach ($folder in $sourceFolders) {
    $targetPath = Get-NormalizedFullPath -Path $folder.FullName
    if (Test-SourceFolderIsEmptyByOriginalRule -FolderPath $targetPath) {
        $emptySourceFolders += [PSCustomObject]@{ FolderName = $folder.Name; TargetPath = $targetPath }
        $emptyTargetSet[$targetPath] = $true
    } else {
        $indexableSourceFolders += $folder
    }
}

$managedBefore = @(Get-ManagedShortcutRecords -FolderPath $idxFull)
$emptyShortcutCandidates = @()
$missingShortcutCandidates = @()

foreach ($record in $managedBefore) {
    if (-not $record.IsManaged) {
        continue
    }
    if ($emptyTargetSet.ContainsKey($record.TargetPath)) {
        $emptyShortcutCandidates += $record
    } elseif (-not (Test-Path -LiteralPath $record.TargetPath -PathType Container)) {
        $missingShortcutCandidates += $record
    }
}

$movedOrDeletedFolderCount = @($missingShortcutCandidates | Select-Object -ExpandProperty TargetPath -Unique).Count
$plannedEmptyShortcutCount = if ($DeleteShortcutsForEmptySourceFolders) { $emptyShortcutCandidates.Count } else { 0 }
$plannedMissingShortcutCount = if ($DeleteBrokenShortcutsDuringRefresh) { $missingShortcutCandidates.Count } else { 0 }
$plannedDeletionCount = $plannedEmptyShortcutCount + $plannedMissingShortcutCount

Write-Host ""
Write-Host "索引刷新删除计划" -ForegroundColor Cyan
Write-Host "空源文件夹：$($emptySourceFolders.Count) 个（仅提示，源目录需由用户手动处理）"
Write-Host "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个"
Write-Host "计划从 $idxFull 删除空源目录对应快捷方式：$plannedEmptyShortcutCount 个"
Write-Host "计划从 $idxFull 删除目标不存在的快捷方式：$plannedMissingShortcutCount 个"
Write-Host "计划删除快捷方式合计：$plannedDeletionCount 个"

$deletedEmptyShortcuts = @()
$deletedMissingShortcuts = @()
$deleteFailures = @()

foreach ($item in $emptyShortcutCandidates) {
    if (-not $DeleteShortcutsForEmptySourceFolders) {
        $item.Action = 'KeptByPolicy'
        continue
    }
    try {
        Remove-ManagedIndexShortcutSafely -ShortcutPath $item.ShortcutPath -ExpectedTargetPath $item.TargetPath -Reason 'EmptySource'
        $item.Action = 'Deleted'
        $deletedEmptyShortcuts += $item
    } catch {
        $item.Action = 'DeleteFailed'
        $deleteFailures += [PSCustomObject]@{
            Category = 'EmptySource'
            ShortcutName = $item.ShortcutName
            ShortcutPath = $item.ShortcutPath
            TargetPath = $item.TargetPath
            ErrorMessage = $_.Exception.Message
        }
    }
}

foreach ($item in $missingShortcutCandidates) {
    if (-not $DeleteBrokenShortcutsDuringRefresh) {
        $item.Action = 'KeptByPolicy'
        continue
    }
    try {
        Remove-ManagedIndexShortcutSafely -ShortcutPath $item.ShortcutPath -ExpectedTargetPath $item.TargetPath -Reason 'MissingTarget'
        $item.Action = 'Deleted'
        $deletedMissingShortcuts += $item
    } catch {
        $item.Action = 'DeleteFailed'
        $deleteFailures += [PSCustomObject]@{
            Category = 'MissingTarget'
            ShortcutName = $item.ShortcutName
            ShortcutPath = $item.ShortcutPath
            TargetPath = $item.TargetPath
            ErrorMessage = $_.Exception.Message
        }
    }
}

$managedShortcutMap = @{}
foreach ($record in @(Get-ManagedShortcutRecords -FolderPath $idxFull)) {
    if ((Test-Path -LiteralPath $record.TargetPath -PathType Container) -and
        -not $emptyTargetSet.ContainsKey($record.TargetPath) -and
        -not $managedShortcutMap.ContainsKey($record.TargetPath)) {
        $managedShortcutMap[$record.TargetPath] = $record.ShortcutPath
    }
}

$created = 0
$updated = 0
$unchanged = 0
$indexFailures = @()

foreach ($folder in $indexableSourceFolders) {
    $targetPath = Get-NormalizedFullPath -Path $folder.FullName
    try {
        if ($managedShortcutMap.ContainsKey($targetPath)) {
            $shortcutPath = $managedShortcutMap[$targetPath]
            $shortcut = $script:shell.CreateShortcut($shortcutPath)
            $desiredDescription = "$ManagedTag $targetPath"
            $needsUpdate = (
                -not $shortcut.TargetPath -or
                -not (Get-NormalizedFullPath -Path $shortcut.TargetPath).Equals($targetPath, [System.StringComparison]::OrdinalIgnoreCase) -or
                -not $shortcut.WorkingDirectory -or
                -not (Get-NormalizedFullPath -Path $shortcut.WorkingDirectory).Equals($targetPath, [System.StringComparison]::OrdinalIgnoreCase) -or
                $shortcut.Description -ne $desiredDescription
            )

            if ($needsUpdate) {
                $shortcut.TargetPath = $targetPath
                $shortcut.WorkingDirectory = $targetPath
                $shortcut.Description = $desiredDescription
                $shortcut.Save()
                $updated++
            } else {
                $unchanged++
            }
        } else {
            $displayName = Get-ShortcutDisplayName -FolderName $folder.Name
            $shortcutPath = Get-UniqueShortcutPath -FolderPath $idxFull -BaseName $displayName
            $shortcut = $script:shell.CreateShortcut($shortcutPath)
            $shortcut.TargetPath = $targetPath
            $shortcut.WorkingDirectory = $targetPath
            $shortcut.Description = "$ManagedTag $targetPath"
            $shortcut.Save()
            $managedShortcutMap[$targetPath] = $shortcutPath
            $created++
        }
    } catch {
        $indexFailures += [PSCustomObject]@{
            FolderName = $folder.Name
            TargetPath = $targetPath
            ErrorMessage = $_.Exception.Message
        }
    }
}

$remainingManaged = @(Get-ManagedShortcutRecords -FolderPath $idxFull)
$remainingEmptyShortcuts = @($remainingManaged | Where-Object { $_.IsManaged -and $emptyTargetSet.ContainsKey($_.TargetPath) })
$remainingMissingShortcuts = @($remainingManaged | Where-Object { $_.IsManaged -and -not (Test-Path -LiteralPath $_.TargetPath -PathType Container) })
$actualDeletedCount = $deletedEmptyShortcuts.Count + $deletedMissingShortcuts.Count
$shortcutReadFailureItems = @($script:shortcutReadFailures | Sort-Object ShortcutPath -Unique)

$summary = [ordered]@{
    Mode = 'Refresh'
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
    Created = $created
    Updated = $updated
    Unchanged = $unchanged
    IndexFailures = $indexFailures.Count
    ShortcutReadFailures = $shortcutReadFailureItems.Count
}

$runLogLines = @(
    "生成时间：$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "运行方式：刷新索引（创建、更新、检测、报告并删除受管快捷方式）"
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
    "新建快捷方式：$created 个"
    "更新快捷方式：$updated 个"
    "保持不变：$unchanged 个"
    "创建或更新失败：$($indexFailures.Count) 个"
    "快捷方式读取失败：$($shortcutReadFailureItems.Count) 个"
    ""
    "【空源文件夹（仅提示，源目录需手动处理）】"
    ""
)

if ($emptySourceFolders.Count -eq 0) {
    $runLogLines += "数量：0"
    $runLogLines += ""
} else {
    foreach ($item in $emptySourceFolders | Sort-Object FolderName) {
        $runLogLines += "文件夹：$($item.FolderName)"
        $runLogLines += "路径：$($item.TargetPath)"
        $runLogLines += "处理：源文件夹保持不变；对应受管快捷方式单独处理"
        $runLogLines += ""
    }
}

$runLogLines += "【空源目录对应快捷方式】"
$runLogLines += ""
if ($emptyShortcutCandidates.Count -eq 0) {
    $runLogLines += "数量：0"
    $runLogLines += ""
} else {
    foreach ($item in $emptyShortcutCandidates | Sort-Object ShortcutName) {
        $actionText = if ($item.Action -eq 'Deleted') { '已删除' } elseif ($item.Action -eq 'DeleteFailed') { '删除失败' } elseif ($item.Action -eq 'KeptByPolicy') { '按规则保留' } else { '待处理' }
        $runLogLines += "快捷方式：$($item.ShortcutName)"
        $runLogLines += "目标：$($item.TargetPath)"
        $runLogLines += "处理：$actionText"
        $runLogLines += ""
    }
}

$runLogLines += "【目标已移动或删除的失效快捷方式】"
$runLogLines += ""
if ($missingShortcutCandidates.Count -eq 0) {
    $runLogLines += "数量：0"
    $runLogLines += ""
} else {
    foreach ($item in $missingShortcutCandidates | Sort-Object ShortcutName) {
        $actionText = if ($item.Action -eq 'Deleted') { '已删除' } elseif ($item.Action -eq 'DeleteFailed') { '删除失败' } elseif ($item.Action -eq 'KeptByPolicy') { '按规则保留' } else { '待处理' }
        $runLogLines += "快捷方式：$($item.ShortcutName)"
        $runLogLines += "原目标：$($item.TargetPath)"
        $runLogLines += "处理：$actionText"
        $runLogLines += ""
    }
}

if ($deleteFailures.Count -gt 0) {
    $runLogLines += "【删除失败】"
    $runLogLines += ""
    foreach ($item in $deleteFailures | Sort-Object ShortcutName) {
        $categoryText = if ($item.Category -eq 'EmptySource') { '空源目录对应快捷方式' } else { '目标不存在的快捷方式' }
        $runLogLines += "类别：$categoryText"
        $runLogLines += "快捷方式：$($item.ShortcutName)"
        $runLogLines += "目标：$($item.TargetPath)"
        $runLogLines += "错误：$($item.ErrorMessage)"
        $runLogLines += ""
    }
}

if ($indexFailures.Count -gt 0) {
    $runLogLines += "【创建或更新失败】"
    $runLogLines += ""
    foreach ($item in $indexFailures | Sort-Object FolderName) {
        $runLogLines += "文件夹：$($item.FolderName)"
        $runLogLines += "目标：$($item.TargetPath)"
        $runLogLines += "错误：$($item.ErrorMessage)"
        $runLogLines += ""
    }
}

if ($shortcutReadFailureItems.Count -gt 0) {
    $runLogLines += "【快捷方式读取失败】"
    $runLogLines += ""
    foreach ($item in $shortcutReadFailureItems | Sort-Object ShortcutName) {
        $runLogLines += "快捷方式：$($item.ShortcutName)"
        $runLogLines += "路径：$($item.ShortcutPath)"
        $runLogLines += "错误：$($item.ErrorMessage)"
        $runLogLines += ""
    }
}

Set-Content -LiteralPath $runLogPath -Value $runLogLines -Encoding UTF8

Write-Host ""
Write-Host "索引刷新汇总" -ForegroundColor Green
Write-Host "快捷方式：新建 $created 个；更新 $updated 个；保持不变 $unchanged 个；创建或更新失败 $($indexFailures.Count) 个"
Write-Host "空源文件夹：$($emptySourceFolders.Count) 个（仅提示，需手动处理）"
Write-Host "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个"
Write-Host "计划删除快捷方式：空源目录对应 $plannedEmptyShortcutCount 个 + 目标不存在 $plannedMissingShortcutCount 个 = $plannedDeletionCount 个"
Write-Host "实际删除快捷方式：空源目录对应 $($deletedEmptyShortcuts.Count) 个 + 目标不存在 $($deletedMissingShortcuts.Count) 个 = $actualDeletedCount 个；失败 $($deleteFailures.Count) 个"
Write-Host "剩余快捷方式：空源目录对应 $($remainingEmptyShortcuts.Count) 个；目标不存在 $($remainingMissingShortcuts.Count) 个"
Write-Host "运行日志：$runLogPath"

# 供管理器解析的稳定机器数据；字段名仅内部使用，不直接显示给用户。
Write-Output ("DOUK_INDEX_SUMMARY_JSON=" + ($summary | ConvertTo-Json -Compress))

if ($OpenIndexFolderAfterRun) {
    Invoke-Item $idxFull
}
