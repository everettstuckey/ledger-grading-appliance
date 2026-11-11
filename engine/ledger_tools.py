"""Locate Ledger's native OCR and PDF tools."""

import os
from pathlib import Path
import shutil
import sys


class ToolNotFoundError(FileNotFoundError):
    """The executable is missing or its configured path is invalid."""


def _path(value):
    return Path(os.path.expanduser(os.path.expandvars(value.strip().strip('"'))))


def _windows_candidates(name):
    executable = name + ".exe"
    # Search only the directories in PATH, not Windows' implicit current directory.
    for entry in os.get_exec_path():
        directory = _path(entry)
        if directory.is_absolute():
            yield directory / executable

    program_dirs = [
        _path(value)
        for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")
        if (value := os.environ.get(key))
    ]
    if local := os.environ.get("LOCALAPPDATA"):
        program_dirs.append(_path(local) / "Programs")
    if name == "tesseract":
        for directory in program_dirs:
            yield directory / "Tesseract-OCR" / executable
        return

    miktex_dirs = [directory / "MiKTeX" for directory in program_dirs]
    if roaming := os.environ.get("APPDATA"):
        miktex_dirs.append(_path(roaming) / "MiKTeX")
    for directory in miktex_dirs:
        yield directory / "miktex" / "bin" / "x64" / executable
        yield directory / "miktex" / "bin" / executable

    texlive = Path((os.environ.get("SystemDrive") or "C:") + "/") / "texlive"
    if not texlive.is_absolute():
        return
    try:
        releases = sorted(
            (entry for entry in texlive.iterdir() if len(entry.name) == 4 and entry.name.isdecimal()),
            key=lambda entry: entry.name,
            reverse=True,
        )
    except OSError:
        return
    for directory in releases:
        yield directory / "bin" / "windows" / executable
        yield directory / "bin" / "win32" / executable


def _macos_candidates(name):
    if name == "xelatex":
        yield Path("/Library/TeX/texbin") / name
    for directory in (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"),
                      Path("/usr/local/bin"), Path("/opt/local/bin")):
        yield directory / name


def _is_executable(path):
    try:
        return path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _resolve(name, setting, install):
    windows = sys.platform == "win32"
    override = os.environ.get(setting)
    if override:
        path = _path(override)
        if _is_executable(path) and (not windows or path.name.casefold() == name + ".exe"):
            return str(path)
        raise ToolNotFoundError(f"Set {setting} to the full path of the {name} executable.")

    if windows:
        for path in _windows_candidates(name):
            if _is_executable(path):
                return str(path)
    else:
        if command := shutil.which(name):
            return os.path.abspath(command)
        if sys.platform == "darwin":
            for path in _macos_candidates(name):
                if _is_executable(path):
                    return str(path)

    raise ToolNotFoundError(f"Install {install} or set {setting} to the full path of the executable.")


def configure_tesseract() -> str:
    """Set and return pytesseract's path to the native Tesseract executable."""
    executable = _resolve("tesseract", "LEDGER_TESSERACT_PATH", "Tesseract OCR with English data")
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = executable
    return executable


def xelatex_command() -> str:
    """Return the path to the native XeLaTeX executable."""
    return _resolve("xelatex", "LEDGER_XELATEX_PATH", "MiKTeX or TeX Live with XeLaTeX")
