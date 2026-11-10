#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「该不该翻」与「翻过没有」的回归测试（v1.5.0）。

针对用户的三条要求固化行为：

  1. 只翻译非中文 —— 中文（含夹英文人名）不送翻；
  2. 已经翻译过的无需再次翻译 —— tr_done_ 标记让第二轮零请求；
  3. 数字类和名称类统一看待 —— 纯数字 / 规格 / 纯大写缩写走同一套语义判断。

用法：
    python3 tests/test_skip.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(os.path.dirname(HERE), "src", "translateMetadata")
sys.path.insert(0, PLUGIN_DIR)

import cache as cache_mod  # noqa: E402
import detect  # noqa: E402
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
    def __init__(self, name="fake"):
        self.name = name
        self.calls = []
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
        self.calls.extend(texts)
        self.chars_sent += sum(len(t) for t in texts)
        self.requests_sent += 1
        return {t: ("译:" + t, self.name, "en") for t in texts}


def make_settings(**overrides):
    values = {
        "skip_policy": "smart",
        "keep_original": True,
        "translate_title": True,
        "translate_details": True,
        "translate_names": True,
        "tag_name_mode": "rename",
        "min_chars": 1,
        "skip_terms": "",
        "max_chars_per_run": 0,
        "daily_char_budget": 0,
    }
    values.update(overrides)
    return Settings(values)


def main():
    print("1) 分类器：数字 / 规格 / 缩写 / 中文 / 外语")

    # 数字类
    for text in ("1080", "2024-01-01", "01", "1/2", "192.168.1.1"):
        check("数字 %s -> numeric" % text, detect.classify(text) == "numeric", detect.classify(text))

    # 规格与编码类
    for text in ("4K", "1080p", "FHD", "HDR10", "x264", "S01E02", "Vol.3",
                 "1080p 60fps", "BluRay 2160p"):
        check("规格 %s -> code" % text, detect.classify(text) == "code", detect.classify(text))

    # 纯大写缩写（名称类里的「代号」）
    for text in ("BBC", "POV", "VR", "JAV", "BBC POV"):
        check("缩写 %s -> code" % text, detect.classify(text) == "code", detect.classify(text))

    # 中文（含夹英文人名 —— 这是以前反复送翻的重灾区）
    for text in ("无码流出", "中文字幕 4K 修复版", "Mika 访谈", "John Smith 访谈",
                 "完全版 Complete Edition", "美人妻"):
        check("中文 %s -> chinese" % text, detect.classify(text) == "chinese", detect.classify(text))

    # 真正的外语
    for text in ("Beautiful Nurse", "Mika", "Tokyo 中出"):
        check("外语 %s -> foreign" % text, detect.classify(text) in ("foreign", "chinese"),
              detect.classify(text))
    check("纯英文名称需要翻译", detect.needs_translation("Beautiful Nurse") is True)
    check("中文不再送翻", detect.needs_translation("Mika 访谈") is False)
    check("数字不再送翻", detect.needs_translation("1080") is False)
    check("规格不再送翻", detect.needs_translation("1080p 60fps") is False)

    # 歧义（纯汉字）由 skip_policy 决定
    check("纯汉字默认跳过", detect.classify("美人妻") == "chinese")
    check("detect 模式交给引擎", detect.classify("美人妻", "detect") == "ambiguous")
    check("detect 模式需要翻译", detect.needs_translation("美人妻", "detect") is True)

    # 其它
    check("空文本", detect.classify("   ") == "empty")
    check("纯 emoji", detect.classify("😂😂") == "other")

    print("\n2) 已完成标记：写回 / 判定 / 回滚")

    record = {"id": 1, "title": "Beautiful Nurse", "details": "", "custom_fields": {}}
    items = [{"field": "title", "original": "Beautiful Nurse",
              "translated": "美丽的护士", "engine": "edge", "label": "标题"}]
    payload, written = fields.build_update_input("scene", record, items, make_settings())
    custom = payload["custom_fields"]["partial"]

    check("写回译文", payload.get("title") == "美丽的护士", str(payload))
    check("写入完成标记", custom.get("tr_done_title") == "1", str(custom))
    check("写入译文快照", custom.get("tr_out_title") == "美丽的护士", str(custom))
    check("写入原文（keep_original）", custom.get("tr_src_title") == "Beautiful Nurse", str(custom))

    after = {"id": 1, "title": "美丽的护士", "details": "", "custom_fields": dict(custom)}
    check("翻译结果被判定为已完成", fields.is_done(after, "title") is True)
    check("完成标记存在", fields.has_done_marker(after, "title") is True)

    edited = {"id": 1, "title": "Another English Title", "details": "",
              "custom_fields": dict(custom)}
    check("用户改过内容 -> 不算已完成", fields.is_done(edited, "title") is False)
    check("但标记仍在（说明不是旧数据）", fields.has_done_marker(edited, "title") is True)

    print("\n3) 标签别名模式也有幂等标记")

    tag = {"id": 2, "name": "Beautiful Nurse", "description": "", "aliases": [], "custom_fields": {}}
    tag_items = [{"field": "name", "original": "Beautiful Nurse",
                  "translated": "美丽的护士", "engine": "edge", "label": "名称"}]
    tag_settings = make_settings(tag_name_mode="alias")
    tag_payload, tag_written = fields.build_update_input("tag", tag, tag_items, tag_settings)
    tag_custom = tag_payload["custom_fields"]["partial"]

    check("别名模式追加 alias", tag_payload.get("aliases") == ["美丽的护士"], str(tag_payload))
    check("别名模式写完成标记", tag_custom.get("tr_done_name") == "1", str(tag_custom))
    check("别名模式不写译文快照（name 未改写）",
          "tr_out_name" not in tag_custom, str(tag_custom))

    tag_after = {"id": 2, "name": "Beautiful Nurse", "description": "",
                 "aliases": ["美丽的护士"], "custom_fields": dict(tag_custom)}
    check("别名模式判定为已完成", fields.is_done(tag_after, "name") is True)
    # 第二轮再跑：translate_record 应当直接跳过，不再请求
    router = FakeRouter()
    cache = cache_mod.TranslationCache(None, False)
    translator = plugin.Translator(tag_settings, router, cache)
    again = translator.translate_record("tag", tag_after)
    check("别名模式第二轮不再产翻译项", again == [], str(again))
    check("别名模式第二轮零请求", router.requests_sent == 0, str(router.requests_sent))
    cache.close()

    print("\n4) 回滚清除全部标记")

    rollback_payload, restored = fields.build_rollback_input("scene", after)
    removed = (rollback_payload or {}).get("custom_fields", {}).get("remove", [])
    check("回滚还原字段", rollback_payload.get("title") == "Beautiful Nurse", str(rollback_payload))
    check("回滚清除完成标记", "tr_done_title" in removed, str(removed))
    check("回滚清除译文快照", "tr_out_title" in removed, str(removed))
    check("回滚清除原文", "tr_src_title" in removed, str(removed))
    check("回滚清除引擎记录", "tr_engine_title" in removed, str(removed))

    print("\n5) 集成：第二次任务零请求")

    router2 = FakeRouter()
    cache2 = cache_mod.TranslationCache(None, False)
    translator2 = plugin.Translator(make_settings(), router2, cache2)
    first = translator2.translate_record("scene", record)
    check("首轮产生翻译项", len(first) == 1, str(first))
    check("首轮发出 1 次请求", router2.requests_sent == 1, str(router2.requests_sent))

    p2, _ = fields.build_update_input("scene", record, first, make_settings())
    translated_record = {"id": 1, "title": "译:Beautiful Nurse", "details": "",
                         "custom_fields": dict(p2["custom_fields"]["partial"])}
    second = translator2.translate_record("scene", translated_record)
    check("第二轮无翻译项", second == [], str(second))
    check("第二轮零请求", router2.requests_sent == 1, str(router2.requests_sent))

    print("\n6) 预取也跳过已完成字段")

    router3 = FakeRouter()
    cache3 = cache_mod.TranslationCache(None, False)
    translator3 = plugin.Translator(make_settings(), router3, cache3)
    done_record = {"id": 3, "title": "译:Beautiful Nurse", "details": "",
                   "custom_fields": dict(p2["custom_fields"]["partial"])}
    fresh_record = {"id": 4, "title": "Filthy Anal Sluts", "details": "",
                    "custom_fields": {}}
    fresh_record2 = {"id": 5, "title": "Hot Asian Milfs", "details": "",
                     "custom_fields": {}}
    prefetched = translator3.prefetch("scene", [done_record, fresh_record, fresh_record2])
    check("只预取未完成的文本", prefetched == 2, str(prefetched))
    check("预取内容不含已翻译文本",
          "译:Beautiful Nurse" not in router3.calls, str(router3.calls))

    cache2.close()
    cache3.close()

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败:")
        for name in FAILED:
            print("  - %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
