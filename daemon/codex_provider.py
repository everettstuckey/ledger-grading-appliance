"""Filesystem handoff for inference in the signed-in Codex desktop app."""

import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time

from grading_schema import grading_fields, grading_json_schema, serialize_grades


CODEX_DEFAULT_MODEL = "codex/gpt-6.1-sol"
CODEX_MODELS = frozenset({CODEX_DEFAULT_MODEL, "codex/gpt-6-astra"})
_SKILL_PATH = Path(".agents/skills/ledger-grading/SKILL.md")
_MAX_RESULT_BYTES = 16 * 1024 * 1024


class CodexProviderError(RuntimeError):
    """A Codex handoff error safe for the daemon log and GUI."""


class CodexProvider:
    def __init__(self, job_dir, timeout_s):
        path = Path(job_dir) if job_dir else Path.home() / "Ledger Workspace" / ".Codex Jobs"
        if not path.is_absolute():
            raise CodexProviderError("invalid_request: LEDGER_CODEX_JOB_DIR must be an absolute path.")
        try:
            self.timeout_s = float(timeout_s)
        except ValueError:
            raise CodexProviderError("invalid_request: Invalid LEDGER_CODEX_TIMEOUT_S.") from None
        if not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 300:
            raise CodexProviderError("invalid_request: LEDGER_CODEX_TIMEOUT_S must be above zero and at most 300.")
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            _check_directory(path)
            self.job_dir = path.resolve(strict=True)
            _install_skill(self.job_dir)
        except OSError:
            raise CodexProviderError("invalid_request: Could not prepare the private Codex jobs folder and skill.") from None

    def respond(self, model, prompt, pngs, *, instructions="", grading_schema=None, master_image_count=0):
        model = model.lower()
        if model not in CODEX_MODELS:
            raise CodexProviderError("invalid_request: Select codex/gpt-6.1-sol or codex/gpt-6-astra.")
        fields = grading_fields(grading_schema) if grading_schema is not None else None
        try:
            _check_directory(self.job_dir)
            with tempfile.TemporaryDirectory(prefix="job-", dir=self.job_dir) as directory:
                job = Path(directory)
                image_names = []
                for index, png in enumerate(pngs, start=1):
                    name = f"page-{index}.png"
                    (job / name).write_bytes(png)
                    image_names.append(name)
                manifest = {
                    "job_id": job.name,
                    "kind": "grade" if fields is not None else "ocr",
                    "model": model.removeprefix("codex/"),
                    "prompt": prompt,
                    "instructions": instructions,
                    "images": image_names,
                }
                if fields is not None:
                    manifest["master_image_count"] = master_image_count
                    manifest["schema"] = grading_json_schema(fields)
                _atomic_write(job / "request.json", json.dumps(manifest).encode("utf-8"))
                result = self._wait_for_result(job / "result.json")
                return _validate_result(result, job.name, fields)
        except OSError:
            raise CodexProviderError("invalid_request: Could not write, read, or clean up the Codex desktop job.") from None

    def _wait_for_result(self, path):
        deadline = time.monotonic() + self.timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                return _read_result(path)
            except FileNotFoundError:
                time.sleep(min(0.2, remaining))
        raise CodexProviderError(
            "invalid_request: Codex desktop did not finish this job in time. "
            "Open the next Ledger job in the Codex app and complete it before the timeout."
        )


def _check_directory(path):
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode):
        raise CodexProviderError("invalid_request: Codex jobs and skill folders must be real directories.")
    if os.name == "posix" and (details.st_uid != os.getuid() or details.st_mode & 0o077):
        raise CodexProviderError("invalid_request: Codex jobs and skill folders must be private to the service user.")


def _install_skill(job_dir):
    daemon_dir = Path(__file__).resolve().parent
    packaged = daemon_dir / "ledger-grading.SKILL.md"
    source = packaged if packaged.is_file() else daemon_dir.parent / _SKILL_PATH
    skill = source.read_bytes()

    directory = job_dir
    for name in _SKILL_PATH.parts[:-1]:
        directory = directory / name
        directory.mkdir(mode=0o700, exist_ok=True)
        _check_directory(directory)
    _atomic_write(directory / _SKILL_PATH.name, skill)


def _atomic_write(path, content):
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_result(path):
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_RESULT_BYTES:
        raise CodexProviderError("invalid_request: Codex desktop returned an invalid result file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise CodexProviderError("invalid_request: Codex desktop returned an invalid result file.")
        data = handle.read(_MAX_RESULT_BYTES + 1)
    if len(data) > _MAX_RESULT_BYTES:
        raise CodexProviderError("invalid_request: Codex desktop returned an invalid result file.")
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise CodexProviderError("invalid_request: Codex desktop returned malformed result JSON.") from None


def _unique_object(pairs):
    document = dict(pairs)
    if len(document) != len(pairs):
        raise ValueError("Duplicate result key")
    return document


def _validate_result(result, job_id, fields):
    if not isinstance(result, dict) or result.get("job_id") != job_id:
        raise CodexProviderError("invalid_request: Codex desktop result did not match the active job.")
    if fields is not None:
        if result.keys() != {"job_id", "results"}:
            raise CodexProviderError("invalid_request: Codex desktop returned an invalid grading document.")
        return serialize_grades({"results": result["results"]}, fields, "Codex desktop")
    if result.keys() != {"job_id", "text"} or not isinstance(result["text"], str) or not result["text"].strip():
        raise CodexProviderError("invalid_request: Codex desktop returned an invalid OCR result.")
    return result["text"]
