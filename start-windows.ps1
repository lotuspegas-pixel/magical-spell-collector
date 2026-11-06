param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CollectorArgs
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is niet gevonden. Installeer uv en open daarna een nieuwe PowerShell.'
}

Push-Location $ProjectRoot
try {
    & uv run --python 3.11 spell-collector @CollectorArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
