# -*- coding: utf-8 -*-
"""豆包网页端：提交提示词 → 抓图 / 抓文字 → 下载原图。

不是反代、不调第三方接口：全程走本机 Chromium 里已登录的豆包网页会话。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from pathlib import Path

import dd_selectors as S


class DoubaoError(Exception):
    pass


class NotLoggedIn(DoubaoError):
    pass


class GenerationCancelled(DoubaoError):
    pass


SSE_PATCH_JS = """
(() => {
  if (window.__ddSsePatchedV2) return;
  window.__ddSsePatchedV2 = true;
  window.__ddChunksV2 = [];
  window.__ddStreamSeqV2 = 0;
  window.__ddCaptureFromV2 = 0;
  const origFetch = window.fetch;
  window.fetch = async function(...args) {
    const resp = await origFetch.apply(this, args);
    try {
      const url = resp.url || (args[0] && args[0].url) || String(args[0] || '');
      if (url.includes('/chat/completion') && resp.body && !resp.bodyUsed) {
        const id = (window.__ddStreamSeqV2 || 0) + 1;
        window.__ddStreamSeqV2 = id;
        const clone = resp.clone();
        (async () => {
          const reader = clone.body.getReader();
          const dec = new TextDecoder();
          try {
            while (true) {
              const {done, value} = await reader.read();
              if (done) break;
              if (id <= (window.__ddCaptureFromV2 || 0)) continue;
              window.__ddChunksV2.push({id: id, t: dec.decode(value, {stream: true})});
              if (window.__ddChunksV2.length > 4000) window.__ddChunksV2.splice(0, 2000);
            }
          } catch (e) {}
        })();
      }
    } catch (e) {}
    return resp;
  };
})();
"""


class _SSEImageParser:
    """实时解析 completion SSE，只提取生成图的原图 URL。"""

    def __init__(self):
        self.bufs: dict[int, str] = {}
        self.seen_urls: set[str] = set()
        self.seen_keys: set[str] = set()

    def feed(self, stream_id: int, text: str) -> list[str]:
        buf = self.bufs.get(stream_id, "") + text
        out: list[str] = []
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            out.extend(self._extract(obj))
        if len(buf) > 1_000_000:
            buf = buf[-200_000:]
        self.bufs[stream_id] = buf
        return out

    def tail(self, limit: int = 500) -> str:
        text = "\n".join(self.bufs.values())
        return text[-limit:]

    def _extract(self, obj) -> list[str]:
        out: list[str] = []
        for block in self._find_creation_blocks(obj):
            creations = block.get("creations")
            if not isinstance(creations, list):
                continue
            for c in creations:
                if not isinstance(c, dict):
                    continue
                img = c.get("image")
                if not isinstance(img, dict):
                    img = c
                raw = img.get("image_ori_raw") or img.get("image_raw")
                url = raw.get("url") if isinstance(raw, dict) else None
                if not url and img.get("status") in (2, "2"):
                    ori = (
                        img.get("image_ori")
                        or img.get("image_preview_resize")
                        or img.get("image_preview")
                    )
                    url = ori.get("url") if isinstance(ori, dict) else None
                if not url:
                    # 原图还没好，等下一次 patch
                    continue
                key = str(img.get("key") or c.get("key") or "")
                if key and key in self.seen_keys:
                    continue
                if key:
                    self.seen_keys.add(key)
                if url not in self.seen_urls:
                    self.seen_urls.add(url)
                    out.append(url)
        return out

    @staticmethod
    def _find_creation_blocks(obj) -> list[dict]:
        blocks: list[dict] = []
        stack = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                cb = node.get("creation_block")
                if isinstance(cb, dict):
                    blocks.append(cb)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return blocks



class DoubaoWeb:
    def __init__(self, browser, store, *, chat_url: str, timeout: int = 150,
                 no_image_timeout: int = 60, compress_for_qq: bool = True,
                 logger=None):
        self.browser = browser
        self.store = store
        self.chat_url = chat_url
        self.timeout = timeout
        self.no_image_timeout = no_image_timeout
        self.compress_for_qq = compress_for_qq
        self.log = logger
        self.last_debug_shot: str | None = None
        self._sse_page = None
        self._generation_epoch = 0

    def cancel_generation(self):
        self._generation_epoch += 1

    # ---------- 对外 ----------

    async def is_logged_in(self) -> bool:
        """以页面状态为准：有输入框、且没有「登录」入口或登录弹窗。"""
        page = await self.browser.page()
        if "doubao.com" not in (page.url or ""):
            async with self.browser.lock:
                await self._open_chat(page)
        return await self._has_input(page)

    async def generate_stream(self, prompt: str, ratio: str = "",
                              refs: list[bytes] | None = None):
        """逐张产出生成图：SSE 一出现原图就下载并 yield，不转发文字。"""
        epoch = self._generation_epoch
        if epoch != self._generation_epoch:
            raise GenerationCancelled()
        page = await self.browser.page()
        async with self.browser.lock:
            if epoch != self._generation_epoch:
                raise GenerationCancelled()
            async for path in self._generate_stream(
                page, prompt, ratio, refs or [], epoch
            ):
                yield path

    async def get_login_qr(self) -> str | None:
        """已登录返回 None；未登录截图二维码（或整页）返回路径。"""
        page = await self.browser.page()
        async with self.browser.lock:
            self.last_debug_shot = None
            await self._open_chat(page)
            if await self._has_input(page):
                return None
            if not await self._open_login_modal(page):
                self.last_debug_shot = await self._snap(page)
                raise DoubaoError("没等到登录弹窗，请稍后重试（可发 /shot 看当前页面）")
            # 扫码登录也要先勾协议，否则部分入口一直灰着
            await self._agree_terms(page)
            # 二维码是异步渲染的，先等真正的码出来，别截到灰色占位
            await self._wait_qr_ready(page)
            qr = await self._qr_png(page)
            if qr:
                return qr
            for sel in S.QR_SELECTORS:
                loc = page.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        path = str(self.store.new_path(".png"))
                        await loc.screenshot(path=path)
                        return path
                except Exception:
                    continue
            path = str(self.store.new_path(".png"))
            await page.screenshot(path=path, full_page=False)
            return path

    async def _qr_png(self, page) -> str | None:
        """把二维码 SVG/img 在页面内画到 canvas 再导出，绕开元素截图抓不到 SVG 的问题。"""
        try:
            data = await page.evaluate(
                """async () => {
                  const el = document.querySelector('[data-testid="qrcode_image"]');
                  if (!el) return '';
                  const pad = 20;
                  const svg = el.querySelector('svg');
                  if (svg) {
                    const xml = new XMLSerializer().serializeToString(svg);
                    const blob = new Blob([xml], {type: 'image/svg+xml;charset=utf-8'});
                    const url = URL.createObjectURL(blob);
                    const img = new Image();
                    await new Promise((res, rej) => {
                      img.onload = res;
                      img.onerror = () => rej(new Error('svg load'));
                      img.src = url;
                    });
                    const canvas = document.createElement('canvas');
                    canvas.width = (img.naturalWidth || 300) + pad * 2;
                    canvas.height = (img.naturalHeight || 300) + pad * 2;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#fff';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    ctx.drawImage(img, pad, pad);
                    URL.revokeObjectURL(url);
                    return canvas.toDataURL('image/png');
                  }
                  const im = el.querySelector('img');
                  if (im && im.complete && im.naturalWidth) {
                    const canvas = document.createElement('canvas');
                    canvas.width = im.naturalWidth + pad * 2;
                    canvas.height = im.naturalHeight + pad * 2;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#fff';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    ctx.drawImage(im, pad, pad);
                    return canvas.toDataURL('image/png');
                  }
                  return '';
                }"""
            )
        except Exception:
            return None
        if not data or not data.startswith("data:image/png;base64,"):
            return None
        try:
            raw = base64.b64decode(data.split(",", 1)[1])
            path = str(self.store.new_path(".png"))
            Path(path).write_bytes(raw)
            return path
        except Exception:
            return None

    async def _wait_qr_ready(self, page, seconds: float = 15) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                ready = await page.evaluate(
                    """() => {
                      const el = document.querySelector('[data-testid="qrcode_image"]');
                      if (!el) return false;
                      const img = el.querySelector('img');
                      if (img && img.complete && img.naturalWidth > 20) return true;
                      const svg = el.querySelector('svg');
                      if (svg && svg.querySelectorAll('circle, path, rect').length > 30) return true;
                      return false;
                    }"""
                )
                if ready:
                    await asyncio.sleep(0.4)
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.4)
        return False

    async def wait_login(self, seconds: int = 120) -> bool:
        page = await self.browser.page()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await self._has_input(page):
                return True
            await asyncio.sleep(2)
        return False

    async def _click_login(self, page):
        """旧版兜底：按文字找登录入口。新版优先用 data-testid。"""
        try:
            locs = page.get_by_text("登录", exact=True)
            total = await locs.count()
        except Exception:
            total = 0

        for i in range(total):
            try:
                el = locs.nth(i)
                if not await el.is_visible():
                    continue
                box = await el.bounding_box()
                try:
                    await el.click(timeout=4000)
                except Exception:
                    if box:
                        await page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                await asyncio.sleep(2.0)
                if await self._login_modal_visible(page):
                    return
                if box:
                    await page.mouse.click(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                    )
                    await asyncio.sleep(2.0)
                    if await self._login_modal_visible(page):
                        return
            except Exception:
                continue

    async def _login_modal_visible(self, page) -> bool:
        for sel in (S.LOGIN_MODAL, S.LOGIN_PHONE_INPUT, S.LOGIN_CODE):
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _login_stage(self, page) -> str:
        """返回登录弹窗当前阶段：code / phone / home / ''。"""
        if await self._first_visible(page, [S.LOGIN_CODE]):
            return "code"
        if await self._first_visible(page, [S.LOGIN_PHONE_INPUT]):
            return "phone"
        if await self._first_visible(page, [S.LOGIN_PHONE_TAB, S.LOGIN_AGREEMENT]):
            return "home"
        return ""

    async def _reset_login_modal(self, page):
        """把登录弹窗恢复初始状态：验证码页没有关闭按钮，直接刷新页面最稳。"""
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(1.5)
        except Exception:
            pass

    async def cancel_login(self) -> str:
        """取消登录：关掉登录弹窗，不退出已有登录。"""
        page = await self.browser.page()
        async with self.browser.lock:
            if not await self._login_modal_visible(page):
                return "当前没有进行中的登录。"
            try:
                close = page.locator(S.LOGIN_CLOSE).first
                if await close.count() and await close.is_visible():
                    await close.click(timeout=3000)
            except Exception:
                pass
            if await self._login_modal_visible(page):
                await self._reset_login_modal(page)
            await asyncio.sleep(0.5)
            if await self._login_modal_visible(page):
                return "取消登录失败，请在 VNC 里手动关掉登录弹窗。"
            return "已取消登录，你可以重新发 /drawlogin 或 /drawlogin phone 手机号。"

    async def _open_login_modal(self, page) -> bool:
        """确保登录弹窗已经打开（调用前先确认当前未登录）。"""
        if await self._login_modal_visible(page):
            return True
        try:
            btn = page.locator(S.LOGIN_BUTTON).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=5000)
        except Exception:
            pass
        if not await self._login_modal_visible(page):
            await self._click_login(page)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if await self._login_modal_visible(page):
                return True
            await asyncio.sleep(0.4)
        return False

    async def _agree_terms(self, page) -> bool:
        """勾选「已阅读并同意」。新版是自绘按钮，点按钮本身，别点协议文字。"""
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                loc = page.locator(S.LOGIN_AGREEMENT).first
                if await loc.count() and await loc.is_visible():
                    if (await loc.get_attribute("aria-checked")) == "true":
                        return True
                    try:
                        await loc.click(timeout=3000)
                    except Exception:
                        box = await loc.bounding_box()
                        if box:
                            await page.mouse.click(
                                box["x"] + box["width"] / 2,
                                box["y"] + box["height"] / 2,
                            )
                    await asyncio.sleep(0.35)
                    if (await loc.get_attribute("aria-checked")) == "true":
                        return True
                    continue
            except Exception:
                pass
            await asyncio.sleep(0.4)

        # 旧版兜底：按「已阅读并同意」左侧圆点坐标点击
        try:
            lab = page.get_by_text("已阅读并同意", exact=False).first
            if await lab.count():
                box = await lab.bounding_box()
                if box:
                    await page.mouse.click(box["x"] - 16, box["y"] + box["height"] / 2)
                    await asyncio.sleep(0.4)
                    loc = page.locator(S.LOGIN_AGREEMENT).first
                    if await loc.count() and (await loc.get_attribute("aria-checked")) == "true":
                        return True
        except Exception:
            pass
        return False

    async def login_with_phone(self, phone: str) -> str:
        """拉起登录弹窗，先勾协议，切手机号方式，填号并请求验证码。"""
        page = await self.browser.page()
        async with self.browser.lock:
            self.last_debug_shot = None
            await self._open_chat(page)
            if await self._has_input(page):
                return "已经是登录状态，不用再登。"
            # 上次已经走到验证码页（手机号输错/验证码过期）：先重置回初始登录页
            if await self._login_stage(page) == "code":
                await self._reset_login_modal(page)
                await self._open_chat(page)
            if not await self._open_login_modal(page):
                self.last_debug_shot = await self._snap(page)
                return "没等到登录弹窗：页面可能没加载完或结构变了，请稍后重试。"

            # 关键：协议必须先勾选，否则「下一步」一直不可点
            if not await self._agree_terms(page):
                self.last_debug_shot = await self._snap(page)
                return "没勾上「已阅读并同意」：登录弹窗结构可能变了，请发 /shot 看看页面。"

            tab = await self._first_visible(page, [S.LOGIN_PHONE_TAB])
            if tab is None:
                for text in S.PHONE_TAB_TEXTS:
                    try:
                        cand = page.get_by_text(text, exact=True).first
                        if await cand.count() and await cand.is_visible():
                            tab = cand
                            break
                    except Exception:
                        continue
            if tab is None:
                self.last_debug_shot = await self._snap(page)
                return "没找到「手机号登录」入口，页面结构可能变了。"
            try:
                await tab.click(timeout=5000)
            except Exception as e:
                self.last_debug_shot = await self._snap(page)
                return f"点「手机号登录」失败：{e}"
            await asyncio.sleep(1.0)

            phone_input = await self._first_visible(page, S.PHONE_INPUT_SELECTORS)
            if phone_input is None:
                self.last_debug_shot = await self._snap(page)
                return "没找到手机号输入框：登录弹窗可能停在扫码模式。"
            digits = re.sub(r"\D", "", phone)
            if not digits:
                return "手机号里没有数字，请重发 /drawlogin phone 手机号。"
            try:
                await phone_input.click()
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.insert_text(digits)
                await asyncio.sleep(0.5)
                val = re.sub(r"\D", "", await phone_input.input_value())
                if val != digits:
                    await phone_input.click()
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")
                    await page.keyboard.type(digits, delay=60)
                    await asyncio.sleep(0.5)

                # 切页后协议状态可能被重置，再确认一次
                await self._agree_terms(page)

                nxt = await self._first_visible(page, [S.LOGIN_NEXT])
                if nxt is None:
                    nxt = await self._first_visible(page, ['button:has-text("下一步")'])
                if nxt is None:
                    self.last_debug_shot = await self._snap(page)
                    return "手机号填好了，但没找到「下一步」按钮。"
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        if not await nxt.is_disabled():
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.4)
                else:
                    self.last_debug_shot = await self._snap(page)
                    return "「下一步」一直不可点：请确认手机号格式，或看下截图里的提示。"

                await nxt.click(timeout=5000)
                # 不假定成功：必须等验证码页真正出现
                if await self._wait_code_page(page, 30):
                    return "code_sent"
                self.last_debug_shot = await self._snap(page)
                text = await self._body_text(page)
                if any(m in text for m in ("滑动", "滑块", "安全验证", "完成验证")):
                    return "需要滑块验证：请管理员在 VNC 里拖一下滑块，收到短信后发 /drawcode 验证码。"
                return "点了「下一步」但没进入验证码页，可能是手机号被风控或页面没响应；截图已附上。"
            except Exception as e:
                self.last_debug_shot = await self._snap(page)
                return f"提交手机号失败：{e}"

    async def _wait_code_page(self, page, seconds: float = 30) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if await self._code_input(page) is not None:
                return True
            if not await self._login_modal_visible(page):
                return False
            await asyncio.sleep(0.5)
        return False

    async def _code_input(self, page):
        frames = [page]
        try:
            frames += [f for f in page.frames if f != page.main_frame]
        except Exception:
            pass
        for fr in frames:
            try:
                cont = fr.locator(S.LOGIN_CODE).first
                if await cont.count() and await cont.is_visible():
                    inner = fr.locator(S.LOGIN_CODE_INPUT)
                    if await inner.count():
                        return inner.last
                    return cont
            except Exception:
                continue
        # 旧版兜底
        for fr in frames:
            for sel in S.CODE_INPUT_SELECTORS:
                if sel == S.LOGIN_CODE_INPUT:
                    continue
                try:
                    loc = fr.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        return loc
                except Exception:
                    continue
        return None

    async def _body_text(self, page) -> str:
        try:
            return await page.inner_text("body")
        except Exception:
            return ""

    async def login_with_code(self, code: str) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            self.last_debug_shot = None
            if await self._has_input(page):
                return "已经登录了，直接 /draw 就行。"
            if "doubao.com" not in (page.url or ""):
                await self._open_chat(page)

            code_input = None
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                code_input = await self._code_input(page)
                if code_input is not None:
                    break
                await asyncio.sleep(0.5)
            if code_input is None:
                self.last_debug_shot = await self._snap(page)
                if await self._first_visible(page, S.PHONE_INPUT_SELECTORS):
                    return "页面还停在手机号这一步：先发 /drawlogin phone 手机号，等提示验证码已发送再发 /drawcode。"
                return "没找到验证码输入框：登录弹窗可能已关闭，请重新 /drawlogin phone 手机号。"

            try:
                try:
                    await code_input.click(force=True, timeout=5000)
                except Exception:
                    pass
                await asyncio.sleep(0.2)
                try:
                    await code_input.press("Control+A")
                    await code_input.press("Backspace")
                except Exception:
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Backspace")

                # 逐位输入，确保 React 受控组件每次都收到 onChange；6 位会自动提交
                for ch in code:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(0.08)

                typed_at = time.monotonic()
                deadline = typed_at + 20
                while time.monotonic() < deadline:
                    if await self._has_input(page):
                        return "ok"
                    # 输满后过一会儿仍停在验证码页且输入被清空，说明验证失败
                    if time.monotonic() - typed_at > 3:
                        try:
                            val = (await code_input.input_value()).strip()
                        except Exception:
                            val = ""
                        if not val and await self._code_input(page) is not None:
                            self.last_debug_shot = await self._snap(page)
                            return (
                                "验证码没通过（输完被清空了）。如果只是输错，直接重发 /drawcode 正确验证码；"
                                "如果已过期或多次失败，发 /drawlogin phone 手机号 重新获取。"
                            )
                    await asyncio.sleep(0.4)
                self.last_debug_shot = await self._snap(page)
                return "验证超时：登录弹窗还开着，可能验证码过期或网络没响应。"
            except Exception as e:
                self.last_debug_shot = await self._snap(page)
                return f"提交验证码失败：{e}"

    async def _snap(self, page) -> str | None:
        try:
            path = str(self.store.new_path(".png"))
            await page.screenshot(path=path, full_page=False)
            return path
        except Exception:
            return None

    async def logout(self) -> bool:
        """退出登录：只清豆包站点的 Cookie/本地存储，不杀共享浏览器。"""
        page = await self.browser.page()
        async with self.browser.lock:
            ctx = self.browser.context
            try:
                await ctx.clear_cookies()
            except Exception:
                pass
            try:
                await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=45000)
                await page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch(e) {} }"
                )
                await page.reload(wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            await asyncio.sleep(1.5)
        return True

    async def _first_visible(self, page, selectors):
        frames = [page]
        try:
            frames += [f for f in page.frames if f != page.main_frame]
        except Exception:
            pass
        for fr in frames:
            for sel in selectors:
                loc = fr.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        return loc
                except Exception:
                    continue
        return None

    # ---------- 远程操控浏览器 ----------

    async def screenshot(self) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            await self._ensure_page(page)
            path = str(self.store.new_path(".png"))
            await page.screenshot(path=path, full_page=False)
            return path

    async def _ensure_page(self, page):
        if "doubao.com" not in (page.url or ""):
            await self._open_chat(page)

    async def current(self) -> dict:
        page = await self.browser.page()
        try:
            title = await page.title()
        except Exception:
            title = ""
        return {"url": page.url, "title": title}

    async def open_url(self, url: str) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)
            return page.url

    async def click_target(self, target: str) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            await self._ensure_page(page)
            loc = None
            if target[:1] in "#.[/":
                try:
                    cand = page.locator(target).first
                    if await cand.count() and await cand.is_visible():
                        loc = cand
                except Exception:
                    loc = None
            if loc is None:
                try:
                    cand = page.get_by_text(target, exact=False).first
                    if await cand.count() and await cand.is_visible():
                        loc = cand
                except Exception:
                    loc = None
            if loc is None:
                return "没找到可点击的目标，换个文字或改用 CSS 选择器"
            await loc.click(timeout=8000)
            await asyncio.sleep(1.5)
            return "ok"

    async def type_text(self, text: str) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            await self._ensure_page(page)
            loc = await self._first_visible(page, S.INPUT_SELECTORS)
            if loc is None:
                return "页面上没找到可输入的框"
            await loc.click()
            await page.keyboard.insert_text(text)
            await asyncio.sleep(0.3)
            return "ok"

    async def press_key(self, key: str) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            await self._ensure_page(page)
            await page.keyboard.press(key)
            await asyncio.sleep(1.0)
            return "ok"

    async def go_back(self) -> str:
        page = await self.browser.page()
        async with self.browser.lock:
            try:
                await page.go_back()
            except Exception:
                pass
            await asyncio.sleep(1.0)
            return page.url

    async def probe(self) -> dict:
        page = await self.browser.page()
        info = await page.evaluate(S.JS_PROBE)
        hits = {}
        for sel in S.INPUT_SELECTORS + S.NEW_CHAT_SELECTORS + S.QR_SELECTORS:
            try:
                hits[sel] = await page.locator(sel).count()
            except Exception:
                hits[sel] = -1
        info["selector_hits"] = hits
        info["logged_in"] = await self._has_input(page)
        return info

    # ---------- 生成主流程 ----------

    async def _install_sse_capture(self, page):
        if page is not self._sse_page:
            try:
                await page.add_init_script(SSE_PATCH_JS)
            except Exception:
                pass
            self._sse_page = page
        try:
            if not await page.evaluate("() => !!window.__ddSsePatchedV2"):
                await page.evaluate(SSE_PATCH_JS)
        except Exception:
            pass

    async def _drain_sse(self, page) -> list:
        try:
            chunks = await page.evaluate(
                "() => { const a = window.__ddChunksV2 || []; window.__ddChunksV2 = []; return a; }"
            )
            return chunks or []
        except Exception:
            return []

    async def _generate_stream(self, page, prompt, ratio, refs, epoch):
        def check():
            if epoch != self._generation_epoch:
                raise GenerationCancelled("已被 /new 取消")

        check()
        await self._open_chat(page)
        check()
        if not await self._has_input(page):
            raise NotLoggedIn("豆包网页未登录")

        await self._install_sse_capture(page)
        try:
            await page.evaluate(
                "() => { window.__ddChunksV2 = []; window.__ddCaptureFromV2 = window.__ddStreamSeqV2 || 0; }"
            )
        except Exception:
            pass

        parser = _SSEImageParser()
        full_prompt = self._compose(prompt, ratio, bool(refs))
        try:
            check()
            if refs:
                await self._upload(page, refs)
            check()
            if self.log:
                self.log.info("[doubao_draw] 已拿到浏览器，提交提示词")
            await self._send(page, full_prompt)
            if self.log:
                self.log.info("[doubao_draw] 提示词已提交，等待 SSE 原图")

            deadline = time.monotonic() + min(self.timeout, self.no_image_timeout)
            first_image_at = None
            got = 0
            while time.monotonic() < deadline:
                check()
                chunks = await self._drain_sse(page)
                for item in chunks:
                    check()
                    if isinstance(item, dict):
                        sid = int(item.get("id") or 0)
                        text = item.get("t") or ""
                    else:
                        sid, text = 0, str(item)
                    if not text:
                        continue
                    for url in parser.feed(sid, text):
                        check()
                        paths = await self._download(page, [url])
                        check()
                        for path in paths:
                            got += 1
                            if first_image_at is None:
                                first_image_at = time.monotonic()
                                if self.log:
                                    self.log.info("[doubao_draw] SSE 原图已出现，开始转发")
                            yield path
                if first_image_at is not None and time.monotonic() - first_image_at > 2.0:
                    break
                await asyncio.sleep(0.2)

            try:
                chunks = await self._drain_sse(page)
                for item in chunks:
                    check()
                    if isinstance(item, dict):
                        sid = int(item.get("id") or 0)
                        text = item.get("t") or ""
                    else:
                        sid, text = 0, str(item)
                    if not text:
                        continue
                    for url in parser.feed(sid, text):
                        check()
                        for path in await self._download(page, [url]):
                            got += 1
                            yield path
            except GenerationCancelled:
                raise
            except Exception:
                pass
            if got == 0 and self.log:
                self.log.info(
                    "[doubao_draw] SSE 未解析到图片，尾部数据：%s",
                    parser.tail(400).replace("\n", " "),
                )
        finally:
            try:
                url = page.url or ""
                if "doubao.com/chat" in url:
                    # 记住当前会话，浏览器被兜底重启后还能回到同一会话
                    self.browser.startup_url = url
            except Exception:
                pass

    # ---------- 页面操作 ----------

    async def _open_chat(self, page):
        """只在离开豆包时回到对话页；不点「新对话」，保证 /draw 复用当前会话。"""
        if "doubao.com" not in (page.url or ""):
            await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(2.0)

    async def new_chat(self) -> str:
        """显式开新会话：只由用户的 /new 触发；会先取消正在跑的生成。"""
        self.cancel_generation()
        page = await self.browser.page()
        async with self.browser.lock:
            if "doubao.com" not in (page.url or ""):
                await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(2.0)
            for sel in S.NEW_CHAT_SELECTORS:
                loc = page.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=5000)
                        await asyncio.sleep(1.5)
                        self.browser.startup_url = self.chat_url
                        return "ok"
                except Exception:
                    continue
            # 兜底：直接回对话首页，下一条消息会开新会话
            try:
                await page.goto(self.chat_url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(1.5)
                self.browser.startup_url = self.chat_url
                return "ok"
            except Exception as e:
                return f"新对话失败：{e}"

    async def _has_input(self, page) -> bool:
        try:
            return bool(await page.evaluate(S.JS_HAS_INPUT))
        except Exception:
            return False

    async def _input(self, page):
        for sel in S.INPUT_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        raise DoubaoError("找不到输入框（页面结构可能变了）")

    async def _send(self, page, text: str):
        loc = await self._input(page)
        await loc.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.insert_text(text)
        await asyncio.sleep(0.4)
        await page.keyboard.press("Enter")

    @staticmethod
    def _image_meta(data: bytes) -> tuple[str, str]:
        if data.startswith(b"\xff\xd8\xff"):
            return "jpg", "image/jpeg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "webp", "image/webp"
        if data.startswith(b"GIF8"):
            return "gif", "image/gif"
        return "png", "image/png"

    async def _upload(self, page, refs: list[bytes]):
        files = []
        for i, data in enumerate(refs):
            ext, mime = self._image_meta(data)
            files.append({"name": f"ref_{i}.{ext}", "mimeType": mime, "buffer": data})
        try:
            before = await page.locator('[data-testid="attachment-image-card"]').count()
        except Exception:
            before = 0
        try:
            inp = page.locator('input[type="file"]')
            if await inp.count():
                await inp.first.set_input_files(files)
            else:
                btn = page.locator('[data-testid="upload_file_button"]').first
                if not await btn.count():
                    btn = page.locator('[data-testid*="upload"]').first
                async with page.expect_file_chooser(timeout=6000) as fc:
                    await btn.click()
                chooser = await fc.value
                await chooser.set_files(files)
        except Exception as e:
            if self.log:
                self.log.warning(f"[doubao_draw] 参考图上传失败：{e}")
            raise DoubaoError(f"参考图上传失败：{e}") from e

        # 等附件卡片出现，确认图确实挂到了输入框
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if await page.locator('[data-testid="attachment-image-card"]').count() > before:
                    await asyncio.sleep(1.0)
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        try:
            n = await page.locator('input[type="file"]').first.evaluate("e => e.files.length")
        except Exception:
            n = 0
        if n:
            await asyncio.sleep(1.5)
            return
        raise DoubaoError("参考图上传失败：页面没有出现附件预览")

    # ---------- 工具 ----------

    @staticmethod
    def _compose(prompt: str, ratio: str, has_refs: bool = False) -> str:
        if has_refs:
            text = (
                "请基于我上传的图片改图，保持原图主体、构图和比例，"
                f"只做以下修改：{prompt}"
            )
        else:
            text = f"请直接生成一张图片：{prompt}"
        if ratio:
            text += f"\n\n画面比例 {ratio}"
        return text

    async def _download(self, page, urls: list[str]) -> list[str]:
        ctx = self.browser.context
        out: list[str] = []
        for u in urls:
            try:
                resp = await ctx.request.get(
                    u, headers={"Referer": "https://www.doubao.com/"}, timeout=60000
                )
                body = await resp.body()
                if not body or len(body) < 8192:
                    continue
                ct = (resp.headers or {}).get("content-type", "")
                if not ct.startswith("image/"):
                    continue
                ext = ".png"
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "webp" in ct:
                    ext = ".webp"
                p = self.store.new_path(ext)
                Path(p).write_bytes(body)
                final = self._compact(Path(p)) if self.compress_for_qq else str(p)
                if self.log:
                    try:
                        size = Path(final).stat().st_size
                        self.log.info(
                            f"[doubao_draw] 原图已下载：{Path(final).name} {size} bytes"
                        )
                    except Exception:
                        pass
                out.append(final)
            except Exception as e:
                if self.log:
                    self.log.warning(f"[doubao_draw] 图片下载失败：{e}")
        return out

    @staticmethod
    def _compact(path: Path) -> str:
        """大图转 JPEG 再发 QQ，减少上传耗时；失败就用原图。"""
        try:
            if path.stat().st_size <= 1_200_000:
                return str(path)
            from PIL import Image

            im = Image.open(path)
            im.load()
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            im.thumbnail((2048, 2048))
            out = path.with_suffix(".jpg")
            im.save(out, "JPEG", quality=90, optimize=True)
            if out.stat().st_size < path.stat().st_size:
                try:
                    path.unlink()
                except Exception:
                    pass
                return str(out)
            try:
                out.unlink()
            except Exception:
                pass
        except Exception:
            pass
        return str(path)
