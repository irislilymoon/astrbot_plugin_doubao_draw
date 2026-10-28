# -*- coding: utf-8 -*-
"""给 AstrBot 沙箱插件页用的 noVNC 静态服务：附加 CORS 头，复用现有 websockify。"""

from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 6081
    ROOT = sys.argv[2] if len(sys.argv) > 2 else "/usr/share/novnc"
    os.chdir(ROOT)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
