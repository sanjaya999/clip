import os
import subprocess
import sys
from pathlib import Path

from clipstudio import server


def test_check_loads_the_app_and_its_pages(capsys):
    server.main(["--check"])
    assert "Clip Studio loads" in capsys.readouterr().out


def test_the_desktop_app_can_move_storage_out_of_the_install_folder(tmp_path):
    env = {**os.environ, "CLIP_STUDIO_STORAGE": str(tmp_path / "data"), "CLIP_STUDIO_DB": str(tmp_path / "t.db")}
    out = subprocess.run(
        [sys.executable, "-c", "from clipstudio.app import CLIPS_DIR, MEDIA_DIR; print(CLIPS_DIR); print(MEDIA_DIR)"],
        env=env, capture_output=True, text=True, check=True, cwd=Path(server.__file__).resolve().parent.parent,
    ).stdout.split()
    assert out == [str(tmp_path / "data" / "clips"), str(tmp_path / "data" / "media")]
    assert (tmp_path / "data" / "clips").is_dir()
