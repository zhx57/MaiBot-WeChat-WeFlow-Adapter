import argparse
import asyncio
import logging
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

# 导入各个组件
# 新版麦麦用 WebSocket 双向通信，Router 在 wx_Processer 里同时处理
# 入站（微信→麦麦）和出站（麦麦→微信），不再需要 mq_Producer/mq_Consumer
# 的 Redis 队列中转，因此移除了这两个组件的启动逻辑。
from wx_Listener import WeChatListener, message_callback, set_global_processor, create_message_processor
from config import WX_TARGET_CHATS, LOG_LEVEL, LOG_FORMAT, LOG_DATE_FORMAT, LOG_FILE

# 配置日志
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)

logger = logging.getLogger(__name__)

# 全局变量
stop_event = Event()
signal_handled = False

# 微信监听器进程
def run_wx_listener(target_chats=None):
    """
    运行微信消息监听器

    同时承担入站（微信→麦麦）和出站（麦麦→微信）：
    - 入站：监听微信消息 → message_callback → MessageProcessor → WebSocket → 麦麦
    - 出站：Router(WebSocket) 接收麦麦回复 → _handle_maibot_response → wx.SendMsg → 微信

    注意：本函数在子线程中运行（由 ThreadPoolExecutor 调度）。
    wxauto 的 UIA 操作需要 COM 初始化，子线程默认不初始化，
    必须先调用 pythoncom.CoInitialize()，否则报
    "[WinError -2147221008] 尚未调用 CoInitialize"。
    
    Args:
        target_chats (list, optional): 要监听的聊天对象列表
    """
    # 子线程使用 COM（UIAutomation）前必须先初始化
    try:
        import pythoncom
        pythoncom.CoInitialize()
        logger.info("COM 已初始化（监听线程）")
    except ImportError:
        logger.warning("pythoncom 不可用，跳过 COM 初始化（UIA 可能报错）")

    try:
        logger.info("启动微信消息监听器...")
        
        # 初始化全局消息处理器
        processor = create_message_processor()
        set_global_processor(processor)
        logger.info("全局消息处理器已初始化")
        
        # 启动Router（在后台线程中运行，处理与麦麦的 WebSocket 双向通信）
        import threading
        router_thread = threading.Thread(target=processor.start_router)
        router_thread.daemon = True
        router_thread.start()
        logger.info("Router已启动")
        
        # 显示监听的聊天对象及其哈希值，便于配置MaiBot的白名单
        if target_chats:
            import hashlib
            logger.info("监听的聊天对象及其ID哈希值（请将需要的群组ID添加到MaiBot的白名单中）：")
            for chat in target_chats:
                chat_id = hashlib.md5(chat.encode('utf-8')).hexdigest()
                logger.info(f"  {chat} -> {chat_id}")
        
        listener = WeChatListener(
            target_chats=target_chats,
            callback=message_callback
        )
        
        # 注册停止事件处理
        def check_stop():
            while not stop_event.is_set():
                time.sleep(1)
            listener.stop_listening()
        
        # 启动停止检查线程
        stop_thread = threading.Thread(target=check_stop)
        stop_thread.daemon = True
        stop_thread.start()
        
        # 开始监听
        listener.start_listening()
        
    except Exception as e:
        logger.error(f"微信监听器发生错误: {str(e)}")
        if not stop_event.is_set():
            logger.info("尝试重启微信监听器...")
            time.sleep(5)  # 等待5秒后重试
            run_wx_listener(target_chats)

# 信号处理函数
def handle_signal(sig, frame):
    """处理终止信号"""
    # 防止重复处理信号
    global signal_handled
    if signal_handled:
        return
    
    signal_handled = True
    logger.info(f"收到信号 {sig}，正在优雅停止...")
    stop_event.set()
    
    # 设置强制退出定时器（30秒后强制退出）
    def force_exit():
        logger.warning("程序未能在30秒内正常退出，强制终止...")
        import os
        os._exit(1)
    
    import threading
    force_timer = threading.Timer(30.0, force_exit)
    force_timer.daemon = True
    force_timer.start()

# 主函数
async def main(args):
    """
    主函数
    
    Args:
        args: 命令行参数
    """
    # 注册信号处理
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    
    # 创建线程池
    # 新版麦麦用 WebSocket 双向通信，Router 统一处理收发，
    # 只需启动微信监听器一个进程即可。
    with ThreadPoolExecutor(max_workers=1) as executor:
        tasks = []
        
        # 启动微信监听器（含 Router 双向通信）
        wx_future = executor.submit(
            run_wx_listener, 
            args.target_chats.split(',') if args.target_chats else WX_TARGET_CHATS
        )
        tasks.append(wx_future)
        
        # 等待停止事件
        try:
            while not stop_event.is_set():
                await asyncio.sleep(1)
                
                # 检查任务状态
                for i, future in enumerate(tasks):
                    if future.done() and not stop_event.is_set():
                        # 如果任务异常终止，记录错误
                        try:
                            future.result()
                        except Exception as e:
                            logger.error(f"任务 {i} 异常终止: {str(e)}")
        except asyncio.CancelledError:
            logger.info("主任务被取消")
        
        # 设置停止事件（以防万一）
        stop_event.set()
        
        logger.info("正在等待所有任务完成...")
        
        # 等待最多10秒让其他任务完成
        timeout = time.time() + 10
        while time.time() < timeout:
            if all(future.done() for future in tasks):
                break
            await asyncio.sleep(0.5)
        
        # 如果还有任务未完成，记录警告
        for i, future in enumerate(tasks):
            if not future.done():
                logger.warning(f"任务 {i} 未能正常结束")
        
        logger.info("所有服务已停止")

if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="WeMai - 微信消息转发服务")
    
    # 服务选择参数（保留向后兼容，但实际都只启动微信监听器+Router）
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--all', action='store_true', help='启动所有服务（默认）')
    group.add_argument('--wx-to-maibot', action='store_true', help='仅启动微信到MaiBot的消息转发')
    group.add_argument('--maibot-to-wx', action='store_true', help='仅启动MaiBot到微信的消息转发')
    
    # 其他参数
    parser.add_argument('--target-chats', type=str, help='要监听的微信聊天对象，多个用逗号分隔')
    
    # 解析参数
    args = parser.parse_args()
    
    # 打印启动信息
    logger.info("=" * 50)
    logger.info("WeMai - 微信消息转发服务")
    logger.info("=" * 50)
    
    logger.info("启动模式: 全部服务（WebSocket 双向通信，无需 Redis）")
    
    if args.target_chats:
        logger.info(f"监听聊天: {args.target_chats}")
    else:
        logger.info("监听聊天: 所有聊天")
    
    logger.info("=" * 50)
    
    # 运行主函数
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常退出: {str(e)}")
        sys.exit(1)
