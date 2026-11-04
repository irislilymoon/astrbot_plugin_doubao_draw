# -*- coding: utf-8 -*-
"""Windows 调试（CDP 画面）。

对标 dd_vnc.py：Linux 用 Xvfb + x11vnc + noVNC 把虚拟屏画面转出来；
Windows 没有 X11，改用 CDP 的 Page.startScreencast 取画面帧、
Input.* 回送鼠标键盘，浏览器打开地址即可看、可操作。

两种使用方式：
  1. 被 main.py import，用 WinVncManager 管（status / ensure，供插件面板调用）
  2. 独立运行：python dd_winvnc.py --serve --port 6090 --cdp http://127.0.0.1:9222
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_PORT = 6090
DEFAULT_CDP = "http://127.0.0.1:9222"
DEFAULT_PASSWORD = "iris"   # 与 dd_vnc.VncManager.DEFAULT_PASSWORD 保持一致
VIEW_W, VIEW_H = 1280, 800

DD_DIR = Path(__file__).resolve().parent
SERVE_SCRIPT = DD_DIR / "dd_winvnc.py"


def _is_windows() -> bool:
    return os.name == "nt"


# =====================================================================
# 管理器（给 main.py 用）
# =====================================================================


class WinVncManager:
    """管理 Windows 调试服务的生命周期。接口对齐 dd_vnc.VncManager。"""

    def __init__(self, port: int = DEFAULT_PORT, cdp_url: str = DEFAULT_CDP,
                 logger=None, password: str = DEFAULT_PASSWORD,
                 auth_enabled: bool = True, bind: str = "127.0.0.1"):
        self.port = int(port) if str(port).strip() else DEFAULT_PORT
        self.cdp_url = (cdp_url or DEFAULT_CDP).strip()
        self.log = logger
        self.password = str(password or "")
        self.auth_enabled = bool(auth_enabled) and bool(self.password)
        self.bind = (bind or "").strip()
        self.default_password_locked = bool(
            self.auth_enabled and self.password == DEFAULT_PASSWORD
        )
        self._proc: subprocess.Popen | None = None

    @property
    def effective_bind(self) -> str:
        """默认密码时强制只监听本机，语义与 dd_vnc 完全一致。"""
        if self.auth_enabled and self.default_password_locked:
            return "127.0.0.1"
        return self.bind or "127.0.0.1"

    # ---------- 对外 ----------

    async def status(self) -> dict:
        info = self._probe()
        ready = bool(info)
        return {
            "supported": _is_windows(),
            "ready": ready,
            "embed_ready": ready,
            "web_port": self.port,
            "viewer_path": "",
            "public_url": "",
            "bind": self.effective_bind,
            "auth_enabled": self.auth_enabled,
            "password_is_default": bool(
                self.auth_enabled and self.password == DEFAULT_PASSWORD
            ),
            "default_password_locked": self.default_password_locked,
            "error": "" if ready else (
                "Windows 调试服务未启动，点“启动 / 修复”"
                if _is_windows() else "仅 Windows 需要，当前系统不是 Windows"
            ),
            "page": (info or {}).get("page"),
            "size": (info or {}).get("size"),
            "clients": (info or {}).get("clients", 0),
            "cdp": self.cdp_url,
        }

    async def ensure(self) -> dict:
        if not _is_windows():
            return await self.status()
        if self._probe():
            return await self.status()
        self._spawn()
        for _ in range(30):
            await asyncio.sleep(0.4)
            if self._probe():
                break
        return await self.status()

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- 内部 ----------

    def _probe(self) -> dict | None:
        """请求本地 /health，返回服务状态；连不上返回 None。"""
        url = f"http://127.0.0.1:{self.port}/health"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return None

    def _spawn(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        cmd = [
            sys.executable,
            str(SERVE_SCRIPT),
            "--serve",
            "--port", str(self.port),
            "--cdp", self.cdp_url,
            "--bind", self.effective_bind,
        ]
        if self.auth_enabled:
            cmd += ["--password", self.password]
        kwargs: dict = {
            "cwd": str(DD_DIR),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if _is_windows():
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(cmd, **kwargs)
            if self.log:
                self.log.info(
                    f"[doubao_draw] Windows 调试服务已启动：http://127.0.0.1:{self.port}"
                )
        except Exception as e:
            self._proc = None
            if self.log:
                self.log.warning(f"[doubao_draw] Windows 调试服务启动失败：{e}")


# =====================================================================
# 服务端（子进程）
# =====================================================================


PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>豆包浏览器画面</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; overflow: hidden; }
  body { background: #0f1220; color: #e8eaf2;
         font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         display: flex; flex-direction: column; }
  .bar { flex: none; display: flex; align-items: center; gap: 10px; padding: 6px 12px;
         background: #171b2d; border-bottom: 1px solid #2a3050; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #f7b955; flex: none; }
  .dot.ok { background: #39d98a; }
  .dot.err { background: #ff6b6b; }
  #stat { color: #8b93ab; }
  #info { margin-left: auto; color: #67708a; font-variant-numeric: tabular-nums; }
  #stage { flex: 1 1 auto; min-height: 0; display: flex; align-items: center;
           justify-content: center; overflow: hidden; padding: 10px; }
  #screen { display: block; max-width: 100%; max-height: 100%; border-radius: 6px;
            background: #0b0c0f; outline: 1px solid #2a3050; }
  #hint { position: absolute; inset: 0; display: flex; align-items: center;
          justify-content: center; color: #67708a; }
</style>
</head>
<body>
<div class="bar">
  <span id="dot" class="dot"></span>
  <span id="stat">连接中…</span>
  <span id="info"></span>
</div>
<div id="stage" style="position:relative">
  <img id="screen" alt="浏览器画面">
  <div id="hint">等待画面…</div>
</div>
<script>
(() => {
  const img = document.getElementById('screen');
  const dot = document.getElementById('dot');
  const stat = document.getElementById('stat');
  const info = document.getElementById('info');
  const hint = document.getElementById('hint');
  let ws = null, frames = 0, lastT = 0, vw = 1280, vh = 800;

  function fit() {
    const stage = document.getElementById('stage');
    const aw = stage.clientWidth - 20, ah = stage.clientHeight - 20;
    const scale = Math.min(aw / vw, ah / vh);
    img.style.width = Math.round(vw * scale) + 'px';
    img.style.height = Math.round(vh * scale) + 'px';
  }
  window.addEventListener('resize', fit);

  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => { stat.textContent = '已连接'; dot.className = 'dot ok'; };
    ws.onclose = () => {
      stat.textContent = '连接断开，重试中…'; dot.className = 'dot err';
      setTimeout(connect, 1500);
    };
    ws.onerror = () => { dot.className = 'dot err'; };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === 'frame') {
        if (m.w && m.h && (m.w !== vw || m.h !== vh)) { vw = m.w; vh = m.h; fit(); }
        img.src = 'data:image/jpeg;base64,' + m.data;
        hint.style.display = 'none';
        frames++; lastT = performance.now();
      } else if (m.type === 'status') {
        stat.textContent = m.text;
        dot.className = 'dot ' + (m.level === 'err' ? 'err' : m.level === 'ok' ? 'ok' : '');
      }
    };
  }

  setInterval(() => {
    info.textContent = (performance.now() - lastT < 5000)
      ? `${vw}x${vh} · ${frames} 帧` : `${vw}x${vh} · 画面静止`;
  }, 1000);

  function pos(e) {
    const r = img.getBoundingClientRect();
    return {
      x: Math.round((e.clientX - r.left) * vw / r.width),
      y: Math.round((e.clientY - r.top) * vh / r.height),
    };
  }
  function send(o) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }

  let dragging = false;
  img.addEventListener('mousedown', (e) => {
    dragging = true;
    const p = pos(e);
    send({ type: 'mouse', event: 'mousePressed', x: p.x, y: p.y,
           button: 'left', clickCount: 1, buttons: 1 });
    e.preventDefault();
  });
  img.addEventListener('mousemove', (e) => {
    const p = pos(e);
    send({ type: 'mouse', event: 'mouseMoved', x: p.x, y: p.y,
           button: 'none', buttons: dragging ? 1 : 0 });
  });
  document.addEventListener('mouseup', (e) => {
    if (!dragging) return;
    dragging = false;
    const p = pos(e);
    send({ type: 'mouse', event: 'mouseReleased', x: p.x, y: p.y,
           button: 'left', clickCount: 1, buttons: 0 });
  });
  img.addEventListener('wheel', (e) => {
    const p = pos(e);
    send({ type: 'wheel', x: p.x, y: p.y, dx: e.deltaX, dy: e.deltaY });
    e.preventDefault();
  }, { passive: false });

  const SPECIAL = ['Enter', 'Backspace', 'Delete', 'Tab', 'Escape',
                   'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'];
  window.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === 'v') return;
    if (SPECIAL.includes(e.key)) {
      send({ type: 'key', key: e.key });
      e.preventDefault();
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      send({ type: 'text', text: e.key });
      e.preventDefault();
    }
  });
  window.addEventListener('paste', (e) => {
    const t = (e.clipboardData || window.clipboardData).getData('text');
    if (t) { send({ type: 'text', text: t }); e.preventDefault(); }
  });

  connect();
})();
</script>
</body>
</html>
"""


class _Server:
    """CDP 推流 + 输入回送。"""

    def __init__(self, port: int, cdp_url: str):
        self.port = port
        self.cdp_url = cdp_url
        self.clients: set = set()
        self.page = None
        self.sess = None
        self.pw = None
        self.size = (VIEW_W, VIEW_H)

    # ---------- 广播 ----------

    async def broadcast(self, payload: dict) -> None:
        if not self.clients:
            return
        msg = json.dumps(payload)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.clients.discard(d)

    async def status(self, text: str, level: str = "ok") -> None:
        await self.broadcast({"type": "status", "text": text, "level": level})

    # ---------- CDP ----------

    def _on_frame(self, params: dict) -> None:
        data = params.get("data")
        sid = params.get("sessionId")
        if data and self.clients:
            w, h = self.size
            asyncio.create_task(
                self.broadcast({"type": "frame", "data": data, "w": w, "h": h})
            )
        if self.sess is not None and sid is not None:
            asyncio.create_task(
                self.sess.send("Page.screencastFrameAck", {"sessionId": sid})
            )

    async def stop_screencast(self) -> None:
        sess = self.sess
        if sess is not None:
            for coro in (sess.send("Page.stopScreencast"),):
                try:
                    await coro
                except Exception:
                    pass
            try:
                await sess.detach()
            except Exception:
                pass
        self.sess = None

    async def attach(self) -> bool:
        if self.pw is None:
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()

        try:
            browser = await self.pw.chromium.connect_over_cdp(self.cdp_url, timeout=5000)
        except Exception as e:
            await self.status(f"连不上 {self.cdp_url}：浏览器未开启调试端口", "err")
            print(f"[winvnc] connect failed: {e}", flush=True)
            return False

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = None
        for p in ctx.pages:
            if "doubao" in (p.url or ""):
                page = p
                break
        if page is None:
            page = ctx.pages[-1] if ctx.pages else await ctx.new_page()

        await self.stop_screencast()
        try:
            sess = await ctx.new_cdp_session(page)
            sess.on("Page.screencastFrame", self._on_frame)
            await sess.send("Page.startScreencast", {
                "format": "jpeg", "quality": 72,
                "maxWidth": VIEW_W, "maxHeight": VIEW_H,
                "everyNthFrame": 1,
            })
        except Exception as e:
            await self.status(f"启动推流失败：{e}", "err")
            print(f"[winvnc] startScreencast failed: {e}", flush=True)
            return False

        self.page = page
        self.sess = sess
        try:
            vp = page.viewport_size or {}
            self.size = (int(vp.get("width") or VIEW_W), int(vp.get("height") or VIEW_H))
        except Exception:
            self.size = (VIEW_W, VIEW_H)

        await self.status("已连接，画面实时推送中", "ok")
        print(f"[winvnc] attached url={page.url} size={self.size}", flush=True)
        return True

    async def watchdog(self) -> None:
        while True:
            await asyncio.sleep(2)
            need = self.sess is None
            if not need and self.page is not None:
                try:
                    need = self.page.is_closed()
                except Exception:
                    need = True
            if need:
                try:
                    await self.attach()
                except Exception as e:
                    print(f"[winvnc] reattach failed: {e}", flush=True)

    # ---------- 输入 ----------

    async def do_input(self, msg: dict) -> None:
        sess = self.sess
        if sess is None:
            return
        t = msg.get("type")
        try:
            if t == "mouse":
                await sess.send("Input.dispatchMouseEvent", {
                    "type": msg.get("event", "mouseMoved"),
                    "x": msg.get("x", 0), "y": msg.get("y", 0),
                    "button": msg.get("button", "none"),
                    "clickCount": msg.get("clickCount", 0),
                    "buttons": msg.get("buttons", 0),
                })
            elif t == "wheel":
                await sess.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": msg.get("x", 0), "y": msg.get("y", 0),
                    "deltaX": msg.get("dx", 0), "deltaY": msg.get("dy", 0),
                })
            elif t == "text":
                await sess.send("Input.insertText", {"text": msg.get("text", "")})
            elif t == "key":
                table = {
                    "Enter": ("Enter", "Enter", 13), "Backspace": ("Backspace", "Backspace", 8),
                    "Delete": ("Delete", "Delete", 46), "Tab": ("Tab", "Tab", 9),
                    "Escape": ("Escape", "Escape", 27), "Home": ("Home", "Home", 36),
                    "End": ("End", "End", 35), "ArrowUp": ("ArrowUp", "ArrowUp", 38),
                    "ArrowDown": ("ArrowDown", "ArrowDown", 40),
                    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
                    "ArrowRight": ("ArrowRight", "ArrowRight", 39),
                }
                item = table.get(msg.get("key", ""))
                if item:
                    k, code, vk = item
                    for etype in ("keyDown", "keyUp"):
                        await sess.send("Input.dispatchKeyEvent", {
                            "type": etype, "key": k, "code": code,
                            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                        })
        except Exception as e:
            print(f"[winvnc] input failed: {e}", flush=True)


def _make_app(srv: _Server, password: str = ""):
    from aiohttp import WSMsgType, web

    @web.middleware
    async def basic_auth(request, handler):
        # /health 供插件自己探活，不含敏感信息，不做认证
        if not password or request.path == "/health":
            return await handler(request)
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "ignore")
                _, _, given = raw.partition(":")
                if given == password:
                    return await handler(request)
            except Exception:
                pass
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="doubao_windows_debug"'},
            text="需要密码",
        )

    async def index(request):
        return web.Response(text=PAGE_HTML, content_type="text/html", charset="utf-8")

    async def health(request):
        # 未认证也能访问，只暴露最少信息
        return web.json_response({
            "ok": srv.sess is not None,
            "clients": len(srv.clients),
        })

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        srv.clients.add(ws)
        if srv.sess is not None:
            await ws.send_str(json.dumps(
                {"type": "status", "text": "已连接，画面实时推送中", "level": "ok"}
            ))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await srv.do_input(json.loads(msg.data))
                except Exception:
                    pass
            elif msg.type == WSMsgType.ERROR:
                break
        srv.clients.discard(ws)
        return ws

    async def on_startup(app):
        ok = await srv.attach()
        if not ok:
            print("[winvnc] 首次连接失败，watchdog 会继续重试", flush=True)
        app["watch"] = asyncio.create_task(srv.watchdog())

    async def on_cleanup(app):
        task = app.get("watch")
        if task:
            task.cancel()
        await srv.stop_screencast()

    app = web.Application(middlewares=[basic_auth])
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def serve(port: int, cdp_url: str, bind: str = "127.0.0.1", password: str = "") -> None:
    from aiohttp import web

    srv = _Server(port, cdp_url)
    app = _make_app(srv, password)
    auth = "密码保护" if password else "无密码"
    print(f"[winvnc] http://{bind}:{port} -> {cdp_url} ({auth})", flush=True)
    web.run_app(app, host=bind, port=port, print=None)


def _parse_args(argv: list[str]) -> tuple[int, str, str, str]:
    port, cdp, bind, password = DEFAULT_PORT, DEFAULT_CDP, "127.0.0.1", ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a == "--cdp" and i + 1 < len(argv):
            cdp = argv[i + 1]; i += 2; continue
        if a == "--bind" and i + 1 < len(argv):
            bind = argv[i + 1]; i += 2; continue
        if a == "--password" and i + 1 < len(argv):
            password = argv[i + 1]; i += 2; continue
        if a.isdigit():
            port = int(a)
        i += 1
    return port, cdp, bind, password


if __name__ == "__main__":
    _p, _c, _b, _pw = _parse_args(sys.argv[1:])
    serve(_p, _c, _b, _pw)
