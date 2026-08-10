param(
    [Parameter(Mandatory = $true)][string]$SourceRoot,
    [Parameter(Mandatory = $true)][string]$IndexRoot,
    [switch]$OpenIndexFolderAfterRun,
    [switch]$PromptDeleteBrokenShortcuts
)

# Compatibility entry point only.  DouK Manager packages and executes the
# authoritative implementation under resources/scripts.
$scriptPath = Join-Path $PSScriptRoot 'resources\scripts\Refresh-DoukIndex.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw "Managed index script not found: $scriptPath"
}

& $scriptPath @PSBoundParameters
exit $LASTEXITCODE
