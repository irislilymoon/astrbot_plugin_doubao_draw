# -*- coding: utf-8 -*-
"""VNC 兜底：复用容器里已有的 Xvfb/x11vnc/noVNC，缺哪个补哪个。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path


class VncManager:
    def __init__(
        self,
        logger=None,
        *,
        display: str = ":99",
        rfb_port: int = 5900,
        web_port: int = 6080,
        embed_port: int = 6081,
        public_url: str = "",
        log_dir: str | None = None,
    ):
        self.log = logger
        self.display = display or ":99"
        self.display_num = self.display.lstrip(":")
        self.rfb_port = int(rfb_port)
        self.web_port = int(web_port)
        self.embed_port = int(embed_port)
        self.public_url = (public_url or "").strip()
        self.web_root = Path("/usr/share/novnc")
        self.log_dir = Path(log_dir) if log_dir else Path("/tmp")
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.log_dir = Path("/tmp")
        self._lock = asyncio.Lock()

    @property
    def viewer_path(self) -> str:
        for name in ("vnc.html", "vnc_lite.html"):
            if (self.web_root / name).exists():
                return name
        return "vnc.html"

    def client_url(self) -> str:
        if self.public_url:
            return self.public_url
        return f"http://127.0.0.1:{self.web_port}/{self.viewer_path}"

    async def _port_open(self, port: int, host: str = "127.0.0.1") -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.2
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False

    async def _proc_running(self, pattern: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep",
                "-f",
                pattern,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await asyncio.wait_for(proc.wait(), timeout=3) == 0
        except Exception:
            return False

    def _xvfb_running(self) -> bool:
        return Path(f"/tmp/.X11-unix/X{self.display_num}").exists()

    @property
    def embed_viewer(self) -> str:
        if (self.web_root / "vnc_lite.html").exists():
            return "vnc_lite.html"
        return self.viewer_path

    async def status(self) -> dict:
        xvfb = self._xvfb_running() or await self._proc_running(f"Xvfb {self.display}")
        rfb = await self._port_open(self.rfb_port)
        web = await self._port_open(self.web_port)
        embed = await self._port_open(self.embed_port)
        novnc = (self.web_root / self.viewer_path).exists()
        embed_viewer = (self.web_root / self.embed_viewer).exists()
        ready = bool(xvfb and rfb and web and novnc)
        return {
            "ready": ready,
            "xvfb": bool(xvfb),
            "x11vnc": rfb,
            "websockify": web,
            "novnc": novnc,
            "embed_ready": bool(embed and embed_viewer),
            "display": self.display,
            "rfb_port": self.rfb_port,
            "web_port": self.web_port,
            "embed_port": self.embed_port,
            "viewer_path": self.viewer_path,
            "embed_viewer": self.embed_viewer,
            "url": self.client_url(),
            "public_url": self.public_url,
        }

    async def ensure(self) -> dict:
        """启动缺失的服务；已有服务直接复用，不杀进程。"""
        async with self._lock:
            errors: list[str] = []
            if not self._xvfb_running():
                if not shutil.which("Xvfb"):
                    errors.append("缺少 Xvfb")
                else:
                    self._clean_x_lock()
                    await self._spawn(
                        "Xvfb",
                        self.display,
                        "-screen",
                        "0",
                        "1440x900x24",
                        "-nolisten",
                        "tcp",
                        log_name="xvfb",
                    )
                    await self._wait(lambda: self._xvfb_running(), 10)
                    if not self._xvfb_running():
                        errors.append("Xvfb 启动失败")

            if not await self._port_open(self.rfb_port):
                if not shutil.which("x11vnc"):
                    errors.append("缺少 x11vnc")
                else:
                    await self._spawn(
                        "x11vnc",
                        "-display",
                        self.display,
                        "-forever",
                        "-shared",
                        "-nopw",
                        "-rfbport",
                        str(self.rfb_port),
                        log_name="x11vnc",
                        env_extra={"DISPLAY": self.display},
                    )
                    await self._wait(lambda: self._port_open(self.rfb_port), 8)
                    if not await self._port_open(self.rfb_port):
                        errors.append("x11vnc 启动失败")

            if not await self._port_open(self.web_port):
                if not shutil.which("websockify"):
                    errors.append("缺少 websockify")
                elif not self.web_root.exists():
                    errors.append("缺少 noVNC 静态文件")
                else:
                    await self._spawn(
                        "websockify",
                        "--web=" + str(self.web_root),
                        str(self.web_port),
                        f"localhost:{self.rfb_port}",
                        log_name="websockify",
                    )
                    await self._wait(lambda: self._port_open(self.web_port), 8)
                    if not await self._port_open(self.web_port):
                        errors.append("websockify 启动失败")

            if not await self._port_open(self.embed_port):
                script = Path(__file__).with_name("dd_vnc_web.py")
                if not script.exists():
                    errors.append("缺少 VNC 嵌入静态服务")
                else:
                    await self._spawn(
                        sys.executable,
                        str(script),
                        str(self.embed_port),
                        str(self.web_root),
                        log_name="vncweb",
                    )
                    await self._wait(lambda: self._port_open(self.embed_port), 8)
                    if not await self._port_open(self.embed_port):
                        errors.append("VNC 嵌入服务启动失败")

            data = await self.status()
            if errors:
                data["error"] = "；".join(errors)
            if self.log:
                if data["ready"] and data["embed_ready"]:
                    self.log.info(f"[doubao_draw] VNC 已就绪：{data['url']}")
                elif data.get("error"):
                    self.log.warning(f"[doubao_draw] VNC 未就绪：{data['error']}")
            return data

    def _clean_x_lock(self):
        for p in (
            Path(f"/tmp/.X{self.display_num}-lock"),
            Path(f"/tmp/.X11-unix/X{self.display_num}"),
        ):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    async def _spawn(self, *args, log_name: str, env_extra: dict | None = None):
        env = {**os.environ, **(env_extra or {})}
        log_path = self.log_dir / f"vnc_{log_name}.log"
        try:
            fh = open(log_path, "ab")
        except Exception:
            fh = None
        try:
            await asyncio.create_subprocess_exec(
                *args,
                env=env,
                stdout=fh if fh else asyncio.subprocess.DEVNULL,
                stderr=fh if fh else asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass

    async def _wait(self, check, seconds: float) -> bool:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await check():
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.4)
        return False
