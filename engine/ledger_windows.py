"""Per-user configuration and daemon lifecycle for the Windows desktop app."""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ledger"
ENV_PATH = CONFIG_DIR / ".env"
SA_PATH = CONFIG_DIR / "sa.json"
INIT_PATH = CONFIG_DIR / "initialized"
DAEMON_PATH = Path(__file__).resolve().parents[1] / "daemon" / "vertex-api-daemon.py"


def workspace_path():
    path = Path(os.environ.get("LEDGER_WORKSPACE_PATH") or Path.home() / "Ledger Workspace").expanduser()
    if not path.is_absolute():
        raise RuntimeError("LEDGER_WORKSPACE_PATH must be an absolute path.")
    return path


class AlreadyRunningError(RuntimeError):
    """The Windows Ledger instance for this user already holds its lock."""


class WindowsRuntime:
    def __init__(self, config_dir=CONFIG_DIR):
        self.config_dir = Path(config_dir)
        self.env_path = self.config_dir / ENV_PATH.name
        self.sa_path = self.config_dir / SA_PATH.name
        self.init_path = self.config_dir / INIT_PATH.name
        self.log_path = self.config_dir / "daemon.log"
        self._lock = None
        self._process = None
        self._port = None
        self._token = None

    def __enter__(self):
        import msvcrt

        self.config_dir.mkdir(parents=True, exist_ok=True)
        lock = (self.config_dir / ".instance.lock").open("a+b")
        try:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            lock.close()
            raise AlreadyRunningError("Ledger is already open for this Windows user.") from exc
        self._lock = lock
        return self

    def __exit__(self, *_):
        try:
            self.close()
        finally:
            if self._lock is not None:
                self._lock.close()
                self._lock = None

    def start(self):
        if self._process is not None and self._process.poll() is None:
            return
        if not self.env_path.is_file():
            raise RuntimeError("Set up a provider before starting the Ledger daemon.")
        self.close()
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory(prefix=".daemon-", dir=self.config_dir) as startup:
            ready_file = Path(startup) / "ready"
            child_env = {
                **os.environ,
                "LEDGER_ENV_PATH": str(self.env_path),
                "LEDGER_DAEMON_READY_FILE": str(ready_file),
                "LEDGER_DAEMON_LOCAL_TOKEN": token,
                "LEDGER_WORKSPACE_PATH": str(workspace_path()),
            }
            if self.sa_path.is_file():
                child_env["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.sa_path)
            with self.log_path.open("wb") as log:
                self._process = subprocess.Popen(
                    [sys.executable, "-u", str(DAEMON_PATH)],
                    env=child_env, cwd=DAEMON_PATH.parent,
                    stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline and self._process.poll() is None:
                    try:
                        port = int(ready_file.read_text(encoding="ascii"))
                    except (FileNotFoundError, ValueError):
                        time.sleep(0.1)
                        continue
                    if not 1 <= port <= 65535:
                        break
                    self._port, self._token = port, token
                    self.request("GET", "/", timeout=2).raise_for_status()
                    return
            except requests.RequestException:
                pass
            finally:
                if self._port is None:
                    self.close()
        self.close()
        raise RuntimeError(f"Ledger's daemon could not start. See {self.log_path}.")

    def request(self, method, endpoint, **kwargs):
        if self._port is None or self._process is None or self._process.poll() is not None:
            raise requests.ConnectionError("Ledger's Windows daemon is stopped. Reopen Ledger to start it.")
        with requests.Session() as session:
            session.trust_env = False
            session.headers["Authorization"] = f"Bearer {self._token}"
            return session.request(
                method, f"http://127.0.0.1:{self._port}{endpoint}", allow_redirects=False, **kwargs,
            )

    def close(self):
        process, self._process = self._process, None
        self._port = self._token = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def save_setup(self, env_text, service_account_path, marker_text):
        """Install private configuration and mark setup complete after daemon readiness."""
        if self.init_path.exists():
            raise RuntimeError("Reset the current Ledger configuration before setting it up again.")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.close()
        config_files = (self.env_path, self.sa_path, self.init_path)
        with tempfile.TemporaryDirectory(prefix=".setup-", dir=self.config_dir) as directory:
            stage = Path(directory)
            (stage / self.env_path.name).write_text(env_text, encoding="utf-8")
            (stage / self.init_path.name).write_text(marker_text, encoding="utf-8")
            if service_account_path is not None:
                shutil.copyfile(service_account_path, stage / self.sa_path.name)

            backed_up = []
            replaced = []
            try:
                for path in config_files:
                    if path.exists():
                        path.replace(stage / (path.name + ".previous"))
                        backed_up.append(path)
                for path in config_files[:2]:
                    source = stage / path.name
                    if source.is_file():
                        source.replace(path)
                        replaced.append(path)
                self.start()
                (stage / self.init_path.name).replace(self.init_path)
            except (OSError, RuntimeError):
                self.close()
                for path in replaced:
                    path.unlink(missing_ok=True)
                for path in backed_up:
                    (stage / (path.name + ".previous")).replace(path)
                raise

    def reset_setup(self):
        self.close()
        # Remove the readiness marker first so an interrupted reset returns to setup.
        for path in (self.init_path, self.env_path, self.sa_path):
            path.unlink(missing_ok=True)
