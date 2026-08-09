param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$IndexRoot,
    [switch]$OpenIndexFolderAfterRun,
    [switch]$GenerateBrokenReport,
    [switch]$PromptDeleteBrokenShortcuts
)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

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
$ManagedBase64Tag        = '[DoukIndexB64]'
$SkipEmptySourceFolders               = $true
$RemoveShortcutsForEmptySourceFolders = $true

$srcFull = Get-NormalizedFullPath -Path $SourceRoot
$idxFull = Get-NormalizedFullPath -Path $IndexRoot
$logRoot = Join-Path $idxFull 'Logs'
$fallbackLauncherRoot = Join-Path $idxFull '.DouKLaunchers'

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
$script:explorerPath = Join-Path $env:WINDIR 'explorer.exe'
$script:powerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

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

function Get-ManagedTargetPath {
    param($Shortcut)

    if (-not $Shortcut.Description) {
        return $null
    }

    $base64Prefix = $ManagedBase64Tag + ' '
    if ($Shortcut.Description.StartsWith($base64Prefix)) {
        try {
            $encodedTarget = $Shortcut.Description.Substring($base64Prefix.Length).Trim()
            if ([string]::IsNullOrWhiteSpace($encodedTarget)) {
                return $null
            }

            $targetBytes = [Convert]::FromBase64String($encodedTarget)
            $decodedTarget = [Text.Encoding]::Unicode.GetString($targetBytes)
            return Get-NormalizedFullPath -Path $decodedTarget
        } catch {
            return $null
        }
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

function Get-ManagedShortcutMap {
    param([string]$FolderPath)

    $map = @{}

    Get-ChildItem -LiteralPath $FolderPath -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $script:shell.CreateShortcut($_.FullName)

            $fullTarget = Get-ManagedTargetPath -Shortcut $sc

            if ($fullTarget -and (Test-PathUnderRoot -Path $fullTarget -Root $srcFull)) {
                $map[$fullTarget] = $_.FullName
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
        throw "Index path is too long for shortcut creation: $FolderPath"
    }
    if ($safeBaseName.Length -gt $maxBaseLength) {
        $safeBaseName = $safeBaseName.Substring(0, $maxBaseLength).Trim().TrimEnd('.')
    }

    $candidate = Join-Path $FolderPath ($safeBaseName + '.lnk')
    $index = 2

    while (Test-Path -LiteralPath $candidate) {
        $suffix = " ($index)"
        $availableBaseLength = $maxBaseLength - $suffix.Length
        $numberedBaseName = $safeBaseName
        if ($numberedBaseName.Length -gt $availableBaseLength) {
            $numberedBaseName = $numberedBaseName.Substring(0, $availableBaseLength).Trim().TrimEnd('.')
        }
        $candidate = Join-Path $FolderPath ($numberedBaseName + $suffix + '.lnk')
        $index++
    }

    return $candidate
}

function Get-FallbackShortcutBaseName {
    param([string]$FolderName)

    if ($FolderName -match '^UID[0-9]+_(A[1-9][0-9]*)') {
        return ($matches[1] + '_Account')
    }

    return 'DouK_Account'
}

function Get-EncodedTargetValue {
    param([Parameter(Mandatory)][string]$TargetPath)

    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($TargetPath))
}

function Get-FallbackLauncherPath {
    param([Parameter(Mandatory)][string]$ShortcutPath)

    $shortcutBaseName = [System.IO.Path]::GetFileNameWithoutExtension($ShortcutPath)
    return Join-Path $fallbackLauncherRoot ($shortcutBaseName + '.ps1')
}

function Get-FallbackLauncherContent {
    param([Parameter(Mandatory)][string]$TargetPath)

    $encodedTarget = Get-EncodedTargetValue -TargetPath $TargetPath
    return @(
        "`$encodedTarget = '$encodedTarget'"
        '$targetPath = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encodedTarget))'
        'if (Test-Path -LiteralPath $targetPath -PathType Container) {'
        '    Invoke-Item -LiteralPath $targetPath'
        '}'
    ) -join "`r`n"
}

function Get-EncodedShortcutArguments {
    param([Parameter(Mandatory)][string]$LauncherPath)

    return "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LauncherPath`""
}

function Get-SafeFallbackShortcutPath {
    param(
        [Parameter(Mandatory)][string]$FolderPath,
        [Parameter(Mandatory)][string]$BaseName
    )

    $canonicalPath = Join-Path $FolderPath ($BaseName + '.lnk')
    if (-not (Test-Path -LiteralPath $canonicalPath -PathType Leaf)) {
        return $canonicalPath
    }

    try {
        $existingShortcut = $script:shell.CreateShortcut($canonicalPath)
        $description = [string]$existingShortcut.Description
        if ($description.StartsWith($ManagedTag + ' ') -or
            $description.StartsWith($ManagedBase64Tag + ' ')) {
            # This is an earlier manager-generated fallback and may be repaired in place.
            return $canonicalPath
        }
    } catch {
    }

    return Get-UniqueShortcutPath -FolderPath $FolderPath -BaseName ($BaseName + '_Encoded')
}

function Test-ManagedShortcutIsCurrent {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$ExpectedTargetPath
    )

    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        return $false
    }

    try {
        $shortcut = $script:shell.CreateShortcut($ShortcutPath)
        $managedTarget = Get-ManagedTargetPath -Shortcut $shortcut

        if (-not $managedTarget -or
            -not $managedTarget.Equals($ExpectedTargetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }

        if ([string]$shortcut.Description -and
            ([string]$shortcut.Description).StartsWith($ManagedBase64Tag + ' ')) {
            $launcherPath = Get-FallbackLauncherPath -ShortcutPath $ShortcutPath
            if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
                return $false
            }

            $expectedLauncherContent = Get-FallbackLauncherContent -TargetPath $ExpectedTargetPath
            $actualLauncherContent = [System.IO.File]::ReadAllText($launcherPath, [Text.Encoding]::ASCII)
            if (-not $actualLauncherContent.Equals($expectedLauncherContent, [System.StringComparison]::Ordinal)) {
                return $false
            }

            $expectedEncodedArguments = Get-EncodedShortcutArguments -LauncherPath $launcherPath
            return (
                $shortcut.TargetPath -and
                $shortcut.TargetPath.Equals($script:powerShellPath, [System.StringComparison]::OrdinalIgnoreCase) -and
                ([string]$shortcut.Arguments).Equals($expectedEncodedArguments, [System.StringComparison]::Ordinal)
            )
        }

        # The original index script used a folder directly as TargetPath. Keep those
        # already-working legacy shortcuts instead of rewriting them through WScript.
        if ($shortcut.TargetPath -and
            $shortcut.TargetPath.Equals($ExpectedTargetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }

        $expectedArguments = '"' + $ExpectedTargetPath + '"'
        return (
            $shortcut.TargetPath -and
            $shortcut.TargetPath.Equals($script:explorerPath, [System.StringComparison]::OrdinalIgnoreCase) -and
            ([string]$shortcut.Arguments).Equals($expectedArguments, [System.StringComparison]::Ordinal)
        )
    } catch {
        return $false
    }
}

function Save-ManagedShortcut {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$TargetPath
    )

    $shortcut = $script:shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $script:explorerPath
    $shortcut.Arguments = '"' + $TargetPath + '"'
    $shortcut.WorkingDirectory = $srcFull
    $shortcut.Description = "$ManagedTag $TargetPath"
    $shortcut.Save()

    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        throw "Shortcut file was not created: $ShortcutPath"
    }
}

function Save-EncodedManagedShortcut {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$TargetPath
    )

    $encodedTarget = Get-EncodedTargetValue -TargetPath $TargetPath
    $launcherPath = Get-FallbackLauncherPath -ShortcutPath $ShortcutPath
    $launcherContent = Get-FallbackLauncherContent -TargetPath $TargetPath

    if (-not (Test-Path -LiteralPath $fallbackLauncherRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $fallbackLauncherRoot | Out-Null
        try {
            $launcherDirectory = Get-Item -LiteralPath $fallbackLauncherRoot -ErrorAction Stop
            $launcherDirectory.Attributes = $launcherDirectory.Attributes -bor [System.IO.FileAttributes]::Hidden
        } catch {
        }
    }

    [System.IO.File]::WriteAllText($launcherPath, $launcherContent, [Text.Encoding]::ASCII)
    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
        throw "Fallback launcher file was not created: $launcherPath"
    }

    $encodedArguments = Get-EncodedShortcutArguments -LauncherPath $launcherPath

    $shortcut = $script:shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $script:powerShellPath
    $shortcut.Arguments = $encodedArguments
    $shortcut.WorkingDirectory = $idxFull
    $shortcut.Description = "$ManagedBase64Tag $encodedTarget"
    $shortcut.Save()

    if (-not (Test-Path -LiteralPath $ShortcutPath -PathType Leaf)) {
        throw "Encoded shortcut file was not created: $ShortcutPath"
    }

    $savedShortcut = $script:shell.CreateShortcut($ShortcutPath)
    $savedTarget = Get-ManagedTargetPath -Shortcut $savedShortcut

    if (-not $savedTarget -or
        -not $savedTarget.Equals($TargetPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not ([string]$savedShortcut.TargetPath).Equals($script:powerShellPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not ([string]$savedShortcut.Arguments).Equals($encodedArguments, [System.StringComparison]::Ordinal) -or
        -not ([System.IO.File]::ReadAllText($launcherPath, [Text.Encoding]::ASCII)).Equals($launcherContent, [System.StringComparison]::Ordinal)) {
        throw "Encoded shortcut verification failed: $ShortcutPath"
    }
}

function Remove-ObsoleteFallbackCopies {
    param(
        [Parameter(Mandatory)][string]$FolderPath,
        [Parameter(Mandatory)][string]$BaseName,
        [Parameter(Mandatory)][string]$KeepPath
    )

    $removedPaths = @()
    $escapedBaseName = [Regex]::Escape($BaseName)
    $duplicatePattern = '^' + $escapedBaseName + ' \([2-9][0-9]*\)\.lnk$'
    $fullKeepPath = Get-NormalizedFullPath -Path $KeepPath

    Get-ChildItem -LiteralPath $FolderPath -Filter ($BaseName + ' *.lnk') -File -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.Name -notmatch $duplicatePattern) {
            return
        }

        $fullCandidatePath = Get-NormalizedFullPath -Path $_.FullName
        if ($fullCandidatePath.Equals($fullKeepPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            return
        }

        try {
            # These numbered names are created only after this manager has already
            # generated the canonical A####_Account shortcut. Removal happens only
            # after that canonical shortcut was saved and verified successfully.
            $obsoleteLauncherPath = Get-FallbackLauncherPath -ShortcutPath $_.FullName
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
            if (Test-Path -LiteralPath $obsoleteLauncherPath -PathType Leaf) {
                Remove-Item -LiteralPath $obsoleteLauncherPath -Force -ErrorAction SilentlyContinue
            }
            $removedPaths += $_.FullName
        } catch {
        }
    }

    return @($removedPaths)
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

    $actualTargetPath = Get-ManagedTargetPath -Shortcut $sc

    if (-not $actualTargetPath) {
        throw "Refusing to delete unmanaged shortcut: $fullShortcutPath"
    }

    if (-not $actualTargetPath.Equals($fullExpectedTargetPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Shortcut target mismatch. Expected: $fullExpectedTargetPath ; Actual: $actualTargetPath"
    }

    if (-not (Test-PathUnderRoot -Path $actualTargetPath -Root $srcFull)) {
        throw "Refusing to delete shortcut whose target is outside source folder: $actualTargetPath"
    }

    Remove-Item -LiteralPath $fullShortcutPath -Force -ErrorAction Stop
}

$managedShortcutMap = Get-ManagedShortcutMap -FolderPath $idxFull

# Index only canonical downloader account folders.
# Keep the implementation deliberately simple for Windows PowerShell 5.1.
$sourceFolderCandidates = @(Get-ChildItem -LiteralPath $srcFull -Directory -ErrorAction SilentlyContinue)
$sourceFolders = @()

foreach ($sourceFolderCandidate in $sourceFolderCandidates) {
    $candidateName = [string]$sourceFolderCandidate.Name

    if ($candidateName -match '^UID[0-9]+_A[1-9][0-9]*([^0-9]|$)') {
        $sourceFolders += $sourceFolderCandidate
    }
}

$sourceFolders = @($sourceFolders | Sort-Object -Property Name)
$ignoredSourceFolderCount = $sourceFolderCandidates.Count - $sourceFolders.Count

$created = 0
$updated = 0
$unchanged = 0
$skippedEmptyFolders = @()
$skippedTargetSet = @{}
$removedEmptyShortcuts = @()
$brokenItems = @()
$shortcutFailures = @()
$fallbackShortcutNames = @()
$removedFallbackCopies = @()

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

    $shortcutAlreadyExists = $managedShortcutMap.ContainsKey($targetPath)
    if ($shortcutAlreadyExists) {
        $shortcutPath = $managedShortcutMap[$targetPath]

        if (Test-ManagedShortcutIsCurrent -ShortcutPath $shortcutPath -ExpectedTargetPath $targetPath) {
            $unchanged++
            continue
        }
    } else {
        $displayName = Get-ShortcutDisplayName -FolderName $folder.Name
        $shortcutPath = Get-UniqueShortcutPath -FolderPath $idxFull -BaseName $displayName
    }

    try {
        Save-ManagedShortcut -ShortcutPath $shortcutPath -TargetPath $targetPath

        $managedShortcutMap[$targetPath] = $shortcutPath
        if ($shortcutAlreadyExists) {
            $updated++
        } else {
            $created++
        }
    } catch {
        $primaryError = $_.Exception.Message
        $fallbackBaseName = Get-FallbackShortcutBaseName -FolderName $folder.Name
        $fallbackShortcutPath = Get-SafeFallbackShortcutPath -FolderPath $idxFull -BaseName $fallbackBaseName

        try {
            Save-EncodedManagedShortcut -ShortcutPath $fallbackShortcutPath -TargetPath $targetPath
            $managedShortcutMap[$targetPath] = $fallbackShortcutPath
            $removedFallbackCopies += @(
                Remove-ObsoleteFallbackCopies `
                    -FolderPath $idxFull `
                    -BaseName $fallbackBaseName `
                    -KeepPath $fallbackShortcutPath
            )
            $fallbackShortcutNames += [PSCustomObject]@{
                FolderName   = $folder.Name
                ShortcutPath = $fallbackShortcutPath
            }
            if ($shortcutAlreadyExists) {
                $updated++
            } else {
                $created++
            }
        } catch {
            $shortcutFailures += [PSCustomObject]@{
                FolderName   = $folder.Name
                TargetPath   = $targetPath
                ShortcutPath = $shortcutPath
                ErrorMessage = "$primaryError ; fallback failed: $($_.Exception.Message)"
            }
        }
    }
}

if ($RemoveShortcutsForEmptySourceFolders -and $skippedTargetSet.Count -gt 0) {
    Get-ChildItem -LiteralPath $idxFull -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $sc = $script:shell.CreateShortcut($_.FullName)

            $fullTarget = Get-ManagedTargetPath -Shortcut $sc

            if ($fullTarget -and $skippedTargetSet.ContainsKey($fullTarget)) {
                Remove-ManagedIndexShortcutSafely -ShortcutPath $_.FullName -ExpectedTargetPath $fullTarget

                $removedEmptyShortcuts += [PSCustomObject]@{
                    ShortcutName = $_.Name
                    TargetPath   = $fullTarget
                }
            }
        } catch {
        }
    }
}

Get-ChildItem -LiteralPath $idxFull -Filter *.lnk -File -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $sc = $script:shell.CreateShortcut($_.FullName)

        $fullTarget = Get-ManagedTargetPath -Shortcut $sc

        if ($fullTarget -and
            (Test-PathUnderRoot -Path $fullTarget -Root $srcFull) -and
            -not (Test-Path -LiteralPath $fullTarget -PathType Container)) {

            $brokenItems += [PSCustomObject]@{
                ShortcutName = $_.Name
                ShortcutPath = $_.FullName
                TargetPath   = $fullTarget
                Action       = 'Pending'
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
    "Scanned source folders: $($sourceFolderCandidates.Count)"
    "Matched account folders: $($sourceFolders.Count)"
    "Ignored non-account folders: $ignoredSourceFolderCount"
    "Created: $created"
    "Updated: $updated"
    "Unchanged: $unchanged"
    "Fallback shortcut names: $($fallbackShortcutNames.Count)"
    "Removed obsolete fallback copies: $($removedFallbackCopies.Count)"
    "Shortcut failures: $($shortcutFailures.Count)"
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

if ($shortcutFailures.Count -eq 0) {
    $runLogLines += "Shortcut failures: none"
    $runLogLines += ""
} else {
    $runLogLines += "Shortcut failures:"
    $runLogLines += ""

    foreach ($item in $shortcutFailures | Sort-Object FolderName) {
        $runLogLines += "Folder: $($item.FolderName)"
        $runLogLines += "Target: $($item.TargetPath)"
        $runLogLines += "Shortcut: $($item.ShortcutPath)"
        $runLogLines += "Error: $($item.ErrorMessage)"
        $runLogLines += ""
    }
}

if ($fallbackShortcutNames.Count -eq 0) {
    $runLogLines += "Fallback shortcut names: none"
    $runLogLines += ""
} else {
    $runLogLines += "Fallback shortcut names:"
    $runLogLines += ""

    foreach ($item in $fallbackShortcutNames | Sort-Object FolderName) {
        $runLogLines += "Folder: $($item.FolderName)"
        $runLogLines += "Shortcut: $($item.ShortcutPath)"
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
Write-Host "Scanned source folders: $($sourceFolderCandidates.Count)"
Write-Host "Matched account folders: $($sourceFolders.Count)"
Write-Host "Ignored non-account folders: $ignoredSourceFolderCount"
Write-Host "Unchanged shortcuts: $unchanged"
Write-Host "Fallback shortcut names: $($fallbackShortcutNames.Count)"
Write-Host "Removed obsolete fallback copies: $($removedFallbackCopies.Count)"
Write-Host "Shortcut failures: $($shortcutFailures.Count)"
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
