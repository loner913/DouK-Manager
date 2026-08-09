param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$IndexRoot,
    [switch]$OpenIndexFolderAfterRun
)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$ManagedTag              = '[DoukIndex]'
$CleanupReportFilePrefix = 'Cleanup-Broken-Shortcut-Report'

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

$srcFull = Get-NormalizedFullPath -Path $SourceRoot
$idxFull = Get-NormalizedFullPath -Path $IndexRoot

if (-not (Test-Path -LiteralPath $srcFull -PathType Container)) {
    throw "Source folder not found: $srcFull"
}

if (-not (Test-Path -LiteralPath $idxFull -PathType Container)) {
    throw "Index folder not found: $idxFull"
}

if (Test-PathUnderRoot -Path $idxFull -Root $srcFull) {
    throw "Index folder cannot be the source folder or inside source folder: $idxFull"
}

if (Test-PathUnderRoot -Path $srcFull -Root $idxFull) {
    throw "Source folder cannot be the index folder or inside index folder: $srcFull"
}

$logRoot = Join-Path $idxFull 'Logs'
if (-not (Test-Path -LiteralPath $logRoot -PathType Container)) {
    New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
}

$runTimestamp = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff'
$reportPath = Join-Path $logRoot ("{0}_{1}.txt" -f $CleanupReportFilePrefix, $runTimestamp)

$shell = New-Object -ComObject WScript.Shell

function Get-ManagedTargetPath {
    param($Shortcut)

    if (-not $Shortcut.Description) {
        return $null
    }

    $prefix = $ManagedTag + ' '
    if (-not $Shortcut.Description.StartsWith($prefix)) {
        return $null
    }

    $storedTarget = $Shortcut.Description.Substring($prefix.Length).Trim()
    if ([string]::IsNullOrWhiteSpace($storedTarget)) {
        return $null
    }

    return Get-NormalizedFullPath -Path $storedTarget
}

function Get-BrokenManagedShortcuts {
    param([string]$FolderPath)

    $items = @()

    Get-ChildItem -LiteralPath $FolderPath -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $shell.CreateShortcut($_.FullName)

            $fullTarget = Get-ManagedTargetPath -Shortcut $sc

            if ($fullTarget -and
                (Test-PathUnderRoot -Path $fullTarget -Root $srcFull) -and
                -not (Test-Path -LiteralPath $fullTarget -PathType Container)) {

                $items += [PSCustomObject]@{
                    ShortcutPath = $_.FullName
                    ShortcutName = $_.Name
                    TargetPath   = $fullTarget
                }
            }
        } catch {
        }
    }

    return $items
}

$brokenBefore = Get-BrokenManagedShortcuts -FolderPath $idxFull
$deletedItems = @()
$failedDeleteItems = @()

foreach ($item in $brokenBefore) {
    try {
        $shortcutPath = Get-NormalizedFullPath -Path $item.ShortcutPath
        $shortcutParent = Get-NormalizedFullPath -Path ([System.IO.Path]::GetDirectoryName($shortcutPath))
        if (-not $shortcutParent.Equals($idxFull, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to delete file outside index folder: $shortcutPath"
        }
        if (-not [System.IO.Path]::GetExtension($shortcutPath).Equals('.lnk', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to delete non-shortcut file: $shortcutPath"
        }
        $verifyShortcut = $shell.CreateShortcut($shortcutPath)
        $verifiedTarget = Get-ManagedTargetPath -Shortcut $verifyShortcut
        if (-not $verifiedTarget) {
            throw "Refusing to delete unmanaged shortcut: $shortcutPath"
        }
        if (-not $verifiedTarget.Equals($item.TargetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Shortcut target changed during cleanup: $shortcutPath"
        }
        if (-not (Test-PathUnderRoot -Path $verifiedTarget -Root $srcFull)) {
            throw "Refusing to delete shortcut whose target is outside source folder: $shortcutPath"
        }
        Remove-Item -LiteralPath $item.ShortcutPath -Force -ErrorAction Stop
        $deletedItems += $item
    } catch {
        $failedDeleteItems += [PSCustomObject]@{
            ShortcutName = $item.ShortcutName
            TargetPath   = $item.TargetPath
            ErrorMessage = $_.Exception.Message
        }
    }
}

$remainingBrokenItems = Get-BrokenManagedShortcuts -FolderPath $idxFull

$reportLines = @(
    "Generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    "Source: $srcFull"
    "Index: $idxFull"
    "Broken before cleanup: $($brokenBefore.Count)"
    "Deleted this run: $($deletedItems.Count)"
    "Delete failed: $($failedDeleteItems.Count)"
    "Remaining broken after cleanup: $($remainingBrokenItems.Count)"
    ""
)

if ($deletedItems.Count -eq 0) {
    $reportLines += "No broken shortcuts were deleted."
    $reportLines += ""
} else {
    $reportLines += "Deleted broken shortcuts:"
    $reportLines += ""

    foreach ($item in $deletedItems | Sort-Object ShortcutName) {
        $reportLines += "Shortcut: $($item.ShortcutName)"
        $reportLines += "Target: $($item.TargetPath)"
        $reportLines += ""
    }
}

if ($failedDeleteItems.Count -gt 0) {
    $reportLines += "Failed to delete:"
    $reportLines += ""

    foreach ($item in $failedDeleteItems | Sort-Object ShortcutName) {
        $reportLines += "Shortcut: $($item.ShortcutName)"
        $reportLines += "Target: $($item.TargetPath)"
        $reportLines += "Error: $($item.ErrorMessage)"
        $reportLines += ""
    }
}

if ($remainingBrokenItems.Count -eq 0) {
    $reportLines += "No remaining broken shortcuts found."
} else {
    $reportLines += "Remaining broken shortcuts:"
    $reportLines += ""

    foreach ($item in $remainingBrokenItems | Sort-Object ShortcutName) {
        $reportLines += "Shortcut: $($item.ShortcutName)"
        $reportLines += "Target: $($item.TargetPath)"
        $reportLines += ""
    }
}

Set-Content -LiteralPath $reportPath -Value $reportLines -Encoding UTF8

Write-Host "Done. Deleted $($deletedItems.Count) broken shortcuts." -ForegroundColor Green
Write-Host "Delete failed: $($failedDeleteItems.Count)"
Write-Host "Remaining broken: $($remainingBrokenItems.Count)"
Write-Host "Logs: $logRoot"
Write-Host "Report: $reportPath"

if ($OpenIndexFolderAfterRun) {
    Invoke-Item $idxFull
}
