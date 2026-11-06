param(
    [string]$GoVersion = '1.27.3',
    [string]$GoArchiveSha256 = 'f26081f438319757f8a1b46d7d0b2c5ec7583d4038e485e0aa725cfde7885fff'
)

$ErrorActionPreference = 'Stop'
$BinDir = Join-Path $HOME '.local\bin'
$Existing = Get-Command arctic -ErrorAction SilentlyContinue
if ($Existing) {
    & $Existing.Source version
    exit $LASTEXITCODE
}

$Go = Get-Command go -ErrorAction SilentlyContinue
if (-not $Go) {
    $InstallRoot = Join-Path $env:LOCALAPPDATA "Programs\GoPortable\$GoVersion"
    $GoExe = Join-Path $InstallRoot 'go\bin\go.exe'
    if (-not (Test-Path -LiteralPath $GoExe)) {
        $Archive = Join-Path $env:TEMP "go$GoVersion.windows-amd64.zip"
        $Uri = "https://go.dev/dl/go$GoVersion.windows-amd64.zip"
        Invoke-WebRequest -Uri $Uri -OutFile $Archive
        $ActualHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($ActualHash -ne $GoArchiveSha256.ToLowerInvariant()) {
            throw "SHA-256 van de officiële Go-zip klopt niet: $ActualHash"
        }
        New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
        Expand-Archive -LiteralPath $Archive -DestinationPath $InstallRoot
    }
}
else {
    $GoExe = $Go.Source
}

New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
$env:GOBIN = $BinDir
$env:GOTOOLCHAIN = 'local'
& $GoExe install github.com/tamnd/arctic-cli/cmd/arctic@latest
if ($LASTEXITCODE -ne 0) {
    throw 'Bouwen van Arctic CLI is mislukt.'
}

$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($UserPath -split ';') -notcontains $BinDir) {
    [Environment]::SetEnvironmentVariable('Path', "$UserPath;$BinDir", 'User')
}
$env:Path = "$env:Path;$BinDir"
& (Join-Path $BinDir 'arctic.exe') version
Write-Host 'Arctic CLI is geïnstalleerd. Open zo nodig een nieuwe PowerShell voor de bijgewerkte PATH.'
