"""Windows instance locking and managed Copilot integration for desktop Ledger."""

import os
import secrets
import sys
import threading
from dataclasses import replace
from pathlib import Path

from ledger_local import AlreadyRunningError, DAEMON_PATH, LocalRuntime, workspace_path


CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ledger"
ENV_PATH = CONFIG_DIR / ".env"
SA_PATH = CONFIG_DIR / "sa.json"
INIT_PATH = CONFIG_DIR / "initialized"
sys.path.insert(0, str(DAEMON_PATH.parents[1]))

from bridge.copilot import copilot_bridge


class WindowsRuntime(LocalRuntime):
    def __init__(self, config_dir=CONFIG_DIR):
        super().__init__(config_dir)
        self._copilot_server = None
        self._copilot_thread = None

    def _lock_instance(self, lock):
        import msvcrt

        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise AlreadyRunningError("Ledger is already open for this Windows user.") from exc

    def _start_local_provider(self):
        values = {}
        for line in self.env_path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in {"LEDGER_COPILOT_LOCAL_ACCOUNT", "LEDGER_COPILOT_CDP_PORT"}:
                values[key.strip()] = value.strip().strip('"').strip("'")
        account = values.get("LEDGER_COPILOT_LOCAL_ACCOUNT")
        if not account:
            return {}

        token = secrets.token_urlsafe(32)
        try:
            settings = copilot_bridge.Settings.from_env({
                "COPILOT_BRIDGE_TOKEN": token,
                "COPILOT_EXPECTED_ACCOUNT": account,
                "COPILOT_CDP_PORT": values.get("LEDGER_COPILOT_CDP_PORT", "9337"),
            })
        except copilot_bridge.BridgeError as exc:
            raise RuntimeError(str(exc)) from None
        self._copilot_server = copilot_bridge.BridgeServer(replace(settings, port=0))
        self._copilot_thread = threading.Thread(target=self._copilot_server.serve_forever, daemon=True)
        self._copilot_thread.start()
        return {
            "LEDGER_MANAGED_COPILOT_URL": f"http://127.0.0.1:{self._copilot_server.server_port}",
            "LEDGER_MANAGED_COPILOT_TOKEN": token,
        }

    def _stop_local_provider(self):
        server, self._copilot_server = self._copilot_server, None
        worker, self._copilot_thread = self._copilot_thread, None
        if server is not None:
            if worker is not None and worker.is_alive():
                server.shutdown()
                worker.join()
            server.server_close()
