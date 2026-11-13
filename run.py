"""Start Client Finder: run the local server and open the app window."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from app import config

URL = f"http://{config.HOST}:{config.PORT}"


def already_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((config.HOST, config.PORT)) == 0


def wait_until_up(timeout: float = 30.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(URL + "/api/stats", timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def find_app_browser() -> str | None:
    """Edge or Chrome can show the app in its own window, without tabs or an address bar."""
    if sys.platform != "win32":
        return None
    roots = [os.environ.get(name, "") for name in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    candidates = []
    for root in roots:
        if root:
            candidates.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            candidates.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def open_window() -> None:
    browser = find_app_browser()
    if browser:
        try:
            subprocess.Popen([browser, f"--app={URL}", "--window-size=1360,880"])
            return
        except OSError:
            pass
    webbrowser.open(URL)


def main() -> None:
    if already_running():
        print("Client Finder is already running. Opening its window...")
        open_window()
        return

    def opener() -> None:
        if wait_until_up():
            open_window()

    if os.environ.get("CFA_NO_WINDOW") != "1":
        threading.Thread(target=opener, daemon=True).start()

    import uvicorn

    print(f"Client Finder is running at {URL}")
    print("Keep this window open while you use the app. Close it to stop.")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
