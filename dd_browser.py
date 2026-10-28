# -*- coding: utf-8 -*-
"""Playwright 浏览器单例：常驻一个 Chromium 进程，复用一个持久化 profile。"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse


class BrowserManager:
    def __init__(self, profile_dir: str, executable_path: str | None = None,
                 headless: bool = True, logger=None, cdp_url: str | None = None,
                 startup_url: str = "https://www.doubao.com/chat/"):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.executable_path = executable_path or None
        self.headless = headless
        self.log = logger
        self.cdp_url = cdp_url or "http://127.0.0.1:9222"
        self.startup_url = startup_url
        self.display = os.environ.get("DISPLAY") or ":99"

        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._external = False
        self._proc = None
        self._start_lock = asyncio.Lock()
        # 生成任务串行执行：一个浏览器只开一个页面
        self.lock = asyncio.Lock()

    # ---------- 对外 ----------

    @property
    def context(self):
        return self._ctx

    async def page(self):
        async with self._start_lock:
            if self._ctx is None:
                await self._launch()
            elif self._external and self._browser is not None and not self._browser.is_connected():
                if not await self._recover("浏览器连接已断开"):
                    raise RuntimeError("浏览器连接已断开，自动拉起失败")
            if self._ctx is None:
                raise RuntimeError("浏览器启动失败")
            if self._page is None or self._page.is_closed():
                self._page = self._pick_page()
                if self._page is None:
                    self._page = await self._ctx.new_page()
            return self._page

    def _pick_page(self):
        try:
            pages = [p for p in self._ctx.pages if not p.is_closed()]
        except Exception:
            pages = []
        for p in pages:
            try:
                if "doubao.com" in (p.url or ""):
                    return p
            except Exception:
                continue
        if pages:
            return pages[-1]
        return None

    async def restart(self):
        await self.close()
        return await self.page()

    async def close(self):
        try:
            if self._external:
                # 外部常驻浏览器不归插件关，只断开连接
                if self._pw:
                    await self._pw.stop()
            else:
                if self._ctx:
                    await self._ctx.close()
                if self._pw:
                    await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._ctx = None
        self._pw = None
        self._page = None
        self._external = False

    # ---------- 兜底自愈 ----------

    async def ensure_alive(self) -> bool:
        """CDP 掉线、窗口被关、没有页面时，自动恢复一个可见的豆包页。"""
        async with self._start_lock:
            try:
                if self._pw is None:
                    from playwright.async_api import async_playwright

                    self._pw = await async_playwright().start()
            except Exception as e:
                self._warn(f"Playwright 启动失败：{e}")
                return False

            if self._external and self._browser is not None and self._browser.is_connected():
                if not await self._has_gui_window():
                    return await self._recover("浏览器窗口被关闭")
                try:
                    return await self._ensure_startup_page(force=False)
                except Exception as e:
                    return await self._recover(f"页面状态异常：{e}")

            self._drop_connection()
            if await self._try_connect_cdp():
                if not await self._has_gui_window():
                    return await self._recover("浏览器窗口被关闭")
                return await self._ensure_startup_page(force=False)
            return await self._recover("CDP 无响应")

    async def _ensure_startup_page(self, force: bool = False) -> bool:
        try:
            pages = [p for p in self._ctx.pages if not p.is_closed()]
        except Exception:
            pages = []

        if not force:
            for p in pages:
                try:
                    if "doubao.com" in (p.url or ""):
                        self._page = p
                        return True
                except Exception:
                    continue
            real = []
            for p in pages:
                try:
                    if not self._is_blank(p.url):
                        real.append(p)
                except Exception:
                    continue
            if real:
                self._page = real[-1]
                return True

        target = None
        for p in pages:
            try:
                if "doubao.com" in (p.url or ""):
                    target = p
                    break
            except Exception:
                continue
        if target is None and pages:
            target = pages[-1]
        if target is None:
            try:
                target = await self._ctx.new_page()
            except Exception:
                return False
        try:
            await target.goto(self.startup_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)
        except Exception:
            try:
                target = await self._ctx.new_page()
                await target.goto(self.startup_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2.0)
            except Exception:
                pass
        self._page = target
        return True

    @staticmethod
    def _is_blank(url: str | None) -> bool:
        u = (url or "").strip().lower()
        return (
            u in ("", "about:blank")
            or u.startswith("chrome://newtab")
            or u.startswith("chrome://new-tab-page")
            or u.startswith("chrome://welcome")
        )

    async def _recover(self, reason: str = "") -> bool:
        self._warn(f"兜底触发：{reason or '重启常驻浏览器'}")
        await self._kill_profile_chromium()
        self._drop_connection()
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (self.profile_dir / name).unlink(missing_ok=True)
            except Exception:
                pass

        exe = self._find_chromium()
        if not exe:
            self._warn("找不到 Chromium 可执行文件")
            return False
        parsed = urlparse(self.cdp_url or "")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9222
        args = [
            "--test-type",
            "--hide-crash-restore-bubble",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-pings",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-address={host}",
            f"--remote-debugging-port={port}",
            "--start-maximized",
            "--window-size=1280,860",
            self.startup_url,
        ]
        env = {**os.environ, "DISPLAY": self.display}
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe,
                *args,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            self._warn(f"拉起 Chromium 失败：{e}")
            return False

        for _ in range(40):
            await asyncio.sleep(0.5)
            if await self._try_connect_cdp():
                await self._ensure_startup_page(force=True)
                for _ in range(20):
                    if await self._has_gui_window():
                        break
                    await asyncio.sleep(0.5)
                if self.log:
                    self.log.info("[doubao_draw] 常驻 Chromium 已自动拉起")
                return True
        self._warn("Chromium 已启动但 CDP 仍不可用")
        return False

    async def _kill_profile_chromium(self):
        profile = str(self.profile_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill", "-f", profile,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        await asyncio.sleep(1.2)

    async def _has_gui_window(self) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "xwininfo", "-root", "-tree",
                env={**os.environ, "DISPLAY": self.display},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            return True
        text = out.decode(errors="ignore")
        low = text.lower()
        if "cannot open display" in low or "unable to open display" in low:
            return True
        for line in text.splitlines():
            if "Chromium" not in line:
                continue
            m = re.search(r"(\d+)x(\d+)\+", line)
            if m and int(m.group(1)) >= 200 and int(m.group(2)) >= 200:
                return True
        return False

    def _find_chromium(self) -> str | None:
        candidates = []
        if self.executable_path:
            candidates.append(self.executable_path)
        candidates += [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ]
        for c in candidates:
            try:
                if Path(c).exists():
                    return c
            except Exception:
                continue
        return None

    def _drop_connection(self):
        self._browser = None
        self._ctx = None
        self._page = None
        self._external = False

    def _warn(self, msg: str):
        if self.log:
            self.log.warning(f"[doubao_draw] {msg}")

    # ---------- 内部 ----------

    async def _try_connect_cdp(self) -> bool:
        """优先接上容器里常驻的有头 Chromium（可用 noVNC 直接操作同一个浏览器）。"""
        if not self.cdp_url:
            return False
        try:
            browser = await self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=8000)
        except Exception:
            return False
        self._browser = browser
        self._external = True
        try:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        except Exception:
            return False
        self._ctx = ctx
        try:
            ctx.set_default_timeout(15000)
        except Exception:
            pass
        pages = [p for p in ctx.pages if not p.is_closed()]
        chosen = None
        for p in pages:
            try:
                if "doubao.com" in (p.url or ""):
                    chosen = p
                    break
            except Exception:
                continue
        self._page = chosen or (pages[-1] if pages else await ctx.new_page())
        if self.log:
            self.log.info(f"[doubao_draw] 已连接常驻 Chromium：{self.cdp_url}")
        return True

    async def _launch(self):
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        if await self._try_connect_cdp():
            return
        if self.cdp_url and await self._recover("常驻浏览器不在线"):
            return

        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-extensions",
            "--disable-background-networking",
            "--renderer-process-limit=2",
            "--js-flags=--max-old-space-size=256",
            "--lang=zh-CN",
            "--font-render-hinting=none",
        ]
        kwargs = dict(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            args=args,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1280, "height": 860},
        )
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path

        self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        self._ctx.set_default_timeout(15000)
        try:
            await self._ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:
            pass
        if self.log:
            self.log.info(f"[doubao_draw] Chromium 已启动，profile={self.profile_dir}")

        pages = [p for p in self._ctx.pages if not p.is_closed()]
        self._page = pages[0] if pages else await self._ctx.new_page()
