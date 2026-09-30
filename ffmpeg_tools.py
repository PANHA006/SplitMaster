"""Shared FFmpeg / FFprobe binary resolver.

Resolution order (first match wins):
  1. Environment override  ->  FFMPEG_BIN / FFPROBE_BIN
  2. PyInstaller bundle    ->  <sys._MEIPASS>/<name>.exe
  3. App-local bin/ folder ->  <app>/bin/<name>.exe
  4. System PATH           ->  shutil.which(<name>)
  5. WinGet package glob   ->  %LOCALAPPDATA%\\Microsoft\\WinGet\\Packages\\*FFmpeg*\\bin\\<name>.exe
  6. Common install paths  ->  Program Files / Chocolatey / Scoop
"""

import glob
import os
import shutil
import sys

_BINARY_NAMES = ("ffmpeg", "ffprobe")


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _candidate_paths(name: str):
    exe = f"{name}.exe" if sys.platform == "win32" else name

    # 1. Explicit environment override
    env_value = os.environ.get(f"{name.upper()}_BIN")
    if env_value:
        yield env_value
        # Also allow pointing at the bin folder itself
        yield os.path.join(env_value, exe)

    # 2. PyInstaller one-file bundle
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield os.path.join(meipass, exe)
        yield os.path.join(meipass, "bin", exe)

    # 3. App-local bin/ folder (next to main.py or the .exe)
    yield os.path.join(_app_dir(), "bin", exe)
    yield os.path.join(_app_dir(), exe)

    # 4. System PATH
    which_path = shutil.which(name)
    if which_path:
        yield which_path

    # 5. WinGet package cache (version-agnostic)
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            pattern = os.path.join(
                local_appdata, "Microsoft", "WinGet", "Packages",
                "Gyan.FFmpeg*", "*", "bin", exe,
            )
            for match in sorted(glob.glob(pattern), reverse=True):
                yield match

        # 6. Common manual installs
        for root in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            r"C:\ffmpeg",
            r"C:\ProgramData\chocolatey\bin",
            os.path.join(os.path.expanduser("~"), "scoop", "shims"),
        ):
            if root:
                yield os.path.join(root, exe)
                yield os.path.join(root, "ffmpeg", "bin", exe)


def find_binary(name: str) -> str | None:
    """Return the first existing path to the requested binary, or None."""
    if name not in _BINARY_NAMES:
        raise ValueError(f"Unsupported binary name: {name}")

    for candidate in _candidate_paths(name):
        try:
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        except OSError:
            continue
    return None


def find_ffmpeg() -> str | None:
    return find_binary("ffmpeg")


def find_ffprobe() -> str | None:
    return find_binary("ffprobe")


if __name__ == "__main__":
    for binary in _BINARY_NAMES:
        print(f"{binary}: {find_binary(binary)}")
