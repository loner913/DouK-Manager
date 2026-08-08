param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$IndexRoot,
    [switch]$OpenIndexFolderAfterRun,
    [switch]$GenerateBrokenReport,
    [switch]$PromptDeleteBrokenShortcuts
)

function Get-NormalizedFullPath {
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "Path is empty."
    }

    $fullPath = [System.IO.Path]::GetFullPath($Path)

    if ($fullPath.Length -gt 3) {
        return $fullPath.TrimEnd('\')
    }

    return $fullPath
}

function Test-PathUnderRoot {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root
    )

    $fullPath = Get-NormalizedFullPath -Path $Path
    $fullRoot = Get-NormalizedFullPath -Path $Root
    $rootPrefix = if ($fullRoot.EndsWith('\')) { $fullRoot } else { $fullRoot + '\' }

    return (
        $fullPath.Equals($fullRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    )
}

$BrokenReportFilePrefix  = 'Broken-Shortcut-Report'
$RunLogFilePrefix        = 'Refresh-DoukIndex-RunLog'
$ManagedTag              = '[DoukIndex]'
$SkipEmptySourceFolders               = $true
$RemoveShortcutsForEmptySourceFolders = $true

$srcFull = Get-NormalizedFullPath -Path $SourceRoot
$idxFull = Get-NormalizedFullPath -Path $IndexRoot
$logRoot = Join-Path $idxFull 'Logs'

$runTimestamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff'
$reportPath = Join-Path $logRoot ("{0}_{1}.txt" -f $BrokenReportFilePrefix, $runTimestamp)
$runLogPath = Join-Path $logRoot ("{0}_{1}.txt" -f $RunLogFilePrefix, $runTimestamp)

if (-not (Test-Path -LiteralPath $srcFull -PathType Container)) {
    throw "Source folder not found: $srcFull"
}

if (Test-PathUnderRoot -Path $idxFull -Root $srcFull) {
    throw "Index folder cannot be the source folder or inside source folder: $idxFull"
}

if (Test-PathUnderRoot -Path $srcFull -Root $idxFull) {
    throw "Source folder cannot be the index folder or inside index folder: $srcFull"
}

if (-not (Test-Path -LiteralPath $idxFull -PathType Container)) {
    New-Item -ItemType Directory -Path $idxFull | Out-Null
}

if (-not (Test-Path -LiteralPath $logRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $logRoot | Out-Null
}

$script:shell = New-Object -ComObject WScript.Shell

function Get-ShortcutDisplayName {
    param([string]$FolderName)

    if ($FolderName -match '^UID\d+_(.+)$') {
        return $matches[1]
    }

    return $FolderName
}

function Test-SourceFolderShouldBeIndexed {
    param([string]$FolderPath)

    if (-not $SkipEmptySourceFolders) {
        return $true
    }

    try {
        # 只要顶层有任何可见子项，就算非空；不使用 -Force，所以隐藏/系统项不计入。
        $firstItem = Get-ChildItem -LiteralPath $FolderPath -ErrorAction SilentlyContinue | Select-Object -First 1
        return ($null -ne $firstItem)
    } catch {
        return $false
    }
}

function Get-ManagedShortcutMap {
    param([string]$FolderPath)

    $map = @{}

    Get-ChildItem -LiteralPath $FolderPath -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $script:shell.CreateShortcut($_.FullName)

            if ($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' ') -and $sc.TargetPath) {
                $fullTarget = Get-NormalizedFullPath -Path $sc.TargetPath

                if (Test-PathUnderRoot -Path $fullTarget -Root $srcFull) {
                    $map[$fullTarget] = $_.FullName
                }
            }
        } catch {
        }
    }

    return $map
}

function Get-UniqueShortcutPath {
    param(
        [string]$FolderPath,
        [string]$BaseName
    )

    $candidate = Join-Path $FolderPath ($BaseName + '.lnk')
    $index = 2

    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $FolderPath ("{0} ({1}).lnk" -f $BaseName, $index)
        $index++
    }

    return $candidate
}

function Remove-ManagedIndexShortcutSafely {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$ExpectedTargetPath
    )

    $fullShortcutPath = Get-NormalizedFullPath -Path $ShortcutPath
    $fullExpectedTargetPath = Get-NormalizedFullPath -Path $ExpectedTargetPath
    $shortcutParent = Get-NormalizedFullPath -Path ([System.IO.Path]::GetDirectoryName($fullShortcutPath))

    if (-not $shortcutParent.Equals($idxFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to delete file outside index folder: $fullShortcutPath"
    }

    if (-not (Test-Path -LiteralPath $fullShortcutPath -PathType Leaf)) {
        throw "Shortcut file not found: $fullShortcutPath"
    }

    $shortcutItem = Get-Item -LiteralPath $fullShortcutPath -ErrorAction Stop

    if ($shortcutItem.PSIsContainer) {
        throw "Refusing to delete a directory: $fullShortcutPath"
    }

    if (-not $shortcutItem.Extension.Equals('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to delete non-shortcut file: $fullShortcutPath"
    }

    $sc = $script:shell.CreateShortcut($fullShortcutPath)

    if (-not ($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' ') -and $sc.TargetPath)) {
        throw "Refusing to delete unmanaged shortcut: $fullShortcutPath"
    }

    $actualTargetPath = Get-NormalizedFullPath -Path $sc.TargetPath

    if (-not $actualTargetPath.Equals($fullExpectedTargetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Shortcut target mismatch. Expected: $fullExpectedTargetPath ; Actual: $actualTargetPath"
    }

    if (-not (Test-PathUnderRoot -Path $actualTargetPath -Root $srcFull)) {
        throw "Refusing to delete shortcut whose target is outside source folder: $actualTargetPath"
    }

    Remove-Item -LiteralPath $fullShortcutPath -Force -ErrorAction Stop
}

$managedShortcutMap = Get-ManagedShortcutMap -FolderPath $idxFull
$sourceFolders = Get-ChildItem -LiteralPath $srcFull -Directory -ErrorAction SilentlyContinue | Sort-Object Name

$created = 0
$updated = 0
$skippedEmptyFolders = @()
$skippedTargetSet = @{}
$removedEmptyShortcuts = @()
$brokenItems = @()

foreach ($folder in $sourceFolders) {
    $targetPath = Get-NormalizedFullPath -Path $folder.FullName

    if (-not (Test-SourceFolderShouldBeIndexed -FolderPath $targetPath)) {
        $skippedEmptyFolders += [PSCustomObject]@{
            FolderName = $folder.Name
            TargetPath = $targetPath
        }
        $skippedTargetSet[$targetPath] = $true
        continue
    }

    if ($managedShortcutMap.ContainsKey($targetPath)) {
        $shortcutPath = $managedShortcutMap[$targetPath]
        $updated++
    } else {
        $displayName = Get-ShortcutDisplayName -FolderName $folder.Name
        $shortcutPath = Get-UniqueShortcutPath -FolderPath $idxFull -BaseName $displayName
        $managedShortcutMap[$targetPath] = $shortcutPath
        $created++
    }

    $shortcut = $script:shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $targetPath
    $shortcut.WorkingDirectory = $targetPath
    $shortcut.Description = "$ManagedTag $targetPath"
    $shortcut.Save()
}

if ($RemoveShortcutsForEmptySourceFolders -and $skippedTargetSet.Count -gt 0) {
    Get-ChildItem -LiteralPath $idxFull -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $script:shell.CreateShortcut($_.FullName)

            if ($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' ') -and $sc.TargetPath) {
                $fullTarget = Get-NormalizedFullPath -Path $sc.TargetPath

                if ($skippedTargetSet.ContainsKey($fullTarget)) {
                    Remove-ManagedIndexShortcutSafely -ShortcutPath $_.FullName -ExpectedTargetPath $fullTarget

                    $removedEmptyShortcuts += [PSCustomObject]@{
                        ShortcutName = $_.Name
                        TargetPath   = $fullTarget
                    }
                }
            }
        } catch {
        }
    }
}

Get-ChildItem -LiteralPath $idxFull -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $sc = $script:shell.CreateShortcut($_.FullName)

        if ($sc.Description -and $sc.Description.StartsWith($ManagedTag + ' ') -and $sc.TargetPath) {
            $fullTarget = Get-NormalizedFullPath -Path $sc.TargetPath

            if ((Test-PathUnderRoot -Path $fullTarget -Root $srcFull) -and
                -not (Test-Path -LiteralPath $fullTarget -PathType Container)) {

                $brokenItems += [PSCustomObject]@{
                    ShortcutName = $_.Name
                    ShortcutPath = $_.FullName
                    TargetPath   = $fullTarget
                    Action       = 'Pending'
                }
            }
        }
    } catch {
    }
}

if ($PromptDeleteBrokenShortcuts -and $brokenItems.Count -gt 0) {
    Write-Host ""
    Write-Host "Detected broken shortcuts. Choose Y to delete or N to keep each one." -ForegroundColor Yellow

    foreach ($item in $brokenItems | Sort-Object ShortcutName) {
        Write-Host ""
        Write-Host "Broken shortcut: $($item.ShortcutName)" -ForegroundColor Yellow
        Write-Host "Missing target: $($item.TargetPath)"

        while ($true) {
            $answer = (Read-Host "Delete this broken shortcut? (Y/N)").Trim()

            if ($answer -match '^[Yy]$') {
                try {
                    Remove-ManagedIndexShortcutSafely -ShortcutPath $item.ShortcutPath -ExpectedTargetPath $item.TargetPath
                    $item.Action = 'Deleted'
                    Write-Host "Deleted." -ForegroundColor Green
                } catch {
                    $item.Action = 'DeleteFailed'
                    Write-Host "Delete failed or blocked by safety check. Shortcut was kept." -ForegroundColor Red
                }
                break
            }

            if ($answer -match '^[Nn]$') {
                $item.Action = 'Kept'
                Write-Host "Kept." -ForegroundColor DarkYellow
                break
            }

            Write-Host "Please enter Y or N." -ForegroundColor Red
        }
    }
} else {
    foreach ($item in $brokenItems) {
        $item.Action = 'Kept'
    }
}

$brokenDeletedCount = @($brokenItems | Where-Object { $_.Action -eq 'Deleted' }).Count
$brokenKeptCount = @($brokenItems | Where-Object { $_.Action -eq 'Kept' -or $_.Action -eq 'DeleteFailed' }).Count
$brokenDetectedCount = $brokenItems.Count

if ($GenerateBrokenReport) {
    $reportLines = @(
        "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        "Source: $srcFull"
        "Index: $idxFull"
        "Detected broken shortcuts: $brokenDetectedCount"
        "Deleted broken shortcuts: $brokenDeletedCount"
        "Kept broken shortcuts: $brokenKeptCount"
        ""
    )

    if ($brokenItems.Count -eq 0) {
        $reportLines += "No broken shortcuts found."
    } else {
        $reportLines += "Broken shortcuts:"
        $reportLines += ""

        foreach ($item in $brokenItems | Sort-Object ShortcutName) {
            $reportLines += "Shortcut: $($item.ShortcutName)"
            $reportLines += "Target: $($item.TargetPath)"
            $reportLines += "Action: $($item.Action)"
            $reportLines += ""
        }
    }

    Set-Content -LiteralPath $reportPath -Value $reportLines -Encoding UTF8
}

$brokenReportSummary = if ($GenerateBrokenReport) { $reportPath } else { 'Disabled' }

$runLogLines = @(
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "Source: $srcFull"
    "Index: $idxFull"
    "Log folder: $logRoot"
    "Created: $created"
    "Updated: $updated"
    "Skipped empty folders: $($skippedEmptyFolders.Count)"
    "Removed shortcuts for empty folders: $($removedEmptyShortcuts.Count)"
    "Detected broken shortcuts: $brokenDetectedCount"
    "Deleted broken shortcuts: $brokenDeletedCount"
    "Kept broken shortcuts: $brokenKeptCount"
    "Broken report: $brokenReportSummary"
    ""
)

if ($skippedEmptyFolders.Count -eq 0) {
    $runLogLines += "Skipped empty folders: none"
    $runLogLines += ""
} else {
    $runLogLines += "Skipped empty folders:"
    $runLogLines += ""

    foreach ($item in $skippedEmptyFolders | Sort-Object FolderName) {
        $runLogLines += "Folder: $($item.FolderName)"
        $runLogLines += "Target: $($item.TargetPath)"
        $runLogLines += ""
    }
}

if ($removedEmptyShortcuts.Count -eq 0) {
    $runLogLines += "Removed shortcuts for empty folders: none"
    $runLogLines += ""
} else {
    $runLogLines += "Removed shortcuts for empty folders:"
    $runLogLines += ""

    foreach ($item in $removedEmptyShortcuts | Sort-Object ShortcutName) {
        $runLogLines += "Shortcut: $($item.ShortcutName)"
        $runLogLines += "Target: $($item.TargetPath)"
        $runLogLines += ""
    }
}

if ($brokenItems.Count -eq 0) {
    $runLogLines += "Broken shortcuts: none"
} else {
    $runLogLines += "Broken shortcuts:"
    $runLogLines += ""

    foreach ($item in $brokenItems | Sort-Object ShortcutName) {
        $runLogLines += "Shortcut: $($item.ShortcutName)"
        $runLogLines += "Target: $($item.TargetPath)"
        $runLogLines += "Action: $($item.Action)"
        $runLogLines += ""
    }
}

Set-Content -LiteralPath $runLogPath -Value $runLogLines -Encoding UTF8

Write-Host ""
Write-Host "Done. Created $created, updated $updated shortcuts." -ForegroundColor Green
Write-Host "Skipped empty folders: $($skippedEmptyFolders.Count)"
Write-Host "Removed shortcuts for empty folders: $($removedEmptyShortcuts.Count)"
Write-Host "Detected broken shortcuts: $brokenDetectedCount"
Write-Host "Deleted broken shortcuts: $brokenDeletedCount"
Write-Host "Kept broken shortcuts: $brokenKeptCount"
Write-Host "Source: $srcFull"
Write-Host "Index: $idxFull"
Write-Host "Logs: $logRoot"

if ($GenerateBrokenReport) {
    Write-Host "Broken report: $reportPath"
}

Write-Host "RunLog: $runLogPath"

if ($OpenIndexFolderAfterRun) {
    Invoke-Item $idxFull
}
