# PyInstaller spec for the desktop app's backend: one folder holding clipstudio-server and everything it imports.
# Build it from the repo root, after installing the app and PyInstaller into the same Python:
#   pip install . pyinstaller
#   pyinstaller desktop/clipstudio-server.spec --noconfirm --distpath desktop/backend-dist --workpath build/pyinstaller
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(Path(SPECPATH) / "backend.py")],
    pathex=[str(ROOT)],
    # app.py serves the pages from clipstudio/static next to its own module, so they ship at that path.
    datas=[(str(ROOT / "clipstudio" / "static"), "clipstudio/static")],
    # uvicorn picks its loop, protocol and lifespan modules by name at run time.
    hiddenimports=[*collect_submodules("uvicorn"), *collect_submodules("clipstudio")],
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="clipstudio-server",
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="clipstudio-server", upx=False)
