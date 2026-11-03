# -*- coding: utf-8 -*-
"""给 AstrBot 沙箱插件页用的 noVNC 静态服务：附加 CORS 头，复用现有 websockify。

用法：python dd_vnc_web.py <port> <novnc_root> [viewer_html] [host]
/ 与 /viewer.html 会被替换成插件自带的查看器，其余路径仍是 noVNC 静态文件。
这里只提供静态页面，不含 RFB 通道；VNC 认证由 x11vnc 负责。
"""

from __future__ import annotations

import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

VIEWER = ""
HOST = "0.0.0.0"
ROOT = "/usr/share/novnc"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path):
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if VIEWER and clean in ("/", "/viewer.html", "/index.html"):
            return VIEWER
        return super().translate_path(path)

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 6081
    ROOT = sys.argv[2] if len(sys.argv) > 2 else "/usr/share/novnc"
    if len(sys.argv) > 3 and os.path.isfile(sys.argv[3]):
        VIEWER = sys.argv[3]
    if len(sys.argv) > 4 and sys.argv[4].strip():
        HOST = sys.argv[4].strip()
    os.chdir(ROOT)
    ThreadingHTTPServer((HOST, port), Handler).serve_forever()
