#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Metadata Translator —— Stash 元数据翻译插件入口。

运行方式（都由 Stash 通过 stdin 传入 JSON）：
  1. 自动 Hook：场景 / 演员 / 工作室 / 标签被创建或更新时触发
  2. 手动任务：设置 -> 任务 -> 插件任务
  3. 前端按钮：通过 runPluginOperation 同步调用，翻译当前页面的实体

只依赖 Python 标准库。
"""
from __future__ import annotations

import json
import sys
import traceback

import cache as cache_mod
import detect
import fields
import log
from config import load_settings, cache_path
from engines import EngineError, Router, normalize_proxy
from stash_api import StashAPI, StashError

VERSION = "1.2.1"

# hook 类型前缀 -> 实体名
_HOOK_ENTITY = {
    "Scene": "scene",
    "Performer": "performer",
    "Studio": "studio",
    "Tag": "tag",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def entity_from_hook_type(hook_type):
    prefix = (hook_type or "").split(".")[0]
    return _HOOK_ENTITY.get(prefix)


def make_router(settings):
    return Router(settings.engine_chain, settings.engine_options())


def make_cache(settings, server_connection):
    path = cache_path(
        (server_connection or {}).get("PluginDir"),
        (server_connection or {}).get("Dir"),
    )
    return cache_mod.TranslationCache(path, settings["cache_enabled"])


class Translator:
    """字段级翻译：判定 -> 查缓存 -> 调引擎 -> 回填缓存。"""

    def __init__(self, settings, router, cache):
        self.settings = settings
        self.router = router
        self.cache = cache
        self.chain_key = "+".join(router.chain_names())
        self.stats = {"translated": 0, "skipped": 0, "cached": 0, "failed": 0, "engine_used": {}}

    def translate_text(self, text):
        """返回 (译文, 引擎名)；无需翻译返回 (None, None)。"""
        original = detect.strip_text(text)
        if not original:
            self.stats["skipped"] += 1
            return None, None

        if self.settings["skip_existing"] and not detect.needs_translation(
            original, self.settings.ambiguous_cjk
        ):
            self.stats["skipped"] += 1
            return None, None

        source = self.settings.source_lang
        target = self.settings.target_lang

        hit = self.cache.get(self.chain_key, source, target, original)
        if hit:
            translated = hit["text"]
            if translated and translated != original and not detect.looks_degenerate(translated):
                self.stats["cached"] += 1
                return translated, hit.get("engine") or self.chain_key
            # 缓存命中退化译文（v1.1.2 之前的老缓存可能是「相相相相…」这类垃圾）——
            # 删掉这条，落到下面的引擎重新翻译，绝不让缓存成为垃圾译文的重放通道。
            if translated and detect.looks_degenerate(translated):
                log.warning("缓存命中退化译文，已删除并重新翻译: %s" % detect.strip_text(translated)[:40])
                self.cache.delete(self.chain_key, source, target, original)
            else:
                # 缓存说「不用翻」（空译文或与原文相同）
                self.stats["skipped"] += 1
                return None, None

        try:
            result = self.router.translate(original, source, target)
        except EngineError as exc:
            self.stats["failed"] += 1
            log.error("翻译失败 [%s]: %s" % (original[:60], exc))
            return None, None

        # 引擎自己判定原文已经就是中文 —— 不要改写
        if not result.text or result.text == original:
            self.cache.put(self.chain_key, source, target, original, original, result.detected)
            self.stats["skipped"] += 1
            return None, None

        if result.attempts:
            log.debug("引擎降级：%s" % " | ".join(result.attempts))

        self.cache.put(self.chain_key, source, target, original, result.text, result.detected)
        self.stats["engine_used"][result.engine] = self.stats["engine_used"].get(result.engine, 0) + 1
        return result.text, result.engine

    def translate_record(self, entity, record, force=False):
        """翻译一条记录的启用字段，返回可直接提交的 translations 列表。"""
        if not record:
            return []

        items = []
        for item in fields.enabled_fields(entity, self.settings):
            key = item["key"]
            original = detect.strip_text(record.get(key))
            if not original:
                continue

            # 已经由本插件翻译过（原文与当前值不一致）就不再重复处理，
            # 这既能避免译文含拉丁字符时的反复翻译，也尊重用户的手工修改。
            if not force and self.settings["keep_original"]:
                stored = fields.original_of(record, key)
                if stored and detect.strip_text(stored) != original:
                    self.stats["skipped"] += 1
                    continue

            translated, engine = self.translate_text(original)
            if not translated:
                continue

            items.append({
                "field": key,
                "original": original,
                "translated": translated,
                "engine": engine or "",
                "label": item["label"],
            })
        return items


# --------------------------------------------------------------------------- #
# 流程：写入
# --------------------------------------------------------------------------- #
def write_entity(api, entity, record, items, settings, dry_run):
    """把译文写回 Stash，返回实际写入的字段列表。"""
    if not items:
        return []

    payload, written = fields.build_update_input(entity, record, items, settings)
    if payload is None:
        return []

    if dry_run:
        for item in items:
            log.info("[干跑] %s#%s %s: %s -> %s"
                     % (fields.label(entity), record.get("id"), item["label"],
                        item["original"][:60], item["translated"][:60]))
        return written

    api.call(fields.update_mutation(entity), {"input": payload})
    for item in items:
        log.info("%s#%s 已翻译%s：%s -> %s"
                 % (fields.label(entity), record.get("id"), item["label"],
                    item["original"][:60], item["translated"][:60]))
    return written


# --------------------------------------------------------------------------- #
# Hook：单个实体自动翻译
# --------------------------------------------------------------------------- #
def run_hook(api, settings, router, cache, hook_context):
    hook_type = (hook_context or {}).get("type") or ""
    entity = entity_from_hook_type(hook_type)
    record_id = (hook_context or {}).get("id")

    if not entity:
        return "忽略：%s 不在翻译范围内" % hook_type

    if not settings.auto_enabled(entity):
        return "忽略：%s 的自动翻译已关闭" % fields.label(entity)

    data = api.call(fields.one_query(entity), {"id": str(record_id)})
    record = fields.extract_one(entity, data)
    if not record:
        return "未找到 %s#%s" % (fields.label(entity), record_id)

    translator = Translator(settings, router, cache)
    items = translator.translate_record(entity, record)
    written = write_entity(api, entity, record, items, settings, dry_run=False)

    if not written:
        return "%s#%s 无需翻译" % (fields.label(entity), record_id)
    return "%s#%s 已翻译 %s" % (fields.label(entity), record_id, ", ".join(written))


# --------------------------------------------------------------------------- #
# 任务：批量翻译
# --------------------------------------------------------------------------- #
def run_batch(api, settings, router, cache, entities, dry_run):
    translator = Translator(settings, router, cache)
    batch_size = max(1, int(settings["batch_size"]))
    max_items = max(0, int(settings["max_items"]))

    summary = []
    for entity in entities:
        log.info("===== 开始处理：%s =====" % fields.label(entity))
        page = 1
        processed = 0
        written_total = 0
        total = 0

        while True:
            data = api.call(fields.list_query(entity), {
                "filter": fields.build_list_filter(page, batch_size),
            })
            total, records = fields.extract_list(entity, data)
            if not records:
                break

            for record in records:
                if max_items and processed >= max_items:
                    break
                processed += 1

                items = translator.translate_record(entity, record)
                if items:
                    try:
                        written_total += len(write_entity(
                            api, entity, record, items, settings, dry_run))
                    except StashError as exc:
                        log.error("%s#%s 写回失败: %s" % (fields.label(entity), record.get("id"), exc))
                log.progress_step(processed, min(total, max_items) if max_items else total)

            log.info("%s 第 %d 页完成（%d/%d）" % (fields.label(entity), page, processed, total))

            if max_items and processed >= max_items:
                log.info("已达到 max_items=%d 上限，停止" % max_items)
                break
            if page * batch_size >= total:
                break
            page += 1

        summary.append("%s %d 个（写入字段 %d）" % (fields.label(entity), processed, written_total))

    stats = translator.stats
    text = "；".join(summary) + "。命中缓存 %d 次，跳过 %d 项，失败 %d 项" % (
        stats["cached"], stats["skipped"], stats["failed"])
    if dry_run:
        text += "【干跑预览，未写回数据库】"
    log.info(text)
    return text


# --------------------------------------------------------------------------- #
# 任务：回滚
# --------------------------------------------------------------------------- #
def run_rollback(api, settings, dry_run):
    summary = []
    for entity in fields.ALL_ENTITIES:
        page = 1
        restored = 0
        processed = 0
        batch_size = max(1, int(settings["batch_size"]))

        while True:
            data = api.call(fields.list_query(entity), {
                "filter": fields.build_list_filter(page, batch_size),
            })
            total, records = fields.extract_list(entity, data)
            if not records:
                break

            for record in records:
                processed += 1
                payload, keys = fields.build_rollback_input(entity, record)
                if payload is None:
                    continue
                if dry_run:
                    log.info("[干跑] 将还原 %s#%s 的 %s"
                             % (fields.label(entity), record.get("id"), ", ".join(keys)))
                else:
                    api.call(fields.update_mutation(entity), {"input": payload})
                    log.info("%s#%s 已还原 %s"
                             % (fields.label(entity), record.get("id"), ", ".join(keys) or "标记"))
                restored += 1
                log.progress_step(processed, total)

            if page * batch_size >= total:
                break
            page += 1

        summary.append("%s 还原 %d 个" % (fields.label(entity), restored))

    text = "；".join(summary)
    if dry_run:
        text += "【干跑预览，未写回】"
    log.info(text)
    return text


# --------------------------------------------------------------------------- #
# 任务：测试引擎
# --------------------------------------------------------------------------- #
def run_selftest(settings, router):
    # 先把「实际生效的配置」摆出来。排查问题时这一屏能省掉很多来回：
    # 引擎被谁覆盖了、代理串是不是写错了、凭证到底读没读到，一眼可见。
    log.info("设置来源：%s" % describe_settings_source(settings))
    raw_proxy = (settings["http_proxy"] or "").strip()
    if raw_proxy:
        fixed = normalize_proxy(raw_proxy)
        if fixed != raw_proxy:
            log.warning("http_proxy：%s -> 已自动修正为 %s" % (raw_proxy, fixed))
        else:
            log.info("http_proxy：%s" % fixed)
    else:
        log.info("http_proxy：未设置")
    log.info("引擎优先级：%s" % " -> ".join(router.chain_names() or ["(无)"]))
    log.info("参数：超时 %ss，请求间隔 %sms，瞬时错误重试 %d 次，熔断阈值 %s"
             % (settings["timeout_s"], settings["rate_limit_ms"], settings["retry_times"],
                settings["engine_skip_after"] or "关闭"))

    if not router.has_engine():
        text = "没有可用引擎，请检查设置里的引擎与凭证"
        log.error(text)
        return text

    for label_text, ok, detail, elapsed in router.test_all():
        if ok:
            log.info("[成功] %s（%dms）：%s" % (label_text, elapsed, detail))
        else:
            log.warning("[失败] %s（%dms）：%s" % (label_text, elapsed, detail))

    return "引擎测试完成，详见日志"


def describe_settings_source(settings):
    """说明配置到底是从哪儿来的 —— 这是「改完没生效」类问题最常见的根因。"""
    keys = getattr(settings, "read_keys", None) or []
    source = getattr(settings, "source", "defaults")
    if source == "settings":
        return "设置页读到 %d 项（%s）" % (len(keys), ", ".join(sorted(keys)))
    if source == "empty":
        return "设置页返回了插件但内容为空，全部走内置默认值"
    return "未能从设置页读到配置，全部走内置默认值"


# --------------------------------------------------------------------------- #
# 前端调用：翻译单条
# --------------------------------------------------------------------------- #
def run_single(api, settings, router, cache, args):
    entity = (args.get("entity") or "").strip().lower()
    record_id = args.get("id")

    if entity not in fields.ENTITY_SPECS or not record_id:
        return {"error": "参数不完整，需要 entity 与 id"}

    data = api.call(fields.one_query(entity), {"id": str(record_id)})
    record = fields.extract_one(entity, data)
    if not record:
        return {"error": "未找到 %s#%s" % (fields.label(entity), record_id)}

    translator = Translator(settings, router, cache)
    only_field = (args.get("field") or "").strip()
    if only_field:
        items = [i for i in translator.translate_record(entity, record) if i["field"] == only_field]
        if not items:
            return {"result": "", "message": "该字段无需翻译"}
        item = items[0]
        return {"result": item["translated"], "engine": item["engine"],
                "original": item["original"]}

    items = translator.translate_record(entity, record)
    if not items:
        return {"result": "", "message": "无需翻译（已是中文或内容为空）"}

    write = str(args.get("write", "true")).strip().lower() not in ("0", "false", "no")
    written = write_entity(api, entity, record, items, settings, dry_run=not write)

    return {
        "result": " | ".join("%s: %s" % (i["label"], i["translated"][:80]) for i in items),
        "written": written if write else [],
        "count": len(items),
    }


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _force_utf8():
    """容器里本来就是 UTF-8；这里主要是让 Windows 上手工调试也能正常输出中文。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _force_utf8()

    raw = sys.stdin.read()
    input_data = {}
    if raw and raw.strip():
        try:
            input_data = json.loads(raw)
        except ValueError as exc:
            print(json.dumps({"error": "无法解析输入 JSON: %s" % exc}))
            return 1

    args = input_data.get("args") or {}
    server_connection = input_data.get("server_connection") or {}
    hook_context = args.get("hookContext")

    mode = str(args.get("mode") or ("hook" if hook_context else "all")).strip().lower()
    dry_run = str(args.get("dry_run", "")).strip().lower() in ("1", "true", "yes", "on")

    # 只有任务模式才上报进度（Hook 没有进度通道）
    log.enable_progress(not hook_context and mode != "single")

    log.info("Metadata Translator v%s 启动，模式=%s" % (VERSION, mode))

    api = StashAPI(server_connection)

    try:
        settings = load_settings(api, args)
    except Exception as exc:
        log.warning("读取插件设置失败，使用默认值：%s" % exc)
        from config import Settings
        settings = Settings()

    # 清缓存不需要引擎
    if mode == "clearcache":
        c = make_cache(settings, server_connection)
        removed = c.clear()
        c.close()
        text = "已清空 %d 条翻译缓存" % removed
        log.info(text)
        print(json.dumps({"output": text}, ensure_ascii=False))
        return 0

    router = make_router(settings)
    cache = make_cache(settings, server_connection)

    try:
        if hook_context:
            result = run_hook(api, settings, router, cache, hook_context)
        elif mode == "selftest":
            result = run_selftest(settings, router)
        elif mode == "rollback":
            result = run_rollback(api, settings, dry_run)
        elif mode == "single":
            result = run_single(api, settings, router, cache, args)
        elif mode in ("all", "scene", "performer", "studio", "tag"):
            entities = fields.ALL_ENTITIES if mode == "all" else [mode]
            result = run_batch(api, settings, router, cache, entities, dry_run)
        else:
            result = "未知模式：%s" % mode
            log.warning(result)

        print(json.dumps({"output": result}, ensure_ascii=False))
        return 0

    except (StashError, EngineError) as exc:
        log.error(str(exc))
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        log.error(traceback.format_exc())
        print(json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)}, ensure_ascii=False))
        return 1
    finally:
        cache.close()


if __name__ == "__main__":
    sys.exit(main())
