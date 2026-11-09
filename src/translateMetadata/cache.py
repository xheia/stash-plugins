# -*- coding: utf-8 -*-
"""翻译缓存（SQLite）。

同一个文本（标签名、工作室名这类重复度极高的内容）只请求一次接口，
对免费额度尤其重要。

缓存键**与引擎无关** —— 只按「源语言 + 目标语言 + 原文」散列。
早期版本把整条引擎链（如 ``tencent+alibaba+baidu``）拼进了键，后果是
用户调整一次引擎顺序、临时去掉一个没配好的引擎、或者某天某个 Key 没读出来，
**整库缓存会瞬间全部失效**，下一次批量任务把几万条文本重新翻译一遍。
译文本身不会因为换了引擎就失去意义，所以键必须只跟文本相关。

``engine`` 列只做记录（写回时塞进 tr_engine_* 便于追溯），不参与键。

兼容：旧库会在首次打开时自动迁移到新的 ``shared-v1`` 键方案，
分块搬数据、单事务完成，迁移失败则回滚保留原表，绝不会把缓存搞丢。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time

# 键方案标识。改键算法时才需要升版本，届时同样走 _migrate 流程。
SHARED_SCOPE = "shared-v1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    key        TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    engine     TEXT NOT NULL,
    src_text   TEXT NOT NULL,
    dst_text   TEXT NOT NULL,
    detected   TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_created ON translations(created_at);
CREATE TABLE IF NOT EXISTS cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_MIGRATE_CHUNK = 1000


class TranslationCache:
    def __init__(self, path, enabled=True):
        self.enabled = bool(enabled)
        self.path = path
        self._conn = None
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self.migrated = 0
        if self.enabled:
            self._connect()

    # -- 内部 -------------------------------------------------------------- #
    def _connect(self):
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=10)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate()
        except Exception:
            self._conn = None

    @staticmethod
    def _make_key(scope, source, target, text):
        raw = "\u0000".join([scope or "", source or "", target or "", text or ""])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _columns(self, table="translations"):
        try:
            return {row[1] for row in self._conn.execute("PRAGMA table_info(%s)" % table)}
        except Exception:
            return set()

    def _migrate(self):
        """把旧版「引擎链参与键」的缓存搬到共享键方案。

        只在确实需要时执行一次：新库 / 已迁移的库直接返回。
        """
        if self._conn is None:
            return
        if "scope" in self._columns():
            row = self._conn.execute(
                "SELECT value FROM cache_meta WHERE key = 'key_scheme'").fetchone()
            if not row or row[0] != SHARED_SCOPE:
                with self._conn:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO cache_meta (key, value) VALUES "
                        "('key_scheme', ?)", (SHARED_SCOPE,))
            return

        # 旧表：没有 scope 列。建新表 -> 分块搬 -> 换名，全程一个事务。
        try:
            self._conn.execute("BEGIN")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS translations_new (
                    key        TEXT PRIMARY KEY,
                    scope      TEXT NOT NULL,
                    source     TEXT NOT NULL,
                    target     TEXT NOT NULL,
                    engine     TEXT NOT NULL,
                    src_text   TEXT NOT NULL,
                    dst_text   TEXT NOT NULL,
                    detected   TEXT,
                    created_at INTEGER NOT NULL
                )
            """)
            last_rowid = 0
            moved = 0
            while True:
                rows = self._conn.execute(
                    "SELECT rowid, engine, source, target, src_text, dst_text, "
                    "detected, created_at FROM translations "
                    "WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (last_rowid, _MIGRATE_CHUNK)).fetchall()
                if not rows:
                    break
                last_rowid = rows[-1][0]
                payload = []
                for _rid, engine, source, target, src_text, dst_text, detected, created in rows:
                    if not src_text:
                        continue
                    payload.append((
                        self._make_key(SHARED_SCOPE, source, target, src_text),
                        SHARED_SCOPE, source or "", target or "", engine or "",
                        src_text, dst_text or "", detected, created or int(time.time()),
                    ))
                if payload:
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO translations_new "
                        "(key, scope, source, target, engine, src_text, dst_text, "
                        "detected, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        payload)
                    moved += len(payload)
            self._conn.execute("DROP TABLE translations")
            self._conn.execute("ALTER TABLE translations_new RENAME TO translations")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_created ON translations(created_at)")
            self._conn.execute(
                "INSERT OR REPLACE INTO cache_meta (key, value) VALUES ('key_scheme', ?)",
                (SHARED_SCOPE,))
            self._conn.commit()
            self.migrated = moved
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass

    # -- 对外 -------------------------------------------------------------- #
    def get(self, scope, source, target, text):
        if not self.enabled or self._conn is None or not text:
            self._misses += 1
            return None
        key = self._make_key(scope, source, target, text)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT dst_text, detected, engine FROM translations WHERE key = ?",
                    (key,)).fetchone()
        except Exception:
            self._misses += 1
            return None
        if row:
            self._hits += 1
            return {"text": row[0], "engine": row[2] or "", "detected": row[1]}
        self._misses += 1
        return None

    def put(self, scope, source, target, text, translated, detected=None, engine=""):
        if not self.enabled or self._conn is None or not text:
            return
        key = self._make_key(scope, source, target, text)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO translations "
                    "(key, scope, source, target, engine, src_text, dst_text, "
                    "detected, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, scope or "", source or "", target or "", engine or "",
                     text, translated, detected, int(time.time())),
                )
                self._conn.commit()
        except Exception:
            pass

    def delete(self, scope, source, target, text):
        """删除一条缓存。失败静默（下轮覆盖即可），返回是否删除。"""
        if not self.enabled or self._conn is None or not text:
            return False
        key = self._make_key(scope, source, target, text)
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM translations WHERE key = ?", (key,)
                )
                self._conn.commit()
                return cursor.rowcount > 0
        except Exception:
            return False

    def stats(self):
        return {"hits": self._hits, "misses": self._misses, "migrated": self.migrated}

    # -- 字符用量（每日预算） ---------------------------------------------- #
    @staticmethod
    def _usage_key(day=None):
        return "chars:" + (day or time.strftime("%Y-%m-%d"))

    def chars_today(self):
        """今日已提交给翻译平台的字符数（跨进程共享）。"""
        if self._conn is None:
            return 0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM cache_meta WHERE key = ?",
                    (self._usage_key(),)).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def add_chars(self, chars):
        """累加今日用量，返回累加后的值。失败静默（预算保护降级为不生效）。"""
        chars = max(0, int(chars or 0))
        if not chars or self._conn is None:
            return self.chars_today()
        try:
            with self._lock:
                key = self._usage_key()
                row = self._conn.execute(
                    "SELECT value FROM cache_meta WHERE key = ?", (key,)).fetchone()
                total = (int(row[0]) if row else 0) + chars
                self._conn.execute(
                    "INSERT OR REPLACE INTO cache_meta (key, value) VALUES (?, ?)",
                    (key, str(total)))
                # 顺手清掉 30 天前的用量键，别让 meta 表无限长
                cutoff = time.strftime(
                    "%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
                self._conn.execute(
                    "DELETE FROM cache_meta WHERE key LIKE 'chars:%' AND key < ?",
                    ("chars:" + cutoff,))
                self._conn.commit()
            return total
        except Exception:
            return self.chars_today()

    def count(self):
        if self._conn is None:
            return 0
        try:
            with self._lock:
                return self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
        except Exception:
            return 0

    def clear(self):
        """清空缓存，返回删除条数。"""
        if self._conn is None:
            return 0
        try:
            with self._lock:
                total = self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
                self._conn.execute("DELETE FROM translations")
                self._conn.commit()
            return total
        except Exception:
            return 0

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
