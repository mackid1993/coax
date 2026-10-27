"""Shared helpers for the providers tests: a fake provider on 127.0.0.1 (no real network).

Contains no test cases itself; named test_providers_* to stay inside the providers file scope.
"""

import gzip
import os
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "providers")


def fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeProvider:
    """Serves scripted responses. `routes` maps a key to a list of (status, headers, body).

    Keys: "account", "get_live_categories", "get_live_streams" for player_api.php, or a path
    like "/list.m3u". The last response in a list repeats once the others are used up.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                if parsed.path == "/player_api.php":
                    key = query.get("action", "account")
                else:
                    key = parsed.path
                fake.requests.append({"key": key, "query": query, "headers": dict(self.headers)})
                script = fake.routes.get(key)
                if not script:
                    status, headers, body = 404, {}, b"not found"
                elif len(script) > 1:
                    status, headers, body = script.pop(0)
                else:
                    status, headers, body = script[0]
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        # Plain TCP server: HTTPServer.server_bind calls getfqdn, which can stall for 30s on macOS.
        self.server = _Server(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def set(self, key, *responses):
        self.routes[key] = list(responses)

    def json(self, key, fixture, status=200):
        self.set(key, (status, {"Content-Type": "application/json"}, fixture_bytes(fixture)))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def gzipped(data):
    return gzip.compress(data)
