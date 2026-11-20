# Link the plugin folder into an MO2 install (directory junction, no admin rights needed).
# Usage: .\scripts\dev_link.ps1 [-Mo2Path "D:\D Games\SkyrimStuff\mo2"]
param([string]$Mo2Path = "D:\D Games\SkyrimStuff\mo2")

$source = Join-Path $PSScriptRoot "..\plugin\texture_downscaler" | Resolve-Path
$target = Join-Path $Mo2Path "plugins\texture_downscaler"

if (Test-Path $target) {
    Write-Host "Already exists: $target"
    exit 0
}
New-Item -ItemType Junction -Path $target -Target $source | Out-Null
Write-Host "Linked $target -> $source. Restart MO2 to load the plugin."
