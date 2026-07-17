# MaiBot-WeChat-WeFlow-Adapter

<div align="center">
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/WeFlow-Required-orange.svg" alt="WeFlow Required">
  <img src="https://img.shields.io/badge/MaiBot->=1.0.0-green.svg" alt="MaiBot >= 1.0.0">
  <img src="https://img.shields.io/badge/Platform-Windows-lightgrey.svg" alt="Platform Windows">
</div>

## 项目简介

基于 [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers) 的 MaiBot 微信适配器。通过 WeFlow 的 SSE 推送实时接收微信消息，通过 HTTP API 获取媒体文件，通过独立聊天窗口发送消息，实现 MaiBot 与微信的双向通信。

- **实时接收**：WeFlow SSE 推送新消息，零延迟
- **媒体支持**：图片、语音、表情包自动下载并转发给 MaiBot
- **独立窗口**：每个聊天打开独立窗口发送，互不干扰
- **自动恢复**：窗口消失自动重开，发送前检查窗口状态
- **稳定运行**：单个聊天打不开不影响其他聊天，失败自动重试

## 环境要求

- Python 3.9+
- Windows + 微信桌面版 4.x
- [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers)（需开启 HTTP API 服务和主动推送）
- [MaiBot](https://github.com/MaiM-with-u/MaiBot) >= 1.0.0（需 `maim_message >= 0.6.2`）

## 快速开始

### 1. 安装 MaiBot

参考 [MaiBot 文档](https://github.com/MaiM-with-u/MaiBot) 部署，记录 WebSocket 地址和端口。

### 2. 安装 WeFlow

安装 WeFlow 并登录微信，在设置页开启：
- `API 服务`（默认端口 5031）
- `主动推送`
- 配置 API Token

### 3. 运行本项目

```bash
git clone https://github.com/zhx57/MaiBot-WeChat-WeFlow-Adapter.git
cd MaiBot-WeChat-WeFlow-Adapter
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env 配置 WeFlow 地址、Token、MaiBot 地址等
python main.py
```

### 4. 配置 MaiBot

在 MaiBot 的 `config/bot_config.toml` 中：

```toml
[bot]
platform = "wx4py"
platforms = ["wx4py:你的微信昵称或wxid"]
nickname = "你的机器人微信昵称"
```

启动后控制台会显示监听的聊天对象及其哈希 ID，将需要的 ID 添加到 MaiBot 白名单。

## 配置说明

在 `.env` 文件中配置，主要配置项：

### WeFlow 连接

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| WEFLOW_API_URL | WeFlow HTTP API 地址 | http://127.0.0.1:5031 |
| WEFLOW_ACCESS_TOKEN | WeFlow API Token | 空（必填） |

### MaiBot 连接

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| MAIBOT_API_URL | MaiBot WebSocket 地址 | ws://127.0.0.1:8000/ws |
| PLATFORM_ID | 平台标识 | wx4py |

### 微信监听

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| WX_TARGET_CHATS | 监听的聊天对象 | 空（必填） |
| WX_EXCLUDED_CHATS | 排除的聊天对象 | 文件传输助手,微信团队,微信支付 |
| WX_BOT_NICKNAME | 机器人自己的微信昵称 | 空 |

`WX_TARGET_CHATS` 支持两种格式：

```dotenv
# 简单格式
WX_TARGET_CHATS=项目群,张总

# 带类型（推荐，避免私聊被误识别为群聊）
WX_TARGET_CHATS=[{"name":"项目群","type":"group"},{"name":"张总","type":"private"}]
```

### 窗口与搜索

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| WX4PY_TICK | 监听轮询间隔（秒） | 0.05 |
| WX4PY_WINDOW_CHECK_INTERVAL | 窗口存活检查间隔（秒） | 5.0 |
| WX4PY_SEARCH_TIMEOUT | 搜索结果等待（秒） | 4.0 |
| WX4PY_SUBWINDOW_TIMEOUT | 独立窗口就绪等待（秒） | 8.0 |

### 媒体处理

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| IMAGE_AUTO_DOWNLOAD | 自动下载图片 | true |
| IMAGE_RECOGNITION_ENABLED | 图片转 MaiBot image 段 | true |
| MAX_MEDIA_BYTES | 单个媒体文件大小上限（字节） | 10485760 |

### UI 工作线程

| 配置项 | 说明 | 默认值 |
|-------|------|-------|
| UI_WORKER_AUTO_RESTART | UIA 卡死时自动重启 | true |
| UI_WORKER_IDLE_TIMEOUT_SECONDS | 空闲卡死判定（秒） | 30 |
| UI_WORKER_BUSY_TIMEOUT_SECONDS | 发送卡死判定（秒） | 60 |

## 系统架构

```
微信 4.x
  │
  ├── WeFlow（SSE 推送新消息 + HTTP API 获取媒体）
  │     │
  │     ▼
  │   weflow_listener.py（SSE 监听 + 媒体下载）
  │     │
  │     ▼
  │   wx_Processor.py（消息格式转换 + WebSocket 双向通信）
  │     │
  │     ▼
  │   MaiBot（maim_message WebSocket）
  │
  └── wx_Listener.py（独立窗口发送消息）
        │
        ├── 会话列表直接查找（优先）
        ├── 搜索框回退（Ctrl+F → 回车跳转）
        └── 双击开独立窗口 → 输入发送
```

### 消息接收流程

1. WeFlow SSE 推送新消息事件（`content` 字段为占位符如 `[图片]`、`[语音]`、`[表情]`）
2. `weflow_listener` 根据消息类型调用 WeFlow HTTP API（`media=1`）获取媒体文件
3. 通过 SSE 的 `rawid` 与 API 返回的 `serverId` 匹配定位消息
4. 下载媒体文件（图片/语音/表情），编码后转发给 MaiBot

### 消息发送流程

1. MaiBot 通过 WebSocket 发送回复消息
2. `wx_Listener` 在对应独立窗口中输入并发送
3. 发送前检查窗口是否存活，消失则自动恢复
4. 窗口不存在时先在会话列表查找，找不到才搜索

## 命令行参数

```
usage: main.py [-h] [--all] [--wx-to-maibot] [--maibot-to-wx] [--target-chats TARGET_CHATS]

optional arguments:
  -h, --help            显示帮助信息
  --all                 启动所有服务（默认）
  --wx-to-maibot        仅启动微信到MaiBot的消息转发
  --maibot-to-wx        仅启动MaiBot到微信的消息转发
  --target-chats TARGET_CHATS
                        要监听的微信聊天对象，多个用逗号分隔
```

## 微信 4.0 说明

微信 4.0.5+ 使用 Qt Quick(QML) 渲染，UIAutomation 默认无法遍历控件。本项目做了以下适配：

- **搜索框不可见时**：自动使用 `Ctrl+F` 快捷键打开搜索，输入后直接回车跳转
- **会话列表优先**：启动时先在会话列表直接查找，避免不必要的搜索
- **窗口自动恢复**：窗口消失后 5 秒内检测到，发送消息前也会检查

建议在 `WX_TARGET_CHATS` 中显式填写聊天类型，避免类型推断失败。

## 注意事项

> [!WARNING]
> - 本项目仅供学习交流使用，请勿用于非法用途
> - UI 自动化存在一定封号风险，请谨慎使用
> - 项目处于开发阶段，可能存在未知问题
> - 使用前请确保已了解并同意微信相关协议

## 许可证

MIT License - 详情请参阅 [LICENSE](LICENSE) 文件

## 致谢

- [MaiBot](https://github.com/MaiM-with-u/MaiBot) - 提供API接口支持
- [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers) - 提供微信消息推送和媒体导出
- [wx4py](https://github.com/claw-codes/wx4py) - 提供微信 4.x 自动化基础
