# Build the release ZIP for the MO2 plugin from a git tag.
#
# Usage:
#   .\scripts\make_release.ps1             # build dist\texture_downscaler-<version>.zip from tag v<version>
#   .\scripts\make_release.ps1 -FromHead   # test build from the current commit (no tag needed)
#
# The version comes from plugin\texture_downscaler\__init__.py. Only files tracked by git under
# plugin\texture_downscaler are included (no __pycache__, tests or dev files). The ZIP contains a
# texture_downscaler\ folder, so users can extract it straight into <MO2>\plugins\.
# This script never creates tags or pushes anything.
param([switch]$FromHead)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$pluginDir = "plugin/texture_downscaler"
Push-Location $root
try {
    function Get-Version([string]$initText) {
        if ($initText -notmatch '__version__\s*=\s*"([^"]+)"') { throw "No __version__ found in __init__.py" }
        return $Matches[1]
    }

    $version = Get-Version (Get-Content "$pluginDir/__init__.py" -Raw)
    $tag = "v$version"

    if ($FromHead) {
        $ref = "HEAD"
        $zipName = "texture_downscaler-$version-dev.zip"
        if (git status --porcelain -- $pluginDir) {
            Write-Warning "Uncommitted changes in $pluginDir are NOT included (the ZIP is built from HEAD)."
        }
    } else {
        git rev-parse -q --verify "refs/tags/$tag" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Tag $tag does not exist. Create it first:  git tag -a $tag -m `"Texture Downscaler $version`"  (or use -FromHead for a test build)"
        }
        # The tag must contain the same version, otherwise MO2 would show a different version.
        $taggedVersion = Get-Version ((git show "${tag}:$pluginDir/__init__.py") -join "`n")
        if ($taggedVersion -ne $version) {
            throw "Tag $tag points at a commit with __version__ = $taggedVersion. Move the tag to the commit that has $version."
        }
        $tagCommit = git rev-list -n 1 $tag
        $head = git rev-parse HEAD
        if ($tagCommit -ne $head) {
            Write-Warning "Tag $tag is not at HEAD; building from the tag ($($tagCommit.Substring(0, 7)))."
        }
        $ref = $tag
        $zipName = "texture_downscaler-$version.zip"
    }

    New-Item -ItemType Directory -Force -Path "dist" | Out-Null
    $zip = Join-Path (Resolve-Path "dist") $zipName
    if (Test-Path $zip) { Remove-Item $zip }

    git archive --format=zip --prefix=texture_downscaler/ -o $zip "${ref}:$pluginDir"
    if ($LASTEXITCODE -ne 0) { throw "git archive failed" }

    # Sanity-check the contents.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try { $names = @($archive.Entries | ForEach-Object { $_.FullName }) } finally { $archive.Dispose() }

    $required = @(
        "texture_downscaler/__init__.py",
        "texture_downscaler/plugin.py",
        "texture_downscaler/LICENSE",
        "texture_downscaler/icon.png",
        "texture_downscaler/bin/texconv.exe",
        "texture_downscaler/bin/LICENSE-texconv.txt"
    )
    $missing = @($required | Where-Object { $names -notcontains $_ })
    $unwanted = @($names | Where-Object { $_ -match '__pycache__|\.pyc$|(^|/)tests/' })
    if ($missing.Count -or $unwanted.Count) {
        Remove-Item $zip  # never leave a broken ZIP lying around to be uploaded by mistake
        if ($missing.Count) { throw "ZIP is missing: $($missing -join ', ')" }
        throw "ZIP contains unwanted files: $($unwanted -join ', ')"
    }

    $files = @($names | Where-Object { -not $_.EndsWith("/") })
    $sizeKb = [math]::Round((Get-Item $zip).Length / 1KB)
    Write-Host "Built $zip ($($files.Count) files, $sizeKb KB) from $ref"
} finally {
    Pop-Location
}
