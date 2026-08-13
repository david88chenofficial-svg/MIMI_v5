import argparse
from pathlib import Path
import os
import socket
import subprocess
import sys
import threading
import uuid
import webbrowser

from MIMI_control import clear_server_state, write_server_state


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
START_PORT = 8000


def find_free_port(start_port):
    for port in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("Could not find a free port between 8000 and 8099.")


def find_chrome():
    candidates = [
        os.environ.get("CHROME_PATH"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate

    return None


def open_in_chrome(url):
    chrome = find_chrome()
    if chrome:
        subprocess.Popen([chrome, url])
        return

    try:
        webbrowser.get("chrome").open(url)
    except webbrowser.Error:
        webbrowser.open(url)


def fresh_launch_url(port, *, source=None):
    source_query = f"&source={source}" if source else ""
    return f"http://localhost:{port}/?launch={uuid.uuid4().hex}{source_query}"


def main(*, pop_phone=False):
    if not WEB_DIR.exists():
        print(f"Could not find web folder: {WEB_DIR}", file=sys.stderr)
        return 1

    from MIMI import request_abort, run_bundle_in_background
    from MIMI_dashboard import serve_web

    port = find_free_port(START_PORT)
    control_token = uuid.uuid4().hex
    url = fresh_launch_url(
        port,
        source="pop-phone" if pop_phone else None,
    )

    print(f"Launching MIMI at {url}")
    print("The server will stop automatically after the MIMI page is closed.")
    browser_timer = threading.Timer(0.5, open_in_chrome, args=(url,))
    browser_timer.daemon = True
    browser_timer.start()

    write_server_state(
        pid=os.getpid(),
        port=port,
        token=control_token,
    )
    try:
        serve_web(
            "127.0.0.1",
            port,
            run_bundle_in_background,
            abort_run=request_abort,
            auto_shutdown=True,
            control_token=control_token,
        )
    except KeyboardInterrupt:
        print("\nStopping MIMI launcher.")
    else:
        print("MIMI page closed. Server stopped.")
    finally:
        clear_server_state(os.getpid())

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch the MIMI browser GUI.")
    parser.add_argument(
        "--pop-phone",
        action="store_true",
        help="open MIMI in Native Union POP Phone voice-intake mode",
    )
    raise SystemExit(main(pop_phone=parser.parse_args().pop_phone))

