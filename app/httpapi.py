"""HTTP interface: backfill submission, snapshots and change continuation.

Endpoints
---------
POST /events                 submit a backfill batch (array or {"events":[]})
GET  /snapshot               full snapshot at the current version
GET  /snapshot/{entity}      one entity; empty candidate lists if unknown/pending
GET  /changes?cursor=N       whole change batches with version strictly > N

All payloads are JSON. Rejected submissions return HTTP 422 with a stable
reason code and never modify state.
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg2

from .db import connect
from .engine import Engine
from .validation import RejectBatch, normalise_batch

ENTITY_PATH_RE = re.compile(r"^/snapshot/([A-Za-z0-9]+)$")


def _json_bytes(obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return status, body


class Handler(BaseHTTPRequestHandler):
    server_version = "offline-replay/1.0"

    def log_message(self, fmt, *args):  # quiet, concise access log
        self.server.log_line("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send(*_json_bytes(obj, status))

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        conn = connect()
        try:
            engine = Engine(conn)
            if path == "/snapshot":
                version, entities = engine.read_snapshot()
                self._send_json({"version": version, "entities": entities})
                return
            m = ENTITY_PATH_RE.match(path)
            if m:
                version, body = engine.read_snapshot(m.group(1))
                if body is None:
                    # unknown or only-pending entity: empty candidates,
                    # never published as a snapshot entity
                    self._send_json({
                        "version": version,
                        "entity": m.group(1),
                        "existence": [], "a": [], "b": [],
                    })
                else:
                    self._send_json({"version": version, **body})
                return
            if path == "/changes":
                raw = query.get("cursor", ["0"])[0]
                if not re.fullmatch(r"\d+", raw):
                    self._send_json(
                        {"error": "bad_cursor", "detail": raw}, 400)
                    return
                batches = engine.read_changes(int(raw))
                self._send_json({"cursor": int(raw), "batches": batches})
                return
            self._send_json({"error": "not_found", "detail": path}, 404)
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/events":
            self._send_json({"error": "not_found",
                             "detail": parsed.path}, 404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw_bytes = self.rfile.read(length) if length else b""
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json({"error": "invalid_json", "detail": str(exc)}, 400)
            return

        try:
            canonical = normalise_batch(raw)
        except RejectBatch as exc:
            self._send_json({"error": exc.reason,
                             "detail": exc.detail}, 422)
            return

        conn = connect()
        try:
            # The engine takes a transaction-scoped advisory lock that
            # serialises all submissions, so the default READ COMMITTED
            # level is sufficient: every transaction observes a complete,
            # previously committed version. A 40001 is still retried
            # defensively.
            engine = Engine(conn)
            for attempt in range(5):
                try:
                    result = engine.submit(canonical)
                    break
                except psycopg2.Error as exc:
                    conn.rollback()
                    if exc.pgcode == "40001" and attempt < 4:
                        continue
                    raise
            result["accepted"] = not result.get("retransmitted", False)
            self._send_json(result, 200)
        except RejectBatch as exc:
            conn.rollback()
            self._send_json({"error": exc.reason,
                             "detail": exc.detail}, 422)
        except psycopg2.Error as exc:
            conn.rollback()
            self._send_json({"error": "db_error", "pgcode": exc.pgcode,
                             "detail": str(exc.diag.message_primary)}, 500)
        except Exception as exc:  # pragma: no cover - defensive
            conn.rollback()
            self._send_json({"error": "internal", "detail": str(exc)}, 500)
        finally:
            conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr):
        super().__init__(addr, Handler)
        self._log_lock = threading.Lock()

    def log_line(self, line):
        with self._log_lock:
            print(line, flush=True)


def main():
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8080"))
    server = Server((host, port))
    print("listening on http://%s:%d" % (host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
