# WeMai 1.0.2 AI 维护说明书

本文档面向后续维护 WeMai 的 AI 或开发者。目标是保持出入站低延迟、消息准确、不同会话并发、同会话有序，并在不修改 MaiBot 核心的前提下完成微信 UI 出站。

## 1. 总体架构

WeMai 由1个 Python 监督器、1个 Python 核心进程、1个 Node 本地入站进程和1个 Windows 语音宿主组成。

```text
微信本地数据库（只读）
        │
        ▼
local_inbound/local-inbound.js
  SQLCipher 解密、消息解析、媒体解密、过滤、会话队列
        │ JSON 行 + ACK/NACK
        ▼
local_wechat_listener.py
        │
        ▼
wx_Processor.py 可靠 SQLite 队列
  同会话串行、跨会话最多 8 路、重试/死信
        │ Legacy WebSocket
        ▼
MaiBot

MaiBot 出站 ──► wx_Processor.py ──► wx_Listener.py ──► 微信独立窗口 UI
                                      │
                                      └─► WeMai.VoiceHost.exe + VB-CABLE（语音）
```

`local_inbound/lib/weflow510-content.js` 只是历史消息映射规则的本地实现名称，运行时数据源是微信本地数据库。

## 2. 入站原理

### 2.1 数据库和水位

`local_inbound/lib/wechat-store.js` 使用内置 `better-sqlite3-multiple-ciphers` 只读打开微信 `db_storage` 下的消息、会话、联系人、群成员和媒体数据库。数据库密钥从 `data/local_inbound_credentials.json` 读取；该文件中的密钥字段由 Windows DPAPI 加密。

启动时扫描消息表和联系人表，建立会话、好友、群聊、群成员原生ID、昵称、群名片和头像索引。每张消息表取当前最大 `local_id` 作为启动水位，因此适配器只处理启动后新写入的消息。运行中发现新表时也从当前末尾开始，不回放停机期间的旧消息。

文件系统变更会触发短延迟扫描，250ms 保险轮询只用于防止文件系统事件丢失。`rawid + 消息表 + local_id` 参与去重，状态文件保存已确认水位。媒体尚未落盘时，在 `LOCAL_INBOUND_MEDIA_WAIT_SECONDS` 内等待并重试。

图片 native addon 只负责微信 DAT 解密，输出可能仍是 `wxgf` 包装的 HEVC 静态图。sidecar 必须按真实文件头验证 native 输出；标准 JPEG/PNG/GIF/WebP/BMP 直接落盘，WXGF 由随包 `local_inbound/vendor/ffmpeg.exe` 异步转换为 PNG，其他未知格式降级为文字。FFmpeg 随 WeMai 独立分发，不得依赖外部 WeFlow 安装目录。Python 层再次校验图片头，残留坏图同样降级为文字；确定性的消息构建错误直接进入死信，不能使用网络错误退避阻塞同会话。

### 2.2 黑白名单前置过滤

`local_inbound/lib/inbound-filter.js` 在读取正文和媒体之前决定会话是否允许入站。候选列表只来自微信本地通讯录好友和已加入群聊；公众号、订阅号、服务号、微信支付、文件传输助手、微信团队等系统身份固定排除，不进入解密和 MaiBot 队列。全部会话、白名单和黑名单三选一。被过滤的消息仍确认数据库水位，避免阻塞后续消息。

### 2.3 身份和会话

- 私聊会话 ID 直接作为对方微信会话ID，如`wxid_xxxxxx`（前期微信用户可能修改过原生ID故格式不固定）。
- 群聊会话 ID 形如 `123456@chatroom`，群本身使用 `group` 命名空间。
- 能从消息行、群成员表或引用/合并记录明确取得成员真 ID 时，直接使用微信原生 ID，因此同一成员出现在私聊和多个群聊中会得到同一个 MaiBot 用户 ID，并与 MaiBot 的平台账号、权限和 @ 配置保持一致。
- 群成员真 ID 暂不可得时，使用 `群真 ID + 当前显示名` 的稳定临时命名空间，不会误合并到系统消息用户；后续不自动合并历史临时身份。
- 群聊的 `group_id` 直接使用微信原生 `...@chatroom`；每条入站消息在 `additional_config` 中携带原生 `self_id`、`platform_io_account_id` 和目标用户/群 ID，使 MaiBot 按平台账号隔离聊天流。升级前生成的 MD5 聊天流保留为历史记录，新消息进入原生 ID 聊天流。
- 明确的微信系统事件才标记为 `微信系统消息`，普通消息不会因缺少 sender ID 被误判为系统用户。
- 机器人自身识别与 NapCat 默认逻辑一致：当前账号原生 ID 作为 `self_id`，发送者 ID 与其相等的自身消息只作为平台回显确认，不再次作为普通入站交给 MaiBot。MaiBot 主动发送的内容由原始 `send_api` 记录保留在聊天流中，避免重复消息和回复回环。

头像采用本地缓存和原子替换，URL 变化会刷新旧文件；群头像和群成员头像分别缓存。sidecar 在启动和联系人资料刷新时独立上报当前机器人账号的原生 ID、昵称和头像，不需要放行自身消息。头像获取失败不阻塞文本入站。

### 2.4 MaiBot 消息段映射

WeMai 入站协议以手机端 MaiBot Adapter 为唯一字段基准：`platform=wx`、
`platform_io_scope=wx`、`platform_io_account_id=<当前微信 wxid>`。微信扩展字段
统一使用 `wx_*`，不再向 MaiBot 发送 `wechat_*` 扩展字段。`wx_message_type`
只允许以下 18 个值：`text`、`image`、`emoji`、`voice`、`video`、`file`、
`reply`、`chat_record`、`location`、`contact_card`、`link`、`mini_program`、
`transfer`、`red_packet`、`pat`、`recall`、`video_channel`、`system`。
MaiBot Legacy 不支持的富媒体段继续用可读 `text`/`file` 段承载，但真实类型和
完整结构必须保存在与手机端相同的 `wx_*` 字段中。

`wx_Processor.py::_build_message` 生成 `maim_message` 的 `MessageBase`、`BaseMessageInfo` 和 `Seg`：

| 微信内容 | MaiBot 段 | 处理方式 |
| --- | --- | --- |
| 文本 | `text` | 保留原文，不添加系统前缀 |
| 图片 | `image` | 验证格式后使用 Base64；本地文件无效时才退化为文本占位 |
| 表情包 | `emoji` | 动图/图片有效时使用 Base64，否则为 `[表情]` |
| 语音 | `voice` | 解密/转换为 WAV 或 MP3 Base64；无法解密时为 `[语音消息]` |
| 普通文件 | `file` | 使用字典 `{name, size, url}`；文件未下载或路径不受信任时仍保留名称和大小 |
| @ | `at` | 只提取当前消息的 `atuserlist`，同时传递微信原生 ID、昵称和群名片；不提升引用正文内部的 @ |
| 引用 | `reply` | 保留原消息 ID、发送者和原文；找不到原行时保留“原消息无法访问”提示 |
| 系统消息 | `text` + 系统身份 | 不再添加 `[系统消息]` 前缀 |
| 合并聊天记录 | `text` + 可恢复媒体段 | 展开文本、文件、图片、表情、链接和红包；当前 Legacy 转换器不接受原生 `ForwardNode` |
| 视频/视频号 | `text` | 保存可读标题和链接；当前 MaiBot Legacy 不直接展示视频 |
| 链接/音乐/小程序 | `text` + `additional_config` | 正文保留标题、说明和链接，结构化卡片字段存入 `wechat_app` |
| 位置 | `text` + `additional_config` | 正文保留地点和坐标，结构化字段存入 `wechat_location` |

当前 MaiBot 1.1.3 的 Legacy 转换器接受 `text/image/emoji/voice/file/at/reply/dict`，不接受 `ForwardNode`。因此合并记录必须保持可读文本并附带本地可用媒体，不能伪造 ForwardNode。

### 2.5 并发和可靠投递

Node 层按 `sessionId` 建立队列，同一会话严格串行，不同会话默认最多 8 路并发。每个事件在 Python 回调成功落盘并返回 ACK 后才推进数据库水位；NACK 或超时采用指数退避，避免消息丢失和乱序。

Python 层使用 `inbound` SQLite 可靠队列。队列记录包含稳定消息 ID、会话键、原始事件和尝试次数；每次只取每个会话的队首，最多由 `INBOUND_ROUTER_CONCURRENCY=8` 个任务并行调用 MaiBot。失败消息重试，超过策略后进入 `dead_letters`，可人工重放。消息文件不会在 MaiBot 可能异步读取前立即删除，由周期 GC 清理。

## 3. 出站原理

MaiBot 当前适配的是本地 Legacy WebSocket。`wx_Processor.py` 将 receiver 的 `user_id/group_id` 映射为微信私聊真 ID 或群聊真 ID，再交给 `wx_Listener.py`。

### 3.1 独立窗口

微信主窗口登录后，WeMai 按需在用户列表点击目标微信会话ID，列表不可见时使用搜索兜底。搜索结果不可信时会校验标题和聊天类型，必要时在结果区域滚动后再验证；成功后记录目标与 HWND。微信不会自动刷新已打开独立窗口标题，因此窗口重启后才重新绑定。适配器停止时关闭由适配器打开的独立窗口。

### 3.2 文本、图片和文件

文本使用剪贴板粘贴和 UI 提交；图片使用剪贴板或图片预览；文件通过聊天输入框的文件选择流程发送。每个会话的 UI 任务有序，普通文本、图片、文件拥有比音频更高的插队优先级。普通文件默认上限 200 MiB，可信本地 `file://` 交付根目录由 `LOCAL_FILE_DELIVERY_ROOT` 限制。

### 3.3 语音和音乐

`bin/WeMai.VoiceHost.exe` 是独立语音宿主。Python 把音频送入 VB-CABLE，聚焦正确微信独立窗口，按住右 Alt 录制/发送，播放完成后松开。微信单条语音按 60 秒限制，超过 60 秒由 `audio_segments.py` 按 WAV 采样边界或 MP3 帧边界无损切片后逐段发送。

以下是语音出站的设计不变量，后续维护不得把它们误判为锁或公平性缺陷：

1. 微信数据库入站和微信 UI 出站是两套独立处理链；入站消息不会与语音出站争抢 `OutboundUIGate`。
2. 全局语音独占只覆盖一个分段的实际录制期：解析并验证目标独立窗口后、右 Alt 按下前取得，右 Alt 松开且确认微信退出录音状态后释放。会话解析、音频检查和分段准备不占用语音门。
3. 文本、图片、文件的优先级高于语音分段。一个语音分段发送完成后必须释放门；已经排队的普通消息可以先快速完成，再开始下一语音分段。这是主动降低普通消息延迟的产品逻辑。
4. `acquire_voice()` 已在等待时，只要当前没有分段处于录制状态，普通发送仍允许从 `acquire_send()` 进入。不要因为存在 voice waiter 就阻塞普通发送，也不要把多段语音扩大成一次连续全局独占。
5. 实际录制期间任何普通 UI 发送都必须等待；不同独立窗口也不能并行激活、粘贴或按键，因为 Windows 只有一个前台焦点和一套全局右 Alt 状态。
6. 每段发送完成后才增加已发送计数。若后续分段失败，`PartialVoiceDeliveryError.retry_safe=False`，不得重放整个原音频，否则会重复已发送分段。
7. 目标 HWND 未取得前台焦点、微信未进入录音状态或录音停止无法确认时必须失败关闭；尤其是无法确认右 Alt 已释放和录音已停止时，应保持全局 UI 独占并要求重启，不能冒险继续发送到其他窗口。

相关回归测试必须同时覆盖：超过 60 秒切片、分段边界普通消息优先、录制期普通消息阻塞、不同会话窗口不会抢焦点、部分分段成功后不可整体重试，以及停止/超时时右 Alt 一定释放。

微信 UI 发送通常无法直接取得平台真实消息 ID，因此不要伪造 `message_id_echo`。若未来能从本地数据库以会话、时间和正文唯一反查，可增加可验证回声；无法唯一匹配时应保持空值。

## 4. 监督器和 WebUI

`wemai_supervisor.py` 运行隐藏的 WebUI 监督器，负责启动、停止、重启核心进程、检测 UI/入站/语音宿主心跳，并展示运行时间、日志和候选会话。`config_webui.py` 只使用 Python 标准库，配置保存在 `.env`；WebUI 密码不回显，远程监听必须设置密码。

首次数据库配置可使用 WebUI，也可运行 `local_inbound_setup.py` 交互式填写。凭据只在当前 Windows 用户和当前电脑可用，不要复制 DPAPI 文件到其他电脑。

### 4.1 停止与完全退出

WebUI 的“停止”只设置 `child_stop_requested`，由监督器主循环调用 `stop_core_from_webui()` 停止 `main.py --all`；WebUI 和监督器保持运行，用户可以再次启动。“完全退出”调用 `/api/shutdown`，用于终止整个 WeMai 进程树。

完整退出链路必须保持以下顺序：

1. HTTP 处理线程先返回 `202 Accepted`，再调用 `shutdown_callback`；回调只能设置监督器的 `stop_requested`，不能在请求线程直接执行 `webui.close()`。
2. 监督器主循环观察到退出事件后进入 `finally`，先执行 `stop_child()`，等待核心安全停止；核心负责结束入站、发送任务、语音宿主并关闭由适配器打开的微信独立窗口。
3. 核心退出后执行 `webui.close()`，停止 HTTP 服务并清理登录会话；`wemai_supervisor.py` 返回后，`bootstrap.py` 随之退出。
4. 页面在确认“完全退出”后断开是预期行为。再次运行必须重新双击 `Start-WeMai.vbs`；不要让 bootstrap 自动拉起由用户明确完全退出的实例。

维护时必须分别测试“停止后可再次启动”和“完全退出后端口释放、监督器/核心/Node/语音宿主均不残留”。如果新增托盘、服务或其他启动器，也必须把完整退出意图向上传递，不能只终止核心子进程。

## 5. 目录结构

- `main.py`：组件编排、生命周期和健康检查
- `wx_Processor.py`：MaiBot 协议、入站队列、消息段和出站分发
- `wx_Listener.py`：微信窗口搜索、读屏和 UI 发送
- `local_wechat_listener.py`：Python 与 Node 入站进程桥接
- `local_inbound/`：数据库读取、解密、消息映射、过滤和内置 Node 运行时
- `voice_host_backend.py`、`audio_segments.py`：语音宿主和 60 秒切片
- `config.py`、`config_webui.py`：环境配置和 WebUI
- `data/`：DPAPI 凭据、消息水位和会话状态
- `WeMai媒体/`：入站媒体与发送临时文件，按保留期清理
- `wheelhouse/`：Python 离线 wheel 依赖

## 6. 重要配置

`LOCAL_INBOUND_DISPATCH_WORKERS` 和 `INBOUND_ROUTER_CONCURRENCY` 默认均为 8；它们控制跨会话并发，不改变同会话顺序。`LOCAL_INBOUND_POLL_INTERVAL_MS` 默认 250，文件变更通常会更快触发扫描。`FILE_MAX_BYTES` 默认 209715200。`IMAGE_TEMP_RETENTION_DAYS` 默认 30，`IMAGE_PERSISTED_RETENTION_DAYS` 默认 180。

文本入站使用长期 SQLCipher 连接，并缓存每张消息表的增量查询 statement 与 `Name2Id` 发送者查询。`contact.db` 和 `session.db` 同样使用长期只读连接与缓存 statement，尤其不能在首次遇到每个群时重新打开并解密 `contact.db`。`fs.watch` 能识别 `message_N.db`、`message_N.db-wal` 和 `message_N.db-shm`，命中时仅扫描对应分库；250ms 周期只比较各分库/WAL 的大小和修改时间，发现变化后定向扫描，5 秒低频全量扫描负责最终兜底和结构刷新。不应在没有埋点证据时直接调低 250ms。

`收到本地微信消息` 日志中的 `source_lag` 由微信整数秒 `create_time` 计算，天然包含 0-999ms 时间戳量化误差，不能当作纯解密耗时。精确诊断应查看同一行的 `trigger_wait`、`structure`、`query`、`content_parse`、`identity`、`parse_total`、`scan`、`media`、`dispatch_queue`、`node_python` 和 `python_callback`；其中 `query` 包含 SQLCipher 查询/页面解密，`identity` 包含联系人和群成员身份补全，`scan` 是本轮扫描总耗时。

### 6.1 2026-08-01 入站性能优化记录

本次版本已部署并通过真实 `/pm` 私聊、群聊链路测试。改动边界如下：

| 文件 | 改动 | 维护约束 |
| --- | --- | --- |
| `local_inbound/lib/wechat-store.js` | 缓存消息表增量查询、`Name2Id` 发送者查询、`contact.db`/`session.db` 长期 SQLCipher 连接及群昵称查询 | 不要在每条消息或每个新群上重新打开并设密钥 |
| `local_inbound/local-inbound.js` | `fs.watch`、WAL/SHM 和轮询按 `message_N.db` 定向扫描；5 秒全量扫描兜底 | 不要删除全量兜底；新增/替换消息库必须标记结构脏 |
| `local_wechat_listener.py` | 记录扫描触发、等待、SQL 查询、正文解析、身份补全、媒体、Node→Python 等阶段 | `source_lag` 不能单独当作解密耗时 |
| `local_inbound/test/wechat-store-change-detection.test.js` | 验证 DB、WAL、SHM 变化能映射到所属消息库 | 修改文件监听逻辑后必须运行 |

实测结果（本机微信 4.1.9.30，17 个会话、12 张消息表）：消息表 SQLCipher 查询 `0.117-0.361ms`，定向扫描总耗时通常 `1.6-7.9ms`；群身份补全从旧版冷缓存约 `393ms` 降至 `0.069-1.779ms`。热窗微信 UI 文本出站约 `0.16-0.25s`；独立窗口冷启动仍可能约 `1.9-2.4s`，该段属于微信窗口查找/UIA 初始化，不是数据库解密。

排查时按以下顺序判断瓶颈：

1. `trigger_wait` 高：先检查文件事件、WAL 更新和轮询周期。
2. `query` 高：检查 SQLCipher 数据库、磁盘 I/O 或消息表索引；不要先调低轮询。
3. `content_parse` 高：检查 XML/JSON 或媒体正文解析。
4. `identity` 高：检查联系人/群成员缓存是否被频繁清空，禁止每条消息重新打开 `contact.db`。
5. `lookup` 高：检查微信独立窗口冷启动、搜索和 UIA 结构等待。

回滚只针对本次改动文件：停止 WeMai，恢复部署前备份的 `wechat-store.js`、`local-inbound.js`、`local_wechat_listener.py` 和本说明书，再通过 `Start-WeMai.vbs` 启动。不要回滚或删除 `data/` 下的 DPAPI 凭据、水位和状态文件；恢复后应确认 sidecar `ready` 日志、Router 连接和一条授权 `/pm` 测试消息。

修改配置后通过 WebUI 重启核心进程。不要在运行时手工编辑数据库、状态文件或正在发送的媒体。

## 7. 测试和维护要求

提交前至少运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\local_inbound\runtime\node.exe --test .\local_inbound\test\wechat-store-change-detection.test.js
```

需要覆盖文本、图片、表情、语音、文件、引用、@、系统消息、合并记录、过滤、启动水位、同会话串行和跨会话并发。真实微信测试只使用用户明确提供的私聊或群聊，不发送未授权消息。

更新微信版本时重点检查数据库表结构、密钥格式、图片/语音解密和 UI 控件；更新 MaiBot 时重点检查 Legacy WebSocket、`Seg` 数据结构、文件 URL 读取和 receiver ID。任何协议变化先加离线测试，再做人工链路测试。

## 8. 已知边界

1. 微信视频和视频号目前只能提供可读文本，MaiBot Legacy 不直接查看视频。
2. 合并记录通过展开文本和可恢复媒体适配 Legacy，不保证保留微信原生转发卡片样式。
3. `file://` 文件段是本机同机部署方案，不应暴露给不可信远端 MaiBot。
4. 微信 UI 出站没有稳定的平台消息 ID 回执。
5. 群聊成员真 ID 只有数据库或消息元数据明确提供时才能准确取得；没有真 ID 时使用隔离的临时身份，不自动改写历史 MaiBot 用户。

## 9. WeFlow 源码基线与日期限制

WeMai 的本地数据库读取、消息字段和媒体映射参考并改造自 WeFlow 5.1.0 源码；`local_inbound/lib/weflow510-content.js` 是随包保留的本地实现，不代表运行时仍需安装或启动外部 WeFlow。

当前离线包自带 Node.js、Python 依赖、图片 native addon、FFmpeg 和语音宿主，启动链不读取外部 WeFlow 安装目录或其运行授权状态。对上游 WeFlow 的版本/日期限制（例如 2026-09-01）不能直接推断 WeMai 是否到期；遇到日期相关问题时，先检查本包代码是否出现明确的日期判断，再用本机 `/pm` 私聊和群聊做冒烟测试，并记录结果。

不要为了绕过上游限制直接替换本包的数据库解密、图片 native addon 或 Node 运行时。更换 WeFlow 上游代码时，必须重新核对微信数据库表结构、媒体格式、WeFlow-LICENSE.txt 许可要求和 MaiBot Legacy 消息段兼容性。

## 10. 首次数据库密钥自举

- WebUI 的“自动获取密钥”按钮直接调用内置 Windows 被动扫描器，不再启动旧版 `wechat_key_helper.exe`，也不触发 UAC。
- 扫描器只使用进程查询和只读内存权限，禁止注入、写内存、暂停进程、附加调试器或建立常驻服务。
- 结构特征只生成候选，最终密钥必须通过 SQLCipher4 HMAC 和 Node只读查询双重验证。
- 验证成功后，完整密钥可经已认证的本机 WebUI API 返回并自动填入数据库密钥栏；保存时继续使用 Windows DPAPI 加密，密钥只对当前 Windows 用户和本机有效。
- 日志、命令行、临时文件和错误信息不得输出完整密钥；关闭 WebUI 密码时服务必须只监听 `127.0.0.1`。
- 自动扫描失败时保留手工配置入口。
