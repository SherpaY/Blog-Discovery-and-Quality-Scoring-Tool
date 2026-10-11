"""Daily scheduling on macOS with launchd (built in, free).

launchd runs the job at the chosen time while you are logged in. If the Mac was
asleep at that time, launchd runs it once when the Mac wakes. If the Mac is shut
down, the run is skipped. Overlapping runs are prevented by the scan lock.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .config import PROJECT_ROOT

LABEL = "com.blogqualityscanner.daily"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_content(hour: int, minute: int, python: str | None = None, project_dir: Path | None = None) -> str:
    python = python or sys.executable
    project_dir = project_dir or PROJECT_ROOT
    log_dir = project_dir / "logs"
    args = [python, "-m", "scanner", "scan", "--trigger", "scheduled", "--auto-resume"]
    arg_xml = "\n".join(f"        <string>{escape(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{arg_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{escape(str(project_dir))}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{int(hour)}</integer>
        <key>Minute</key>
        <integer>{int(minute)}</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>{escape(str(log_dir / 'launchd.out.log'))}</string>
    <key>StandardErrorPath</key>
    <string>{escape(str(log_dir / 'launchd.err.log'))}</string>
    <key>RunAtLoad</key>
    <false/>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _is_mac() -> bool:
    return platform.system() == "Darwin"


def install(hour: int, minute: int) -> str:
    content = plist_content(hour, minute)
    if not _is_mac():
        return ("launchd is only available on macOS. Plist that would be installed:\n\n" + content)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "logs").mkdir(exist_ok=True)
    PLIST_PATH.write_text(content)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST_PATH)], capture_output=True)
    res = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST_PATH)], capture_output=True, text=True)
    if res.returncode != 0:
        return f"Wrote {PLIST_PATH} but launchctl failed: {res.stderr.strip()}"
    return f"Daily scan scheduled at {hour:02d}:{minute:02d}. Job file: {PLIST_PATH}"


def uninstall() -> str:
    if not _is_mac():
        return "launchd is only available on macOS."
    if not PLIST_PATH.exists():
        return "No scheduled scan is installed."
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST_PATH)], capture_output=True)
    PLIST_PATH.unlink()
    return "Daily scan removed."


def status() -> str:
    if not _is_mac():
        return "launchd is only available on macOS."
    if not PLIST_PATH.exists():
        return "Not scheduled."
    res = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    loaded = "loaded" if res.returncode == 0 else "file present but not loaded"
    return f"Scheduled ({loaded}). Job file: {PLIST_PATH}"
