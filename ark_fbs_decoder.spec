# PyInstaller spec — run after build.ps1 prepares vendor/
from pathlib import Path

block_cipher = None
root = Path(SPECPATH)
vendor = root / "vendor"
flatc = vendor / "flatc.exe"
fbs = vendor / "OpenArknightsFBS" / "FBS"
studio = vendor / "studio"
rules = root / "k_postprocess_rules.json"
embedded_cfg = root / "build" / "config.embedded.json"
template_gamedata = root / "build" / "template" / "gamedata"

if not flatc.is_file():
    raise SystemExit(f"missing {flatc}, run .\\build.ps1 first")
if not fbs.is_dir():
    raise SystemExit(f"missing {fbs}, run .\\build.ps1 first")
if not (studio / "ArknightsStudioCLI.exe").is_file():
    raise SystemExit(f"missing StudioCLI under {studio}, run .\\build.ps1 first")
if not rules.is_file():
    raise SystemExit(f"missing {rules}")
if not embedded_cfg.is_file():
    raise SystemExit(
        f"missing {embedded_cfg}; run .\\build.ps1 (needs config.local.json)"
    )
if not template_gamedata.is_dir():
    raise SystemExit(
        f"missing {template_gamedata}; run .\\build.ps1 (needs zh_CN/gamedata)"
    )

a = Analysis(
    ["fbs_to_json.py"],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(flatc), "."),
        (str(fbs), "FBS"),
        (str(studio), "studio"),
        (str(rules), "."),
        (str(embedded_cfg), "."),
        (str(template_gamedata), "template/gamedata"),
    ],
    hiddenimports=["Crypto.Cipher.AES", "Crypto.Util.Padding"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Ark-FbsDecoder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
