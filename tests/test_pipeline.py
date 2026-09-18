#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端流程测试（不依赖外网、不依赖真实 Stash）。

做法：
  * 起一个假的 Stash GraphQL 服务，内存里存场景/演员/工作室/标签，
    实现插件真正会调用的那几个查询与 mutation（含 custom_fields 的 partial / remove 语义）。
  * 把引擎层替换成桩，返回确定性的「中文」译文，从而验证的是流程而不是翻译质量。
  * 以真实方式喂 stdin、捕获 stdout，覆盖：配置读取、干跑、写回、幂等、
    Hook 分发、标签别名模式、原文回滚、缓存命中。

用法：
    python3 tests/test_pipeline.py
退出码 0 表示全部通过。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(os.path.dirname(HERE), "src", "translateMetadata")
# 契约模块在 tests/ 目录下，也要能被 import
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, PLUGIN_DIR)

import _schema  # noqa: E402
import cache as cache_mod  # noqa: E402
import config as config_mod  # noqa: E402
import fields  # noqa: E402
import log  # noqa: E402
import translateMetadata as plugin  # noqa: E402
from config import Settings  # noqa: E402
import engines as engines_mod  # noqa: E402
from engines import EngineError, TranslateResult  # noqa: E402
from stash_api import StashAPI  # noqa: E402

PLUGIN_ID = "translateMetadata"

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s %s" % (name, detail))


# --------------------------------------------------------------------------- #
# 假 Stash：内存数据库 + GraphQL 网关
# --------------------------------------------------------------------------- #
QUERY_ON_ONE = {
    "scene": "findScene",
    "performer": "findPerformer",
    "studio": "findStudio",
    "tag": "findTag",
}
QUERY_ON_LIST = {
    "scene": ("findScenes", "scenes"),
    "performer": ("findPerformers", "performers"),
    "studio": ("findStudios", "studios"),
    "tag": ("findTags", "tags"),
}
MUTATION_ON = {
    "sceneUpdate": "scene",
    "performerUpdate": "performer",
    "studioUpdate": "studio",
    "tagUpdate": "tag",
}


class FakeStash:
    def __init__(self):
        self.db = {"scene": [], "performer": [], "studio": [], "tag": []}
        self.settings = {
            "engine_fallback": "edge",
            "target_lang": "zh-CN",
            "source_lang": "auto",
            "rate_limit_ms": 0,
            "cache_enabled": True,
            "keep_original": True,
            "tag_name_mode": "alias",
            "translate_names": "true",
        }
        self.mutations = 0

    # -- 造数据 ------------------------------------------------------------ #
    def seed(self, entity, records):
        for index, record in enumerate(records, start=1):
            item = {"id": str(index), "custom_fields": {}}
            item.update(record)
            self.db[entity].append(item)


def make_handler(state: FakeStash):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            query = body.get("query") or ""
            variables = body.get("variables") or {}
            try:
                data = dispatch(state, query, variables)
                payload = {"data": data}
            except Exception as exc:  # 让插件看到 GraphQL 错误
                payload = {"errors": [{"message": str(exc)}]}
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def dispatch(state, query, variables):
    # 先按真实 Stash 的规矩校验一遍请求。
    # 插件曾经因为拼接类型名时踩了运算符优先级，发出 "SUpdateInputcene"
    # 这种畸形类型，被 Stash 以 GRAPHQL_VALIDATION_FAILED 拒掉。
    # 假服务以前只按正则匹配 mutation 名字，畸形的类型名照样当成功处理，
    # 于是这个 bug 一路骗过了全部测试。这里补上校验。
    _schema.validate(query)

    # 设置读取
    if "configuration" in query and "plugins" in query:
        return {"configuration": {"plugins": {PLUGIN_ID: state.settings}}}

    # 单个实体
    for entity, field in QUERY_ON_ONE.items():
        match = re.search(r"\b%s\s*\(\s*id\s*:" % field, query)
        if match and field in query:
            target = str(variables.get("id"))
            for record in state.db[entity]:
                if record["id"] == target:
                    return {field: dict(record)}
            return {field: None}

    # 列表
    for entity, (field, list_key) in QUERY_ON_LIST.items():
        if re.search(r"\b%s\s*\(" % field, query):
            records = sorted(state.db[entity], key=lambda r: int(r["id"]))
            find_filter = variables.get("filter") or {}
            page = int(find_filter.get("page") or 1)
            per_page = int(find_filter.get("per_page") or 100)
            start = (page - 1) * per_page
            chunk = records[start:start + per_page]
            return {field: {"count": len(records), list_key: [dict(r) for r in chunk]}}

    # 更新
    for mutation_name, entity in MUTATION_ON.items():
        if re.search(r"\b%s\s*\(" % mutation_name, query):
            payload = variables.get("input") or {}
            target = str(payload.get("id"))
            for record in state.db[entity]:
                if record["id"] != target:
                    continue
                custom_in = payload.get("custom_fields")
                for key, value in payload.items():
                    if key in ("id", "custom_fields"):
                        continue
                    record[key] = value
                if isinstance(custom_in, dict):
                    if "partial" in custom_in:
                        record["custom_fields"].update(custom_in["partial"] or {})
                    if "remove" in custom_in:
                        for key in custom_in["remove"] or []:
                            record["custom_fields"].pop(key, None)
                    if "full" in custom_in:
                        record["custom_fields"] = dict(custom_in["full"] or {})
                state.mutations += 1
                return {mutation_name: {"id": target}}
            raise ValueError("no such record: %s#%s" % (entity, target))

    raise ValueError("未处理的查询: %s" % query[:120])


# --------------------------------------------------------------------------- #
# 桩引擎：确定性中文译文
# --------------------------------------------------------------------------- #
class StubEngine:
    def __init__(self, with_latin=False):
        self.calls = 0
        self.with_latin = with_latin

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        self.calls += 1
        if self.with_latin:
            # 故意让译文含拉丁字母，用来验证 tr_src 幂等保护是否生效
            return "Chinese(%s)" % text, "en"
        return "译文%s" % self.calls, "en"

    def translate(self, text, source="auto", target="zh-CN"):
        return self.translate_detailed(text, source, target)[0]


class StubRouter:
    def __init__(self, engine):
        self.engine = engine
        self.missing = []

    def has_engine(self):
        return True

    def chain_names(self):
        return ["edge"]

    def translate(self, text, source="auto", target="zh-CN"):
        translated, detected = self.engine.translate_detailed(text, source, target)
        return TranslateResult(translated, "edge", detected, text)

    def test_all(self, sample="Hello"):
        return [("EDGE（桩）", True, "译文", 1)]


# --------------------------------------------------------------------------- #
# 驱动插件
# --------------------------------------------------------------------------- #
def run_plugin(state, port, args, hook_context=None):
    """以真实方式运行插件入口，返回 (stdout 解析结果, stderr 文本)。"""
    import io

    payload = {
        "server_connection": {
            "Scheme": "http",
            "Host": "127.0.0.1",
            "Port": port,
            "SessionCookie": {"Name": "session", "Value": "fake"},
            "Dir": "/tmp",
            "PluginDir": PLUGIN_DIR,
        },
        "args": dict(args),
    }
    if hook_context:
        payload["args"]["hookContext"] = hook_context

    old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    try:
        sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
        sys.stdout, sys.stderr = out, err
        code = plugin.main()
    finally:
        sys.stdin, sys.stdout, sys.stderr = old_stdin, old_stdout, old_stderr

    text = out.getvalue().strip()
    parsed = json.loads(text.splitlines()[-1]) if text else {}
    return code, parsed, err.getvalue()


def install_stub(engine):
    """把引擎层替换成桩。"""
    plugin.make_router = lambda settings: StubRouter(engine)


class FakeApi:
    """假的 StashAPI：只回 configuration.plugins，用来验证设置读取与键名兼容。"""

    def __init__(self, values):
        self.values = values

    def call(self, query, variables=None):
        return {"configuration": {"plugins": {PLUGIN_ID: self.values}}}


# --------------------------------------------------------------------------- #
# 主测试
# --------------------------------------------------------------------------- #
def main():
    log.enable_progress(False)

    # 每次从干净的缓存开始
    cache_file = os.path.join(PLUGIN_DIR, "translate-cache.sqlite3")
    if os.path.exists(cache_file):
        os.remove(cache_file)

    state = FakeStash()
    state.seed("scene", [
        {"title": "Beautiful Nurse", "details": "A story about a nurse.", "custom_fields": {}},
        {"title": "纯中文标题", "details": "已经是中文的简介", "custom_fields": {}},
    ])
    state.seed("performer", [
        {"name": "Akiho Yoshizawa", "details": "Famous performer", "custom_fields": {}},
    ])
    state.seed("studio", [
        {"name": "Tokyo Studio", "details": "A studio in Tokyo", "custom_fields": {}},
    ])
    state.seed("tag", [
        {"name": "Nurse", "description": "medical role", "aliases": [], "custom_fields": {}},
    ])

    server = HTTPServer(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("假 Stash 服务已启动: 127.0.0.1:%d\n" % port)

    engine = StubEngine()
    install_stub(engine)

    print("1) 测试引擎（selftest）")
    code, result, _ = run_plugin(state, port, {"mode": "selftest"})
    check("selftest 返回 0", code == 0, str(code))
    check("selftest 有输出", bool(result.get("output")), str(result))

    print("\n2) 干跑预览（不写回）")
    before = json.dumps(state.db, ensure_ascii=False, sort_keys=True)
    code, result, _ = run_plugin(state, port, {"mode": "all", "dry_run": "true"})
    check("干跑返回 0", code == 0, str(code))
    check("干跑未改动数据库", before == json.dumps(state.db, ensure_ascii=False, sort_keys=True))
    check("干跑结果标注", "干跑" in (result.get("output") or ""), str(result))

    print("\n3) 正式批量翻译")
    code, result, _ = run_plugin(state, port, {"mode": "all"})
    check("批量返回 0", code == 0, str(code))
    scene0 = state.db["scene"][0]
    check("场景标题已翻译", scene0["title"].startswith("译文"), scene0["title"])
    check("场景简介已翻译", scene0["details"].startswith("译文"), scene0["details"])
    check("原文已存入 custom_fields",
          scene0["custom_fields"].get("tr_src_title") == "Beautiful Nurse",
          str(scene0["custom_fields"]))
    check("记录了所用引擎",
          scene0["custom_fields"].get("tr_engine_title") == "edge",
          str(scene0["custom_fields"]))
    check("纯中文场景未被改动",
          state.db["scene"][1]["title"] == "纯中文标题",
          state.db["scene"][1]["title"])
    check("演员姓名已翻译", state.db["performer"][0]["name"].startswith("译文"))
    check("工作室名称已翻译", state.db["studio"][0]["name"].startswith("译文"))

    print("\n4) 标签别名模式（默认 alias）")
    tag = state.db["tag"][0]
    check("标签原名保留", tag["name"] == "Nurse", tag["name"])
    check("中文别名已追加", len(tag.get("aliases") or []) == 1, str(tag.get("aliases")))

    print("\n5) 幂等：再跑一次不应有新的翻译")
    calls_before = engine.calls
    mutations_before = state.mutations
    code, result, _ = run_plugin(state, port, {"mode": "all"})
    check("二次运行返回 0", code == 0, str(code))
    check("二次运行未再调用引擎", engine.calls == calls_before,
          "calls %d -> %d" % (calls_before, engine.calls))
    check("二次运行未产生写操作", state.mutations == mutations_before,
          "mutations %d -> %d" % (mutations_before, state.mutations))

    print("\n6) Hook：场景更新后自动翻译")
    state.db["scene"].append({
        "id": "99",
        "title": "Freshly Scraped Scene",
        "details": "Scraper just wrote this.",
        "custom_fields": {},
    })
    code, result, _ = run_plugin(
        state, port, {"mode": "hook"},
        hook_context={"id": 99, "type": "Scene.Update.Post", "input": {}, "inputFields": ["title"]},
    )
    check("hook 返回 0", code == 0, str(code))
    check("hook 已翻译新场景",
          state.db["scene"][2]["title"].startswith("译文"),
          state.db["scene"][2]["title"])
    check("hook 输出可读", "场景" in (result.get("output") or ""), str(result))

    print("\n7) Hook：不在范围内的实体应被忽略")
    code, result, _ = run_plugin(
        state, port, {"mode": "hook"},
        hook_context={"id": 1, "type": "Image.Update.Post", "input": {}},
    )
    check("Image 钩子被忽略", "忽略" in (result.get("output") or ""), str(result))

    print("\n8) 缓存命中：同文本不重复请求")
    code, _, _ = run_plugin(state, port, {"mode": "clearcache"})
    check("清缓存返回 0", code == 0, str(code))

    # 先跑一次把缓存填上
    state.db["scene"][0]["title"] = "Beautiful Nurse"
    state.db["scene"][0]["custom_fields"] = {"tr_src_title": "Beautiful Nurse"}
    run_plugin(state, port, {"mode": "scene"})
    check("缓存已写入", os.path.exists(cache_file))

    # 把字段改回英文，模拟「又需要翻译」，这次应命中缓存而不调引擎
    state.db["scene"][0]["title"] = "Beautiful Nurse"
    calls_before = engine.calls
    code, result, _ = run_plugin(state, port, {"mode": "scene"})
    check("缓存命中不再调引擎", engine.calls == calls_before,
          "calls %d -> %d" % (calls_before, engine.calls))
    check("缓存命中被统计", "命中缓存" in (result.get("output") or ""), str(result))

    print("\n8b) 缓存里的退化译文：命中即删除并重新翻译")
    # 复现 v1.1.2 之前的问题：老版本把「相相相相…」写进了缓存，
    # 升级后缓存命中会把垃圾原样回放。现在必须删条目、重新调引擎。
    run_plugin(state, port, {"mode": "clearcache"})
    state.db["scene"][0]["title"] = "Beautiful Nurse"
    state.db["scene"][0]["details"] = "A story about a nurse."
    state.db["scene"][0]["custom_fields"] = {}
    poisoned = cache_mod.TranslationCache(cache_file, True)
    poisoned.put("edge", "auto", "zh-CN", "Beautiful Nurse", "相" * 40, "en")
    poisoned.close()
    check("退化缓存条目已就位", True)

    calls_before = engine.calls
    code, result, _ = run_plugin(state, port, {"mode": "scene"})
    # 标题命中退化缓存重翻 +1；简介无缓存条目也要翻 +1
    check("命中退化缓存后重新调用了引擎", engine.calls == calls_before + 2,
          "calls %d -> %d" % (calls_before, engine.calls))
    check("标题没有被垃圾回放污染",
          "相相" not in state.db["scene"][0]["title"],
          state.db["scene"][0]["title"])
    check("标题写入了正常译文",
          state.db["scene"][0]["title"].startswith("译文"),
          state.db["scene"][0]["title"])
    reopened = cache_mod.TranslationCache(cache_file, True)
    row = reopened.get("edge", "auto", "zh-CN", "Beautiful Nurse")
    reopened.close()
    check("退化缓存条目已被删除", row is None or "相相" not in (row.get("text") or ""),
          str(row))

    print("\n9) 回滚：还原原文并清理标记")
    state.db["scene"][0]["title"] = "译文1"
    state.db["scene"][0]["details"] = "译文2"
    state.db["scene"][0]["custom_fields"] = {
        "tr_src_title": "Beautiful Nurse",
        "tr_engine_title": "edge",
        "tr_src_details": "A story about a nurse.",
        "tr_engine_details": "edge",
    }
    code, result, _ = run_plugin(state, port, {"mode": "rollback"})
    check("回滚返回 0", code == 0, str(code))
    check("标题已还原", state.db["scene"][0]["title"] == "Beautiful Nurse",
          state.db["scene"][0]["title"])
    check("简介已还原",
          state.db["scene"][0]["details"] == "A story about a nurse.",
          state.db["scene"][0]["details"])
    check("标记键已清理", state.db["scene"][0]["custom_fields"] == {},
          str(state.db["scene"][0]["custom_fields"]))

    print("\n10) 含拉丁字母的译文：靠 tr_src 保护实现幂等")
    latin_engine = StubEngine(with_latin=True)
    install_stub(latin_engine)
    state.db["scene"][1]["title"] = "Some English Title"
    state.db["scene"][1]["custom_fields"] = {}
    code, _, _ = run_plugin(state, port, {"mode": "scene"})
    first = state.db["scene"][1]["title"]
    check("含拉丁译文已写入", first.startswith("Chinese(") or first.startswith("译文"), first)
    calls_before = latin_engine.calls
    mutations_before = state.mutations
    code, _, _ = run_plugin(state, port, {"mode": "scene"})
    check("二次运行不再重复翻译", latin_engine.calls == calls_before,
          "calls %d -> %d" % (calls_before, latin_engine.calls))
    check("二次运行不再写库", state.mutations == mutations_before)

    print("\n11) 引擎全部不可用时应优雅报错")
    plugin.make_router = lambda settings: StubRouter(engine)
    broken = StubEngine()
    broken.translate_detailed = lambda *a, **k: (_ for _ in ()).throw(EngineError("全部挂了"))

    class BrokenRouter(StubRouter):
        def translate(self, text, source="auto", target="zh-CN"):
            raise EngineError("所有引擎均失败 -> edge: 全部挂了")

    plugin.make_router = lambda settings: BrokenRouter(broken)
    state.db["performer"][0]["details"] = "needs translation"
    code, result, err = run_plugin(state, port, {"mode": "performer"})
    check("引擎故障时返回 0（任务不崩）", code == 0, str(code))
    check("统计里记录了失败", "失败" in (result.get("output") or ""), str(result))

    install_stub(engine)

    print("\n12) 设置读取：旧插件键名兼容")
    # 用户手里往往已经存着 translateXxx 命名的一套凭证，这里验证能自动认领
    alias_cfg = {
        "translateAlibabaAccessKey": "LTAI-test",
        "translateAlibabaAccessSecret": "secret-test",
        "translateAlibabaRegion": "https://mt.cn-hangzhou.aliyuncs.com",
        "translateTencentSecretId": "AKID-test",
        "translateTencentSecretKey": "key-test",
        "translateTencentRegion": "https://tmt.tencentcloudapi.com",
        "translateLibretranslateUrl": "http://192.168.3.96:5353/translate",
        "translateLibretranslateApiKey": "uuid-test",
    }
    opts = config_mod.load_settings(FakeApi(alias_cfg), {}).engine_options()
    check("旧键名 translateAlibabaAccessKey 被读取", opts["alibaba_access_key"] == "LTAI-test")
    check("旧键名 translateAlibabaAccessSecret 被读取", opts["alibaba_access_secret"] == "secret-test")
    check("旧键名 translateAlibabaRegion 被读取",
          opts["alibaba_region"] == "https://mt.cn-hangzhou.aliyuncs.com")
    check("旧键名 translateTencentSecretId 被读取", opts["tencent_secret_id"] == "AKID-test")
    check("旧键名 translateTencentSecretKey 被读取", opts["tencent_secret_key"] == "key-test")
    check("旧键名 translateLibretranslateUrl 被读取",
          opts["libretranslate_url"] == "http://192.168.3.96:5353/translate")
    check("旧键名 translateLibretranslateApiKey 被读取", opts["libretranslate_api_key"] == "uuid-test")

    # translateTencentRegion 存的是接口地址而不是地域，故意不映射
    check("translateTencentRegion 不被误当作地域",
          opts["tencent_region"] == "ap-guangzhou", opts["tencent_region"])

    # 本插件自己的键优先级更高
    own = config_mod.load_settings(FakeApi(dict(alias_cfg, alibaba_access_key="OWN")), {}).engine_options()
    check("本插件键名优先于旧键名", own["alibaba_access_key"] == "OWN")

    # 任务传入的 args 优先级介于「设置页」与默认值之间：这里设置页为空，应由 args 生效
    args_only = config_mod.load_settings(FakeApi({}), {"alibaba_access_key": "FROM-ARGS"}).engine_options()
    check("args 可覆盖空设置", args_only["alibaba_access_key"] == "FROM-ARGS")

    # 什么设置都没有也要能跑起来（退回内置默认）
    blank = config_mod.load_settings(FakeApi({}), {})
    check("无任何设置时回退内置默认引擎链",
          blank.engine_chain == list(config_mod.DEFAULT_ENGINE_CHAIN), str(blank.engine_chain))
    check("默认链非空且不含已下线的 edge（要用得手动加）",
          bool(blank.engine_chain) and "edge" not in blank.engine_chain,
          str(blank.engine_chain))
    check("无任何设置时阿里云凭证为空",
          not blank.engine_options()["alibaba_access_key"])
    check("超时 / 重试不再由设置页下发（改由各引擎默认值兜底）",
          "timeout_s" not in blank.engine_options()
          and "retry_times" not in blank.engine_options()
          and "retry_backoff_ms" not in blank.engine_options(),
          str(sorted(blank.engine_options())))
    check("机翻引擎默认 20s / 重试 1 次",
          engines_mod.BaseEngine.default_timeout_s == 20
          and engines_mod.BaseEngine.default_retry_times == 1)
    check("AI 引擎默认超时更长（120s）",
          engines_mod.OpenAIEngine.default_timeout_s == 120)
    check("任务参数显式给了才下发超时 / 重试",
          config_mod.load_settings(FakeApi({}), {"timeout_s": "45", "retry_times": "0"})
          .engine_options().get("timeout_s") == 45
          and config_mod.load_settings(FakeApi({}), {"retry_times": "0"})
          .engine_options().get("retry_times") == 0)
    check("默认开启引擎熔断", blank.engine_options()["engine_skip_after"] == 3)

    # v1.2.6：主翻译引擎设置已取消，只剩一条引擎链
    chained = config_mod.load_settings(FakeApi({"engine_fallback": "deepl, google ; mymemory，baidu"}), {})
    check("引擎链按顺序生效",
          chained.engine_chain == ["deepl", "google", "mymemory", "baidu"], str(chained.engine_chain))
    check("引擎链去重",
          config_mod.load_settings(FakeApi({"engine_fallback": "google,google, google"}), {}).engine_chain
          == ["google"])
    check("引擎链只有分隔符时回退默认链",
          config_mod.load_settings(FakeApi({"engine_fallback": " , ;; ，"}), {}).engine_chain
          == list(config_mod.DEFAULT_ENGINE_CHAIN))
    check("引擎链里的未知名字保留原样（由 Router 过滤并在自检里列出）",
          config_mod.load_settings(FakeApi({"engine_fallback": "tengcent,google"}), {}).engine_chain
          == ["tengcent", "google"])

    # 兼容：老配置里单独的 engine（主引擎）键
    legacy = config_mod.load_settings(FakeApi({"engine": "deepl"}), {})
    check("旧版主引擎键在链留空时被当作链首",
          legacy.engine_chain == ["deepl"], str(legacy.engine_chain))
    both = config_mod.load_settings(FakeApi({"engine": "edge", "engine_fallback": "mymemory,google"}), {})
    check("链一旦填了，旧的主引擎键就被忽略",
          both.engine_chain == ["mymemory", "google"], str(both.engine_chain))

    # 自检任务要能说清"这条链是从哪来的"（默认值在代码里，不看日志根本猜不到）
    check("引擎链来源说明标出「设置页」",
          config_mod.describe_engine_chain(chained).startswith("设置页："),
          config_mod.describe_engine_chain(chained))
    check("链路留空时来源说明标出内置默认",
          "内置默认" in config_mod.describe_engine_chain(blank),
          config_mod.describe_engine_chain(blank))
    check("沿用旧版主引擎键时来源说明里点出来",
          "旧版" in config_mod.describe_engine_chain(legacy),
          config_mod.describe_engine_chain(legacy))

    # 设置来源要能被看出来，这是「改完没生效」类问题的第一手线索
    check("无设置时标注来源为设置页内容为空", blank.source == "empty", blank.source)
    filled = config_mod.load_settings(FakeApi({"tencent_secret_id": "x"}), {})
    check("有设置时标注来源为设置页", filled.source == "settings", filled.source)
    check("来源里带上生效的键名",
          "tencent_secret_id" in filled.read_keys, str(filled.read_keys))
    check("旧版主引擎键被读出时也标出来源",
          any("engine" in k for k in legacy.read_keys), str(legacy.read_keys))

    # 读设置失败（接口报错）不应中断任务
    class BoomApi:
        def call(self, query, variables):
            raise RuntimeError("configuration 查询失败")

    safe = config_mod.load_settings(BoomApi(), {})
    check("读设置失败时退回默认值",
          safe.engine_chain == list(config_mod.DEFAULT_ENGINE_CHAIN), str(safe.engine_chain))
    check("读设置失败时标注来源不可读", safe.source == "unreadable", safe.source)

    # v1.2.7：跳过策略（原来的 skip_existing + ambiguous_cjk 合并成一个键）
    check("默认跳过策略 = smart（中文与纯汉字都跳过）",
          blank.skip_policy == "smart" and blank.skip_existing is True
          and blank.ambiguous_cjk == "skip", str(blank.skip_policy))
    detect_mode = config_mod.load_settings(FakeApi({"skip_policy": "detect"}), {})
    check("detect：仍是中文跳过，但纯汉字交给引擎判断",
          detect_mode.skip_existing is True and detect_mode.ambiguous_cjk == "detect")
    none_mode = config_mod.load_settings(FakeApi({"skip_policy": "none"}), {})
    check("none：一律翻译（连已是中文的也送引擎）", none_mode.skip_existing is False)
    check("填错时按 smart 处理（宁可少翻也别把中文再翻一遍）",
          config_mod.load_settings(FakeApi({"skip_policy": "Smart?"}), {}).skip_policy == "smart")
    check("老配置 skip_existing=false 映射成 none",
          config_mod.load_settings(FakeApi({"skip_existing": "false"}), {}).skip_policy == "none")
    check("老配置 ambiguous_cjk=detect 映射成 detect",
          config_mod.load_settings(FakeApi({"skip_existing": "true",
                                            "ambiguous_cjk": "detect"}), {}).skip_policy == "detect")
    check("老键被读到时标出来源",
          any("skip_policy" in k for k in
              config_mod.load_settings(FakeApi({"skip_existing": "false"}), {}).read_keys),
          str(config_mod.load_settings(FakeApi({"skip_existing": "false"}), {}).read_keys))

    print("\n13) max_items 名额：跳过的已翻译记录不占名额（v1.2.4 回归）")
    # 背景：列表按 id ASC 分页，旧版把「看过的记录」全计入 max_items，
    # 于是每次任务都在重复扫描最前面那批已翻译记录，永远到不了后面。
    state.db["scene"] = []
    state.seed("scene", [
        {"title": "已经翻译过的场景一", "details": "这是中文简介一"},
        {"title": "已经翻译过的场景二", "details": "这是中文简介二"},
        {"title": "已经翻译过的场景三", "details": "这是中文简介三"},
        {"title": "English Scene Four", "details": "Needs translation four."},
        {"title": "English Scene Five", "details": "Needs translation five."},
    ])
    state.settings["max_items"] = "2"
    state.settings["batch_size"] = "2"
    calls_before = engine.calls
    mutations_before = state.mutations
    code, result, _ = run_plugin(state, port, {"mode": "scene"})
    output = result.get("output") or ""
    check("跳过 3 个已翻译后仍翻到了 2 个未翻译的", "翻译 2 个" in output, output)
    check("全程扫描了全部 5 条", "扫描 5 个" in output, output)
    check("只有未翻译的记录调了引擎（2 条 × 标题+简介）", engine.calls == calls_before + 4,
          "calls %d -> %d" % (calls_before, engine.calls))
    check("前 3 条中文记录未被改写", state.mutations == mutations_before + 2,
          "mutations %d -> %d" % (mutations_before, state.mutations))
    check("场景#4 已被翻译", state.db["scene"][3]["title"] == "译文：English Scene Four"
          or "译文" in state.db["scene"][3]["title"],
          state.db["scene"][3]["title"])
    check("场景#5 已被翻译", "译文" in state.db["scene"][4]["title"],
          state.db["scene"][4]["title"])
    state.settings["max_items"] = "0"
    state.settings["batch_size"] = "100"

    # ------------------------------------------------------------------ #
    print("\n14) 默认值初始化：空缺的键补写进设置页（v1.2.8）")
    # 背景：Stash 插件清单声明不了设置项默认值，没填过的键在设置页显示为空
    # （NUMBER 渲染成 0）。v1.2.8 起用 configurePlugin 把默认值补写进去，
    # UI 上就能看到真实默认值。关键约束：只补空缺，绝不覆盖用户已填的值；
    # configurePlugin 是整体替换语义，必须先读全量再合并写回。

    class FakeConfigApi:
        """既会答 configuration 查询、也会记 configurePlugin 写入的假接口。"""

        def __init__(self, stored):
            self.stored = dict(stored)
            self.mutations = []

        def call(self, query, variables=None):
            if "mutation" in query:
                self.mutations.append(dict(variables or {}))
                # 整体替换语义：input 就是插件配置的新全量
                self.stored = dict(variables["input"])
                return {"configurePlugin": self.stored}
            return {"configuration": {"plugins": {config_mod.PLUGIN_ID: self.stored}}}

    # 全空配置（带一个旧版兼容键）→ 补种全部 INIT 键，且写回保住旧键
    api = FakeConfigApi({"skip_existing": "false"})
    seeded = config_mod.initialize_default_settings(api)
    check("空配置时补种了一整批默认值", len(seeded) >= 15, str(sorted(seeded)))
    check("引擎链默认值被种进去",
          seeded.get("engine_fallback") == ",".join(config_mod.DEFAULT_ENGINE_CHAIN),
          seeded.get("engine_fallback"))
    check("请求间隔默认 300", seeded.get("rate_limit_ms") == "300", str(seeded.get("rate_limit_ms")))
    check("接口地址取自引擎类的官方地址",
          seeded.get("google_url") == "https://translate.googleapis.com/translate_a/single",
          seeded.get("google_url"))
    check("布尔默认值是真布尔（前端复选框对字符串 false 按真值渲染）",
          seeded.get("keep_original") is True and seeded.get("translate_names") is False,
          "%r / %r" % (seeded.get("keep_original"), seeded.get("translate_names")))
    check("凭证类键不种（空着就该空着）",
          not any(k in seeded for k in ("baidu_appid", "openai_api_key", "http_proxy")),
          str(sorted(seeded)))
    # 写回的 input 是「raw 全量 + 种子」，旧版兼容键不能丢
    written = api.mutations[0]["input"]
    check("写回时保留了 raw 里的旧版键",
          written.get("skip_existing") == "false",
          str({k: written.get(k) for k in ("skip_existing",)}))

    # 已填的值绝不能被覆盖（raw 里再放一个旧版主引擎键，验证全量合并）
    api2 = FakeConfigApi({"engine_fallback": "deepl", "rate_limit_ms": "999",
                          "keep_original": False, "engine": "google"})
    seeded2 = config_mod.initialize_default_settings(api2)
    check("用户已填的键不会被补种",
          "engine_fallback" not in seeded2 and "rate_limit_ms" not in seeded2
          and "keep_original" not in seeded2, str(sorted(seeded2)))
    # 但其它空缺键仍会补，且写回保住了这三个已填的值
    written2 = api2.mutations[0]["input"]
    check("整体替换写回时保住了用户已填的值",
          written2.get("engine_fallback") == "deepl" and written2.get("rate_limit_ms") == "999"
          and written2.get("keep_original") is False, str(written2.get("rate_limit_ms")))
    check("写回后也带上了旧版兼容键（raw 全量合并）",
          written2.get("engine") == "google",
          str(written2.get("engine")))

    # 旧版「主翻译引擎」还在生效时，引擎链不能被默认链顶掉
    api3 = FakeConfigApi({"engine": "deepl"})
    seeded3 = config_mod.initialize_default_settings(api3)
    check("旧主引擎键生效时不种引擎链", "engine_fallback" not in seeded3, str(sorted(seeded3)))
    check("但其余空缺键照常补种", "rate_limit_ms" in seeded3, str(sorted(seeded3)))

    # 全部键都有值 → 不发起任何写请求
    api4 = FakeConfigApi(dict(config_mod.missing_init_seeds({})))
    seeded4 = config_mod.initialize_default_settings(api4)
    check("无空缺时不发起 configurePlugin", seeded4 == {} and not api4.mutations,
          str(seeded4))

    # 配置读不到（接口异常）→ 静默放弃，不影响任务
    class BoomApi:
        def call(self, query, variables=None):
            raise RuntimeError("configuration 查询失败")

    seeded5 = config_mod.initialize_default_settings(BoomApi())
    check("读不到配置时静默放弃", seeded5 == {}, str(seeded5))

    # 补种之后再读设置：source 应是 settings（键都有了），引擎链与默认链一致
    after = config_mod.load_settings(FakeConfigApi({}), {})
    check("冒烟：空配置 load_settings 仍回默认链", after.engine_chain == list(config_mod.DEFAULT_ENGINE_CHAIN),
          str(after.engine_chain))

    # ------------------------------------------------------------------ #
    print("\n15) 初始化种子不顶掉旧键真值 + libretranslate 迁回（v1.2.9）")
    # v1.2.8 事故：libretranslate_url 被种成 localhost:5000，把用户旧插件键里的
    # 真实自托管地址顶掉了（兼容读取只在本插件键为空时生效）。

    seeds_alias = config_mod.missing_init_seeds(
        {"translateLibretranslateUrl": "http://192.168.3.96:5353"})
    check("旧键里有真值时，本键不种默认值",
          "libretranslate_url" not in seeds_alias, str(sorted(seeds_alias)))

    api6 = FakeConfigApi({"libretranslate_url": "http://localhost:5000",
                          "translateLibretranslateUrl": "http://192.168.3.96:5353"})
    seeded6 = config_mod.initialize_default_settings(api6)
    written6 = api6.mutations[0]["input"] if api6.mutations else {}
    check("已被种成 localhost:5000 的地址从旧键迁回",
          written6.get("libretranslate_url") == "http://192.168.3.96:5353",
          str(written6.get("libretranslate_url")))

    seeds_lt = config_mod.missing_init_seeds({})
    check("种子清单里不再有 libretranslate_url（无公共实例，种了必错）",
          "libretranslate_url" not in seeds_lt, str(sorted(seeds_lt)))

    # ------------------------------------------------------------------ #
    print("\n16) MyMemory 500 字节预检：超限跳过，不发请求不计失败（v1.2.9）")
    _real_http = engines_mod.http_request

    def _must_not_request(*args, **kwargs):
        raise AssertionError("超限文本不应发出请求")

    engines_mod.http_request = _must_not_request
    router = engines_mod.Router(["mymemory"], {})
    check("MyMemory 引擎可用（匿名）", router.has_engine())
    try:
        router.translate("x" * 600)
        check("超限文本全部引擎跳过应抛错", False)
    except EngineError as exc:
        check("超限文本报错信息含「已跳过」", "已跳过" in str(exc), str(exc))
    check("超限不计失败（不触发熔断）", not router._benched and not router._fail_streak,
          str(router._fail_streak))
    check("超限未发出任何请求", True)  # http_request 被替换成必炸桩，走到这说明没发
    engines_mod.http_request = _real_http

    # 正常长度仍能正常请求（stub 返回译文）
    engines_mod.http_request = lambda *a, **k: (200, b'{"responseData":{"translatedText":"hi"},"responseStatus":200}')
    router2 = engines_mod.Router(["mymemory"], {})
    out = router2.translate("hello")
    check("正常长度照常请求", out.engine == "mymemory" and out.text, str(out))
    engines_mod.http_request = _real_http

    server.shutdown()
    if os.path.exists(cache_file):
        os.remove(cache_file)

    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        for name in FAILED:
            print("  失败: %s" % name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
