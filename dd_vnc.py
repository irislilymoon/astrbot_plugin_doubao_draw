# -*- coding: utf-8 -*-
"""VNC 兜底：复用容器里已有的 Xvfb/x11vnc/noVNC，缺哪个补哪个。

安全默认：x11vnc 默认带密码启动（-rfbauth），密码来自插件配置（默认 iris）。
只有显式关闭 vnc_password_enabled 才会退回 -nopw。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import sys
from pathlib import Path


class VncManager:
    DEFAULT_PASSWORD = "iris"

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
        state_dir: str | None = None,
        password: str = "iris",
        auth_enabled: bool = True,
        bind: str = "0.0.0.0",
    ):
        self.log = logger
        self.display = display or ":99"
        self.display_num = self.display.lstrip(":")
        self.rfb_port = int(rfb_port)
        self.web_port = int(web_port)
        self.embed_port = int(embed_port)
        self.public_url = (public_url or "").strip()
        self.bind = (bind or "").strip()
        self.password = (password or "").strip()
        # 密码为空时视为未启用，避免出现“要求密码但密码是空串”的尴尬状态
        self.auth_enabled = bool(auth_enabled) and bool(self.password)
        self.web_root = Path("/usr/share/novnc")
        self.log_dir = Path(log_dir) if log_dir else Path("/tmp")
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.log_dir = Path("/tmp")
        self.state_dir = Path(state_dir) if state_dir else self.log_dir
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.state_dir = self.log_dir
        self.auth_file = self.state_dir / "vnc_passwd"
        self.auth_stamp = self.state_dir / "vnc_passwd.sha256"
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
    def viewer_file(self) -> Path:
        """插件自带的查看器（自适应缩放 + 缩放按钮），由本插件静态服务提供。"""
        return Path(__file__).with_name("pages") / "vnc" / "viewer.html"

    @property
    def embed_viewer(self) -> str:
        if self.viewer_file.exists():
            return "viewer.html"
        if (self.web_root / "vnc_lite.html").exists():
            return "vnc_lite.html"
        return self.viewer_path

    # ---------------- 密码 ----------------

    def _password_digest(self) -> str:
        return hashlib.sha256(self.password.encode("utf-8")).hexdigest()

    async def _ensure_auth_file(self) -> tuple[str | None, str | None]:
        """生成/复用 x11vnc 密码文件。返回 (路径, 错误)。密码内容不落日志。"""
        if not self.auth_enabled:
            return None, None
        digest = self._password_digest()
        try:
            stamp = self.auth_stamp.read_text(encoding="utf-8").strip()
        except Exception:
            stamp = ""
        try:
            fresh = (
                self.auth_file.exists()
                and self.auth_file.stat().st_size > 0
                and stamp == digest
            )
        except Exception:
            fresh = False
        if fresh:
            return str(self.auth_file), None
        if not shutil.which("x11vnc"):
            return None, "缺少 x11vnc，无法生成 VNC 密码文件"
        code = await self._run(
            "x11vnc", "-storepasswd", self.password, str(self.auth_file)
        )
        try:
            ok = self.auth_file.exists() and self.auth_file.stat().st_size > 0
        except Exception:
            ok = False
        if code != 0 or not ok:
            return None, "x11vnc 生成密码文件失败"
        try:
            os.chmod(self.auth_file, 0o600)
            self.auth_stamp.write_text(digest, encoding="utf-8")
        except Exception:
            pass
        return str(self.auth_file), None

    async def _run(self, *args: str) -> int:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await asyncio.wait_for(proc.wait(), timeout=15)
        except Exception:
            return 1

    # ---------------- 已运行实例的认证校准 ----------------

    async def _x11vnc_procs(self) -> list[tuple[int, str]]:
        """列出占用本插件 RFB 端口的 x11vnc 进程。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep",
                "-af",
                "x11vnc",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            return []
        procs: list[tuple[int, str]] = []
        for line in out.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            if not line or "dd_vnc_web.py" in line:
                continue
            pid_s, _, cmd = line.partition(" ")
            if not pid_s.isdigit() or "x11vnc" not in cmd:
                continue
            if (
                f"-rfbport {self.rfb_port}" in cmd
                or f"-rfbport={self.rfb_port}" in cmd
            ):
                procs.append((int(pid_s), cmd))
        return procs

    def _matches_expected_auth(self, cmd: str, auth_path: str | None) -> bool:
        if self.auth_enabled:
            return bool(auth_path) and auth_path in cmd
        return "-nopw" in cmd

    def _matches_expected_bind(self, cmd: str) -> bool:
        if not self.bind or self.bind in ("0.0.0.0", "*"):
            return "-listen " not in cmd
        return f"-listen {self.bind}" in cmd

    def _pid_start_epoch(self, pid: int) -> float:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                data = f.read().decode("utf-8", "ignore")
            fields = data[data.rfind(")") + 2 :].split()
            starttime = int(fields[19])  # /proc/<pid>/stat 第 22 字段
            btime = 0
            with open("/proc/stat", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("btime "):
                        btime = int(line.split()[1])
                        break
            if not btime:
                return 0.0
            return btime + starttime / os.sysconf("SC_CLK_TCK")
        except Exception:
            return 0.0

    async def _reconcile_x11vnc(self, auth_path: str | None) -> bool:
        """让已在跑的 x11vnc 与本插件当前的认证配置一致；不一致就重启它。"""
        procs = await self._x11vnc_procs()
        if not procs:
            return False
        stale: list[tuple[int, str]] = []
        for pid, cmd in procs:
            if not self._matches_expected_auth(cmd, auth_path):
                stale.append((pid, "认证方式与当前配置不一致"))
                continue
            if not self._matches_expected_bind(cmd):
                stale.append((pid, "监听地址与当前配置不一致"))
                continue
            if self.auth_enabled:
                try:
                    started = self._pid_start_epoch(pid)
                    changed = self.auth_file.stat().st_mtime
                    if started and changed > started + 1:
                        stale.append((pid, "密码已更新"))
                except Exception:
                    pass
        if not stale:
            return False
        for pid, why in stale:
            if self.log:
                self.log.info(f"[doubao_draw] 重启 x11vnc（{why}），pid={pid}")
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
        if await self._wait(self._port_closed, 3):
            return True
        for pid, _ in stale:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
        await self._wait(self._port_closed, 4)
        return True

    async def _port_closed(self) -> bool:
        return not await self._port_open(self.rfb_port)

    # ---------------- 状态 ----------------

    async def status(self) -> dict:
        xvfb = self._xvfb_running() or await self._proc_running(f"Xvfb {self.display}")
        rfb = await self._port_open(self.rfb_port)
        web = await self._port_open(self.web_port)
        embed = await self._port_open(self.embed_port)
        novnc = (self.web_root / self.viewer_path).exists()
        if self.viewer_file.exists():
            embed_viewer = True
        else:
            embed_viewer = (self.web_root / self.embed_viewer).exists()
        ready = bool(xvfb and rfb and web and novnc)
        # 配置意图与正在运行的 x11vnc 是否一致：不一致说明还在切换窗口期
        procs = await self._x11vnc_procs()
        auth_path = str(self.auth_file) if self.auth_enabled else None
        auth_mismatch = any(
            not self._matches_expected_auth(cmd, auth_path) for _, cmd in procs
        )
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
            "auth_mismatch": bool(auth_mismatch),
            "url": self.client_url(),
            "public_url": self.public_url,
            "bind": self.bind or "0.0.0.0",
            "auth_enabled": self.auth_enabled,
            "password_set": bool(self.password),
            "password_is_default": bool(
                self.auth_enabled and self.password == self.DEFAULT_PASSWORD
            ),
        }

    async def ensure(self) -> dict:
        """启动缺失的服务；已有服务直接复用，认证方式不符时重启校准。"""
        async with self._lock:
            errors: list[str] = []
            auth_path, auth_err = await self._ensure_auth_file()
            if auth_err:
                errors.append(auth_err)

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

            if await self._port_open(self.rfb_port):
                await self._reconcile_x11vnc(auth_path)

            if not await self._port_open(self.rfb_port):
                if not shutil.which("x11vnc"):
                    errors.append("缺少 x11vnc")
                elif self.auth_enabled and not auth_path:
                    # 安全兜底：要求密码但密码文件没生成时，绝不退回无密码启动
                    errors.append("VNC 密码未生成，已拒绝以无密码方式启动 x11vnc")
                else:
                    args = [
                        "x11vnc",
                        "-display",
                        self.display,
                        "-forever",
                        "-shared",
                        "-rfbport",
                        str(self.rfb_port),
                    ]
                    if self.bind and self.bind not in ("0.0.0.0", "*"):
                        args += ["-listen", self.bind]
                    if auth_path:
                        args += ["-rfbauth", auth_path]
                    else:
                        args += ["-nopw"]
                    await self._spawn(
                        *args,
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
                    listen = (
                        f"{self.bind}:{self.web_port}"
                        if self.bind
                        else str(self.web_port)
                    )
                    await self._spawn(
                        "websockify",
                        "--web=" + str(self.web_root),
                        listen,
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
                        str(self.viewer_file),
                        self.bind or "0.0.0.0",
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
                    mode = "密码保护" if self.auth_enabled else "无密码"
                    self.log.info(
                        f"[doubao_draw] VNC 已就绪（{mode}）：{data['url']}"
                    )
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
