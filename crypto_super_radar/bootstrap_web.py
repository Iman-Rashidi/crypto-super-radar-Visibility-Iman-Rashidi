"""Open the Render web port before importing the full dashboard application.

The dashboard imports many research and exchange modules. This small bootstrap
binds the injected Render port first, then loads the application on a background
thread. During initialization, requests receive 503 until the real handler is
ready; /health then delegates to the full application's readiness endpoint.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_HANDLER: type[BaseHTTPRequestHandler] | None = None
APP_ERROR: str | None = None
APP_READY = threading.Event()


class BootstrapHandler(BaseHTTPRequestHandler):
    server_version = "CryptoSuperRadarBootstrap/1.0"

    def _respond_booting(self) -> None:
        body = json.dumps(
            {"status": "starting", "application_ready": False},
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self) -> None:
        handler = APP_HANDLER
        if handler is None:
            self._respond_booting()
            return
        original_class = self.__class__
        try:
            self.__class__ = handler
            method = getattr(handler, "do_" + self.command, None)
            if method is None:
                self.send_error(405, "Method not supported")
            else:
                method(self)
        finally:
            self.__class__ = original_class

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_OPTIONS(self) -> None:
        self._dispatch()

    def log_message(self, fmt: str, *args: object) -> None:
        print("BOOTSTRAP_HTTP " + fmt % args, flush=True)


def _initialize_application() -> None:
    global APP_HANDLER, APP_ERROR
    try:
        from . import web_app

        APP_HANDLER = web_app.Handler
        role = os.getenv("BLOFIN_DEMO_AUTOPILOT_ROLE", "WEB").strip().upper() or "WEB"
        if role == "WEB":
            web_app._start_notion_sync_worker()
            web_app._start_blofin_demo_position_manager()
        web_app._start_blofin_demo_autopilot()
        APP_READY.set()
        print("CRYPTO SUPER RADAR application handler ready", flush=True)
    except BaseException as exc:
        APP_ERROR = type(exc).__name__
        print("CRYPTO SUPER RADAR bootstrap failed: " + APP_ERROR, flush=True)
        traceback.print_exc()


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer((host, port), BootstrapHandler)
    server.daemon_threads = True
    print(f"CRYPTO SUPER RADAR bootstrap listening on {host}:{port}", flush=True)
    threading.Thread(
        target=_initialize_application,
        daemon=True,
        name="crypto-super-radar-app-init",
    ).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
