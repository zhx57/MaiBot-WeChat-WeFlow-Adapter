# WeMai 1.0.2

WeMai 是 MaiBot 的 Windows 微信适配器：入站直接只读微信本地数据库，出站通过微信独立窗口 UI 发送。Node 运行时和数据库模块已随项目提供。

本项目仅供学习和个人研究，禁止商业使用。使用微信自动化、数据库解密和 UI 操作存在账号与数据风险，请自行评估并遵守相关服务条款。

## 推荐环境

- Windows 10/11 x64
- 微信：`4.1.9.30`（本机验证版本；不要在运行中随意升级）
- Python：`3.12.x x64`，推荐 `3.12.10`
- MaiBot：使用与当前适配器匹配的 Legacy WebSocket 适配器接口
- 虚拟声卡：VB-Audio Virtual Cable，推荐当前稳定版；本机控制面板版本为 `3.3.1.7`

Node.js 不需要另行安装，项目内置 x64 运行时。语音宿主和用于 WXGF/HEVC 图片转换的独立 FFmpeg x64 运行时也已内置，不依赖 WeFlow 安装状态。语音和音乐插件请按照 MaiBot 官方文档自行安装和配置。

## 首次部署

1. 将整个项目目录放到固定路径，例如 `C:\Program Files\WeMai`。目录需要可写入日志、媒体和 `data` 文件。
2. 双击 `Start-WeMai.vbs`。首次运行会使用项目内的 `wheelhouse` 离线安装 Python 依赖，不需要联网下载。
3. 打开 WebUI：`http://127.0.0.1:8888`。首次没有数据库凭据时，先登录电脑版微信，选择账号并点击“自动获取密钥”。内置扫描器会以普通用户权限只读微信进程内存，用本机数据库验证后显示、回填并通过 DPAPI 保存完整密钥；也可继续手工填写微信账号目录、微信原生 ID 和64位十六进制密钥。图片 XOR/AES 密钥仍按本机数据库情况手工填写。
4. 在 WebUI 保存 MaiBot WebSocket 地址、媒体目录、名单和其他配置。数据库密钥由 Windows DPAPI 加密保存（1机1密钥）。

## 启动顺序（推荐）

先登录微信并保持主窗口运行，再启动 MaiBot，最后启动 WeMai。WeMai 启动时会从每张消息表当前最大 `local_id` 建立水位，不会转发启动前积压的历史消息。

## 日常运行

- 推荐双击 `Start-WeMai.vbs`，它会隐藏控制台并保持 WebUI 监督器运行。
- 通过 WebUI 的“启动”“停止”“重启”按钮管理核心进程。“停止”只停止消息核心，WebUI 和监督器继续运行，可随时再次启动。
- 需要关闭全部后台进程时点击“完全退出”并确认。WeMai 会先安全结束发送任务、停止入站与语音宿主、关闭由适配器打开的微信独立窗口，再关闭 WebUI、监督器和 bootstrap；页面断开属于正常现象。再次使用需重新双击 `Start-WeMai.vbs`。
- 首次打开某个会话时，WeMai 按微信会话ID 搜索并绑定独立窗口；之后复用 HWND。微信独立窗口由微信自行维护，适配器停止时会关闭由它打开的窗口。

## 故障排查

- WebUI 显示凭据无效：确认微信账号目录包含 `db_storage`，密钥为 64 位十六进制，并使用当前登录账号的原生 ID。
- 自动获取按钮不可用：请先启动并登录电脑版微信；若微信进程退出、当前用户无只读权限或无法通过数据库验真，可重新点击或使用手工配置。
- 入站无消息：确认 MaiBot 已监听 `MAIBOT_API_URL`，并检查 WebUI 日志中的数据库连接和名单过滤状态。
- 图片无法显示：补充图片 XOR/AES 密钥，确认 `WeMai媒体` 目录可写，并检查 `local_inbound/vendor/ffmpeg.exe` 是否存在。无法解码的图片会降级为文字占位，不会阻塞同会话后续消息。
- 语音无法发送：确认 VB-CABLE 播放/录音设备存在，且 `WeMai.VoiceHost.exe` 可启动；文本、图片、文件发送不依赖虚拟声卡。
- 关闭 WebUI 密码时只监听 `127.0.0.1`；需要局域网访问时必须设置密码并使用防火墙限制来源。

## 参考项目

-消息字段和本地数据库映射参考 WeFlow 5.1.0 源码
-MaiBot 消息结构参考 [MaiBot 官方文档](https://docs.mai-mai.org/)
- [zhx57/MaiBot-WeChat-WeFlow-Adapter](https://github.com/zhx57/MaiBot-WeChat-WeFlow-Adapter)：WeMai 二次开发所基于的原始适配器
- [MaiBot](https://github.com/MaiM-with-u/MaiBot) 与 [MaiBot 文档](https://docs.mai-mai.org/)：消息协议和聊天流
- [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers)：基于此源码开发
- [wx4py](https://github.com/claw-codes/wx4py)：内部微信 UI 自动化参考
- [scottfly189/WeChatAuto.SDK](https://github.com/scottfly189/WeChatAuto.SDK)：微信ui元素依赖库
- [VB-CABLE](https://vb-audio.com/Cable/)：虚拟音频路由
项目内置第三方组件及其许可证以各自目录中的声明为准。
