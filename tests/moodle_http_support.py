from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar


class RecordingHandler(BaseHTTPRequestHandler):
    status: ClassVar[int] = 200
    headers_to_send: ClassVar[dict[str, str]] = {"Content-Type": "application/json"}
    body: ClassVar[bytes] = b"{}"
    requests: ClassVar[list[tuple[str, str, bytes]]]

    def do_GET(self) -> None:  # noqa: N802
        self._reply()

    def do_POST(self) -> None:  # noqa: N802
        self._reply()

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self).requests.append((self.command, self.path, self.rfile.read(length)))
        self.send_response(type(self).status)
        for name, value in type(self).headers_to_send.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(type(self).body)


@contextmanager
def moodle_server(
    status: int = 200, body: bytes = b"{}", headers: dict[str, str] | None = None
) -> Iterator[tuple[str, type[RecordingHandler]]]:
    RecordingHandler.status = status
    RecordingHandler.body = body
    RecordingHandler.headers_to_send = headers or {"Content-Type": "application/json"}
    RecordingHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", RecordingHandler
    finally:
        server.shutdown()
        thread.join()
