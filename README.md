# Akasha WeChat WeFlow 适配器

> 把微信（经 [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers) SSE）接入 [MaiBot](https://github.com/Mai-with-u/MaiBot) 的双工消息网关适配器，移植自 [Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat)。

本插件以 **MaiBot 原生插件**形式运行，通过 `@MessageGateway(route_type="duplex")` 声明双工网关：

- **入站**：订阅 WeFlow 的 SSE 推送（`GET /api/v1/push/messages`），将微信消息转换为 MaiBot `MessageDict` 后经 `ctx.gateway.route_message()` 注入 Host。
- **出站**：Host 通过 `@MessageGateway` handler 下发 `MessageDict`，插件解析后调用 UIA 或 WeFlow REST API 发送到微信。

## 功能特性

- **双工网关**：一条 SSE 长连接完成入站订阅，`@MessageGateway` handler 处理出站发送。
- **WeFlow SSE 入站**：自动重连、401 检测、`message.new` / `message.revoke` 事件过滤。
- **消息缓冲合并**：群聊按 `buffer_seconds` 合并多条连续消息，避免 AI 被频繁打断。
- **三种群回复模式**：`mention`（仅 @ 回复）/ `all`（全部回复）/ `batch`（整群批处理）。
- **图片描述**：支持 `ollama` / `openai`（兼容接口）视觉模型，自动为图片生成中文描述。
- **图片转发**：可选下载图片并以 `image` 消息段（base64 + sha256）转发给 MaiBot。
- **双发送器**：`uia`（Windows UI 自动化，默认）/ `weflow_api`（WeFlow REST API，需第三方扩展支持）。
- **稳定 ID 映射**：md5 确定性 ID + 持久化 `id_contact_map.json`，重启后出站反查不丢失。
- **多重去重**：rawid 去重（TTL）+ 内容去重（防 AI 回复回流，TTLCache）+ 历史消息过滤。
- **配置热重载**：`on_config_update` 响应 `config.toml` 变更，自动重启桥接应用新配置。
- **WebUI 集成**：`PluginConfigBase` 配置模型自动生成 WebUI 可渲染的配置表单。

## 修复的原版缺陷

相对原版 Akasha-WeChat 的改进：

| 原版缺陷 | 本插件修复 |
|----------|-----------|
| `_sent_recently` 字典无限增长导致内存泄漏 | `cachetools.TTLCache` 自动过期 |
| `hash()` 受 `PYTHONHASHSEED` 影响导致跨进程 ID 不稳定 | `md5` 确定性映射 |
| `raw_message` 丢失 `@` 段文本 | mention 模式前置 `at` Seg，`processed_plain_text` 保留完整文本 |
| 图片消息仅注入文本描述，AI 看不到真实图片 | 同时发送真实 `image` Seg（base64 + sha256） |
| `paused` 变量声明但未生效 | 移除，改用 `plugin.enabled` 配置项 |

## 环境要求

- **MaiBot** Host `>=1.0.0`，MaiBot SDK `>=2.0.0`
- **WeFlow**：本地运行，已启用 `API 服务` + `主动推送`，默认地址 `http://127.0.0.1:5031`
- **微信** 4.0+（WeFlow 要求）
- **Python 依赖**：`aiohttp>=3.9.0`、`cachetools>=5.3.0`、`pyperclip>=1.8.2`、`uiautomation>=2.0.17`
- **UIA 发送方式额外要求**：Windows 10+，微信 PC 版已登录并保持窗口可前台

> WeFlow 官方文档明确其为「微信聊天记录查看、分析与导出工具」，仅提供**读取类** HTTP API。因此发送消息默认依赖 UIA（Windows UI 自动化）。`weflow_api` 发送方式面向兼容 WeFlow 协议的第三方扩展。

## 安装

将本目录放入 MaiBot 的插件目录（通常是 `plugins/`），MaiBot 会自动发现并加载：

```
<maibot>/plugins/MaiBot-WeChat-WeFlow-Adapter/
├── _manifest.json
├── plugin.py
├── config.py
├── bridge.py
├── ...
└── config.example.toml
```

首次加载时 Runner 会根据 `config_model` 自动生成默认 `config.toml`（若不存在）。

## 配置

复制 `config.example.toml` 为 `config.toml`，按实际环境填写：

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[weflow]
base_url = "http://127.0.0.1:5031"
access_token = "你的 WeFlow Access Token"
send_api = "http://127.0.0.1:5031/api/v1/message"
send_method = "uia"          # uia=Windows UI 自动化（推荐），weflow_api=WeFlow REST API
request_timeout = 30.0

[bot]
nicknames = ["我的机器人昵称"]  # 机器人微信昵称，群聊 @ 检测与自回复过滤用
wxid = "wxid_xxxxxxxxx"       # 机器人自身 wxid（self_id）

[bridge]
buffer_seconds = 5
group_reply_mode = "mention"  # mention / all / batch
reconnect_delay_sec = 10
history_filter_enabled = true

[image_caption]
provider = "none"             # none / ollama / openai
model = "llava:7b"
api_key = ""
api_base = "https://api.moonshot.cn/v1"
prompt = "请用中文简短描述这张图片的内容"
ollama_base_url = "http://127.0.0.1:61000"
ollama_timeout = 60
download_images = true
attachments_dir = "wechat_images"

[chat]
group_list_type = "whitelist" # whitelist / blacklist
group_list = []               # 群名或群ID
private_list_type = "whitelist"
private_list = []
ban_user_id = []

[filters]
ignore_self_message = true
content_dedupe_ttl_sec = 120
regex_filter_enabled = false
regex_filter_mode = "blacklist"
regex_filter_patterns = []
```

也可以不编辑文件，直接在 MaiBot WebUI 的插件配置页面修改。

### 关键配置说明

| 配置项 | 说明 |
|--------|------|
| `weflow.access_token` | 在 WeFlow「设置 → API 服务」中获取，SSE 与 HTTP API 鉴权用 |
| `weflow.send_method` | `uia` 需 Windows + 微信 PC 版；`weflow_api` 需第三方扩展提供发送端点 |
| `bot.nicknames` | 机器人微信昵称列表，用于群聊 `@` 检测与自回复过滤 |
| `bot.wxid` | 机器人自身 wxid，作为 `self_id` 上报网关，并参与自回复过滤 |
| `bridge.group_reply_mode` | `mention`=仅被 @ 时回复；`all`=群内所有消息都回复；`batch`=整群合并为一条批处理消息 |
| `image_caption.provider` | `none`=不描述仅转发图片；`ollama`=本地 Ollama 视觉模型；`openai`=OpenAI 兼容接口 |
| `chat.group_list_type` | `whitelist`=仅响应 `group_list` 中的群；`blacklist`=屏蔽 `group_list` 中的群 |

## 架构

```
┌─────────────┐    SSE 推送     ┌──────────────────────────┐    route_message    ┌─────────┐
│   WeFlow    │ ──────────────► │  transport.py (SSE 客户端)│ ──────────────────► │         │
│ (微信数据层) │                 │  bridge.py (过滤/缓冲/合并)│                     │  MaiBot │
│             │ ◄────────────── │  codecs/inbound.py (编解码)│ ◄────────────────── │  Host   │
└─────────────┘   UIA / REST    │  codecs/outbound.py        │   @MessageGateway   └─────────┘
                  发送           │  senders.py (uia / weflow) │
                                 └──────────────────────────┘
```

### 消息处理流水线（入站）

`bridge.py` 的 `_on_message` 依次执行六级过滤，任一命中即丢弃：

1. **事件类型过滤**：仅处理 `message.new`，丢弃 `message.revoke` 等
2. **历史消息过滤**：`history_filter_enabled` 时丢弃 `timestamp < start_timestamp` 的消息
3. **rawid 去重**：TTL 集合，防止重复推送
4. **自回复/语音/表情/空内容过滤**：`should_ignore()` 检测 `sourceName ∈ bot_nicknames`、`sessionId == bot_wxid`、`[语音]`/`[表情]`、空内容
5. **内容去重**：TTLCache，防止 AI 回复被 SSE 回流再次触发
6. **名单/正则过滤**：群/私聊白/黑名单、`ban_user_id`、正则白/黑名单

通过后进入缓冲区，按 `buffer_seconds` 合并，最终调用 `ctx.gateway.route_message()` 注入 Host。

### 出站发送

Host 通过 `@MessageGateway` handler 下发 `MessageDict`，`plugin.py` 的 `handle_outbound`：

1. `resolve_contact()` 反查 `ContactRef`（优先 `group_id`，其次 `user_id`）
2. `iter_send_actions()` 遍历 `raw_message` 段：
   - `text` → `sender.send_text(contact, text)`
   - `image`/`emoji` → base64 解码到临时文件 → `sender.send_image(contact, path)` → 删除临时文件
   - `face` → 转为 `[表情]` 文本发送
   - `record`/`video`/未知 → 跳过

## 文件结构

```
MaiBot-WeChat-WeFlow-Adapter/
├── _manifest.json          # 插件元信息（manifest v2）
├── plugin.py               # 主插件入口，@MessageGateway handler + 生命周期
├── config.py               # Pydantic 配置模型（7 分组，WebUI Schema）
├── constants.py            # 共享常量
├── bridge.py               # 桥接核心：过滤流水线 + 缓冲合并 + 图片编排 + 状态上报
├── state.py                # RuntimeState 运行时状态聚合（asyncio.Lock 保护）
├── transport.py            # WeFlow SSE 异步客户端（自动重连）
├── filters.py              # should_ignore / ContentDeduper / RawIdDeduper / ChatFilter
├── id_mapping.py           # 确定性 md5 ID + 双向 ContactRef 映射 + 持久化
├── image_caption.py        # 图片下载 + ollama/openai 视觉描述
├── senders.py              # BaseSender 抽象 + create_sender 工厂
├── uia_sender.py           # Windows UI Automation 发送器
├── weflow_api_sender.py    # WeFlow REST API 发送器
├── codecs/
│   ├── inbound.py          # WeFlow SSE 事件 → MaiBot MessageDict
│   └── outbound.py         # MessageDict → 发送动作
├── config.example.toml     # 配置示例
└── .gitignore
```

## WeFlow SSE 事件字段

本插件处理的 WeFlow SSE `data:` 负载字段（对照 [WeFlow HTTP-API 文档](https://github.com/Techuouo520/UserDataIsSafeFromUsers/blob/main/docs/HTTP-API.md)）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `event` | string | 事件类型：`message.new` / `message.revoke` |
| `sessionId` | string | 会话 ID，群为 `xxx@chatroom`，私聊为 `wxid_xxx` |
| `sessionType` | string | `group` / `private` / `other`（可能为空，靠 `sessionId` 兜底判断） |
| `rawid` | string | 消息唯一 ID，用于去重 |
| `sourceName` | string | 发送者昵称（机器人自身发送时命中 `bot.nicknames`） |
| `groupName` | string | 群名（仅群聊，可能带 `(123)` 人数后缀，本插件自动清洗） |
| `content` | string | 消息内容，`[图片]`/`[语音]`/`[表情]` 为特殊标记 |
| `timestamp` | int | 秒级 Unix 时间戳 |

> WeFlow SSE 事件**不含** `talkerId`、`type`、`senderName` 字段。私聊对方用 `sessionId` 兜底，语音靠 `content` 含 `[语音]` 兜底。

## 开发与调试

### 日志

插件日志通过 `self.ctx.logger` 输出，可在 MaiBot 日志中按插件 ID `akasha.wechat-weflow-adapter` 过滤。

### 运行时数据

- `data/id_contact_map.json`：ID ↔ 联系人映射，重启后自动加载
- `data/wechat_images/`：下载的图片缓存（按 `image_caption.attachments_dir` 配置）

### 验证

```bash
# 语法检查
python -c "import ast; [ast.parse(open(f).read()) for f in ['plugin.py','bridge.py','state.py','transport.py','config.py','filters.py','id_mapping.py','image_caption.py','senders.py','uia_sender.py','weflow_api_sender.py','codecs/inbound.py','codecs/outbound.py']]; print('AST OK')"

# 代码规范
ruff check .
```

## 致谢

- [Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) — 原项目，本插件移植自其 UIA 发送与消息处理逻辑
- [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers) — 微信聊天记录查看、分析与导出工具，提供 SSE 推送与 HTTP API
- [MaiBot](https://github.com/Mai-with-u/MaiBot) — AI 聊天机器人框架，本插件为其消息平台适配器

## License

MIT
