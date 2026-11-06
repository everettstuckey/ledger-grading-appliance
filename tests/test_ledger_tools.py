import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
import ledger_tools


class WindowsToolsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.program_files = self.root / "Program Files"
        self.local_data = self.root / "Local AppData"
        self.enterContext(patch.dict(os.environ, {
            "PATH": "",
            "ProgramFiles": str(self.program_files),
            "LOCALAPPDATA": str(self.local_data),
            "SystemDrive": str(self.root),
        }, clear=True))
        self.enterContext(patch.object(ledger_tools.sys, "platform", "win32"))
        self.pytesseract = SimpleNamespace(tesseract_cmd="tesseract")
        self.enterContext(patch.dict(sys.modules, {
            "pytesseract": SimpleNamespace(pytesseract=self.pytesseract),
        }))

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test fixture")
        path.chmod(0o755)
        return path

    def test_tesseract_search_order_and_wrapper_configuration(self):
        default = self.executable(self.program_files / "Tesseract-OCR" / "tesseract.exe")
        self.assertEqual(ledger_tools.configure_tesseract(), str(default))
        on_path = self.executable(self.root / "tool path" / "tesseract.exe")
        os.environ["PATH"] = str(on_path.parent)
        self.assertEqual(ledger_tools.configure_tesseract(), str(on_path))
        override = self.executable(self.root / "chosen install" / "tesseract.exe")
        os.environ["LEDGER_TESSERACT_PATH"] = str(override)
        self.assertEqual(ledger_tools.configure_tesseract(), str(override))
        self.assertEqual(self.pytesseract.tesseract_cmd, str(override))

    def test_xelatex_finds_per_user_miktex_and_newest_tex_live(self):
        older = self.executable(self.root / "texlive" / "2025" / "bin" / "windows" / "xelatex.exe")
        newer = self.executable(self.root / "texlive" / "2026" / "bin" / "windows" / "xelatex.exe")
        self.assertEqual(ledger_tools.xelatex_command(), str(newer))
        newer.unlink()
        self.assertEqual(ledger_tools.xelatex_command(), str(older))
        miktex = self.executable(self.local_data / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64" / "xelatex.exe")
        self.assertEqual(ledger_tools.xelatex_command(), str(miktex))

    def test_invalid_override_never_falls_back_to_other_install(self):
        self.executable(self.program_files / "Tesseract-OCR" / "tesseract.exe")
        command_file = self.executable(self.root / "tesseract.cmd")
        for override in ("tesseract.exe", self.root / "missing" / "tesseract.exe", command_file):
            with self.subTest(override=override), patch.dict(os.environ, {"LEDGER_TESSERACT_PATH": str(override)}):
                with self.assertRaisesRegex(ledger_tools.ToolNotFoundError, "LEDGER_TESSERACT_PATH"):
                    ledger_tools.configure_tesseract()
        self.assertEqual(self.pytesseract.tesseract_cmd, "tesseract")

    def test_missing_tools_raise_actionable_errors(self):
        with self.assertRaisesRegex(ledger_tools.ToolNotFoundError, "Tesseract OCR.*LEDGER_TESSERACT_PATH"):
            ledger_tools.configure_tesseract()
        with self.assertRaisesRegex(ledger_tools.ToolNotFoundError, "MiKTeX or TeX Live.*LEDGER_XELATEX_PATH"):
            ledger_tools.xelatex_command()


if __name__ == "__main__":
    unittest.main()
