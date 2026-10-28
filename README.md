# 豆包生图（astrbot_plugin_doubao_draw）

用你自己的豆包账号在网页端生图，不调第三方 API、不做中转。

## 用法

- `/draw 一只橘猫` 直接出图
- `/draw 16:9 赛博朋克城市` 指定比例（1:1 / 3:4 / 4:3 / 16:9 / 9:16 / 2:3）
- 发图或引用图片 + `/draw 改成水彩` → 图生图（最多 3 张参考图）
- 也可以先发图，再单独发 `/draw 改成水彩`（图片暂存 10 分钟；群里建议发图时 @ 机器人或引用图片）
- 转发给豆包时会自动加“请直接生成一张图片”/“请参考我上传的图片…”前缀，避免被当成聊天
- 直接对 AI 说“帮我画只猫”，也会调用（LLM 工具 `doubao_draw`）
- 所有 `/draw` 都在当前豆包会话里继续；只有发 `/new` 才会同时开启新的豆包会话（`/new` 本身仍是机器人的新对话命令）
- `/drawlogin` 取二维码，用豆包 App 扫码（登录态存在 profile 目录，掉线才需要重扫）
- `/drawlogin phone 13800138000` 手机号登录，收到短信后直接回复 6 位验证码（或 `/drawcode 123456`）；登录前会自动勾选隐私协议
- 手机号输错：直接重发 `/drawlogin phone 正确号码`，会自动重置登录弹窗；验证码输错/过期：重发 `/drawcode 正确验证码`，或重发手机号重新收码
- `/drawcancel` 取消当前登录并关闭登录弹窗
- `/drawlogout` 退出登录（只清豆包站点登录态，不会关掉共享浏览器）
- `/drawstatus` 看浏览器、登录态、今日剩余额度
- `/ddprobe` 页面结构探测，豆包改版后用来修选择器

## 抓图方式

不抓 DOM、不做视觉识别：插件在页面内克隆豆包 `/chat/completion` 的 SSE 流，实时解析 `image_ori_raw` 原图地址，一出现就下载并转发；多图会逐张发送。**只转发图片**，豆包的文字回复不会发到 QQ。

## VNC 图形调试

插件 WebUI 里有「VNC 调试」页面：

- 已有 Xvfb / x11vnc / noVNC 直接复用
- 缺哪个自动拉起哪个；浏览器窗口也会由兜底任务恢复
- 页面内嵌 noVNC（6081 的 CORS 静态服务），也可复制直连地址 `http://<host>:6080/vnc.html`
- VNC 默认无密码，只适合内网/本机；对外暴露请自行加密码和 HTTPS 反向代理
- 镜像缺少 `Xvfb` / `x11vnc` / `novnc` / `websockify` 时需要先安装
- Docker 部署需要映射 `6080`（noVNC/websockify）和 `6081`（面板内嵌静态服务）；插件会复用已有服务，缺失时自动启动

## 失败怎么处理

豆包没有给出图片（拒绝、繁忙、只有文字）时，插件只发一条固定的“这次没有生成出图片，请稍后再试。”，不转发豆包原话。

## 配置

| 项 | 说明 |
|---|---|
| default_ratio | 默认比例 |
| timeout | 单次超时，默认 150 秒 |
| no_image_timeout | 等待首张图超时，默认 60 秒 |
| compress_for_qq | 大图转 JPEG 再发 QQ，默认开 |
| vnc_auto_start | 自动复用/补齐 VNC，默认开 |
| vnc_public_url | VNC 直连地址（可选） |
| vnc_web_port | noVNC/websockify 端口，默认 6080 |
| vnc_embed_port | 面板内嵌静态端口，默认 6081 |
| vnc_rfb_port | x11vnc 端口，默认 5900 |
| vnc_display | X 显示，默认 :99 |
| cooldown | 单用户冷却，默认 20 秒 |
| daily_limit | 每人每日张数，0 为不限 |
| max_refs | 参考图上限 |
| headless | 默认 true |
| chromium_path | 默认 /usr/bin/chromium |
| profile_dir | 登录态目录，留空用插件数据目录 |
| chat_url | 豆包对话页 |
| browser_watchdog | 浏览器兜底自愈，默认开 |
| watchdog_interval | 兜底检查间隔，默认 30 秒 |

浏览器兜底：每 30 秒检查一次，浏览器窗口被误关、页面全关或 Chromium 卡死时会自动重新拉起并打开豆包页。

## 改版了怎么办

1. `/ddprobe` 看 `img` 数量、`editables`/`textarea` 数量、各选择器命中数；
2. 打开 `dd_selectors.py`，把新的选择器加到对应列表最前面；
3. 重启 AstrBot 或重载插件。

## 注意

- 这是网页自动化，不是官方接口；豆包改版会失效，账号也可能被风控。
- 默认 20 秒冷却、每日限额，只适合自己或小群用。
- 2C2G 机器跑 Chromium 偏紧，出现 OOM 就把 `renderer-process-limit` 调小或加 swap。
