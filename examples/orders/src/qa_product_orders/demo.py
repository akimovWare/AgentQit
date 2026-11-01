"""Synthetic loopback-only application. This is an example backend, not runtime business logic."""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class OrderState:
    def __init__(self, data_file: Path | None = None) -> None:
        self._lock = threading.RLock()
        self.data_file = data_file
        self.orders = {}
        self.deliveries = {}
        if data_file is not None and data_file.exists():
            data = json.loads(data_file.read_text())
            self.orders, self.deliveries = data["orders"], data["deliveries"]

    def snapshot(self, order_id: str | None = None):
        with self._lock:
            if order_id is not None:
                return dict(self.orders[order_id]) if order_id in self.orders else None
            return [dict(value) for value in self.orders.values()]

    def change(self, action: str, delivery: str, title: str = "", order_id: str | None = None):
        with self._lock:
            identity = f"{action}:{order_id or ''}:{delivery}"
            if identity in self.deliveries:
                return dict(self.orders[self.deliveries[identity]])
            if action == "create":
                if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
                    raise ValueError("title must contain 1..100 characters")
                order_id = str(uuid.uuid4())
                self.orders[order_id] = {"id": order_id, "title": title.strip(), "status": "CREATED"}
            elif order_id in self.orders:
                self.orders[order_id]["status"] = "CANCELLED"
            else:
                raise KeyError("order not found")
            self.deliveries[identity] = order_id
            if self.data_file is not None:
                self.data_file.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.data_file.with_suffix(".tmp")
                temporary.write_text(json.dumps({"orders": self.orders, "deliveries": self.deliveries}, indent=2))
                temporary.chmod(0o600)
                temporary.replace(self.data_file)
            return dict(self.orders[order_id])


def create_server(port: int = 8765, *, defect_ui: bool = False, data_file: Path | None = None) -> ThreadingHTTPServer:
    state = OrderState(data_file)
    html = Path(__file__).with_name("ui.html").read_text().replace("__DEFECT_UI__", json.dumps(defect_ui)).encode()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, payload, content_type="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/":
                return self.reply(200, html, "text/html; charset=utf-8")
            if path == "/api/orders":
                return self.reply(200, {"orders": state.snapshot()})
            if path.startswith("/api/orders/"):
                order = state.snapshot(path.removeprefix("/api/orders/"))
                return self.reply(200 if order else 404, order or {"error": "not_found"})
            return self.reply(404, {"error": "not_found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 16 * 1024:
                    return self.reply(413, {"error": "body_too_large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    return self.reply(400, {"error": "invalid_input"})
                delivery = self.headers.get("idempotency-key", "")
                if not 1 <= len(delivery) <= 256:
                    return self.reply(400, {"error": "idempotency_key_required"})
                path = urlsplit(self.path).path
                if path == "/api/orders":
                    return self.reply(201, state.change("create", delivery, body.get("title", "")))
                if path.startswith("/api/orders/") and path.endswith("/cancel"):
                    order_id = path.removeprefix("/api/orders/").removesuffix("/cancel")
                    return self.reply(200, state.change("cancel", delivery, order_id=order_id))
                return self.reply(404, {"error": "not_found"})
            except KeyError:
                return self.reply(404, {"error": "not_found"})
            except (ValueError, TypeError):
                return self.reply(400, {"error": "invalid_input"})

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic Orders UI/API on loopback")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--defect-ui", action="store_true", help="Deliberately display stale cancellation status")
    parser.add_argument("--data-file", type=Path, default=Path(".agentqit/orders-demo/records.json"))
    args = parser.parse_args()
    server = create_server(args.port, defect_ui=args.defect_ui, data_file=args.data_file)
    print(f"Synthetic Orders UI: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
