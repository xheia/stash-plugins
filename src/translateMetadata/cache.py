# -*- coding: utf-8 -*-
"""翻译缓存（SQLite）。

同一个文本（标签名、工作室名这类重复度极高的内容）只请求一次接口，
对免费额度尤其重要。缓存放在插件目录下，键包含引擎 / 源语言 / 目标语言 / 文本，
所以换引擎或换目标语言都不会命中旧结果。

缓存读写失败一律降级成「没有缓存」，绝不影响主流程。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    key        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    target     TEXT NOT NULL,
    engine     TEXT NOT NULL,
    src_text   TEXT NOT NULL,
    dst_text   TEXT NOT NULL,
    detected   TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_created ON translations(created_at);
"""


class TranslationCache:
    def __init__(self, path, enabled=True):
        self.enabled = bool(enabled)
        self.path = path
        self._conn = None
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
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
        except Exception:
            self._conn = None

    @staticmethod
    def _make_key(engine, source, target, text):
        raw = "\u0000".join([engine or "", source or "", target or "", text or ""])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # -- 对外 -------------------------------------------------------------- #
    def get(self, engine, source, target, text):
        if not self.enabled or self._conn is None or not text:
            self._misses += 1
            return None
        key = self._make_key(engine, source, target, text)
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT dst_text, detected FROM translations WHERE key = ?", (key,)
                ).fetchone()
        except Exception:
            self._misses += 1
            return None
        if row:
            self._hits += 1
            return {"text": row[0], "engine": engine, "detected": row[1]}
        self._misses += 1
        return None

    def put(self, engine, source, target, text, translated, detected=None):
        if not self.enabled or self._conn is None or not text:
            return
        key = self._make_key(engine, source, target, text)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO translations "
                    "(key, source, target, engine, src_text, dst_text, detected, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, source or "", target or "", engine or "", text, translated,
                     detected, int(time.time())),
                )
                self._conn.commit()
        except Exception:
            pass

    def delete(self, engine, source, target, text):
        """删除一条缓存。失败静默（下轮覆盖即可），返回是否删除。"""
        if not self.enabled or self._conn is None or not text:
            return False
        key = self._make_key(engine, source, target, text)
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
        return {"hits": self._hits, "misses": self._misses}

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
