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
        $shortcut.Description = 'DouK 清理自检用非受管快捷方式'
    }
    $shortcut.Save()
}

try {
    Assert-SelfTest -Condition (Test-Path -LiteralPath $cleanupScript -PathType Leaf) -Message "找不到清理脚本：$cleanupScript"

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

    Assert-SelfTest -Condition (Test-Path -LiteralPath $validShortcut -PathType Leaf) -Message '目标存在的受管快捷方式被误删。'
    Assert-SelfTest -Condition (-not (Test-Path -LiteralPath $brokenShortcut)) -Message '目标不存在的受管快捷方式未被删除。'
    Assert-SelfTest -Condition (Test-Path -LiteralPath $unmanagedShortcut -PathType Leaf) -Message '非受管快捷方式被误删。'
    Assert-SelfTest -Condition (Test-Path -LiteralPath $validTarget -PathType Container) -Message '现存源文件夹被修改或删除。'
    Assert-SelfTest -Condition ((Get-Content -LiteralPath (Join-Path $validTarget 'keep.txt') -Raw).Trim() -eq 'keep') -Message '现存源文件被修改。'
    $firstSummaryMatch = [regex]::Match($firstOutput, 'DOUK_INDEX_SUMMARY_JSON=(\{[^\r\n]+\})')
    Assert-SelfTest -Condition $firstSummaryMatch.Success -Message "第一次清理未返回数字汇总。`n$firstOutput"
    $firstSummary = $firstSummaryMatch.Groups[1].Value | ConvertFrom-Json
    Assert-SelfTest -Condition ($firstSummary.DeletedShortcutsTotal -eq 1) -Message "第一次清理没有准确报告删除 1 个快捷方式。`n$firstOutput"

    $secondOutput = @(
        & $cleanupScript -SourceRoot $sourceRoot -IndexRoot $indexRoot *>&1
    ) | Out-String

    Assert-SelfTest -Condition (Test-Path -LiteralPath $validShortcut -PathType Leaf) -Message '目标存在的受管快捷方式未通过第二次清理。'
    Assert-SelfTest -Condition (Test-Path -LiteralPath $unmanagedShortcut -PathType Leaf) -Message '非受管快捷方式未通过第二次清理。'
    Assert-SelfTest -Condition ((Get-ChildItem -LiteralPath $indexRoot -Filter *.lnk -File).Count -eq 2) -Message '第二次清理后的快捷方式数量异常。'
    $secondSummaryMatch = [regex]::Match($secondOutput, 'DOUK_INDEX_SUMMARY_JSON=(\{[^\r\n]+\})')
    Assert-SelfTest -Condition $secondSummaryMatch.Success -Message "第二次清理未返回数字汇总。`n$secondOutput"
    $secondSummary = $secondSummaryMatch.Groups[1].Value | ConvertFrom-Json
    Assert-SelfTest -Condition ($secondSummary.DeletedShortcutsTotal -eq 0) -Message "第二次清理不是幂等操作。`n$secondOutput"
} catch {
    $failure = $_ | Out-String
} finally {
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($failure) {
    [Console]::Error.WriteLine("DouK 清理功能隔离自检失败。`n$failure")
    exit 1
}

Write-Host 'DouK 清理功能隔离自检通过。' -ForegroundColor Green
Write-Host '第一次运行：只删除了目标不存在的受管快捷方式。'
Write-Host '第二次运行：删除 0 个快捷方式。'
Write-Host '已保留：有效受管快捷方式、非受管快捷方式、现存源文件夹及文件。'
Write-Host '测试范围：仅使用 Windows 临时目录，未读取或修改正式源目录和索引目录。'
