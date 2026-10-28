# -*- coding: utf-8 -*-
"""集中存放页面选择器与页面内 JS。

豆包改版时优先改这里：先用 /ddprobe 看当前页面的候选元素，再把新的选择器补到列表最前面。
"""

# 聊天输入框候选（按优先级）
INPUT_SELECTORS = [
    'textarea[data-testid="chat_input_input"]',
    'div[data-testid="chat_input_input"][contenteditable="true"]',
    'div[contenteditable="true"][data-slate-editor="true"]',
    'div[contenteditable="true"][role="textbox"]',
    "textarea",
    'div[contenteditable="true"]',
]

# 新对话按钮候选（仅在 /new 时使用）
NEW_CHAT_SELECTORS = [
    '[data-testid="create_conversation_button"]',
    '[data-testid="new_chat_button"]',
    'div[class*="sidebar_nav_item"]:has-text("新对话")',
    'button[aria-label="新对话"]',
    'button:has-text("新对话")',
    'text="新对话"',
]

# 登录弹窗（豆包 2025 新版，带稳定的 data-testid）
LOGIN_BUTTON = '[data-testid="to_login_button"]'
LOGIN_MODAL = '[data-testid="login_content"]'
LOGIN_CLOSE = '[data-testid="modal_close"]'
LOGIN_PHONE_TAB = '[data-testid="login_phone_verify_button"]'
LOGIN_AGREEMENT = '[data-testid="login_agreement_check"]'
LOGIN_PHONE_INPUT = '[data-testid="login_phone_number_input"]'
LOGIN_NEXT = '[data-testid="login_next_button"]'
LOGIN_CODE = '[data-testid="code_input"]'
LOGIN_CODE_INPUT = '[data-testid="code_input"] input'

# 登录二维码候选
QR_SELECTORS = [
    '[data-testid="qrcode_image"]',
    'img[src*="qrcode"]',
    'canvas[class*="qr"]',
    '[class*="qrcode"] canvas',
    '[class*="qrcode"] img',
    '[class*="qr-code"] img',
    '[class*="qrCode"] img',
]

# 手机号登录：输入框、验证码输入框、发送按钮、提交按钮
PHONE_INPUT_SELECTORS = [
    LOGIN_PHONE_INPUT,
    'input[type="tel"]',
    'input[placeholder*="手机号"]',
    'input[placeholder*="手机"]',
    'input[name*="phone"]',
    'input[name*="mobile"]',
]

CODE_INPUT_SELECTORS = [
    LOGIN_CODE_INPUT,
    'input[placeholder*="验证码"]',
    'input[placeholder*="校验码"]',
    'input[name*="code"]',
    'input[autocomplete="one-time-code"]',
]

# 切换到手机号登录的入口文案
PHONE_TAB_TEXTS = ["手机号登录", "验证码登录", "手机号/验证码登录", "手机登录"]


# 页面内 JS：判断是否已登录（有输入框，且没有登录入口/登录弹窗）
JS_HAS_INPUT = """
() => {
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  if (visible(document.querySelector('[data-testid="login_content"], [data-testid="to_login_button"], [data-testid="login_phone_number_input"], [data-testid="code_input"]'))) return false;
  const t = document.body.innerText || '';
  const lines = t.split('\\n').map(s => s.trim());
  if (lines.indexOf('登录') >= 0) return false;
  if (t.indexOf('扫码登录') >= 0 || t.indexOf('登录后即可') >= 0 || t.indexOf('登录以解锁') >= 0) return false;
  if (document.querySelector('input[placeholder*="手机号"], input[type="tel"]')) return false;
  if (document.querySelector('[class*="avatar"] img, img[class*="avatar"], [class*="user-avatar"], [class*="userInfo"]')) return true;
  if (t.indexOf('暂无对话') >= 0) return true;
  const el = document.querySelector('textarea, div[contenteditable="true"]');
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
"""

# 页面内 JS：探测页面结构，供 /ddprobe 排障
JS_PROBE = """
() => {
  const info = {url: location.href, title: document.title, img_count: 0, img_sample: [], text_nodes: 0, text_sample: [], editables: 0, textareas: 0, file_inputs: 0, buttons: [], body_text: ''};
  const imgs = Array.from(document.querySelectorAll('img'))
    .map(i => i.currentSrc || i.src || '')
    .filter(s => s && !s.startsWith('data:'));
  info.img_count = imgs.length;
  info.img_sample = imgs.slice(-8).map(s => s.slice(0, 160));
  const t = Array.from(document.querySelectorAll('[class*="markdown"], [data-testid*="message"]'))
    .map(n => (n.innerText || '').trim()).filter(Boolean);
  info.text_nodes = t.length;
  info.text_sample = t.slice(-3).map(s => s.slice(0, 120));
  info.editables = document.querySelectorAll('div[contenteditable="true"]').length;
  info.textareas = document.querySelectorAll('textarea').length;
  info.file_inputs = document.querySelectorAll('input[type="file"]').length;
  info.buttons = Array.from(document.querySelectorAll('button')).slice(0, 40).map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 20)).filter(Boolean);
  info.body_text = (document.body.innerText || '').replace(/\\n+/g, ' | ').slice(0, 300);
  return info;
}
"""
