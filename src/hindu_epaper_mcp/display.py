"""Virtual X display for running a visible (headful) Chromium on a server.

Google is far more willing to let a real, headful browser sign in than a
headless one, so in remote mode we start Xvfb when no display exists and run
Chromium on it. Nobody looks at that display directly: the login page streams
the browser to the user's phone instead.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import time
from pathlib import Path

_proc: subprocess.Popen | None = None


def ensure_display() -> bool:
    """Make sure DISPLAY points at a usable X server. Return False if none is possible."""
    global _proc
    if os.environ.get("DISPLAY"):
        return True
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        return False
    for num in range(99, 120):
        if Path(f"/tmp/.X11-unix/X{num}").exists() or Path(f"/tmp/.X{num}-lock").exists():
            continue
        _proc = subprocess.Popen(
            [xvfb, f":{num}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if Path(f"/tmp/.X11-unix/X{num}").exists():
                os.environ["DISPLAY"] = f":{num}"
                atexit.register(_stop)
                return True
            if _proc.poll() is not None:
                break
            time.sleep(0.1)
        _stop()
    return False


def _stop() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
    _proc = None
