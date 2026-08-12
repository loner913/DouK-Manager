param(
    [string]$SourceRoot = 'F:\DouK-Downloader',
    [string]$IndexRoot = 'F:\Douk videos',
    [string]$LogRoot = (Join-Path $PSScriptRoot 'Logs\IndexRefresh'),
    [switch]$OpenIndexFolderAfterRun,
    [switch]$PromptDeleteBrokenShortcuts
)

# 兼容入口；DouK 管理器实际打包并执行 resources/scripts 下的正式脚本。
$scriptPath = Join-Path $PSScriptRoot 'resources\scripts\Refresh-DoukIndex.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "找不到正式索引脚本：$scriptPath"
}

$PSBoundParameters['LogRoot'] = $LogRoot
& $scriptPath @PSBoundParameters
exit $LASTEXITCODE
