#!/usr/bin/env python3
"""Serve the viewer at http://localhost:8777 with browser caching disabled,
so a rebuilt model shows up on a normal reload.  Usage: python3 serve.py [port]"""
import functools
import http.server
import os
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, *args):  # keep the terminal quiet
        pass


if __name__ == "__main__":
    handler = functools.partial(NoCacheHandler, directory=ROOT)
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler) as httpd:
        print(f"The Gap 3D: http://localhost:{PORT}  (Ctrl+C to stop)")
        httpd.serve_forever()
