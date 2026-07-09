# MaiBot 微信适配器（WeFlow）

把微信接入 [MaiBot](https://github.com/Mai-with-u/MaiBot) AI 聊天机器人框架的适配器插件，通过 [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers) 软件读取微信消息，再让 MaiBot 的 AI 自动回复。本插件由 [Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) 移植为 MaiBot 原生插件。

---

## 一、它是怎么工作的

简单说，整个过程分四步：WeFlow 负责读取微信里的消息，本插件接住这些消息后转交给 MaiBot 的 AI 去思考怎么回，AI 想好回复后，插件再用「模拟操作微信窗口」的方式把回复发出去。

为什么发送要模拟操作窗口？因为 WeFlow 官方定位是「微信聊天记录查看、分析与导出工具」，只提供读取类的 API，没有发送 API。所以发送消息这一步默认走 UIA（Windows UI 自动化），由程序模拟键盘鼠标操作微信 PC 版窗口完成发送。

```
┌──────────┐  1. 读取微信消息(SSE推送)  ┌──────────┐  2. 转交AI思考  ┌──────────┐
│  WeFlow  │ ─────────────────────────► │  本插件   │ ──────────────► │  MaiBot  │
│ (微信端)  │                            │          │                 │   (AI)   │
└──────────┘ ◄───────────────────────── └──────────┘ ◄────────────── └──────────┘
             4. 模拟操作微信窗口发回复                3. AI 给出回复内容
                        (UIA)
```

插件通过 `@MessageGateway(route_type="duplex")` 声明一个「双工网关」，意思是收消息和发消息都走它一个入口，一条 SSE 长连接搞定接收，发送则交给 UIA 模块。

---

## 二、前置准备

开始配置前，请确认你已准备好下面这些：

1. **一台 Windows 电脑**：因为发送消息要用 UIA 模拟操作微信 PC 版窗口，仅支持 Windows 系统。
2. **已安装并运行的 MaiBot**：版本需 `>=1.0.0`（MaiBot SDK `>=2.0.0`）。如果还没装，先去 [MaiBot 仓库](https://github.com/Mai-with-u/MaiBot) 装好并能正常启动。
3. **已安装 WeFlow 软件**：到 [WeFlow 项目](https://github.com/Techuouo520/UserDataIsSafeFromUsers) 下载安装，并在微信 PC 版里登录。
4. **微信 PC 版 4.0+ 已登录机器人微信号**：WeFlow 要求微信 4.0 以上版本，且机器人自己的微信号要在 PC 版上保持登录状态。
5. **Python 依赖会自动安装**：插件需要的 `aiohttp`、`cachetools`、`pyperclip`、`uiautomation` 这几个包，MaiBot 加载插件时会按 `_manifest.json` 自动装好，无需手动 `pip install`。

---

## 三、三步配置（核心）

跟着这三步走，跑通基本流程。

### 第 1 步：准备 WeFlow

1. 打开 WeFlow 软件，按提示登录微信。
2. 进入 WeFlow 的「设置 → API 服务」页面。
3. 把「API 服务」和「主动推送」两个开关都打开。
4. 复制页面上的 **Access Token**（访问令牌），等会儿要填到插件里。
5. 记下 WeFlow 运行的地址，本机默认是 `http://127.0.0.1:5031`。

> 这一步是为了让插件能从 WeFlow 拿到微信消息。WeFlow 会通过 SSE（一种服务器主动推送）把新消息实时推给插件。

### 第 2 步：确认微信机器人信息

1. 确认微信 PC 版已经登录了你要当机器人的那个微信号，窗口保持打开（不要最小化到托盘）。
2. 在 WeFlow 软件里找到机器人自己那条会话，能看到机器人的 **wxid**（形如 `wxid_xxxxxxxxx`），记下来。
3. 记下机器人的**微信昵称**（就是微信里设置的那个名字），用于插件识别群里 @ 机器人和过滤自己发的消息。

> wxid 是微信给每个账号分配的唯一 ID，跟微信号（自定义的那个）不是一回事，一定要在 WeFlow 里看准 `wxid_` 开头的那串。

### 第 3 步：在 MaiBot 安装并配置插件

1. **放插件目录**：把整个插件文件夹放到 MaiBot 的 `plugins/` 目录下，结构如下：

   ```text
   <maibot>/plugins/MaiBot-WeChat-WeFlow-Adapter/
   ├── _manifest.json
   ├── plugin.py
   ├── config.py
   ├── config.example.toml
   └── ...
   ```

2. **重启 MaiBot**（或在 WebUI 里点热重载），插件会被自动发现并加载。首次加载时会根据配置模型自动生成一份 `config.toml`（如果还没有的话）。

3. **配置插件**，两种方式任选其一：

   - **方式 A（推荐新手）**：打开 MaiBot WebUI → 进入「插件配置」页 → 找到本插件 → 用中文表单填写各项。每项都有标签和提示文字，照着填即可。
   - **方式 B（编辑文件）**：把插件目录下的 `config.example.toml` 复制一份改名为 `config.toml`，用文本编辑器打开按注释填写。

4. **必填这三项**（缺一不可，否则插件不会启动桥接）：

   | 配置项 | 在哪填 | 说明 |
   |--------|--------|------|
   | WeFlow 访问令牌 | `[weflow].access_token` | 第 1 步复制的 Access Token |
   | 机器人 wxid | `[bot].wxid` | 第 2 步记下的 `wxid_xxx` |
   | 机器人微信昵称 | `[bot].nicknames` | 第 2 步记下的昵称，可填多个 |

5. **保存后重载插件**：WebUI 改完点保存并重载；改文件的话重启 MaiBot 或触发热重载。

> 配置校验：插件启动时会检查 `access_token` 和 `wxid` 是否为空。只要有一个为空，桥接就不会启动，并在日志里输出中文告警，提示你哪一项没填。

### 第 4 步：配置 MaiBot 主配置（关键，否则无法回复）

插件本身配好后，还需要让 **MaiBot 主程序**知道「机器人在微信平台上的账号是谁」，否则发送消息时会报错 `平台 wechat 未配置机器人账号`。

打开 MaiBot 的主配置文件 `config/bot_config.toml`（或在 WebUI 的「Bot 配置」页），在 `[bot]` 段加入微信平台信息：

**只接微信**：
```toml
[bot]
platform = "wechat"
qq_account = ""
platforms = ["wechat:wxid_xxxxxxxxx"]   # ← 把 wxid_xxxxxxxxx 换成你机器人的 wxid
nickname = "你的机器人微信昵称"
alias_names = []
```

**同时接 QQ 和微信**：
```toml
[bot]
platform = "qq"
qq_account = "你的QQ号"
platforms = ["qq:你的QQ号", "wechat:wxid_xxxxxxxxx"]
nickname = "你的机器人微信昵称"
```

> `platforms` 的格式是 `平台名:账号ID`。微信平台填 `wechat:wxid_xxx`，这个 wxid 要和插件配置里的 `[bot].wxid` 一致。

改完保存后重启 MaiBot。这一步漏了的话，消息能收但不能回（日志会出现 `平台 wechat 未配置机器人账号`）。

---

## 四、图片理解如何配置

机器人默认只能看懂文字。想让机器人也能看懂图片，需要单独开一下图片理解功能。

本插件**不单独配置视觉模型**，而是直接复用 MaiBot Host 里已经配好的视觉模型（通过 `ctx.llm.generate` 调用）。所以**插件里不需要填任何 API Key 或模型地址**，只要在 MaiBot 那边配好一个支持看图的模型就行。

配置步骤：

1. 打开 MaiBot WebUI → 进入「模型配置」页。
2. 配置一个支持视觉（看图）的模型，比如 `gpt-4o`、`qwen-vl-plus`、`llava` 等，确保模型可用。
3. 回到本插件的配置页（或 `config.toml` 的 `[image_caption]` 段），把**「启用图片理解」**（`enabled`）打开。
4. 保存并重载插件。

开启后，群里或私聊发来的图片会被下载下来，交给 MaiBot 配好的视觉模型生成一段中文描述，连同原图一起交给 AI 思考回复。

> 强调一遍：插件配置里不需要填任何 API Key、模型名称、模型地址，这些全在 MaiBot 模型配置页统一管理。

---

## 五、验证是否成功

配置完重载插件后，按下面方法检查是否跑通。

**看 MaiBot 日志**，出现下面这些关键词就表示成功：

- `配置校验通过：WeFlow 令牌、bot.wxid 已配置` —— 必填项校验通过。
- `WeFlow SSE 已连接` 或 `网关已就绪` —— SSE 长连接已建立，开始收消息。

**实测一条消息**：用另一个微信号（或群里其他人）给机器人发一条消息，观察 MaiBot 日志是否收到，以及机器人微信是否自动回复。

**常见失败排查**：

- 日志里出现 `⚠️ WeFlow 访问令牌未填写` —— 说明 `access_token` 没填或填错，回第 1 步重新复制。
- 日志里出现 `⚠️ 机器人 wxid 未填写` —— 说明 `wxid` 没填，回第 2 步确认。
- SSE 连不上 —— 检查 WeFlow 是否在运行、「API 服务」和「主动推送」是否都开了、地址端口对不对。
- 收到消息但不回复 —— 检查群聊名单模式（默认 `whitelist` 但名单为空，等于不响应任何群，需要把要响应的群名加进 `group_list`，或改成 `blacklist` 模式）。

---

## 六、常见问题

**Q1：Access Token 哪里来？**
A：打开 WeFlow 软件 →「设置 → API 服务」页面，开启 API 服务后就能看到并复制 Access Token。

**Q2：提示「Manifest 校验失败」怎么办？**
A：确认你的 MaiBot 版本在插件要求的范围内（`>=1.0.0`），并检查插件目录下的 `_manifest.json` 没有被改动或损坏。如果是从 GitHub 重新下载一份覆盖即可。

**Q3：UIA 找不到微信窗口怎么办？**
A：确保微信 PC 版已登录且窗口处于可前台状态（不要最小化到系统托盘）。可以运行插件目录下 `uia_sender.py` 里的 `diagnose()` 诊断函数，它会输出当前能否找到微信窗口、输入框等调试信息，帮助定位问题。

**Q4：机器人对图片不做描述怎么办？**
A：两步检查：①打开 MaiBot WebUI 的「模型配置」页，确认配了一个支持看图的视觉模型并能正常调用；②回到本插件配置，确认 `[image_caption].enabled` 已设为 `true`。

**Q5：群里发消息机器人不回复怎么办？**
A：检查 `[bridge].group_reply_mode`（群聊回复模式）：默认是 `mention`，即只有被 @ 才回；想全部回就改成 `all`。同时检查 `[chat]` 段的群聊名单：默认 `whitelist` 模式且名单为空，等于不响应任何群，需要把目标群名或群 ID 加进 `group_list`，或把 `group_list_type` 改成 `blacklist`（名单留空即响应所有群）。

**Q6：日志提示「平台 wechat 未配置机器人账号」怎么办？**
A：这是 MaiBot 主配置没加微信平台。按上面「第 4 步」在 `bot_config.toml` 的 `[bot].platforms` 里加上 `wechat:wxid_xxx`。

**Q7：支持非 Windows 系统吗？**
A：UIA 发送方式只支持 Windows。如果你的环境不是 Windows，可以把 `[weflow].send_method` 改成 `weflow_api`，但这需要第三方扩展提供兼容 WeFlow 协议的发送端点，原生 WeFlow 不带发送 API。

---

## 七、配置项速查表

配置分七个段：`[plugin]`、`[weflow]`、`[bot]`、`[bridge]`、`[image_caption]`、`[chat]`、`[filters]`。下表列出各字段的中文含义，可在 `config.toml` 或 WebUI 表单中对照修改。

### `[plugin]` 插件基础设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `enabled` | `true` | 是否启用本插件，关掉后插件不工作 |
| `config_version` | `"1.0.0"` | 配置文件版本号，用于兼容性，一般不用改 |

### `[weflow]` WeFlow 连接设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `base_url` | `http://127.0.0.1:5031` | WeFlow 软件运行地址，本机一般是这个 |
| `access_token` | `""` | WeFlow 访问令牌，在 WeFlow「设置 → API 服务」复制，**必填** |
| `send_api` | `http://127.0.0.1:5031/api/v1/message` | 用 WeFlow 接口发消息时的地址，用 UIA 发送时无需关心 |
| `send_method` | `"uia"` | 消息发送方式：`uia`=Windows 自动化操作微信窗口（推荐）；`weflow_api`=WeFlow 接口发送（需第三方扩展） |
| `request_timeout` | `30.0` | 和 WeFlow 通信的超时时间（秒） |

### `[bot]` 微信机器人设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `nicknames` | `[]` | 机器人微信昵称列表，用于识别群里 @ 机器人和过滤自己发的消息，可填多个，**必填** |
| `wxid` | `""` | 机器人微信号的唯一 ID（`wxid_xxx`），在 WeFlow 里能看到，**必填** |

### `[bridge]` 消息处理设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `buffer_seconds` | `5` | 群里连续发多条消息时，等几秒合并成一条再交给 AI，避免刷屏 |
| `group_reply_mode` | `"mention"` | 群聊回复模式：`mention`=只在被 @ 时回复；`all`=群里每条都回；`batch`=把群里消息合并成一条处理 |
| `reconnect_delay_sec` | `10` | 和 WeFlow 断开后等多久再重连（秒） |
| `history_filter_enabled` | `true` | 是否忽略插件启动前就收到的历史消息，避免 AI 回复旧消息 |

### `[image_caption]` 图片理解设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `enabled` | `false` | 是否启用图片理解，开启后机器人能看懂图片（需先在 MaiBot 模型配置页配视觉模型） |
| `prompt` | `"请用中文简短描述这张图片的内容"` | 让视觉模型描述图片时用的提示词，一般不用改 |
| `download_images` | `true` | 是否把图片以原图形式一起转发给 MaiBot，看得更清楚但更耗流量 |
| `attachments_dir` | `"wechat_images"` | 下载的图片临时存放子目录，一般不用改 |
| `timeout` | `60` | 等待视觉模型返回描述的最长时间（秒），超时就放弃避免卡住 |

### `[chat]` 聊天名单设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `group_list_type` | `"whitelist"` | 群聊名单模式：`whitelist`=只响应名单里的群；`blacklist`=屏蔽名单里的群 |
| `group_list` | `[]` | 群聊名单（群名或群 ID），每行一个 |
| `private_list_type` | `"whitelist"` | 私聊名单模式：`whitelist`=只响应名单里的私聊；`blacklist`=屏蔽名单里的私聊 |
| `private_list` | `[]` | 私聊名单（对方昵称或 wxid），每行一个 |
| `ban_user_id` | `[]` | 封禁用户列表，这些用户的消息会被完全忽略 |

### `[filters]` 过滤设置

| 字段 | 默认值 | 含义 |
|------|--------|------|
| `ignore_self_message` | `true` | 是否忽略机器人自己发的消息，避免自己回复自己 |
| `content_dedupe_ttl_sec` | `120` | 内容去重时间（秒），防止 AI 回复被 WeFlow 回流再次触发 |
| `regex_filter_enabled` | `false` | 是否启用正则过滤 |
| `regex_filter_mode` | `"blacklist"` | 正则过滤模式：`blacklist`=匹配的被屏蔽；`whitelist`=只有匹配的才处理 |
| `regex_filter_patterns` | `[]` | 正则表达式列表，每行一个 |

---

## 八、致谢

- [Akasha-WeChat](https://github.com/alingalingling/Akasha-WeChat) —— 原项目，本插件移植自其 UIA 发送与消息处理逻辑。
- [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers) —— 微信聊天记录查看、分析与导出工具，提供 SSE 推送与读取 API。
- [MaiBot](https://github.com/Mai-with-u/MaiBot) —— AI 聊天机器人框架，本插件为其消息平台适配器。

---

## 九、License

MIT
