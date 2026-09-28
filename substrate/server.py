"""Local demo server: serves the dashboard and implements the same /api routes
as the Cloudflare Worker, so the full demo runs on a laptop with no hosting."""
from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import backtest, chat, config, db, parts, scoring
from . import bom as bom_mod


def _part_payload(mpn: str) -> dict:
    with db.open_db() as con:
        snap = parts.lookup(con, mpn)
        mpn_n = parts.normalize_mpn(mpn)
        bom_row = next((r for r in bom_mod.load(con) if r["mpn"] == mpn_n), None)
        res = scoring.score_part(con, mpn_n, bom_row, {}, backtest.load_calibration())
        db.dump_warehouse(con)
    return {"mpn": mpn_n, "found": bool(snap), "segment": res["segment"], "supply_risk": res["supply_risk"],
            "price_trend": res["price_trend"], "confidence": res["confidence"], "labels": res["labels"],
            "signals": res["signals"][:10], "snapshot": snap}


class Handler(SimpleHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type, x-access-code")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json({})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/part":
            mpn = (parse_qs(u.query).get("mpn") or [""])[0]
            return self._json(_part_payload(mpn) if mpn else {"error": "mpn required"}, 200 if mpn else 400)
        if u.path == "/api/health":
            return self._json({"ok": True, "providers": parts.configured(), "llm": bool(config.GEMINI_API_KEY)})
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/chat":
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        msgs = [m for m in req.get("messages", []) if m.get("role") in ("user", "assistant")][-12:]
        part = _part_payload(req["mpn"]) if req.get("mpn") else None
        return self._json(chat.answer(msgs, part))

    def log_message(self, fmt, *args):
        pass


def serve(port: int = 8787) -> None:
    site = config.ROOT / "site"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(site)))
    print(f"Substrate running at http://localhost:{port}  (Ctrl+C to stop)")
    httpd.serve_forever()
