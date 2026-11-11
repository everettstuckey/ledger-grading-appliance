"""Exercise the real per-user daemon and instance lock on macOS."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import requests

ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE_DIR))

from ledger_local import LocalRuntime


@unittest.skipUnless(sys.platform == "darwin", "macOS local runtime test")
class MacOSRuntimeTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = root / "Synthetic workspace"
        self.enterContext(patch.dict(os.environ, {"LEDGER_WORKSPACE_PATH": str(self.workspace)}))
        self.config = root / "Application Support" / "Ledger"

    def test_private_configuration_authenticated_daemon_and_reset(self):
        with LocalRuntime(self.config) as runtime:
            runtime.save_setup('LEDGER_CODEX_ENABLED="true"\n', None, "test initialization\n")
            self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(runtime.env_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(runtime.log_path.stat().st_mode), 0o600)

            health = runtime.request("GET", "/", timeout=5)
            health.raise_for_status()
            self.assertEqual(health.json()["codex_route"], "desktop")
            self.assertEqual(Path(health.json()["codex_job_dir"]), (self.workspace / ".Codex Jobs").resolve())
            self.assertTrue(health.url.startswith("http://127.0.0.1:"))
            with requests.Session() as unauthenticated:
                unauthenticated.trust_env = False
                self.assertEqual(unauthenticated.get(health.url, timeout=5).status_code, 401)

            process = runtime._process
            runtime.reset_setup()
            self.assertEqual(process.poll(), 0)
            self.assertFalse(runtime.init_path.exists())
            self.assertFalse(runtime.env_path.exists())

    def test_instance_lock_blocks_another_process_and_releases_on_exit(self):
        code = """
import sys
from ledger_local import AlreadyRunningError, LocalRuntime
try:
    with LocalRuntime(sys.argv[1]):
        pass
except AlreadyRunningError:
    sys.exit(17)
"""
        command = [sys.executable, "-c", code, str(self.config)]
        with LocalRuntime(self.config):
            blocked = subprocess.run(command, cwd=ENGINE_DIR, timeout=5, check=False)
            self.assertEqual(blocked.returncode, 17)
        subprocess.run(command, cwd=ENGINE_DIR, timeout=5, check=True)

    def test_failed_setup_restores_prior_configuration(self):
        with LocalRuntime(self.config) as runtime:
            original = "# prior configuration\n"
            runtime.env_path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "daemon could not start"):
                runtime.save_setup("# no provider\n", None, "test initialization\n")
            self.assertEqual(runtime.env_path.read_text(encoding="utf-8"), original)
            self.assertFalse(runtime.init_path.exists())
            self.assertIsNone(runtime._process)
