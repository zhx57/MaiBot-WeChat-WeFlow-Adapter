# WeMai - 微信分麦

<div align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/Platform-Windows-lightgrey.svg" alt="Platform Windows">
</div>

## ❗ 前排提醒

本项目已适配新版麦麦（MaiBot >= 1.0.0，需 `maim_message >= 0.6.2`）。

原仓库已被删除，本仓库是原仓库的进一步维护版本

原作者：[aki66938](https://github.com/aki66938)

## 📝 项目简介

**🚀 WeMai是一个微信消息双向转发系统**

- 💬 **微信监听**：实时监听微信消息并转发
- 🔄 **双向通信**：支持微信→MaiBot和MaiBot→微信双向消息传递（WebSocket，无需 Redis）
- 🔧 **灵活配置**：支持通过环境变量自定义配置
- 🛡️ **稳定可靠**：自动监控和重启机制
- 😘 **表情包**：支持发送表情包
- 🖨 **图片识别**：支持自动识别聊天中的图片

## 🎯 功能特性

| 模块 | 主要功能 | 特点 |
|------|---------|------|
| 🔄 **消息转发** | • 微信消息→MaiBot<br>• MaiBot回复→微信<br>• 消息格式转换<br>• 消息去重 | 双向通信 |
| 🛠 **统一管理** | • 统一入口<br>• 命令行参数<br>• 自动重启<br>• 优雅停止 | 稳定可靠 |
| 🔧 **灵活配置** | • 环境变量配置<br>• 聊天对象过滤<br>• 自定义监听范围 | 易于定制 |
| 🔍 **消息过滤** | • 自动过滤自己发送的消息<br>• 排除系统消息<br>• 自定义排除列表 | 精准转发 |
| 📊 **日志记录** | • 详细日志<br>• 群组ID哈希映射<br>• 错误追踪 | 便于排查 |

## 🚀 快速开始

### 环境要求

- Python 3.9+
- 微信桌面版 4.x（已验证 4.1.7.59、4.1.8.29）
- MaiBot（>=1.0.0）

### 安装步骤

1. 安装maibot，参考https://github.com/MaiM-with-u/MaiBot
   > [!INFO]
   > - 请自己研究maibot，找到适合自己的部署方式。
   > - 这里仅提供部署思路，maibot本质上是一个完善的闭环机器人，她拥有消息收发处理机制
   > - 消息的循环在于其内部构建的消息链，类似于人的思考和回复
   > - 因此，你可以将她当作一个独立的机器人，自己创建一个机器人框架和她进行交互（本项目基于此思路实现）。
   > 详细处理逻辑和maibot部署方式可参考：https://mp.weixin.qq.com/s/79ZdJqZ7VI0bFsg14Hf00w
   > - 注意记录服务器地址和ip，后续修改.env环境变量要用到

2. 运行本项目
   > 如果看完了第一步，你应该部署好了属于自己的maibot
   > 那么，可以开始部署和运行本项目了
   ```bash
   git clone https://github.com/zhx57/WeMai.git
   cd wemai
   pip install -r requirements.txt
   cp .env.example .env
   # 编辑.env文件，配置你的环境变量
   python main.py
   ```
   > - 在完成.env文件的修改后，初次运行启动命令，你将在控制台看到类似于：
   > - 2025-05-01 16:18:18 - INFO -   test -> 098f6bcd4621d373cade4e832627b4f6
   > - 这样的log信息，这里记录了你在本项目设置的聊天白名单，其中包含的就是你的群组id
   > - 将这个id复制粘贴到你的maibot的bot_config中，重新运行maibot就可以完成群聊交互

### 本项目命令行参数

```
usage: main.py [-h] [--all] [--wx-to-maibot] [--maibot-to-wx] [--target-chats TARGET_CHATS]

WeMai - 微信消息转发服务

optional arguments:
  -h, --help            显示帮助信息
  --all                 启动所有服务（默认）
  --wx-to-maibot        仅启动微信到MaiBot的消息转发
  --maibot-to-wx        仅启动MaiBot到微信的消息转发
  --target-chats TARGET_CHATS
                        要监听的微信聊天对象，多个用逗号分隔
```

## 📐 系统架构

```mermaid
graph TD
    A[main.py] --> B[wx_Listener.py]
    B -->|监听微信消息| F[微信]
    B --> E[wx_Processor.py]
    E -->|WebSocket 双向通信| G[MaiBot]
    E -->|Router 接收回复| F
```

新版麦麦用 `maim_message` 的纯 WebSocket 做双向通信，`wx_Processor` 里的 Router 同时处理入站（微信→麦麦）和出站（麦麦→微信），**无需 Redis 队列中转**。

收到微信 4.0 图片消息时，监听器会在对应独立聊天窗口中自动打开预览，
把原图保存到 `IMAGE_SAVE_DIR`（默认项目目录下的 `wxauto文件`），再编码成
MaiBot 的 `image` 消息段。可通过 `IMAGE_AUTO_DOWNLOAD=false` 关闭自动保存，
或通过 `IMAGE_RECOGNITION_ENABLED=false` 关闭图片段转换。

UIAutomation/COM 调用若停止返回，主线程会按心跳时限打印卡住线程的堆栈，
写入 `wemai_exit.log`，并默认重新执行当前 WeMai 进程。设置
`UI_WORKER_AUTO_RESTART=false` 可只记录诊断并退出，不自动重启。

## ⚙️ 配置说明

WeMai使用`.env`文件进行配置，主要配置项包括：

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| WX_TARGET_CHATS | 要监听的聊天对象列表 | 空（由命令行参数决定） |
| WX_LISTEN_ALL_IF_EMPTY | 兼容配置；微信 4.x 仍要求显式列出目标，空目标且为 true 时启动失败 | false |
| WX_EXCLUDED_CHATS | 排除的聊天对象 | 文件传输助手,微信团队,微信支付 |
| WX4PY_AUTO_CONNECT | 创建 wx4py 客户端时自动连接 | true |
| WX4PY_TICK | 监听轮询间隔（秒） | 0.05 |
| WX4PY_BATCH_SIZE | 兼容参数；到期聊天始终全部扫描 | 8 |
| WX4PY_TAIL_SIZE | 图片重试时检查的可见尾部行数；不截断新增消息 | 8 |
| WX4PY_UI_LOOKUP_TIMEOUT | 单次 UIA 控件探测最长等待（秒） | 0.08 |
| WX4PY_UI_POLL_INTERVAL | 微信界面状态轮询间隔（秒） | 0.05 |
| WX4PY_SEARCH_TIMEOUT | 搜索结果最长等待（秒） | 4.0 |
| WX4PY_SUBWINDOW_TIMEOUT | 双击后等待独立窗口就绪（秒） | 8.0 |
| UI_WORKER_STARTUP_TIMEOUT_SECONDS | 全部初始目标的累计启动时限；0 表示关闭 | 0 |
| UI_WORKER_IDLE_TIMEOUT_SECONDS | 空闲轮询无心跳的卡死判定时限（秒） | 30 |
| UI_WORKER_BUSY_TIMEOUT_SECONDS | 发送、恢复或图片保存无心跳的卡死判定时限（秒） | 60 |
| UI_WORKER_AUTO_RESTART | 确认 UIA/COM 卡死后自动重启 WeMai 进程 | true |
| MAIBOT_API_URL | MaiBot WebSocket地址 | ws://127.0.0.1:8000/ws |
| PLATFORM_ID | 平台标识 | wx4py |
| WX_BOT_NICKNAME | 机器人自己的微信昵称（可选，用于排除自身） | 空 |

微信 4.0.5 及以上版本使用 Qt Quick，UIAutomation 不能稳定读取聊天头部的
群成员控件。建议在 `WX_TARGET_CHATS` 中显式填写聊天类型，尤其是已经出现在
左侧会话列表、启动时不一定需要搜索的私聊：

```dotenv
WX_TARGET_CHATS=[{"name":"项目群","type":"group"},{"name":"张总","type":"private"}]
```

显式配置始终优先。未填写 `type` 时，监听器会依次使用微信搜索结果的“群聊”或
“联系人”分类、窗口与消息结构进行推断；仍无法判断时按 `group` 处理。启动日志会
输出每个目标最终使用的 `chat_type` 和判断来源 `type_source`，可据此修正配置。

## 🔌 适配新版麦麦（重要）

新版麦麦（MaiBot >= 1.0.0）的消息服务改用 `maim_message` 库的**纯 WebSocket**，旧版 WeMai 用的 `maim_message 0.3.x` 是 socket.io 协议，连不上会报 `404 Not Found`。适配需要做三件事：

### 1. 安装固定版本的 maim_message

WeMai 使用 `maim_message==0.6.8` 的 Legacy API；重新安装依赖即可：

```bash
pip install -r requirements.txt --upgrade
```

### 2. 修改 WeMai 的 .env

把 `MAIBOT_API_URL` 指向麦麦的 WebSocket 服务地址。端口对应麦麦 `config/bot_config.toml` 里 `[maim_message].ws_server_port`（默认 8000）：

```bash
# 本机部署
MAIBOT_API_URL=ws://127.0.0.1:8000/ws

# 远程部署（把 IP 换成麦麦服务器地址）
MAIBOT_API_URL=ws://192.168.x.x:8000/ws
```

> 注意：端口是 `ws_server_port`（默认 8000），**不是 WebUI 端口**（默认 8001）。如果之前填的是 8060 或别的端口，改回 8000。

### 3. 配置麦麦主配置的 platforms

在麦麦的 `config/bot_config.toml` 的 `[bot]` 段，把微信平台加进 `platforms`，否则麦麦会因为未配置 wx4py 机器人账号而拒绝回复：

```toml
[bot]
platform = "wx4py"
platforms = ["wx4py:你的微信昵称或wxid"]
nickname = "你的机器人微信昵称"
```

> `platforms` 的格式是 `平台名:账号标识`。WeMai 默认用 `wx4py` 作为平台名（对应 `.env` 里的 `PLATFORM_ID`），冒号后面可以填机器人微信昵称。

### 验证连接

启动 WeMai 后，日志里不再出现 `连接失败: {'detail': 'Not Found'}` 和 `404`，而是出现类似 `连接成功` 或正常收到消息，就说明连上了。

## 📋 使用说明

1. **配置MaiBot白名单**
   
   启动程序后，控制台会显示监听的聊天对象及其哈希ID，将需要的群组ID添加到MaiBot的白名单中。

2. **消息流向**

   - **微信→MaiBot**：监听微信消息，转换格式后发送到MaiBot API
   - **MaiBot→微信**：通过 WebSocket 接收 MaiBot 响应并转发到微信

3. **自动过滤**

   - 自动过滤自己发送的消息
   - 自动过滤系统消息
   - 可配置排除特定聊天对象

## 📌 注意事项

> [!WARNING]
> - 本项目仅供学习交流使用，请勿用于非法用途
> - 尽管UI自动化相对来说风险较小，但小不等于没有封号风险，请谨慎使用
> - 项目处于开发阶段，可能存在未知问题
> - 使用前请确保已了解并同意微信相关协议
> - 本项目使用Cursor编写，很可能代码上不够优雅


## 📝 许可证

本项目采用 MIT 许可证 - 详情请参阅 [LICENSE](LICENSE) 文件

## 🙏 致谢

- [MaiBot](https://github.com/MaiM-with-u/MaiBot) - 提供API接口支持
- [wx4py](https://github.com/claw-codes/wx4py) - 提供微信 4.x 自动化支持
- [wepush](https://github.com/friend-nicen/wepush) - 提供框架设计基础及灵感
- 所有贡献者和使用者
