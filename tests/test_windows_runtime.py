"""Exercise the managed daemon with native Windows processes and synthetic setup."""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from test_azure_daemon import png

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from ledger_windows import AlreadyRunningError, WindowsRuntime


TOKEN = "synthetic_windows_provider_bridge_token"


@unittest.skipUnless(sys.platform == "win32", "Windows launcher test")
class WindowsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.config = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = self.enterContext(WindowsRuntime(self.config))

    def test_setup_starts_authenticated_daemon_and_reset_stops_it(self):
        calls = []

        class FakeBridge(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                calls.append((
                    self.path,
                    self.headers.get("Authorization"),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                ))
                response = json.dumps({
                    "status": "success",
                    "model": "copilot/gpt-6.1-sol",
                    "text": '{"bartonian_id": 312}',
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        bridge = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge))
        worker = threading.Thread(target=bridge.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(bridge.shutdown)
        self.runtime.save_setup(
            f'LEDGER_COPILOT_BRIDGE_URL="http://127.0.0.1:{bridge.server_port}"\n'
            f'LEDGER_COPILOT_BRIDGE_TOKEN="{TOKEN}"\n',
            None,
            "synthetic initialization\n",
        )
        with self.assertRaises(AlreadyRunningError):
            with WindowsRuntime(self.config):
                self.fail("The same user opened a second Windows instance.")

        health = self.runtime.request("GET", "/", timeout=5)
        self.assertEqual(health.json()["copilot_default_model"], "copilot/gpt-6.1-sol")
        with requests.Session() as untrusted:
            untrusted.trust_env = False
            self.assertEqual(untrusted.get(health.url, timeout=5).status_code, 401)

        image = png("blue")
        ocr = self.runtime.request("POST", "/api/silo1", json={
            "prompt": 'Transcribe the synthetic ID as {"bartonian_id": 312}.',
            "image_b64": image,
        }, timeout=5)
        self.assertEqual(json.loads(ocr.json()["text"]), {"bartonian_id": 312})
        self.assertEqual(calls[0][:2], ("/review", f"Bearer {TOKEN}"))
        self.assertEqual(calls[0][2]["images_b64"], [image])

        process = self.runtime._process
        self.runtime.reset_setup()
        self.assertEqual(process.poll(), 0)
        self.assertFalse(self.runtime.env_path.exists())
        self.assertFalse(self.runtime.init_path.exists())

    def test_failed_setup_restores_existing_uninitialized_configuration(self):
        original = "# synthetic prior configuration\n"
        self.runtime.env_path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "daemon could not start"):
            self.runtime.save_setup("# no configured provider\n", None, "synthetic setup\n")
        self.assertEqual(self.runtime.env_path.read_text(encoding="utf-8"), original)
        self.assertFalse(self.runtime.init_path.exists())
        self.assertIsNone(self.runtime._process)


if __name__ == "__main__":
    unittest.main()
