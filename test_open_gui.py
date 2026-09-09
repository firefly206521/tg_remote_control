import subprocess
import tempfile
import unittest
from pathlib import Path
from shutil import copy2


ROOT = Path(__file__).resolve().parent


class OpenGuiLauncherTests(unittest.TestCase):
    def test_existing_token_produces_url_without_opening_browser(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            copy2(ROOT / "open-gui.bat", temp / "open-gui.bat")
            (temp / ".gui-token").write_text("test-token\n", encoding="ascii")

            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "call", "open-gui.bat", "--dry-run"],
                cwd=temp,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
            )
            output = (result.stdout + result.stderr).decode("ascii", errors="replace")

        self.assertEqual(result.returncode, 0, output)
        self.assertIn("http://127.0.0.1:8765/?token=test-token", output)


if __name__ == "__main__":
    unittest.main()
