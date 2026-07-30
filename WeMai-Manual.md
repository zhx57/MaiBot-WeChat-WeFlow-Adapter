# WeMai 使用说明书

## 1. 适用环境

推荐环境为 Windows 10/11 64 位、微信 4.1.9.30、Python 3.9 或更高版本。运行电脑应保持微信已登录且微信进程运行、不锁屏、不休眠。WeMai 启动时会自动恢复微信主窗口，不要求用户提前把主窗口放到前台。远程桌面断开可能改变桌面会话或 UI 缩放，长期运行更适合使用保持真实桌面的远程控制方式。

WeMai 不包含微信客户端、WeFlow、MaiBot、语音生成插件或 VB-CABLE 安装包，需要分别部署。

## 2. 首次安装

1. 解压整个 `WeMai` 文件夹，不要单独复制 `main.py`。
2. 双击 `Start-WeMai.vbs`。该无控制台启动器会直接创建唯一的 WeMai 控制台，不会先闪出临时 CMD。
3. 启动器在项目内创建 `.venv`，打印缺少或版本不符的依赖，并自动安装 `requirements.txt`。
4. 首次启动会由 `.env.example` 创建 `.env`，命令行显示 WebUI 访问地址。
5. 在浏览器打开该地址，填写配置并保存，点击“重启 WeMai”。

若没有 Python，窗口会明确提示安装 64 位 Python 3.9+ 并加入 `PATH`；错误窗口不会立即消失。首次自动安装需要联网访问 Python 软件源。

以后修改配置直接访问命令行显示的 WebUI 地址；默认是 `http://127.0.0.1:8888`，不再提供单独的配置启动文件。

## 3. 基础配置

| 配置 | 说明 | 默认值 |
| --- | --- | --- |
| WeFlow API 地址 | WeFlow HTTP API 与 SSE 服务地址 | `http://127.0.0.1:5031` |
| WeFlow Token | WeFlow 鉴权 Token，必填 | 空 |
| MaiBot WebSocket | MaiBot 消息接口 | `ws://127.0.0.1:8000/ws` |
| MaiBot 数据目录 | 用于需要读取 MaiBot 本地媒体的部署 | 空 |
| MaiBot 平台标识 | WeMai 与 MaiBot 共同使用的身份命名空间 | `wx`（固定） |
| 机器人微信昵称 | 当前机器人账号昵称 | 空 |
| 媒体保存目录 | 入站媒体落盘目录 | `WeMai媒体` |
| 日志级别 | `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` | `INFO` |
| WebUI 地址 | 仅本机访问用 `127.0.0.1` | `127.0.0.1` |
| WebUI 端口 | 配置中心端口 | `8888` |
| WebUI 密码 | 对外监听时必填 | 空 |

WeFlow 自己管理黑白名单。WeMai 接收 WeFlow 推送的全部消息，不需要另配群聊或私聊名单。

微信合并聊天记录的 SSE 正文只有 `[聊天记录]`。WeMai 会使用同一条消息的历史 `rawContent` 展开 `appmsg type=19`，按原顺序显示发送者、时间、文本、链接标题/描述/完整 URL、图片/文件说明、小程序、视频号及嵌套合并记录。群聊历史在 XML 前附加的任意格式发送者真 ID 会被安全剥离；合并记录还会绕过可能过早写入的群聊历史缓存，短暂重查完整 XML，不影响普通消息的快速缓存路径。历史发送者仅作为记录正文展示，不会被当成当前会话用户。微信原生语音在合并记录中只保留类似 `[语音] 55"` 的时长文本，缺少音频实体时会明确显示“原始音频不可提取”。解析使用不带媒体导出的快速历史查询，失败时保留 `[聊天记录]`，不会阻塞其他消息。

### 3.1 MaiBot 端平台配置

WeMai 发给 MaiBot 的 `platform` 固定为 `wx`。MaiBot 的 `config/bot_config.toml` 也必须使用相同值：

```toml
[bot]
platform = "wx"
platforms = ["wx:你的机器人微信昵称或wxid"]
nickname = "你的机器人微信昵称"
```

`wx:` 后面的机器人账号标识应与 MaiBot 实际配置一致。`wx4py` 是 WeMai 使用的第三方 Python 依赖名称，不是机器人平台名，不能填写到 MaiBot 的 `platform` 或 `platforms` 中。从旧版升级时应先同步修改 MaiBot；平台名参与用户、群和聊天流身份计算，旧 `wx4py` 与新 `wx` 会被视为不同平台。

## 4. 身份与独立窗口

WeMai 把 WeFlow `session_id` 作为唯一会话身份：

- 群聊：`45808329857@chatroom`
- 私聊：`wxid_ev4txoa8f2mm22force`

名称仅用于首次找到窗口和向 MaiBot 展示，不参与唯一身份判断。用户修改昵称、群昵称或群名后，真实 ID 不变，MaiBot 聊天流 ID 也不变。

微信 4.1.9.30 已打开的独立窗口不会随着私聊昵称或群名修改而刷新标题。WeMai 会让真实 ID 继续绑定原窗口句柄；只有该独立窗口关闭，或者 WeMai 停止/重启后，旧绑定才失效。名称更新由后续 WeFlow 入站消息带回，首次重新打开时会优先使用最新名称。私聊直接以 `session_id` 作为用户真 ID，不需要额外查询历史，因此改名不会改变用户身份，也不会增加私聊入站延迟。

启动 WeMai 时只自动恢复微信主窗口，不会预开任何聊天独立窗口。某个会话第一次出站时先查会话列表，找不到再使用兼容搜索。每次粘贴搜索词后等待 3 秒，最多搜索两次；仍未找到时只冷却当前目标。成功打开后，文本、图片、文件和语音都复用独立窗口，避免每次搜索。

文本、图片和文件按会话排队，不同会话可以并行进行消息准备和窗口解析。由于 Windows 只有一个前台焦点，最终的窗口激活、输入框聚焦、粘贴、回车和发送确认会作为一个很短的全局焦点事务执行，避免并发时把内容发到另一个窗口。语音和音乐也先复用同一套真实 ID/HWND 窗口解析流程；解析成功后、按下右 Alt 前才进入完整全局独占，直到右 Alt 释放并确认录音结束。长音频的每个片段是独立队列项，片段之间会优先处理已经到达的文本、图片和文件。

已绑定独立窗口如果被微信或用户关闭，WeMai 会在默认约 5 秒一次的窗口状态检查中自动恢复。恢复前先按真实 `session_id` 取得 WeFlow 最近一次推送的显示名称，再打开新窗口，把同一真实 ID 从旧 HWND 换绑到新 HWND。这个自动恢复只针对运行期间已经按需建立的会话，不会在启动时批量打开聊天窗口。

## 5. 语音发送

### 5.1 准备 VB-CABLE

1. 从 [VB-CABLE 官网](https://vb-audio.com/Cable/) 安装并按安装程序要求重启。
2. 在微信的音视频设置中，将麦克风选择为 `CABLE Output (VB-Audio Virtual Cable)`。
3. VoiceHost 会把生成的 MP3/WAV 播放到 `CABLE Input`。
4. 启动日志出现“语音设备检查通过”表示设备已找到；检查失败不会影响文本、图片或文件。

音频生成插件可以继续输出 MP3，无需为 WeMai 强制改成 WAV。MP3 通常更小，网络传输和临时文件处理更轻；实际生成速度主要取决于 TTS 服务，不由容器格式单独决定。

### 5.2 时长和锁

微信电脑端一次录音约一分钟。WeMai 接受 MP3/WAV，单个输入文件默认不超过 15 MiB；超过 60 秒时在碰微信 UI 前按音频帧边界拆分，每段不超过 60 秒，再逐段发送。WAV 按采样帧无重编码切分，MP3 按 MPEG 音频帧无重编码切分。

流程如下：先校验文件和时长，再使用和文本、图片、文件相同的真实 ID/HWND 窗口解析流程。窗口解析成功后取得全局 UI 独占，确认独立窗口取得前台焦点并存在“发语音”按钮，通过 Windows `SendInput` 按下右 Alt。只有微信界面确认切换到录音状态后才开始播放音频；播放完成时释放右 Alt，并确认界面恢复“发语音”状态，最后释放独占。录音态的撤销、波形和发送按钮不参与定位。正常流程不添加两秒或三秒固定等待，也不补静音。小于 1 秒的音频会在碰微信 UI 前拒绝。若录音尚未开始，失败可以安全重试；若录音已经开始而结果无法确认，则禁止自动重发，避免重复语音。

成功日志会同时出现 `right_alt_down=True`、`recording_started=True`、`right_alt_up=True` 和 `recording_stopped=True`。缺少任一确认时 WeMai 不会再把该次操作记录为成功。

全局独占仅用于防止当前片段录音被其他出站消息改变焦点。片段发送结束会释放独占；已经等待的文本、图片和文件优先完成后，下一语音片段才重新取得独占。若宿主响应丢失，65 秒看门狗只负责故障恢复；它不是正常语音时长，也不会故意持锁 65 秒。前面片段已成功、后续片段失败时不会自动重放整条音频，以免重复发送。

停止 WeMai 时会请求 VoiceHost 取消当前任务，并等待录音停止动作完成，然后清理 WeMai 创建的临时语音文件。若日志提示无法确认取消，为避免把语音误发到其他窗口，锁会保留，此时应从 WebUI 重启 WeMai。

WeFlow 可能把机器人刚发出的语音再次作为入站消息推送。WeMai 使用出站登记和回显抑制逻辑避免重复交给 MaiBot；日志可用于确认是否命中抑制。

## 6. 高级功能与 OCR

联系人管理和群管理均默认关闭。完整版只有至少开启一项时才启动打包的 WeChatAuto.SDK 社区版高级宿主并加载 OCR；核心版不携带该宿主，WebUI 会显示未安装并禁用开关。核心消息链路始终是 WeFlow 入站、WeMai 独立窗口/VoiceHost 出站，不依赖 OCR。朋友圈在上游社区版 Server 中没有实现，属于 VIP 能力，因此 WeMai 不提供朋友圈开关。

高级宿主仅监听本机内部端口 `5088`；联系人或群管理任一开关打开时会启动同一个社区版 Server，两项全部关闭时该进程不存在。这两个开关用于控制宿主是否需要运行，不是 Server 内部的接口权限系统，因此端口不对局域网开放。OCR 会增加启动时间、内存和一定 CPU 使用，不需要管理功能时应保持关闭。

### 6.1 群管理使用方法

1. 在 WeMai WebUI 打开“群管理”，保存配置并重启 WeMai。
2. 确认 `wemai.log` 出现“高级扩展已启动”，服务地址为 `ws://127.0.0.1:5088/ws`。
3. 群管理使用 WeChatAuto.SDK 官方 Python 客户端调用。上游当前的 `wechat-auto4x-sdk` 要求 Python 3.13，可单独安装，不影响 WeMai 主程序使用的 Python 3.9+：

```powershell
py -3.13 -m pip install wechat-auto4x-sdk
```

以下示例连接 WeMai 启动的本机服务并读取群成员：

```python
import asyncio

from wechat_auto_sdk import WeChatConfig, WechatFactory


async def main():
    uri = "ws://127.0.0.1:5088/ws"
    async with WechatFactory.create_factory(uri, WeChatConfig()) as factory:
        await factory.initialize()
        if not factory.client_list:
            raise RuntimeError("没有检测到已登录且已打开主窗口的微信")
        client = next(iter(factory.client_list.values()))
        members = await client.get_chat_group_member_list("群聊名称")
        print(members)


asyncio.run(main())
```

社区版可调用的群管理方法包括：`is_owner_chat_group`、`get_group_owner`、`add_owner_chat_group_member`、`create_owner_chat_group`、`change_owner_chat_group_name`、`change_chat_group_nick_name`、`change_chat_group_memo`、`update_group_notice`、`get_chat_group_member_list`、`remove_owner_chat_group_member`、`quit_chat_group`、`invite_chat_group_member` 和 `add_chat_group_member_to_friends`。参数定义以 [WeChatAuto.SDK](https://github.com/scottfly189/WeChatAuto.SDK) 的 `wechat_client.py` 为准。

群管理接口按当前显示名称操作，不使用 WeFlow 的真实 `session_id`。修改群名后，后续群管理调用应传新名称；WeMai 核心收发仍按真实 ID 复用原独立窗口，两条链路互不改变身份规则。添加、移除成员、退群、改公告等操作会真实修改微信数据，建议先在测试群验证。

## 7. 无人值守运行

- 使用 `Start-WeMai.vbs` 运行监督器，不要直接双击 `main.py`。
- 监督器保持 WebUI 存活；核心进程异常退出后按退避时间自动重启。
- WebUI 的“重启 WeMai”会在原监督器和命令行窗口内停止核心、重新读取 `.env` 并完整启动，不再替换或关闭控制台进程。
- WebUI 的“停止 WeMai”只安全停止核心，关闭独立窗口并最小化微信主窗口，监督器和 WebUI 保持可访问；“启动 WeMai”使用已保存配置重新拉起核心。
- 控制台按 `Ctrl+C` 后会显示中文确认；输入 `Y` 才会安全退出，输入 `N` 或直接回车继续运行。
- 核心启动时自动恢复微信主窗口；聊天独立窗口仍按需打开。停止时会关闭当前微信进程中能验证为聊天窗口的全部独立窗口，然后最小化主窗口。
- 保持微信、WeFlow 和 MaiBot开机自启，关闭系统睡眠和自动锁屏。
- 不要让杀毒软件隔离 `bin/WeMai.VoiceHost.exe` 或 `extensions/WeChatAutoServer/Server.exe`。
- 定期检查 `wemai.log` 和磁盘余量。入站媒体目录按内置保留规则清理，用户自行放入的文件不会被当作 WeMai 临时文件删除。

WeMai 启动时不执行托盘微信检测，不读取 Explorer 托盘数据，也不会展开或点击微信托盘图标。启动前必须登录微信并保持微信进程运行；启动后会直接通过现有主窗口 HWND 和 UIAutomation 自动恢复主窗口。聊天独立窗口仍只在首次需要时按需打开。

## 8. 常见故障

### 启动窗口一闪而过

请通过 `Start-WeMai.vbs` 启动。启动器会保留安装错误并显示缺少的 Python 或依赖。运行日志在 `wemai.log`。

### WeFlow 连接失败

确认 WeFlow 正在监听配置的地址，Token 正确，端口未被防火墙拦截。WeFlow 黑白名单也决定哪些消息会被推送。

### 文本可以发送，语音没有动作

确认使用微信 4.1.9.30、独立窗口中存在语音按钮、VB-CABLE 已安装、微信麦克风为 `CABLE Output`，并检查日志中的 VoiceHost 设备诊断。

### 修改名称后发送失败

只要原独立窗口仍存在，真实 ID 会继续复用原 HWND，不依赖已过期的窗口标题。若窗口被关闭，等待新名称从 WeFlow 入站更新后再发送；列表找不到时会自动搜索兜底。

### WebUI 局域网打不开

把监听地址设为 `0.0.0.0`，设置访问密码，重启 WeMai，并允许 Windows 防火墙放行所选端口。不要把无密码 WebUI 暴露到局域网或公网。

## 9. 参考与致谢

WeMai 是基于原始适配器继续开发的二改项目。项目名称统一为 WeMai，平台标识统一为 `wx`，但改名不改变上游代码来源和许可证归属。参考、派生与组合来源如下：

- [zhx57/MaiBot-WeChat-WeFlow-Adapter](https://github.com/zhx57/MaiBot-WeChat-WeFlow-Adapter)：WeMai 二次开发所基于的原始适配器
- [MaiBot](https://github.com/MaiM-with-u/MaiBot) 与 [官方文档](https://docs.mai-mai.org/)：消息协议和聊天流
- [WeFlow](https://github.com/Techuouo520/UserDataIsSafeFromUsers)：微信消息推送和媒体接口
- [wx4py](https://github.com/claw-codes/wx4py)：内部微信 4.x UI 自动化依赖；该名称不作为 WeMai 或 MaiBot 平台名
- [scottfly189/WeChatAuto.SDK](https://github.com/scottfly189/WeChatAuto.SDK)：联系人、群管理与 OCR 高级扩展基础
- [Emilia-awa_maibot-mimotts-voice](https://github.com/Emilia-awa/maibot-mimotts-voice)：MaiBot 语音生成插件的消息格式与接入参考
- [VB-CABLE](https://vb-audio.com/Cable/)：虚拟音频路由

发行包中的 `THIRD_PARTY_NOTICES.md` 汇总了上述来源和上游链接。上述项目及其打包依赖分别遵循各自许可证；WeMai 根目录 `LICENSE` 仅用于说明 WeMai 自身新增和可独立许可部分，第三方许可证正文以对应上游仓库和组件内嵌声明为准。

WeMai 是由 AI 参与整合与优化的学习研究项目，仅供学习和技术研究，不可用于商业运营、收费服务或其他商业用途。该项目用途声明不改变或缩减第三方许可证已经授予的权利。
