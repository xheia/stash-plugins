#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""额度与缓存回归测试（v1.4.0）。

针对用户反馈的「同文本反复提交、请求碎片多、短时间打爆额度」，固化四件事：

  1. 缓存键与引擎链无关 —— 换引擎 / 调整顺序不会让整库缓存失效
  2. 旧版缓存库能自动迁移到共享键（不丢已有译文）
  3. 同一批里的重复文本只请求一次（进程内 memo）
  4. 批量合并：一页文本一次批量调用，而不是一条一个请求
  5. 额度上限：单次任务 / 每日字符上限生效

用法：
    python3 tests/test_quota.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(os.path.dirname(HERE), "src", "translateMetadata")
sys.path.insert(0, PLUGIN_DIR)

import cache as cache_mod  # noqa: E402
import fields  # noqa: E402
import translateMetadata as plugin  # noqa: E402
from config import Settings  # noqa: E402
from engines import TranslateResult  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


class FakeRouter:
    """记录调用次数与批量合并情况。"""

    def __init__(self, name="fake"):
        self.name = name
        self.calls = []
        self.batch_calls = []
        self.chars_sent = 0
        self.requests_sent = 0

    def chain_names(self):
        return [self.name]

    def stop_exception(self):
        return None

    def translate(self, text, source="auto", target="zh-CN"):
        self.calls.append(text)
        self.chars_sent += len(text)
        self.requests_sent += 1
        return TranslateResult("译:" + text, self.name, "en", text)

    def translate_many(self, texts, source="auto", target="zh-CN"):
        self.batch_calls.append(list(texts))
        self.chars_sent += sum(len(t) for t in texts)
        self.requests_sent += 1
        return {t: ("译:" + t, self.name, "en") for t in texts}


def make_settings(**overrides):
    values = {
        "skip_policy": "smart",
        "keep_original": True,
        "skip_terms": "",
        "min_chars": 2,
        "max_chars_per_run": 0,
        "daily_char_budget": 0,
    }
    values.update(overrides)
    return Settings(values)


def old_key(source, target, text):
    return hashlib.sha256(
        "\u0000".join(["edge", source, target, text]).encode("utf-8")).hexdigest()


def main():
    tmp = tempfile.mkdtemp(prefix="wb-quota-")
    try:
        print("\n1) 缓存键与引擎链无关（换引擎不再全库重翻）")
        db = os.path.join(tmp, "shared.sqlite3")
        cache = cache_mod.TranslationCache(db, True)
        router_a = FakeRouter("tencent")
        t1 = plugin.Translator(make_settings(), router_a, cache)
        first = t1.translate_text("Beautiful Nurse")
        check("首次翻译走了引擎", first[0] == "译:Beautiful Nurse", str(first))
        check("首次只请求一次", router_a.requests_sent == 1, str(router_a.requests_sent))

        router_b = FakeRouter("deepl+google")   # 模拟用户改了引擎链
        t2 = plugin.Translator(make_settings(), router_b, cache)
        second = t2.translate_text("Beautiful Nurse")
        check("换引擎后仍命中缓存", second[0] == "译:Beautiful Nurse", str(second))
        check("换引擎后没有重新请求", router_b.requests_sent == 0,
              str(router_b.requests_sent))
        check("缓存作用域与引擎无关", t2.cache_scope == cache_mod.SHARED_SCOPE,
              t2.cache_scope)
        cache.close()

        print("\n2) 旧版缓存库自动迁移（引擎链参与键的老库）")
        legacy = os.path.join(tmp, "legacy.sqlite3")
        conn = sqlite3.connect(legacy)
        conn.executescript("""
            CREATE TABLE translations (
                key TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
                engine TEXT NOT NULL, src_text TEXT NOT NULL, dst_text TEXT NOT NULL,
                detected TEXT, created_at INTEGER NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO translations VALUES (?,?,?,?,?,?,?,?)",
            (old_key("auto", "zh-CN", "Legacy Text"), "auto", "zh-CN",
             "tencent+alibaba", "Legacy Text", "旧译文", "en", 1700000000))
        conn.commit()
        conn.close()

        c2 = cache_mod.TranslationCache(legacy, True)
        check("迁移条数正确", c2.migrated == 1, str(c2.migrated))
        row = c2.get(cache_mod.SHARED_SCOPE, "auto", "zh-CN", "Legacy Text")
        check("迁移后能按新键命中", row is not None and row["text"] == "旧译文", str(row))
        check("迁移保留了原引擎记录",
              row is not None and row["engine"] == "tencent+alibaba", str(row))
        c2.close()

        print("\n3) 同文本只请求一次（进程内去重）")
        db3 = os.path.join(tmp, "memo.sqlite3")
        c3 = cache_mod.TranslationCache(db3, True)
        r3 = FakeRouter()
        t3 = plugin.Translator(make_settings(), r3, c3)
        for _ in range(5):
            t3.translate_text("POV")
        check("5 次相同文本只打 1 次接口", r3.requests_sent == 1, str(r3.requests_sent))
        check("统计里记为缓存命中", t3.stats["cached"] == 4, str(t3.stats))
        c3.close()

        print("\n4) 批量合并：一页文本一次调用")
        db4 = os.path.join(tmp, "batch.sqlite3")
        c4 = cache_mod.TranslationCache(db4, True)
        r4 = FakeRouter()
        t4 = plugin.Translator(make_settings(), r4, c4)
        records = [
            {"id": 1, "title": "Alpha", "details": "Shared details", "custom_fields": {}},
            {"id": 2, "title": "Alpha", "details": "Shared details", "custom_fields": {}},
            {"id": 3, "title": "Beta", "details": "Shared details", "custom_fields": {}},
            {"id": 4, "title": "Gamma", "details": "", "custom_fields": {}},
        ]
        prefetched = t4.prefetch("scene", records)
        check("预取做了去重", prefetched == 4, str(prefetched))
        check("只发 1 次批量请求", len(r4.batch_calls) == 1, str(r4.batch_calls))
        check("合并后的文本集合正确",
              sorted(r4.batch_calls[0]) == ["Alpha", "Beta", "Shared details", "Gamma"][:0]
              or sorted(r4.batch_calls[0]) == ["Alpha", "Beta", "Gamma", "Shared details"],
              str(r4.batch_calls))
        before = r4.requests_sent
        for record in records:
            t4.translate_record("scene", record)
        check("预取后逐条翻译不再请求",
              r4.requests_sent == before, "%d -> %d" % (before, r4.requests_sent))
        c4.close()

        print("\n5) 额度上限")
        db5 = os.path.join(tmp, "budget.sqlite3")
        c5 = cache_mod.TranslationCache(db5, True)
        r5 = FakeRouter()
        s5 = make_settings(max_chars_per_run=12, min_chars=2)
        t5 = plugin.Translator(s5, r5, c5)
        t5.translate_text("abcdefgh")
        check("未达上限时还能继续", t5.budget_exhausted() == "", t5.budget_exhausted())
        t5.translate_text("ijklmnop")
        check("达到单次上限后停止", "单次上限" in t5.budget_exhausted(),
              t5.budget_exhausted())
        c5.close()

        db6 = os.path.join(tmp, "daily.sqlite3")
        c6 = cache_mod.TranslationCache(db6, True)
        c6.add_chars(500)
        check("每日用量能跨会话读取", c6.chars_today() == 500, str(c6.chars_today()))
        s6 = make_settings(daily_char_budget=500)
        t6 = plugin.Translator(s6, FakeRouter(), c6)
        check("达到每日上限后停止", "每日上限" in t6.budget_exhausted(),
              t6.budget_exhausted())
        c6.close()

        print("\n6) 白名单跳过（跳过词 / 最短长度）")
        db7 = os.path.join(tmp, "skip.sqlite3")
        c7 = cache_mod.TranslationCache(db7, True)
        r7 = FakeRouter()
        s7 = make_settings(skip_terms="OnlyFans, POV", min_chars=3)
        t7 = plugin.Translator(s7, r7, c7)
        t7.translate_text("OnlyFans exclusive")
        t7.translate_text("4K")
        t7.translate_text("POV scene")
        check("命中跳过词不发请求", r7.requests_sent == 0, str(r7.requests_sent))
        check("过短文本不发请求", t7.stats["skipped"] == 3, str(t7.stats))
        c7.close()

        print("\n7) 跳过词表解析")
        from config import parse_skip_terms
        check("中英文逗号/换行都能拆",
              parse_skip_terms("A，B;C\nD") == ["A", "B", "C", "D"],
              str(parse_skip_terms("A，B;C\nD")))
        check("空值返回空列表", parse_skip_terms("") == [], str(parse_skip_terms("")))

        print("\n8) Router 批量切块（按条数 / 字符数）")
        from engines import Router

        class BatchEngine:
            name = "stub"
            batch_items = 2
            batch_chars = 5
            max_source_bytes = None

            def __init__(self):
                self.batches = []

            def translate_many(self, texts, source="auto", target="zh-CN"):
                self.batches.append(list(texts))
                return [("译:" + t, "en") for t in texts]

            def translate_detailed(self, text, source="auto", target="zh-CN"):
                return "译:" + text, "en"

        router = Router([])
        engine = BatchEngine()
        router.engines = [engine]
        result = router.translate_many(["a", "bb", "ccc", "dddd"])
        check("按条数/字符切块", len(engine.batches) == 3, str(engine.batches))
        check("合并结果完整", len(result) == 4, str(result))
        check("字符用量按实际提交统计",
              router.chars_sent == 10, str(router.chars_sent))
        check("请求数按批次统计", router.requests_sent == 3, str(router.requests_sent))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILED:
        print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
        for name in FAILED:
            print("  失败: %s" % name)
        return 1
    print("通过 %d 项，失败 0 项" % len(PASSED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
