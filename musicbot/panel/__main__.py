"""Start the control panel (which starts the bot):  python -m musicbot.panel"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import webbrowser

from aiohttp import web

from .. import settings
from .server import make_app, sup


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("PANEL_PORT", "8484"))
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Already running (start.bat opened twice)? Starting again would start a second bot on the
    # same token before failing on the port, so just point at the running panel instead.
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            print(f"The panel is already running: http://localhost:{port}")
            if os.environ.get("OPEN_BROWSER") == "1":
                webbrowser.open(f"http://localhost:{port}")
            return
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1", "::") else host
    sup.panel_log(f"Control panel on http://{shown}:{port}  (data in {settings.DATA_DIR})")
    if os.environ.get("OPEN_BROWSER") == "1":
        threading.Timer(1.5, webbrowser.open, (f"http://localhost:{port}",)).start()
    web.run_app(make_app(), host=host, port=port, print=None, access_log=None, shutdown_timeout=3)


if __name__ == "__main__":
    main()
