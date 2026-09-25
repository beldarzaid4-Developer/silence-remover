# silence_remover.spec
# Build with:  arch -x86_64 python3 -m PyInstaller silence_remover.spec --noconfirm
# (build_macos.sh does this for you, including fetching the ffmpeg binaries
# into ./ffmpeg-bin first - this spec just expects that folder to exist.)

import os

block_cipher = None

FFMPEG_BIN_DIR = os.path.join(os.getcwd(), "ffmpeg-bin")

a = Analysis(
    ["main.py"],
    pathex=[os.getcwd()],
    binaries=[],
    datas=[
        (os.path.join(FFMPEG_BIN_DIR, "ffmpeg"), "ffmpeg-bin"),
        (os.path.join(FFMPEG_BIN_DIR, "ffprobe"), "ffmpeg-bin"),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DeadAirRemover",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed app; --selftest still prints to a
                             # terminal fine when launched via CLI directly
    target_arch="x86_64",   # force Intel build regardless of host arch
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="DeadAirRemover",
)

app = BUNDLE(
    coll,
    name="DeadAirRemover.app",
    icon=None,
    bundle_identifier="com.zaid.deadairremover",
    info_plist={
        "CFBundleName": "Dead-Air Remover",
        "CFBundleDisplayName": "Dead-Air Remover",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "10.15.0",
        "LSApplicationCategoryType": "public.app-category.video",
    },
)
