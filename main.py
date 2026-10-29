# -*- coding: utf-8 -*-
"""豆包生图插件：/draw 直接让已登录的豆包网页出图。

设计要点：
- 不调第三方 API、不做中转，只驱动本机 Chromium 里的豆包网页会话；
- 豆包拒绝/繁忙时，把它那段文字原样发回，不改写；
- 全局串行 + 单用户冷却 + 每日额度，避免 2C2G 机器被拖垮。
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp

PLUGIN_NAME = "astrbot_plugin_doubao_draw"

_BASE = Path(__file__).parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

for _m in ("dd_selectors", "dd_browser", "dd_store", "dd_doubao", "dd_vnc"):
    sys.modules.pop(_m, None)

import dd_browser  # noqa: E402
import dd_doubao  # noqa: E402
import dd_selectors  # noqa: E402
import dd_store  # noqa: E402
import dd_vnc  # noqa: E402

RATIO_RE = re.compile(r"^(\d{1,2}:\d{1,2})[\s,，、]+(.+)$", re.S)
CMD_RE = re.compile(r"^[/!.]?\s*(?:doubao_draw|draw|绘图|画图)\s*", re.I)


def _cmd(*names: str):
    """注册指令并带别名；AstrBot 版本不支持 alias 参数时退回只注册主名。"""
    try:
        return filter.command(names[0], alias=set(names[1:]))
    except TypeError:
        return filter.command(names[0])


@register(
    PLUGIN_NAME,
    "16422",
    "豆包生图：SSE 实时抓原图，支持比例、参考图、LLM 自动调用，只回图片",
    "0.1.0",
)
class DoubaoDrawPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        data_dir.mkdir(parents=True, exist_ok=True)

        profile = str(config.get("profile_dir") or (data_dir / "profile"))
        chat_url = str(config.get("chat_url") or "https://www.doubao.com/chat/")
        self.store = dd_store.Store(str(data_dir), str(data_dir / "out"))
        self.browser = dd_browser.BrowserManager(
            profile_dir=profile,
            executable_path=str(config.get("chromium_path") or "") or None,
            headless=bool(config.get("headless", True)),
            logger=logger,
            startup_url=chat_url,
        )
        self.doubao = dd_doubao.DoubaoWeb(
            self.browser,
            self.store,
            chat_url=chat_url,
            timeout=int(config.get("timeout", 150)),
            no_image_timeout=int(config.get("no_image_timeout", 60)),
            compress_for_qq=bool(config.get("compress_for_qq", True)),
            logger=logger,
        )
        self.vnc = dd_vnc.VncManager(
            logger=logger,
            display=str(config.get("vnc_display") or ":99"),
            rfb_port=int(config.get("vnc_rfb_port", 5900)),
            web_port=int(config.get("vnc_web_port", 6080)),
            embed_port=int(config.get("vnc_embed_port", 6081)),
            public_url=str(config.get("vnc_public_url") or ""),
            log_dir=str(data_dir / "vnc_logs"),
        )
        self._register_vnc_api()
        self._last_at: dict[str, float] = {}
        self._pending_code: dict[str, float] = {}
        self._pending_refs: dict[str, tuple[float, list[bytes]]] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._llm_switch_task: asyncio.Task | None = None
        self._start_watchdog_if_needed()
        self._schedule_llm_tool_switch()

    # ---------------- 指令 ----------------

    @_cmd("draw", "画图", "绘图")
    async def draw(self, event: AstrMessageEvent):
        """用豆包生图。用法：/draw [比例] 画面描述（/画图、/绘图 同义）"""
        self._start_watchdog_if_needed()
        event.should_call_llm(False)
        if not self.config.get("enabled", True):
            yield event.plain_result("生图功能已关闭。")
            return

        user_id = str(event.get_sender_id())
        raw = (event.message_str or "").strip()
        text = CMD_RE.sub("", raw, count=1).strip()

        ratio, prompt = self._split_ratio(text)
        prompt = (prompt or text).strip()
        # 指令里没写比例时，用配置里的 default_ratio 兜底
        ratio = ratio or str(self.config.get("default_ratio") or "").strip()
        if not prompt:
            yield event.plain_result(
                "用法：/draw [比例] 描述\n例如：/draw 16:9 赛博朋克城市夜景"
            )
            return

        limit = int(self.config.get("daily_limit", 20))
        remain = self.store.remaining_today(user_id, limit)
        if remain == 0:
            yield event.plain_result(f"今天的 {limit} 张额度用完了，明天再来。")
            return

        left = self._cooldown_left(user_id)
        if left > 0:
            yield event.plain_result(f"慢一点，{left} 秒后再试。")
            return

        refs = await self._collect_refs(event)
        self._last_at[user_id] = time.monotonic()
        if self.config.get("ai_optimize_prompt", False):
            yield event.plain_result("正在用 AI 优化提示词…")
            prompt = await self._optimize_prompt(event, prompt, ratio, bool(refs))
        yield event.plain_result(
            "正在生成（已带上参考图），稍等…" if refs else "正在生成，稍等…"
        )

        got = 0
        try:
            async for path in self.doubao.generate_stream(prompt, ratio, refs):
                got += 1
                self.store.incr_today(user_id, 1)
                yield event.image_result(path)
        except dd_doubao.GenerationCancelled:
            return
        except dd_doubao.NotLoggedIn:
            if got == 0:
                yield event.plain_result("豆包登录态失效了，发 /drawlogin 扫码登录。")
            return
        except Exception as e:
            logger.exception(f"[doubao_draw] 生成异常：{e}")
            if got == 0:
                yield event.plain_result(f"生图失败：{e}")
            return

        if got == 0:
            yield event.plain_result("这次没有生成出图片，请稍后再试。")
        self.store.cleanup()

    @filter.command("drawlogin")
    async def drawlogin(self, event: AstrMessageEvent):
        """登录豆包：默认发二维码；/drawlogin phone 手机号 走短信验证码。"""
        self._start_watchdog_if_needed()
        event.should_call_llm(False)
        raw = (event.message_str or "").strip()
        body = re.sub(r"^[/!.]?\s*drawlogin\s*", "", raw, count=1, flags=re.I).strip()
        m = re.match(
            r"^(?:phone|mobile|手机号?|手机登录)\s*[:：]?\s*(\+?[\d][\d\s\-]{5,19})$",
            body,
            re.I,
        )

        if m:
            phone = re.sub(r"\D", "", m.group(1))
            if not 6 <= len(phone) <= 15:
                yield event.plain_result("手机号格式不对，请发：/drawlogin phone 13800138000")
                return
            user_id = str(event.get_sender_id())
            # 重新提交手机号视为重来，旧验证码作废
            self._pending_code.pop(user_id, None)
            yield event.plain_result("正在提交手机号，请稍等…")
            try:
                msg = await self.doubao.login_with_phone(phone)
            except Exception as e:
                logger.exception(f"[doubao_draw] 手机号登录失败：{e}")
                yield event.plain_result(f"手机号登录失败：{e}")
                return
            if msg == "code_sent":
                self._pending_code[user_id] = time.monotonic()
                yield event.plain_result(
                    "验证码已发送，直接回复 6 位验证码即可，或发 /drawcode 123456（10 分钟内有效）。"
                )
            else:
                yield event.plain_result(msg)
                shot = getattr(self.doubao, "last_debug_shot", None)
                if shot:
                    yield event.image_result(shot)
            return

        yield event.plain_result("正在打开豆包登录页…")
        try:
            qr = await self.doubao.get_login_qr()
        except Exception as e:
            logger.exception(f"[doubao_draw] 打开登录页失败：{e}")
            yield event.plain_result(f"打开登录页失败：{e}")
            return

        if qr is None:
            yield event.plain_result("现在已经是登录状态，直接 /draw 就行。")
            return

        yield event.image_result(qr)
        yield event.plain_result(
            "扫码后请在手机上确认登录；二维码过期就重发 /drawlogin。"
            "也可发 /drawlogin phone 手机号。"
        )
        ok = await self.doubao.wait_login(120)
        if ok:
            yield event.plain_result("登录成功，可以 /draw 了。")
        else:
            yield event.plain_result(
                "没检测到登录：二维码可能过期，重发 /drawlogin 换新码；"
                "刚扫完请先在手机上确认。"
            )

    @filter.command("drawcode")
    async def drawcode(self, event: AstrMessageEvent):
        """提交手机验证码，完成豆包登录。用法：/drawcode 123456"""
        event.should_call_llm(False)
        raw = (event.message_str or "").strip()
        body = re.sub(r"^[/!.]?\s*drawcode\s*", "", raw, count=1, flags=re.I).strip()
        digits = re.sub(r"\D", "", body)
        if not digits:
            yield event.plain_result("用法：/drawcode 123456（先发 /drawlogin phone 手机号）")
            return
        user_id = str(event.get_sender_id())
        yield event.plain_result("正在验证…")
        try:
            msg = await self.doubao.login_with_code(digits)
        except Exception as e:
            logger.exception(f"[doubao_draw] 验证码登录失败：{e}")
            yield event.plain_result(f"验证码登录失败：{e}")
            return
        if msg == "ok":
            self._pending_code.pop(user_id, None)
            yield event.plain_result("登录成功，可以 /draw 了。")
        else:
            yield event.plain_result(msg)
            shot = getattr(self.doubao, "last_debug_shot", None)
            if shot:
                yield event.image_result(shot)

    @filter.command("drawcancel")
    async def drawcancel(self, event: AstrMessageEvent):
        """取消当前登录，关掉登录弹窗。用法：/drawcancel"""
        event.should_call_llm(False)
        try:
            msg = await self.doubao.cancel_login()
        except Exception as e:
            logger.exception(f"[doubao_draw] 取消登录失败：{e}")
            yield event.plain_result(f"取消登录失败：{e}")
            return
        self._pending_code.pop(str(event.get_sender_id()), None)
        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def plain_code_reply(self, event: AstrMessageEvent):
        """等待验证码期间，直接回复 6 位数字也能完成登录。"""
        user_id = str(event.get_sender_id())
        if not self._code_pending(user_id):
            return
        msg = (event.message_str or "").strip()
        if not re.fullmatch(r"\d{6}", msg):
            return
        yield event.plain_result("正在验证…")
        try:
            res = await self.doubao.login_with_code(msg)
        except Exception as e:
            logger.exception(f"[doubao_draw] 回复验证码失败：{e}")
            yield event.plain_result(f"验证码登录失败：{e}")
            event.stop_event()
            return
        if res == "ok":
            self._pending_code.pop(user_id, None)
            yield event.plain_result("登录成功，可以 /draw 了。")
        else:
            yield event.plain_result(res)
            shot = getattr(self.doubao, "last_debug_shot", None)
            if shot:
                yield event.image_result(shot)
        event.stop_event()

    @filter.after_message_sent()
    async def doubao_new_chat_after_new(self, event: AstrMessageEvent):
        """内置 /new 成功后，同步开一个新的豆包会话。"""
        if not event.get_extra("_clean_group_context_session", False):
            return
        # AstrBot 已剥掉唤醒前缀，这里看到的是 "new"；/reset 也带同一标记，需要排除
        if (event.message_str or "").strip().lower() != "new":
            return
        logger.info("[doubao_draw] 检测到 /new，准备切换豆包会话")
        self._start_watchdog_if_needed()
        try:
            res = await self.doubao.new_chat()
            if res == "ok":
                logger.info("[doubao_draw] /new：已切换到新的豆包会话")
            else:
                logger.warning(f"[doubao_draw] /new：切换豆包会话失败：{res}")
        except Exception as e:
            logger.warning(f"[doubao_draw] /new：切换豆包会话失败：{e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def cache_draw_ref(self, event: AstrMessageEvent):
        """先发图、再单独发 /draw 时，暂存最近一张/几张图作为参考图。"""
        msg = (event.message_str or "").strip()
        if re.match(r"^(?:doubao_draw|draw|绘图|画图)(?:\s|$)", msg, re.I):
            return
        refs = await self._refs_from_event(event)
        if not refs:
            return
        uid = str(event.get_sender_id())
        self._cleanup_ref_cache()
        self._pending_refs[uid] = (time.monotonic(), refs)
        logger.info(f"[doubao_draw] 已暂存 {len(refs)} 张参考图，等待下一条 /draw")

    def _code_pending(self, user_id: str, ttl: int = 600) -> bool:
        ts = self._pending_code.get(user_id)
        if ts is None:
            return False
        if time.monotonic() - ts > ttl:
            self._pending_code.pop(user_id, None)
            return False
        return True

    def _register_vnc_api(self):
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/vnc/status",
                self._vnc_status,
                ["GET"],
                "VNC status",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/vnc/ensure",
                self._vnc_ensure,
                ["POST"],
                "Ensure VNC services",
            )
        except Exception as e:
            logger.warning(f"[doubao_draw] VNC API 注册失败：{e}")

    async def _vnc_status(self):
        try:
            return {"status": "ok", "data": await self.vnc.status()}
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}}

    async def _vnc_ensure(self):
        try:
            return {"status": "ok", "data": await self.vnc.ensure()}
        except Exception as e:
            return {"status": "error", "message": str(e), "data": {}}

    def _start_watchdog_if_needed(self):
        if not self.config.get("browser_watchdog", True):
            return
        if self._watchdog_task and not self._watchdog_task.done():
            return
        try:
            self._watchdog_task = asyncio.create_task(self._browser_watchdog())
        except RuntimeError:
            self._watchdog_task = None

    async def _browser_watchdog(self):
        interval = max(15, int(self.config.get("watchdog_interval", 30)))
        logger.info(f"[doubao_draw] 浏览器兜底已启用，每 {interval} 秒检查一次")
        await asyncio.sleep(interval)
        while True:
            try:
                if self.config.get("vnc_auto_start", True):
                    await self.vnc.ensure()
            except Exception as e:
                logger.warning(f"[doubao_draw] VNC 兜底检查异常：{e}")
            try:
                ok = await self.browser.ensure_alive()
                if not ok:
                    logger.warning("[doubao_draw] 浏览器兜底恢复失败，下一轮重试")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[doubao_draw] 浏览器兜底检查异常：{e}")
            await asyncio.sleep(interval)

    @filter.command("drawlogout")
    async def drawlogout(self, event: AstrMessageEvent):
        """退出豆包登录，清掉本地登录态。"""
        event.should_call_llm(False)
        try:
            await self.doubao.logout()
        except Exception as e:
            logger.exception(f"[doubao_draw] 退出登录失败：{e}")
            yield event.plain_result(f"退出登录失败：{e}")
            return
        yield event.plain_result("已退出登录，本地登录态已清除。下次用 /drawlogin 重新登。")

    @filter.command("shot")
    async def shot(self, event: AstrMessageEvent):
        """截当前浏览器画面，不跳转，停在哪页截哪页。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        try:
            path = await self.doubao.screenshot()
        except Exception as e:
            yield event.plain_result(f"截图失败：{e}")
            return
        yield event.image_result(path)

    @_cmd("doubao", "豆包")
    async def doubao_home(self, event: AstrMessageEvent):
        """回到豆包对话页。用法：/doubao"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        yield event.plain_result("正在回到豆包对话页…")
        try:
            url = await self.doubao.go_chat()
        except Exception as e:
            yield event.plain_result(f"打开豆包页失败：{e}")
            return
        yield event.plain_result(f"已回到：{url}")

    @filter.command("where")
    async def where(self, event: AstrMessageEvent):
        """看当前浏览器停在哪一页。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        try:
            cur = await self.doubao.current()
        except Exception as e:
            yield event.plain_result(f"读取失败：{e}")
            return
        yield event.plain_result(f"{cur.get('title', '')}\n{cur.get('url', '')}")

    @filter.command("open")
    async def open_page(self, event: AstrMessageEvent):
        """打开网址。用法：/open https://www.doubao.com/chat/"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        raw = (event.message_str or "").strip()
        body = re.sub(r"^[/!.]?\s*open\s*", "", raw, count=1, flags=re.I).strip()
        if not body.startswith("http"):
            yield event.plain_result("用法：/open https://example.com（需以 http 开头）")
            return
        try:
            url = await self.doubao.open_url(body)
        except Exception as e:
            yield event.plain_result(f"打开失败：{e}")
            return
        yield event.plain_result(f"已打开：{url}")

    @filter.command("tap")
    async def tap(self, event: AstrMessageEvent):
        """按文字或 CSS 选择器点击。用法：/tap 登录"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        raw = (event.message_str or "").strip()
        body = re.sub(r"^[/!.]?\s*tap\s*", "", raw, count=1, flags=re.I).strip()
        if not body:
            yield event.plain_result("用法：/tap 登录 或 /tap #login-btn")
            return
        try:
            msg = await self.doubao.click_target(body)
        except Exception as e:
            yield event.plain_result(f"点击失败：{e}")
            return
        yield event.plain_result("点到了。" if msg == "ok" else msg)

    @filter.command("input")
    async def input_text(self, event: AstrMessageEvent):
        """往当前输入框写内容。用法：/input 一只橘猫"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        raw = (event.message_str or "").strip()
        body = re.sub(r"^[/!.]?\s*input\s*", "", raw, count=1, flags=re.I).strip()
        if not body:
            yield event.plain_result("用法：/input 要输入的内容")
            return
        try:
            msg = await self.doubao.type_text(body)
        except Exception as e:
            yield event.plain_result(f"输入失败：{e}")
            return
        yield event.plain_result("已写入输入框。" if msg == "ok" else msg)

    @filter.command("key")
    async def key(self, event: AstrMessageEvent):
        """按一个键。用法：/key Enter"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        raw = (event.message_str or "").strip()
        body = re.sub(r"^[/!.]?\s*key\s*", "", raw, count=1, flags=re.I).strip() or "Enter"
        try:
            await self.doubao.press_key(body)
        except Exception as e:
            yield event.plain_result(f"按键失败：{e}")
            return
        yield event.plain_result(f"已按键：{body}")

    @filter.command("back")
    async def back(self, event: AstrMessageEvent):
        """浏览器后退。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能操控浏览器。")
            return
        try:
            url = await self.doubao.go_back()
        except Exception as e:
            yield event.plain_result(f"后退失败：{e}")
            return
        yield event.plain_result(f"已后退：{url}")

    @filter.command("drawstatus")
    async def drawstatus(self, event: AstrMessageEvent):
        """查看浏览器、登录态与今日额度。"""
        event.should_call_llm(False)
        user_id = str(event.get_sender_id())
        limit = int(self.config.get("daily_limit", 20))
        remain = self.store.remaining_today(user_id, limit)
        try:
            logged = await self.doubao.is_logged_in()
            browser_txt = "就绪"
        except Exception as e:
            logged = False
            browser_txt = f"异常（{e}）"
        yield event.plain_result(
            f"浏览器：{browser_txt}\n登录态：{'已登录' if logged else '未登录'}\n"
            f"今日剩余：{'不限' if remain is None else f'{remain} 张'}"
        )

    @filter.command("ddprobe")
    async def ddprobe(self, event: AstrMessageEvent):
        """页面结构探测，豆包改版后用来修选择器。"""
        event.should_call_llm(False)
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能用 /ddprobe。")
            return
        try:
            info = await self.doubao.probe()
        except Exception as e:
            yield event.plain_result(f"探测失败：{e}")
            return

        lines = [
            f"url: {info.get('url')}",
            f"登录: {info.get('logged_in')}",
            f"img: {info.get('img_count')} / txt: {info.get('text_nodes')} "
            f"/ editable: {info.get('editables')} / textarea: {info.get('textareas')} "
            f"/ file: {info.get('file_inputs')}",
            "命中：" + ", ".join(
                f"{k.split('[')[0][:28]}={v}"
                for k, v in (info.get("selector_hits") or {}).items()
                if v
            ),
            "图片样例：" + " | ".join((info.get("img_sample") or [])[-2:]),
            "文字样例：" + " | ".join((info.get("text_sample") or [])[-2:]),
        ]
        yield event.plain_result("\n".join(lines)[:1800])

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="doubao_draw")
    async def tool_draw(self, event: AstrMessageEvent, prompt: str, ratio: str = ""):
        """用豆包绘制图片。用户要求画图、生成图片、出图时调用。

        Args:
            prompt(string): 画面描述，尽量具体
            ratio(string): 画面比例，可选 1:1、3:4、4:3、16:9、9:16，留空使用默认
        """
        if not self.config.get("enable_llm_tool", True):
            yield event.plain_result("AI 识别生图已关闭，请用 /draw 触发。")
            return
        if not self.config.get("enabled", True):
            return
        user_id = str(event.get_sender_id())
        limit = int(self.config.get("daily_limit", 20))
        if self.store.remaining_today(user_id, limit) == 0:
            yield event.plain_result(f"用户今天的 {limit} 张生图额度已用完。")
            return
        if self.browser.lock.locked():
            yield event.plain_result("正在处理另一个生图任务，请稍后再试。")
            return

        refs = await self._collect_refs(event)
        got = 0
        try:
            async for path in self.doubao.generate_stream(prompt, ratio or "", refs):
                got += 1
                self.store.incr_today(user_id, 1)
                yield event.image_result(path)
        except dd_doubao.GenerationCancelled:
            return
        except dd_doubao.NotLoggedIn:
            if got == 0:
                yield event.plain_result("豆包登录态失效，需要管理员发 /drawlogin 重新扫码。")
            return
        except Exception as e:
            logger.exception(f"[doubao_draw] 工具调用异常：{e}")
            if got == 0:
                yield event.plain_result(f"生图失败：{e}")
            return

        if got == 0:
            yield event.plain_result("这次没有生成出图片，请稍后再试。")

    # ---------------- 内部 ----------------

    def _schedule_llm_tool_switch(self):
        try:
            self._llm_switch_task = asyncio.create_task(self._apply_llm_tool_switch())
        except RuntimeError:
            self._llm_switch_task = None

    async def _apply_llm_tool_switch(self):
        await asyncio.sleep(2.5)
        active = bool(self.config.get("enable_llm_tool", True)) and bool(
            self.config.get("enabled", True)
        )
        try:
            if active:
                ok = await self.context.activate_llm_tool_async("doubao_draw")
            else:
                ok = await self.context.deactivate_llm_tool_async("doubao_draw")
            logger.info(
                f"[doubao_draw] AI 识别生图工具：{'启用' if active else '停用'}（{ok}）"
            )
        except Exception as e:
            logger.warning(f"[doubao_draw] 应用 AI 识别开关失败：{e}")

    async def _optimize_prompt(
        self, event: AstrMessageEvent, prompt: str, ratio: str, has_refs: bool
    ) -> str:
        try:
            provider = await self.context.get_using_provider_async(
                umo=event.unified_msg_origin
            )
        except Exception as e:
            logger.warning(f"[doubao_draw] 获取 LLM Provider 失败：{e}")
            return prompt
        if provider is None:
            logger.warning("[doubao_draw] 没有可用 LLM Provider，跳过提示词优化")
            return prompt

        system = (
            "你是生图提示词优化器。把用户的描述改写成适合图像生成模型的具体提示词。"
            "只输出优化后的提示词，不要解释、不要标题、不要引号。"
            "保留用户原意、必须出现的文字、需要修改的部分和风格要求，不要新增无关主体。"
        )
        if has_refs:
            system += "这是基于参考图的改图，必须保持原图主体、构图和比例，只改用户要求的部分。"
        user_prompt = prompt
        if ratio:
            user_prompt += f"\n（画面比例 {ratio} 由程序附加，不要在输出中写比例）"

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=user_prompt,
                    contexts=[],
                    system_prompt=system,
                ),
                timeout=20,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:
            logger.warning(f"[doubao_draw] 提示词优化失败，使用原文：{e}")
            return prompt

        text = re.sub(r"^```(?:text)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
        text = re.sub(
            r"^(?:优化后的?提示词|提示词|prompt)\s*[:：]\s*", "", text, flags=re.I
        ).strip()
        text = text.strip('"“”\'')
        text = re.sub(r"\s*画面比例\s*\d+\s*[:：]\s*\d+\s*$", "", text).strip()
        if len(text) < 2:
            return prompt
        logger.info(f"[doubao_draw] AI 优化提示词：{text[:80]}")
        return text[:1500]

    async def _collect_refs(self, event: AstrMessageEvent) -> list[bytes]:
        uid = str(event.get_sender_id())
        refs = await self._refs_from_event(event)
        if refs:
            # 本条消息自带图/引用图，旧缓存作废
            self._pending_refs.pop(uid, None)
            return refs
        cached = self._get_cached_refs(uid)
        if cached:
            logger.info(f"[doubao_draw] 使用暂存的 {len(cached)} 张参考图")
            return cached
        return []

    async def _refs_from_event(self, event: AstrMessageEvent) -> list[bytes]:
        max_refs = int(self.config.get("max_refs", 3))
        comps = []
        try:
            comps = event.get_messages() or []
        except Exception:
            comps = list(getattr(event.message_obj, "message", []) or [])

        images = []
        for c in comps:
            if isinstance(c, Comp.Image):
                images.append(c)
            elif isinstance(c, Comp.Reply):
                for cc in (getattr(c, "chain", None) or []):
                    if isinstance(cc, Comp.Image):
                        images.append(cc)

        out: list[bytes] = []
        for img in images[:max_refs]:
            try:
                p = await img.convert_to_file_path()
                data = Path(p).read_bytes()
                if 0 < len(data) <= 10 * 1024 * 1024:
                    out.append(data)
            except Exception as e:
                logger.warning(f"[doubao_draw] 参考图读取失败：{e}")
        return out

    def _cleanup_ref_cache(self):
        now = time.monotonic()
        for k in [k for k, (ts, _) in self._pending_refs.items() if now - ts > 600]:
            self._pending_refs.pop(k, None)

    def _get_cached_refs(self, user_id: str) -> list[bytes] | None:
        item = self._pending_refs.pop(user_id, None)
        if not item:
            return None
        ts, refs = item
        if time.monotonic() - ts > 600:
            return None
        return refs

    def _cooldown_left(self, user_id: str) -> int:
        cooldown = int(self.config.get("cooldown", 20))
        last = self._last_at.get(user_id)
        if not last or cooldown <= 0:
            return 0
        left = cooldown - (time.monotonic() - last)
        return max(0, int(left))

    @staticmethod
    def _split_ratio(text: str) -> tuple[str, str]:
        m = RATIO_RE.match(text.strip())
        if m:
            return m.group(1), m.group(2).strip()
        return "", text.strip()

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        for attr in ("is_admin", "is_admin_user"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:
                    continue
        role = getattr(event, "role", None)
        return role in ("admin", "owner")

    async def terminate(self):
        task = self._watchdog_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        llm_task = self._llm_switch_task
        if llm_task:
            llm_task.cancel()
            try:
                await llm_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        try:
            await self.browser.close()
        except Exception:
            pass
