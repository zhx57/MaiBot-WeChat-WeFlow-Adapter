"""群成员显示名到微信真 ID 的持久化、安全别名索引。

旧版入站不保证携带群成员真 ID；该兼容索引只接受已经由可靠来源确认的关系，
并且仅在一个显示名于同一群内唯一对应一个真 ID 时返回。
入站查询始终读取内存；SQLite 写入由独立线程批量完成，不占用消息解析线程。
"""

import contextlib
import logging
import queue
import sqlite3
import threading
import time

from chat_name_utils import normalize_chat_name

logger = logging.getLogger(__name__)

_RETENTION_SECONDS = 180 * 24 * 60 * 60
_PERSIST_INTERVAL_SECONDS = 6 * 60 * 60
_MAX_ROWS = 100_000
_WRITE_QUEUE_SIZE = 4096
_WRITE_BATCH_SIZE = 128


class SenderIdentityAliasStore:
    """线程安全的群成员别名索引，歧义时永远拒绝猜测。"""

    def __init__(self, database_path):
        self._database_path = str(database_path)
        self._lock = threading.RLock()
        # {(chat_id, normalized_name): {sender_wxid: [last_seen, persisted_at]}}
        self._aliases = {}
        self._write_queue = queue.Queue(maxsize=_WRITE_QUEUE_SIZE)
        self._stop_event = threading.Event()
        self._writer_thread = None
        self._init_storage()
        self._load()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="wemai-sender-alias-writer",
            daemon=True,
        )
        self._writer_thread.start()

    @staticmethod
    def _name_key(value):
        return normalize_chat_name(value).casefold()

    @staticmethod
    def _chat_key(value):
        return str(value or "").strip()

    @staticmethod
    def _sender_key(value):
        return str(value or "").strip()

    def _connect(self):
        database = sqlite3.connect(self._database_path, timeout=10)
        database.execute("PRAGMA busy_timeout=10000")
        database.execute("PRAGMA wal_autocheckpoint=100")
        return database

    def _init_storage(self):
        now = time.time()
        with contextlib.closing(self._connect()) as database, database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS sender_identity_aliases ("
                "chat_id TEXT NOT NULL, normalized_name TEXT NOT NULL, "
                "sender_wxid TEXT NOT NULL, last_seen REAL NOT NULL, "
                "PRIMARY KEY(chat_id,normalized_name,sender_wxid))"
            )
            database.execute(
                "CREATE INDEX IF NOT EXISTS sender_identity_aliases_seen_idx "
                "ON sender_identity_aliases(last_seen)"
            )
            database.execute(
                "DELETE FROM sender_identity_aliases WHERE last_seen<?",
                (now - _RETENTION_SECONDS,),
            )
            count = database.execute(
                "SELECT COUNT(*) FROM sender_identity_aliases"
            ).fetchone()[0]
            if count > _MAX_ROWS:
                database.execute(
                    "DELETE FROM sender_identity_aliases WHERE rowid IN ("
                    "SELECT rowid FROM sender_identity_aliases "
                    "ORDER BY last_seen ASC LIMIT ?)",
                    (count - _MAX_ROWS,),
                )

    def _load(self):
        cutoff = time.time() - _RETENTION_SECONDS
        with contextlib.closing(self._connect()) as database:
            rows = database.execute(
                "SELECT chat_id,normalized_name,sender_wxid,last_seen "
                "FROM sender_identity_aliases WHERE last_seen>=?",
                (cutoff,),
            ).fetchall()
        with self._lock:
            for chat_id, normalized_name, sender_wxid, last_seen in rows:
                identities = self._aliases.setdefault(
                    (chat_id, normalized_name), {}
                )
                identities[sender_wxid] = [last_seen, last_seen]

    def resolve(self, chat_id, *display_names):
        """返回同群内唯一匹配的真 ID；同名歧义或未命中返回空字符串。"""
        chat_id = self._chat_key(chat_id)
        names = {self._name_key(name) for name in display_names}
        names.discard("")
        if not chat_id or not names:
            return ""

        now = time.time()
        cutoff = now - _RETENTION_SECONDS
        matched_keys = []
        candidates = set()
        with self._lock:
            for normalized_name in names:
                key = (chat_id, normalized_name)
                identities = self._aliases.get(key)
                if not identities:
                    continue
                expired = [
                    sender_wxid
                    for sender_wxid, values in identities.items()
                    if values[0] < cutoff
                ]
                for sender_wxid in expired:
                    identities.pop(sender_wxid, None)
                if not identities:
                    self._aliases.pop(key, None)
                    continue
                candidates.update(identities)
                matched_keys.append(key)
            if len(candidates) != 1:
                return ""
            sender_wxid = next(iter(candidates))
            # 命中只刷新已经存在的名称，不把本次消息中的其他名称未经确认地扩散。
            for key in matched_keys:
                values = self._aliases[key].get(sender_wxid)
                if values is not None:
                    self._touch_locked(key, sender_wxid, values, now)
            return sender_wxid

    def observe(self, chat_id, sender_wxid, *display_names):
        """记录由权威来源确认的一个或多个显示名，不等待磁盘写入。"""
        chat_id = self._chat_key(chat_id)
        sender_wxid = self._sender_key(sender_wxid)
        names = {self._name_key(name) for name in display_names}
        names.discard("")
        if not chat_id or not sender_wxid or not names:
            return 0

        now = time.time()
        observed = 0
        with self._lock:
            for normalized_name in names:
                key = (chat_id, normalized_name)
                identities = self._aliases.setdefault(key, {})
                values = identities.get(sender_wxid)
                if values is None:
                    values = identities[sender_wxid] = [now, 0.0]
                else:
                    values[0] = now
                self._touch_locked(key, sender_wxid, values, now)
                observed += 1
        return observed

    def _touch_locked(self, key, sender_wxid, values, now):
        values[0] = now
        if now - values[1] < _PERSIST_INTERVAL_SECONDS:
            return
        item = (key[0], key[1], sender_wxid, now)
        try:
            self._write_queue.put_nowait(item)
        except queue.Full:
            logger.warning("群成员身份别名写入队列已满；本次仅保留内存记录")
            return
        # 乐观标记可抑制高频消息重复排队；写入失败时后台会将它复位。
        values[1] = now

    def _writer_loop(self):
        last_gc_at = time.monotonic()
        while not self._stop_event.is_set() or not self._write_queue.empty():
            try:
                first = self._write_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if first is None:
                continue
            batch = [first]
            while len(batch) < _WRITE_BATCH_SIZE:
                try:
                    item = self._write_queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    batch.append(item)
            # 同一个键只写批次内最新时间，减少 WAL 和 fsync 次数。
            latest = {
                (chat_id, name, sender_wxid): last_seen
                for chat_id, name, sender_wxid, last_seen in batch
            }
            try:
                with contextlib.closing(self._connect()) as database, database:
                    database.executemany(
                        "INSERT INTO sender_identity_aliases("
                        "chat_id,normalized_name,sender_wxid,last_seen) "
                        "VALUES(?,?,?,?) ON CONFLICT("
                        "chat_id,normalized_name,sender_wxid) DO UPDATE SET "
                        "last_seen=excluded.last_seen",
                        [(*key, last_seen) for key, last_seen in latest.items()],
                    )
                    if time.monotonic() - last_gc_at >= 24 * 60 * 60:
                        database.execute(
                            "DELETE FROM sender_identity_aliases WHERE last_seen<?",
                            (time.time() - _RETENTION_SECONDS,),
                        )
                        last_gc_at = time.monotonic()
            except sqlite3.Error:
                logger.warning("持久化群成员身份别名失败", exc_info=True)
                with self._lock:
                    for key in latest:
                        values = self._aliases.get(key[:2], {}).get(key[2])
                        if values is not None:
                            values[1] = 0.0

    def close(self, timeout=5):
        """停止后台写线程；已入队记录会在超时前尽量写完。"""
        self._stop_event.set()
        with contextlib.suppress(queue.Full):
            self._write_queue.put_nowait(None)
        thread = self._writer_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("群成员身份别名写线程未在时限内退出")

    def snapshot(self):
        """返回只读诊断快照，供测试和健康检查使用。"""
        with self._lock:
            return {
                key: set(identities)
                for key, identities in self._aliases.items()
            }
