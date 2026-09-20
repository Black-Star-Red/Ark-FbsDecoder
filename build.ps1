# Build Ark-FbsDecoder single-file exe (Windows)
# Usage: powershell -ExecutionPolicy Bypass -File .\build.ps1
# Output: dist\Ark-FbsDecoder.exe  ->  upload to GitHub Releases

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Vendor = Join-Path $Root "vendor"
$FlatcVer = "v25.2.10"
$StudioTag = "ak-v1.2.3"
$StudioZipName = "ArknightsStudioCLI_net472_win32_64.zip"

New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

# --- flatc ---
$FlatcExe = Join-Path $Vendor "flatc.exe"
if (-not (Test-Path $FlatcExe)) {
    $Zip = Join-Path $Vendor "flatc-win.zip"
    $Url = "https://github.com/google/flatbuffers/releases/download/$FlatcVer/Windows.flatc.binary.zip"
    Write-Host "Downloading flatc: $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Zip
    Expand-Archive -Path $Zip -DestinationPath $Vendor -Force
    Remove-Item $Zip
    if (-not (Test-Path $FlatcExe)) {
        throw "flatc.exe not found under $Vendor"
    }
}

# --- OpenArknightsFBS schema ---
$FbsRepo = Join-Path $Vendor "OpenArknightsFBS"
$FbsDir = Join-Path $FbsRepo "FBS"
if (-not (Test-Path $FbsDir)) {
    Write-Host "Cloning OpenArknightsFBS ..."
    git clone --depth 1 https://github.com/MooncellWiki/OpenArknightsFBS.git $FbsRepo
}

# --- ArknightsStudioCLI (bundled into exe) ---
$StudioDir = Join-Path $Vendor "studio"
$StudioExe = Join-Path $StudioDir "ArknightsStudioCLI.exe"
if (-not (Test-Path $StudioExe)) {
    New-Item -ItemType Directory -Force -Path $StudioDir | Out-Null
    $Zip = Join-Path $Vendor $StudioZipName
    $Url = "https://github.com/aelurum/AssetStudio/releases/download/$StudioTag/$StudioZipName"
    Write-Host "Downloading StudioCLI: $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Zip
    Expand-Archive -Path $Zip -DestinationPath $StudioDir -Force
    Remove-Item $Zip
    if (-not (Test-Path $StudioExe)) {
        $found = Get-ChildItem -Path $StudioDir -Recurse -Filter "ArknightsStudioCLI.exe" | Select-Object -First 1
        if ($null -eq $found) {
            throw "ArknightsStudioCLI.exe not found under $StudioDir"
        }
        # flatten to studio\ for simpler packing
        Copy-Item $found.FullName -Destination $StudioExe -Force
        $dlls = Get-ChildItem -Path $found.DirectoryName -Filter "*.dll"
        foreach ($d in $dlls) {
            Copy-Item $d.FullName -Destination (Join-Path $StudioDir $d.Name) -Force
        }
    }
}

# --- Bundle config.local.json into exe as config.embedded.json ---
$CfgLocal = Join-Path $Root "config.local.json"
$BuildDir = Join-Path $Root "build"
$EmbeddedCfg = Join-Path $BuildDir "config.embedded.json"
if (-not (Test-Path $CfgLocal)) {
    throw "missing config.local.json (need chat_mask etc. to embed into exe)"
}
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
Copy-Item $CfgLocal -Destination $EmbeddedCfg -Force
Write-Host "Embedded config -> build\config.embedded.json"

# --- Bundle zh_CN template (excel + battle only, for pad / compare) ---
$ZhGamedata = Join-Path $Root "zh_CN\gamedata"
$TemplateOut = Join-Path $BuildDir "template\gamedata"
if (-not (Test-Path $ZhGamedata)) {
    throw "missing zh_CN\gamedata — copy K reference pack before build (excel+battle used as template)"
}
if (Test-Path (Join-Path $BuildDir "template")) {
    Remove-Item -Recurse -Force (Join-Path $BuildDir "template")
}
New-Item -ItemType Directory -Force -Path $TemplateOut | Out-Null
$Packed = 0
foreach ($sub in @("excel", "battle")) {
    $src = Join-Path $ZhGamedata $sub
    if (Test-Path $src) {
        Copy-Item $src -Destination (Join-Path $TemplateOut $sub) -Recurse -Force
        $n = (Get-ChildItem -Path (Join-Path $TemplateOut $sub) -Recurse -File).Count
        Write-Host "Template pack: $sub ($n files)"
        $Packed += $n
    }
}
if ($Packed -eq 0) {
    throw "no template files under zh_CN\gamedata\excel or battle"
}

# --- Python deps ---
Write-Host "Installing build deps ..."
python -m pip install --upgrade pip
python -m pip install pyinstaller "pycryptodome>=3.20,<4"

# --- PyInstaller ---
Write-Host "Packaging ..."
Push-Location $Root
try {
    python -m PyInstaller ark_fbs_decoder.spec --noconfirm --clean
} finally {
    Pop-Location
}

$Out = Join-Path $Root "dist\Ark-FbsDecoder.exe"
if (Test-Path $Out) {
    $sizeMb = [math]::Round((Get-Item $Out).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Done: $Out ($sizeMb MB)"
    Write-Host "Upload dist\Ark-FbsDecoder.exe to GitHub Releases (do not git commit)."
} else {
    throw "Build failed: $Out not found"
}
