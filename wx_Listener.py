import asyncio
import json
import logging
import time
from datetime import datetime
from wxauto import WeChat
from config import WX_TARGET_CHATS, WX_LISTEN_ALL_IF_EMPTY, WX_EXCLUDED_CHATS

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

class WeChatListener:
    def __init__(self, target_chats=None, callback=None):
        """
        初始化微信消息监听器

        Args:
            target_chats (list, optional): 要监听的聊天对象列表，如果为None则监听所有聊天
            callback (function, optional): 收到新消息时的回调函数，接收参数为(chat_name, message_data)
        """
        self.wx = WeChat()
        # 把 wx 实例挂到模块级全局变量，供 wx_Processer 发送消息时复用
        # （发送和监听必须用同一个 WeChat 实例，多实例同时操作同一微信窗口会冲突）
        global wx
        wx = self.wx
        self.target_chats = target_chats
        self.callback = callback
        self.listen_chats = {}
        # 跟踪监听失败的聊天，主循环里定期重试
        self._failed_chats = set()
        self._last_retry_time = 0
        self.running = False
        logger.info(f"微信监听器初始化成功，登录账号：{self.wx.nickname}")

    def start_listening(self):
        """开始监听微信消息"""
        logger.info("开始监听微信消息...")
        self.running = True

        # 启动前等一下，让微信窗口完全就绪（刚启动时 UIA 树可能还没稳定）
        time.sleep(1)

        # 如果指定了目标聊天，则只监听这些聊天
        if self.target_chats:
            for chat in self.target_chats:
                self._add_listen_chat(chat)
                # 聊天之间加小延迟，避免 ChatWith 切换太快导致 UIA 找不到控件
                time.sleep(0.5)
        elif WX_LISTEN_ALL_IF_EMPTY:
            # 监听当前所有聊天窗口，但排除指定的聊天
            session_list = self.wx.GetSessionList(reset=True)
            for chat in session_list:
                if chat not in WX_EXCLUDED_CHATS:
                    self._add_listen_chat(chat)
                    time.sleep(0.5)
        else:
            logger.info("未指定目标聊天且未启用监听所有聊天，将不会监听任何聊天")

        # 打印监听状态摘要
        if self.target_chats:
            ok = [c for c in self.target_chats if c in self.listen_chats]
            fail = [c for c in self.target_chats if c not in self.listen_chats]
            logger.info(f"监听初始化完成: 成功 {len(ok)}/{len(self.target_chats)}"
                        + (f"，失败 {fail} 将在运行中重试" if fail else ""))

        # 开始监听循环
        wechat_lost = False  # 标记微信窗口是否丢失
        lost_since = None    # 窗口丢失的开始时间
        reconnect_fail_count = 0  # 重连连续失败次数
        last_alive_check = 0
        last_lost_log = 0    # 上次打印"仍在等待"的时间
        MAX_RECONNECT_FAILS = 5     # 重连连续失败上限，超过则抛异常触发整体重启
        MAX_LOST_DURATION = 600     # 窗口丢失最长等待时间(秒)，超过则放弃(10分钟)
        try:
            while self.running:
                # 每5秒检测一次微信窗口是否还在（避免每秒都 FindWindow 开销）
                now = time.time()
                if now - last_alive_check > 5:
                    last_alive_check = now
                    if not self._is_wechat_alive():
                        if not wechat_lost:
                            logger.warning("⚠️ 微信主窗口已关闭，等待重新打开...")
                            wechat_lost = True
                            lost_since = now
                            reconnect_fail_count = 0
                        else:
                            # 检查是否超过最大等待时间
                            elapsed = now - lost_since
                            if elapsed > MAX_LOST_DURATION:
                                logger.error(f"❌ 微信窗口已关闭超过{int(elapsed)}秒（上限{MAX_LOST_DURATION}秒），"
                                             f"放弃等待，触发整体重启")
                                raise RuntimeError(f"微信窗口丢失超过{MAX_LOST_DURATION}秒")
                            # 每30秒提示一次还在等待，避免日志刷屏
                            if now - last_lost_log > 30:
                                last_lost_log = now
                                logger.warning(f"⚠️ 微信窗口仍关闭，已等待{int(elapsed)}秒"
                                               f"（最长等待{MAX_LOST_DURATION}秒）...")
                        time.sleep(3)  # 窗口丢失时降低轮询频率
                        continue
                    elif wechat_lost:
                        # 窗口恢复了，重新初始化监听
                        logger.info("✅ 检测到微信窗口恢复")
                        if self._reconnect_wechat():
                            wechat_lost = False
                            reconnect_fail_count = 0
                            self._failed_chats.clear()  # 重连成功，清空失败列表
                        else:
                            reconnect_fail_count += 1
                            if reconnect_fail_count >= MAX_RECONNECT_FAILS:
                                logger.error(f"❌ 微信重连连续失败{reconnect_fail_count}次"
                                             f"（上限{MAX_RECONNECT_FAILS}次），触发整体重启")
                                raise RuntimeError(f"微信重连连续失败{MAX_RECONNECT_FAILS}次")
                            logger.warning(f"重连失败（第{reconnect_fail_count}/{MAX_RECONNECT_FAILS}次），稍后重试")
                            time.sleep(3)
                            continue

                # 微信窗口正常时才检查消息
                if not wechat_lost:
                    self._check_new_messages()
                    # 定期重试失败的聊天（每30秒）
                    self._retry_failed_chats()
                time.sleep(1)  # 每秒检查一次新消息
        except KeyboardInterrupt:
            logger.info("监听被用户中断")
        except Exception as e:
            logger.error(f"监听过程中发生错误: {str(e)}")
            raise  # 重新抛出，让 main.py 的重启逻辑接管
        finally:
            self.stop_listening()

    def stop_listening(self):
        """停止监听微信消息"""
        self.running = False
        logger.info("停止监听微信消息")

    def _retry_failed_chats(self):
        """定期重试添加失败的聊天对象"""
        if not self._failed_chats:
            return
        now = time.time()
        if now - self._last_retry_time < 30:
            return
        self._last_retry_time = now
        pending = list(self._failed_chats)
        logger.info(f"重试 {len(pending)} 个失败的监听聊天: {pending}")
        for chat in pending:
            if self._add_listen_chat(chat):
                self._failed_chats.discard(chat)
            time.sleep(0.5)
        if self._failed_chats:
            logger.warning(f"仍有 {len(self._failed_chats)} 个聊天监听失败: {list(self._failed_chats)}")

    def _clear_search_box(self):
        """彻底清空微信主窗口的搜索框。

        ChatWith 内部用 Ctrl+F 打开搜索框后 B_Search.SendKeys(who) 追加文字，
        但不会清空已有内容。如果上次 ChatWith 中途被打断，搜索框残留文字，
        下次重试会追加变成 '元宝原宝元宝原宝' 搜不到结果。

        微信搜索框的 Esc 行为：第一次只关闭搜索结果下拉，文字仍在。
        必须先聚焦搜索框 → Ctrl+A 全选 → Delete 删除 → Esc 关闭。

        如果微信窗口不存在，_show 会抛异常，这里直接捕获跳过。
        """
        try:
            # 先确保主窗口在前台（窗口不存在会抛异常，直接跳过）
            self.wx._show()
            # Esc 先关闭可能打开的搜索结果下拉
            self.wx.UiaAPI.SendKeys('{Esc}', waitTime=0.3)
            # Ctrl+F 打开搜索框（如果没打开的话），聚焦到搜索框
            self.wx.UiaAPI.SendKeys('{Ctrl}f', waitTime=0.5)
            # Ctrl+A 全选搜索框内容
            self.wx.B_Search.SendKeys('{Ctrl}a', waitTime=0.3)
            # Delete 删除选中的文字
            self.wx.B_Search.SendKeys('{Delete}', waitTime=0.3)
            # Esc 关闭搜索框
            self.wx.UiaAPI.SendKeys('{Esc}', waitTime=0.3)
        except Exception as e:
            # 窗口不存在或 UIA 操作失败，忽略（ChatWith 内部会再处理）
            logger.debug(f"清空搜索框跳过: {e}")

    def _add_listen_chat(self, chat_name, max_retries=3):
        """添加监听的聊天对象，带重试。

        启动时 WeChat 窗口可能还没完全就绪，或 ChatWith 切换中途被打断，
        单次失败不放弃，重试 max_retries 次，每次间隔递增（1s/2s/3s）。
        成功则加入 self.listen_chats，失败则加入 self._failed_chats 待主循环重试。
        每次重试前清空搜索框，避免搜索词重复累积。
        每次重试前检查微信窗口是否存活，不存活则跳过（等主循环的重连逻辑处理）。

        注意：wxauto 的 AddListenChat 内部已经做了 ChatWith + 双击弹独立窗口
        + ChatWnd(who)（会调 GetAllMessage 读全部记录，10s+）。
        之前 WeMai 在调用 AddListenChat 前先 ChatWith 预检，导致 ChatWith
        被调用两次，初始化时间翻倍。现在直接调 AddListenChat，用 try-catch
        判断成功/失败。
        """
        import os
        savepic = os.getenv('IMAGE_AUTO_DOWNLOAD', 'true').lower() == 'true'

        for attempt in range(1, max_retries + 1):
            try:
                # 微信窗口不存在时直接跳过本次尝试（不浪费 UIA 超时时间）
                if not self._is_wechat_alive():
                    logger.warning(f"微信窗口不存在，跳过 {chat_name} (第{attempt}/{max_retries}次)")
                    if attempt < max_retries:
                        time.sleep(attempt)
                    continue

                # 重试前清空搜索框，避免上次残留的搜索词和本次叠加
                self._clear_search_box()

                # 直接调用 AddListenChat（内部会 ChatWith + 双击弹窗 + ChatWnd）
                # 不再预先 ChatWith，避免重复切换聊天导致初始化时间翻倍
                self.wx.AddListenChat(chat_name, savepic=savepic, savefile=False, savevoice=False)
                self.listen_chats[chat_name] = True
                self._failed_chats.discard(chat_name)
                if attempt > 1:
                    logger.info(f"✅ 第{attempt}次重试成功，添加监听聊天: {chat_name}")
                else:
                    logger.info(f"添加监听聊天: {chat_name}")
                return True
            except Exception as e:
                logger.warning(f"添加监听聊天 {chat_name} 异常 (第{attempt}/{max_retries}次): {str(e)}")
                # ChatWith 内部可能因 _show 失败留下脏状态，尝试刷新一下
                try:
                    self.wx._refresh()
                except Exception:
                    pass

            # 还有重试机会则等待
            if attempt < max_retries:
                time.sleep(attempt)  # 1s, 2s 递增

        # 全部重试失败，记录到待重试集合
        self._failed_chats.add(chat_name)
        logger.error(f"❌ 添加监听聊天 {chat_name} 失败（重试{max_retries}次），将在运行中定期重试")
        return False
    
    def _check_new_messages(self):
        """检查所有监听的聊天是否有新消息"""
        try:
            # 获取所有监听聊天的新消息
            all_messages = self.wx.GetListenMessage()

            if all_messages:
                for chat, messages in all_messages.items():
                    chat_name = chat.who
                    if messages:
                        logger.info(f"收到来自 {chat_name} 的 {len(messages)} 条新消息")

                        # 处理每条消息
                        for msg in messages:
                            self._process_message(chat_name, msg)
            # 成功调用，重置连续失败计数
            self._msg_fail_count = 0
        except Exception as e:
            logger.error(f"检查新消息时发生错误: {str(e)}")
            # 用连续失败计数器判断是否需要重建监听，避免偶发错误误清空
            # 之前用宽泛关键词（'none'/'attribute'）匹配，一次 AttributeError
            # 就把所有 listen_chats 清空，导致监听被频繁重建
            self._msg_fail_count = getattr(self, '_msg_fail_count', 0) + 1
            if self._msg_fail_count >= 5:
                logger.warning(f"GetListenMessage 连续失败 {self._msg_fail_count} 次，"
                               f"监听可能已失效，将重新初始化所有监听聊天")
                for chat_name in list(self.listen_chats.keys()):
                    self._failed_chats.add(chat_name)
                self.listen_chats.clear()
                self._msg_fail_count = 0  # 重置，等重建后再计

    def _is_wechat_alive(self):
        """检测微信主窗口是否还存在。

        wxauto 初始化时缓存了 HWND 和 UIA 控件树，如果用户中途关掉微信窗口，
        后续所有操作都会失败。用 FindWindow 重新检测主窗口是否还在。
        """
        try:
            from wxauto.utils import FindWindow
            return bool(FindWindow(classname='WeChatMainWndForPC'))
        except Exception:
            return False

    def _reconnect_wechat(self):
        """微信窗口恢复后重新初始化监听。

        微信窗口被关掉再重新打开后，原 self.wx 的 HWND 和 UIA 控件引用全部失效。
        这里重新创建 WeChat 实例并重新添加所有监听聊天。
        """
        logger.info("检测到微信窗口恢复，重新初始化监听...")
        try:
            # 重新创建 WeChat 实例（会重新查找窗口和 UIA 树）
            self.wx = WeChat()
            global wx
            wx = self.wx
            logger.info(f"微信重新连接成功，登录账号：{self.wx.nickname}")

            # 清空旧的监听列表，重新添加
            self.listen_chats = {}
            time.sleep(1)  # 等窗口稳定

            chats_to_restore = list(self.target_chats) if self.target_chats else []
            if not chats_to_restore:
                # 没有指定目标聊天时，恢复当前所有会话
                try:
                    session_list = self.wx.GetSessionList(reset=True)
                    chats_to_restore = [c for c in session_list if c not in WX_EXCLUDED_CHATS]
                except Exception as e:
                    logger.error(f"恢复会话列表失败: {e}")

            for chat in chats_to_restore:
                self._add_listen_chat(chat)
                time.sleep(0.5)

            ok = len(self.listen_chats)
            total = len(chats_to_restore)
            logger.info(f"重新初始化完成: 成功 {ok}/{total}")
            return ok > 0
        except Exception as e:
            logger.error(f"重新初始化微信监听失败: {e}")
            return False
    
    def _process_message(self, chat_name, message):
        """处理单条消息"""
        try:
            # 提取消息信息
            msg_type = message.type
            sender = message.sender
            content = message.content
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 过滤系统消息，如"以下为新消息"等
            if msg_type == "sys" and ("以下为新消息" in content or "新消息" in content):
                return None
                
            # 过滤自己发送的消息
            if msg_type == "self" or sender == "Self":
                logger.info(f"过滤自己发送的消息: {chat_name} - {sender}: {content}")
                return None
            
            # 过滤纯时间系统消息
            if msg_type == "sys" and len(content.strip()) <= 10 and ":" in content:
                return None
            
            # 构建消息数据
            message_data = {
                "chat": chat_name,
                "sender": sender,
                "type": msg_type,
                "content": content,
                "timestamp": timestamp
            }
            
            # 记录消息
            logger.info(f"消息: {chat_name} - {sender}: {content} ({msg_type})")
            
            # 如果有回调函数，则调用回调函数
            if self.callback:
                self.callback(chat_name, message_data)
                
            return message_data
        except Exception as e:
            logger.error(f"处理消息时发生错误: {str(e)}")
            return None


# 全局消息处理器实例
global_processor = None

# 全局 wxauto.WeChat 实例
# 由 WeChatListener.__init__ 创建并赋值，供 wx_Processer 发送消息时复用
# （发送和监听必须用同一个 WeChat 实例，多实例同时操作同一微信窗口会冲突）
wx = None

def set_global_processor(processor):
    """设置全局消息处理器"""
    global global_processor
    global_processor = processor
    print(f"全局消息处理器已设置: {type(processor)}")

def create_message_processor():
    """创建消息处理器实例"""
    from wx_Processer import MessageProcessor
    return MessageProcessor()

# 消息处理回调函数
def message_callback(chat_name, message_data):
    """
    收到新消息的回调函数，将消息转发到 MaiBot
    
    Args:
        chat_name (str): 聊天对象名称
        message_data (dict): 消息数据
    """
    global global_processor
    
    print(f"\n收到新消息 - {chat_name}:")
    print(f"发送者: {message_data['sender']}")
    print(f"内容: {message_data['content']}")
    print(f"类型: {message_data['type']}")
    print(f"时间: {message_data['timestamp']}")
    print("-" * 50)
    
    # 使用全局消息处理器处理消息并转发到 MaiBot
    if global_processor:
        result = global_processor.process_message(chat_name, message_data)
        
        # 打印处理结果
        if result.get("success"):
            print(f"消息已成功转发到 MaiBot")
        else:
            print(f"消息转发失败: {result.get('error')}")
    else:
        print("消息处理器未初始化")
        print(f"global_processor: {global_processor}")
        print(f"type: {type(global_processor)}")


# 主程序入口
if __name__ == "__main__":
    # 使用全局消息处理器实例（已在main.py中创建）
    # 创建监听器实例，使用配置文件中的目标聊天列表
    # 同时设置回调函数，将消息转发到 MaiBot
    listener = WeChatListener(
        target_chats=WX_TARGET_CHATS,
        callback=message_callback
    )
    
    print("微信消息监听器已启动，消息将转发到 MaiBot")
    print("WebSocket监听器将接收MaiBot的回复并发送到微信")
    print("按 Ctrl+C 停止监听")
    print("请确保已打开要监听的聊天窗口")
    print("-" * 50)
    
    # Router已在main.py中启动，这里不需要重复启动
    
    # 开始监听
    listener.start_listening()
