param()

$ErrorActionPreference = 'Stop'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$cleanupScript = Join-Path $PSScriptRoot 'Cleanup-BrokenDoukIndex.ps1'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("DouK-Cleanup-SelfTest-{0}" -f [System.Guid]::NewGuid().ToString('N'))
$sourceRoot = Join-Path $testRoot 'source'
$indexRoot = Join-Path $testRoot 'index'
$validTarget = Join-Path $sourceRoot 'UID100_A1Valid_works'
$brokenTarget = Join-Path $sourceRoot 'UID200_A2Missing_works'
$validShortcut = Join-Path $indexRoot 'A1Valid_works.lnk'
$brokenShortcut = Join-Path $indexRoot 'A2Missing_works.lnk'
$unmanagedShortcut = Join-Path $indexRoot 'Unmanaged.lnk'

$firstOutput = ''
$secondOutput = ''
$failure = $null

function Assert-SelfTest {
    param(
        [Parameter(Mandatory)][bool]$Condition,
        [Parameter(Mandatory)][string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function New-SelfTestShortcut {
    param(
        [Parameter(Mandatory)][string]$ShortcutPath,
        [Parameter(Mandatory)][string]$TargetPath,
        [Parameter(Mandatory)][bool]$Managed
    )

    $shortcut = $script:shell.CreateShortcut($ShortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.WorkingDirectory = $TargetPath
    if ($Managed) {
        $shortcut.Description = "[DoukIndex] $TargetPath"
    } else {
        $shortcut.Description = 'DouK cleanup self-test unmanaged shortcut'
    }
    $shortcut.Save()
}

try {
    Assert-SelfTest -Condition (Test-Path -LiteralPath $cleanupScript -PathType Leaf) -Message "Cleanup script not found: $cleanupScript"

    New-Item -ItemType Directory -Path $validTarget -Force | Out-Null
    New-Item -ItemType Directory -Path $brokenTarget -Force | Out-Null
    New-Item -ItemType Directory -Path $indexRoot -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $validTarget 'keep.txt') -Value 'keep' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $brokenTarget 'remove-folder-after-link.txt') -Value 'temporary' -Encoding ASCII

    $script:shell = New-Object -ComObject WScript.Shell
    New-SelfTestShortcut -ShortcutPath $validShortcut -TargetPath $validTarget -Managed $true
    New-SelfTestShortcut -ShortcutPath $brokenShortcut -TargetPath $brokenTarget -Managed $true
    New-SelfTestShortcut -ShortcutPath $unmanagedShortcut -TargetPath $validTarget -Managed $false

    Remove-Item -LiteralPath $brokenTarget -Recurse -Force

    $firstOutput = @(
        & $cleanupScript -SourceRoot $sourceRoot -IndexRoot $indexRoot *>&1
    ) | Out-String

    Assert-SelfTest -Condition (Test-Path -LiteralPath $validShortcut -PathType Leaf) -Message 'Managed shortcut with an existing target was deleted.'
    Assert-SelfTest -Condition (-not (Test-Path -LiteralPath $brokenShortcut)) -Message 'Managed shortcut with a missing target was not deleted.'
    Assert-SelfTest -Condition (Test-Path -LiteralPath $unmanagedShortcut -PathType Leaf) -Message 'Unmanaged shortcut was deleted.'
    Assert-SelfTest -Condition (Test-Path -LiteralPath $validTarget -PathType Container) -Message 'Existing source folder was modified or deleted.'
    Assert-SelfTest -Condition ((Get-Content -LiteralPath (Join-Path $validTarget 'keep.txt') -Raw).Trim() -eq 'keep') -Message 'Existing source file was modified.'
    Assert-SelfTest -Condition ($firstOutput -match 'Deleted 1 broken shortcuts') -Message "First cleanup did not report exactly one deletion.`n$firstOutput"

    $secondOutput = @(
        & $cleanupScript -SourceRoot $sourceRoot -IndexRoot $indexRoot *>&1
    ) | Out-String

    Assert-SelfTest -Condition (Test-Path -LiteralPath $validShortcut -PathType Leaf) -Message 'Valid managed shortcut did not survive the second cleanup.'
    Assert-SelfTest -Condition (Test-Path -LiteralPath $unmanagedShortcut -PathType Leaf) -Message 'Unmanaged shortcut did not survive the second cleanup.'
    Assert-SelfTest -Condition ((Get-ChildItem -LiteralPath $indexRoot -Filter *.lnk -File).Count -eq 2) -Message 'Unexpected shortcut count after the second cleanup.'
    Assert-SelfTest -Condition ($secondOutput -match 'Deleted 0 broken shortcuts') -Message "Second cleanup was not idempotent.`n$secondOutput"
} catch {
    $failure = $_ | Out-String
} finally {
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($failure) {
    [Console]::Error.WriteLine("DouK cleanup self-test FAILED.`n$failure")
    exit 1
}

Write-Host 'DouK cleanup self-test PASSED.' -ForegroundColor Green
Write-Host 'First run: deleted only the managed broken shortcut.'
Write-Host 'Second run: deleted 0 shortcuts.'
Write-Host 'Kept: valid managed shortcut, unmanaged shortcut, existing source folder and file.'
Write-Host 'Scope: Windows temporary directory only; formal source and index folders were not used.'

