param(
    [string]$Version = "0.1.0-alpha"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$AppName = "BeamNG Hand Drive Converter"
$DistExe = Join-Path $Root "dist\$AppName.exe"
$StageDir = Join-Path $Root "dist\BeamHDC"
$StageExe = Join-Path $StageDir "$AppName.exe"
$ReleaseDir = Join-Path $Root "release"
$ReleaseZip = Join-Path $ReleaseDir "BeamHDC-$Version-windows.zip"

Set-Location $Root

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install "pyinstaller>=6.0.0"

python -m PyInstaller --noconfirm --clean ".\packaging\BeamNG Hand Drive Converter.spec"

if (!(Test-Path $DistExe)) {
    throw "Expected PyInstaller output not found: $DistExe"
}

if (Test-Path $StageDir) {
    $ResolvedRoot = (Resolve-Path $Root).Path
    $ResolvedStage = (Resolve-Path $StageDir).Path
    if (!$ResolvedStage.StartsWith($ResolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove staging directory outside repository root: $ResolvedStage"
    }
    Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
}
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

Copy-Item -LiteralPath $DistExe -Destination $StageExe -Force
Copy-Item -LiteralPath ".\README.md" -Destination $StageDir -Force
Copy-Item -LiteralPath ".\LICENSE" -Destination $StageDir -Force

$StageExamples = Join-Path $StageDir "examples\conversion_configs"
New-Item -ItemType Directory -Path $StageExamples -Force | Out-Null
Copy-Item -Path ".\examples\conversion_configs\*.json" -Destination $StageExamples -Force

New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null
if (Test-Path $ReleaseZip) {
    Remove-Item -LiteralPath $ReleaseZip -Force
}
Compress-Archive -LiteralPath $StageDir -DestinationPath $ReleaseZip -CompressionLevel Optimal

Write-Host "Built release archive:"
Write-Host $ReleaseZip
