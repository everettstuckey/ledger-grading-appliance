"""Per-user configuration and managed daemon lifecycle for desktop Ledger."""

import errno
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


DAEMON_PATH = Path(__file__).resolve().parents[1] / "daemon" / "vertex-api-daemon.py"


def state_path():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Ledger"
    return Path.home() / "Library" / "Application Support" / "Ledger"


def workspace_path():
    path = Path(os.environ.get("LEDGER_WORKSPACE_PATH") or Path.home() / "Ledger Workspace").expanduser()
    if not path.is_absolute():
        raise RuntimeError("LEDGER_WORKSPACE_PATH must be an absolute path.")
    return path


class AlreadyRunningError(RuntimeError):
    """The current user's Ledger instance already holds its lock."""


class LocalRuntime:
    def __init__(self, config_dir=None):
        self.config_dir = Path(config_dir) if config_dir is not None else state_path()
        self.env_path = self.config_dir / ".env"
        self.sa_path = self.config_dir / "sa.json"
        self.init_path = self.config_dir / "initialized"
        self.log_path = self.config_dir / "daemon.log"
        self._lock = None
        self._process = None
        self._port = None
        self._token = None

    def _ensure_config_dir(self):
        self.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            self.config_dir.chmod(0o700)

    def _lock_instance(self, lock):
        import fcntl

        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise AlreadyRunningError("Ledger is already open for this macOS user.") from exc

    def __enter__(self):
        self._ensure_config_dir()
        # Keep this file at a stable path. Closing the handle releases the lock
        # even if the app exits unexpectedly.
        lock = (self.config_dir / ".instance.lock").open("a+b")
        try:
            self._lock_instance(lock)
        except BaseException:
            lock.close()
            raise
        self._lock = lock
        return self

    def __exit__(self, *_):
        try:
            self.close()
        finally:
            if self._lock is not None:
                self._lock.close()
                self._lock = None

    def _start_local_provider(self):
        return {}

    def _stop_local_provider(self):
        pass

    def start(self):
        if self._process is not None and self._process.poll() is None:
            return
        if not self.env_path.is_file():
            raise RuntimeError("Set up a provider before starting the Ledger daemon.")
        self.close()
        self._ensure_config_dir()
        token = secrets.token_urlsafe(32)
        started = False
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
            for key in ("LEDGER_MANAGED_COPILOT_URL", "LEDGER_MANAGED_COPILOT_TOKEN"):
                child_env.pop(key, None)
            try:
                child_env.update(self._start_local_provider())
                with self.log_path.open("wb") as log:
                    if os.name == "posix":
                        os.fchmod(log.fileno(), 0o600)
                    self._process = subprocess.Popen(
                        [sys.executable, "-u", str(DAEMON_PATH)],
                        env=child_env, cwd=DAEMON_PATH.parent,
                        stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
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
                    started = True
                    return
            except requests.RequestException:
                pass
            finally:
                if not started:
                    self.close()
        raise RuntimeError(f"Ledger's daemon could not start. See {self.log_path}.")

    def request(self, method, endpoint, **kwargs):
        if self._port is None or self._process is None or self._process.poll() is not None:
            raise requests.ConnectionError("Ledger's daemon is stopped. Reopen Ledger to start it.")
        with requests.Session() as session:
            session.trust_env = False
            session.headers["Authorization"] = f"Bearer {self._token}"
            return session.request(
                method, f"http://127.0.0.1:{self._port}{endpoint}", allow_redirects=False, **kwargs,
            )

    def close(self):
        process, self._process = self._process, None
        self._port = self._token = None
        try:
            if process is not None:
                try:
                    if process.stdin is not None:
                        process.stdin.close()
                finally:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        finally:
            self._stop_local_provider()

    def save_setup(self, env_text, service_account_path, marker_text):
        """Install private configuration and mark setup complete after daemon readiness."""
        if self.init_path.exists():
            raise RuntimeError("Reset the current Ledger configuration before setting it up again.")
        self._ensure_config_dir()
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
                        if os.name == "posix":
                            source.chmod(0o600)
                        source.replace(path)
                        replaced.append(path)
                self.start()
                (stage / self.init_path.name).replace(self.init_path)
            except BaseException:
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
