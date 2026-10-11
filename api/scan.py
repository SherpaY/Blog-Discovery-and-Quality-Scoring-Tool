"""Vercel serverless function: POST /api/scan runs a time-limited live search.

GET  /api/scan  -> {"access_code_required": bool, "limits": {...}}
POST /api/scan  -> JSON body {"keywords": [...], "competitor_domains": [...],
                              "window_hours": 168, "max_urls": 20, "access_code": "..."}
Returns the same JSON format the dashboard uses for published scans.
"""

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scanner.live import (MAX_DOMAINS, MAX_KEYWORDS, MAX_URLS_LIMIT, WINDOWS, LiveSearchError,  # noqa: E402
                          check_access_code, run_live_search)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api.scan")
MAX_BODY = 10_000


class handler(BaseHTTPRequestHandler):  # noqa: N801 - name required by Vercel
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._send(200, {
            "access_code_required": bool(os.environ.get("SCAN_ACCESS_CODE")),
            "limits": {"keywords": MAX_KEYWORDS, "competitor_domains": MAX_DOMAINS,
                       "max_urls": MAX_URLS_LIMIT, "windows": list(WINDOWS)},
        })

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._send(400, {"error": "request body missing or too large"})
        try:
            params = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(params, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "body must be a JSON object"})
        if not check_access_code(params.get("access_code") or self.headers.get("X-Access-Code")):
            return self._send(401, {"error": "wrong or missing access code"})
        try:
            payload = run_live_search(params)
        except LiveSearchError as exc:
            return self._send(400, {"error": str(exc)})
        except Exception:  # noqa: BLE001 - never leak internals to the browser
            log.exception("live search failed")
            return self._send(500, {"error": "the search failed unexpectedly; check the Vercel function logs"})
        return self._send(200, payload)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)
