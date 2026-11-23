# Link the plugin folder into an MO2 install (directory junction, no admin rights needed).
# Usage: .\scripts\dev_link.ps1 -Mo2Path "C:\Path\To\MO2"
param([Parameter(Mandatory = $true)][string]$Mo2Path)

$ErrorActionPreference = "Stop"

# When PowerShell prompts for the parameter, typed quotes are kept literally; strip them.
$Mo2Path = $Mo2Path.Trim().Trim('"', "'")

if (-not (Test-Path -LiteralPath (Join-Path $Mo2Path "ModOrganizer.exe"))) {
    Write-Error "'$Mo2Path' doesn't look like a Mod Organizer 2 folder (no ModOrganizer.exe found)."
}

$source = (Resolve-Path (Join-Path $PSScriptRoot "..\plugin\texture_downscaler")).Path
$target = Join-Path $Mo2Path "plugins\texture_downscaler"

if (Test-Path -LiteralPath $target) {
    $item = Get-Item -LiteralPath $target -Force
    $linkTarget = @($item.Target) -join ""
    if ($item.LinkType -eq "Junction" -and $linkTarget -eq $source) {
        Write-Host "Already linked: $target -> $source"
        exit 0
    }
    if ($item.LinkType) {
        Write-Error "$target already exists and links to '$linkTarget'. Remove it first if you want to relink."
    }
    Write-Error "$target already exists as a normal folder (e.g. an installed release). Rename or remove it first."
}

New-Item -ItemType Junction -Path $target -Target $source | Out-Null
Write-Host "Linked $target -> $source. Restart MO2 to load the plugin."
