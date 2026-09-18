#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话过期（HTTP 401）与「全引擎失败即停」的回归测试。

线上真实故障：批量任务跑到某个时刻起，每一条都在报

    [Plugin / Metadata Translator] 场景#6340 写回失败: Stash GraphQL 返回 HTTP 401

根因不在插件逻辑，而在 Stash 的会话 Cookie 有**硬性有效期**：

  * internal/manager/config/config.go: DefaultMaxSessionAge = 60 * 60 * 1（1 小时）
  * gorilla/securecookie 解码时校验
        if s.maxAge != 0 && t1 < t2 - s.maxAge { return errTimestampExpired }
    （t1 = Cookie 里签名的时间戳）
  * 超龄后 pkg/session 的 GetSessionUserID() 把错误当成"未登录"，返回空 userID
  * internal/api/authentication.go 看到 userID == "" 且路径是 /graphql，
    直接 w.WriteHeader(401)（无响应体）

Stash 每次认证成功都会 `session.Save()` 一次，**在响应里塞一个刷新了时间戳的新
Cookie**（滑动续期）。官方 JS 插件用的 pkg/plugin/util.NewClient 带 cookiejar，
会自动接住；Python 端如果一直复用插件启动那一刻的 Cookie，连续跑满 1 小时就会
全线 401 —— 现象正是"读请求（每页开头才一次）还在成功、写回开始成批失败"。

本文件用一个「会过期的假 Stash」把这条服务端规则固化下来：
  1. 复现：不跟随 Set-Cookie 时，超龄必然 401
  2. 修复：跟随续期后，能跨过过期点跑完整轮批量翻译
  3. API Key 通道：没有会话 Cookie 也能认证；带错 Key 会被直接拒（Stash 的行为）
  4. 凭据来源：设置页 > config.yml（server_connection.Dir 自动定位）> 环境变量
  5. 401 的报错文本要能自证：已运行多久 + 该怎么修
  6. 认证失效时立刻停任务，不再逐条刷「写回失败」
  7. 所有引擎连续失败时停任务（fail_fast_after，0 = 关闭）

用法：
    python3 tests/test_session.py
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(os.path.dirname(HERE), "src", "translateMetadata")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, PLUGIN_DIR)

import config as config_mod  # noqa: E402
import log  # noqa: E402
import stash_api  # noqa: E402
import test_pipeline as pipe  # noqa: E402
import translateMetadata as plugin  # noqa: E402
from engines import EngineError  # noqa: E402
from stash_api import AUTH_PROBE_QUERY, StashAPI, StashAuthError, StashError  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def strip_levels(text):
    """去掉 log 模块的 SOH/级别/STX 前缀，方便断言。"""
    return re.sub(r"\x01[a-z]\x02", "", text or "")


# --------------------------------------------------------------------------- #
# 假 Stash：带会话过期语义
# --------------------------------------------------------------------------- #
class SessionStash:
    """模拟 Stash 的会话行为。

      * `max_age` 秒之后，某个 Cookie 值判为过期（securecookie 的 t1 < t2 - maxAge）
      * 每次认证成功都下发一个时间戳刷新的新 Cookie（session.Save 的滑动续期）
      * 带了 ApiKey 头就必须匹配，不匹配直接 401（session.Store.Authenticate）
    """

    def __init__(self, max_age=60.0, api_key="", delay=0.0, initial_cookie="fake"):
        self.data = pipe.FakeStash()
        self.max_age = max_age
        self.api_key = api_key
        self.delay = delay
        self.initial_cookie = initial_cookie
        self.tokens = {}
        self.issued = 0
        if initial_cookie:
            # 插件启动时从 server_connection 拿到的那个 Cookie
            self.tokens[initial_cookie] = time.time()
        self.refreshed_used = 0     # 客户端用了几个"续期后"的 Cookie
        self.rejected = 0
        self.authed = 0
        self.mutations_401 = False  # 只拒 mutation：复现"读成功、写 401"
        self.extra_cookies = []     # 顺带塞进来的无关 Cookie

    def seed(self, entity, records):
        self.data.seed(entity, records)

    def issue(self):
        self.issued += 1
        token = "refresh-%d" % self.issued
        self.tokens[token] = time.time()
        return token

    def accept(self, token):
        if not token:
            return False
        created = self.tokens.get(token)
        if created is None:
            return False
        if time.time() - created > self.max_age:   # ← securecookie 的判定
            return False
        if token != self.initial_cookie:
            self.refreshed_used += 1
        return True


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reject(self):
            state.rejected += 1
            self.send_response(401)
            self.send_header("WWW-Authenticate", "FormBased")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _session_token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "session":
                    return value
            return ""

        def do_POST(self):
            if state.delay:
                time.sleep(state.delay)

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            body = json.loads(raw) if raw.strip() else {}
            query = body.get("query") or ""
            variables = body.get("variables") or {}

            api_key = (self.headers.get("ApiKey") or "").strip()
            if api_key and api_key != state.api_key:
                return self._reject()
            authed = bool(api_key)
            if not authed:
                authed = state.accept(self._session_token())
            if not authed:
                return self._reject()

            refreshed = state.issue()
            if state.mutations_401 and "mutation" in query:
                return self._reject()

            state.authed += 1
            try:
                if "AuthProbe" in query:
                    payload = {"data": {"version": {"version": "v0.31.1-fake"}}}
                else:
                    payload = {"data": pipe.dispatch(state.data, query, variables)}
            except Exception as exc:
                payload = {"errors": [{"message": str(exc)}]}

            out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Set-Cookie",
                             "session=%s; Path=/; Max-Age=3600; HttpOnly" % refreshed)
            for extra in state.extra_cookies:
                self.send_header("Set-Cookie", extra)
            self.end_headers()
            self.wfile.write(out)

    return Handler


def serve(state):
    server = HTTPServer(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def close_server(server):
    """shutdown 只是停循环，监听套接字还开着 —— 得 server_close 才会真正释放。"""
    server.shutdown()
    server.server_close()


def conn_for(port, cookie="fake"):
    return {
        "Scheme": "http",
        "Host": "127.0.0.1",
        "Port": port,
        "SessionCookie": ({"Name": "session", "Value": cookie} if cookie else {}),
        "Dir": os.path.dirname(PLUGIN_DIR),
        "PluginDir": PLUGIN_DIR,
    }


def run_plugin(connection, args, hook_context=None):
    """以真实入口跑一遍插件，返回 (退出码, stdout 解析结果, stderr 文本)。"""
    payload = {"server_connection": connection, "args": dict(args)}
    if hook_context:
        payload["args"]["hookContext"] = hook_context

    old = sys.stdin, sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    try:
        sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False))
        sys.stdout, sys.stderr = out, err
        code = plugin.main()
    finally:
        sys.stdin, sys.stdout, sys.stderr = old

    text = out.getvalue().strip()
    parsed = json.loads(text.splitlines()[-1]) if text else {}
    return code, parsed, strip_levels(err.getvalue())


class DeadRouter:
    """所有引擎都失败的 Router 桩（模拟凭证失效 / 全部被限流）。"""

    def __init__(self, message="所有引擎均失败（edge: 连接被拒）"):
        self.message = message
        self.calls = 0
        self.missing = []

    def has_engine(self):
        return True

    def chain_names(self):
        return ["edge"]

    def translate(self, text, source="auto", target="zh-CN"):
        self.calls += 1
        raise EngineError(self.message)

    def test_all(self, sample="Hello"):
        return [("EDGE（桩）", False, self.message, 1)]

    def stop_exception(self):
        # 本桩只模拟「单引擎反复失败」，不模拟「全链停用」，所以永不返回停止信号。
        return None


# --------------------------------------------------------------------------- #
# 主测试
# --------------------------------------------------------------------------- #
def main():
    log.enable_progress(False)

    cache_file = os.path.join(PLUGIN_DIR, "translate-cache.sqlite3")
    if os.path.exists(cache_file):
        os.remove(cache_file)

    print("1) 服务端规则：Cookie 超龄即 401（复现线上现象）")
    state = SessionStash(max_age=1.0, delay=0.2)
    server, port = serve(state)
    api = StashAPI(conn_for(port))
    # 打桩：模拟修复前的行为（不跟随服务端下发的续期 Cookie）
    api._absorb_set_cookie = lambda response: None

    first_ok = True
    try:
        api.call(AUTH_PROBE_QUERY)
    except StashError:
        first_ok = False
    check("启动时的 Cookie 可用", first_ok)

    time.sleep(1.3)   # 越过 max_age
    expired = None
    try:
        api.call(AUTH_PROBE_QUERY)
    except StashAuthError as exc:
        expired = exc
    except StashError as exc:  # 认成普通错误也要能看见
        expired = exc
    check("不跟随续期 → 超龄后必然 401", isinstance(expired, StashAuthError), repr(expired))
    check("401 归类为认证错误（不是普通 StashError）", type(expired) is StashAuthError)
    check("401 时服务端确实拒了", state.rejected >= 1, str(state.rejected))
    close_server(server)

    print("\n2) 跟随 Set-Cookie：跨越过期点仍然可用")
    # 滑动续期只能扛住"请求间隔 < 有效期"的情况 —— 间隔比有效期还长，
    # 任何 Cookie 都会过期（这也是正常的：真停了那么久就该重新登录）。
    # 这里模拟长任务：间隔 0.25s、有效期 0.6s，连打 10 次共 2.5s，
    # 早已越过"启动时那个 Cookie 的死亡时刻"。
    state2 = SessionStash(max_age=0.6, delay=0.25)
    server2, port2 = serve(state2)
    api2 = StashAPI(conn_for(port2))
    failed = None
    started = time.time()
    for _ in range(10):
        try:
            api2.call(AUTH_PROBE_QUERY)
        except StashError as exc:
            failed = exc
            break
    elapsed = time.time() - started
    check("续期后跨越过期点仍能一直请求", failed is None, str(failed))
    check("本轮确实跑过了有效期", elapsed > state2.max_age,
          "%.2fs vs %.1fs" % (elapsed, state2.max_age))
    check("确实在同一次运行里吸收了多次续期", api2.refreshed >= 5, str(api2.refreshed))
    check("全程没有被 401 拒过", state2.rejected == 0, str(state2.rejected))
    close_server(server2)

    print("\n3) 批量任务跨过会话过期点仍然写回成功（真实入口）")
    state3 = SessionStash(max_age=1.0, delay=0.3)
    state3.seed("scene", [
        {"title": "Session Alpha", "custom_fields": {}},
        {"title": "Session Beta", "custom_fields": {}},
        {"title": "Session Gamma", "custom_fields": {}},
    ])
    server3, port3 = serve(state3)
    pipe.install_stub(pipe.StubEngine())
    started = time.time()
    code, result, err = run_plugin(conn_for(port3), {"mode": "scene"})
    elapsed = time.time() - started
    check("批量任务返回 0", code == 0, "%s / %s" % (code, result))
    check("标题已翻译并写回", state3.data.db["scene"][0]["title"].startswith("译文"),
          state3.data.db["scene"][0]["title"])
    check("本轮耗时确实越过了会话有效期",
          elapsed > state3.max_age, "%.2fs vs %.1fs" % (elapsed, state3.max_age))
    check("全程没有被 401 拒过", state3.rejected == 0, str(state3.rejected))
    check("客户端用了服务端续期的 Cookie", state3.refreshed_used >= 1,
          str(state3.refreshed_used))
    close_server(server3)

    print("\n4) API Key 通道：没有会话 Cookie 也能认证")
    state4 = SessionStash(max_age=0.5, api_key="s3cret", initial_cookie="")
    server4, port4 = serve(state4)
    no_auth = None
    try:
        StashAPI(conn_for(port4, cookie="")).call(AUTH_PROBE_QUERY)
    except StashAuthError as exc:
        no_auth = exc
    check("无 Cookie 无 Key → 401", no_auth is not None)

    keyed_ok = True
    try:
        StashAPI(conn_for(port4, cookie=""), "s3cret").call(AUTH_PROBE_QUERY)
    except StashError as exc:
        keyed_ok = False
        check("正确的 API Key 可独立认证", False, str(exc))
    if keyed_ok:
        check("正确的 API Key 可独立认证", True)

    wrong_key = None
    try:
        StashAPI(conn_for(port4, cookie=""), "typo").call(AUTH_PROBE_QUERY)
    except StashAuthError as exc:
        wrong_key = str(exc)
    check("错的 API Key 会被拒（Stash 不会退回 Cookie 认证）", wrong_key is not None)
    check("错 Key 的报错指向 API Key 本身",
          wrong_key is not None and "API Key" in wrong_key, str(wrong_key))

    print("\n5) 凭据来源：设置页 > config.yml > 环境变量（config.yml 全自动，零维护）")
    tmp_root = tempfile.mkdtemp(prefix="wb-stash-cfg-")
    cfg_dir = os.path.join(tmp_root, "stash-home")
    os.makedirs(cfg_dir)
    with open(os.path.join(cfg_dir, "config.yml"), "w", encoding="utf-8") as fh:
        fh.write('stash:\n  path: /media/stash\napi_key: "yaml-key"\n')
    empty_dir = os.path.join(tmp_root, "empty-home")
    os.makedirs(empty_dir)
    with open(os.path.join(empty_dir, "config.yml"), "w", encoding="utf-8") as fh:
        fh.write('api_key: ""\n')
    tricky_dir = os.path.join(tmp_root, "tricky-home")
    os.makedirs(tricky_dir)
    with open(os.path.join(tricky_dir, "config.yml"), "w", encoding="utf-8") as fh:
        fh.write(
            "stash_boxes:\n"
            "  - endpoint: http://box/graphql\n"
            "    api_key: box-nested-key   # 嵌套键不许顶替顶层键\n"
            "# api_key: commented-out\n"
            "api_key: top-level-key\n"
        )

    os.environ["STASH_API_KEY"] = "from-env"
    try:
        key, src = stash_api.resolve_api_key("", cfg_dir)
        check("config.yml 自动读取（零维护）", key == "yaml-key", "%s %s" % (key, src))
        check("来源说明指向 config.yml", "config.yml" in src, src)

        key, src = stash_api.resolve_api_key("page-key", cfg_dir)
        check("设置页的值优先于 config.yml", key == "page-key" and "设置页" in src, src)

        key, src = stash_api.resolve_api_key("", empty_dir)
        check("config.yml 值为空时回退环境变量",
              key == "from-env" and "STASH_API_KEY" in src, "%s %s" % (key, src))

        key, src = stash_api.resolve_api_key("", os.path.join(tmp_root, "no-such-dir"))
        check("读不到 config.yml 时回退环境变量", key == "from-env", "%s %s" % (key, src))

        key, src = stash_api.resolve_api_key("", tricky_dir)
        check("只认顶层 api_key（嵌套/注释键不顶替）", key == "top-level-key",
              "%s %s" % (key, src))

        key, src = stash_api.resolve_api_key("", "")
        check("config_dir 为空时回退环境变量", key == "from-env", "%s %s" % (key, src))

        api_env = StashAPI(conn_for(port4, cookie=""))
        check("环境变量兜底生效", api_env.api_key == "from-env", api_env.api_key)
        api_env.use_api_key("")
        check("空值不覆盖环境变量", api_env.api_key == "from-env", api_env.api_key)
        api_env.use_api_key("from-page")
        check("设置页的值优先", api_env.api_key == "from-page", api_env.api_key)
    finally:
        os.environ.pop("STASH_API_KEY", None)
        shutil.rmtree(tmp_root, ignore_errors=True)

    settings = config_mod.load_settings(pipe.FakeApi({"stash_api_key": "page-key"}), {})
    check("设置页的「Stash - API KEY」会被读到",
          settings["stash_api_key"] == "page-key", settings["stash_api_key"])
    check("默认值是空（只走会话 Cookie）",
          config_mod.load_settings(pipe.FakeApi({}), {})["stash_api_key"] == "")

    print("\n6) 401 的报错文本能自证")
    msg = None
    try:
        StashAPI(conn_for(port4, cookie="")).call(AUTH_PROBE_QUERY)
    except StashAuthError as exc:
        msg = str(exc)
    check("提到 HTTP 401", msg is not None and "401" in msg, str(msg))
    check("说明插件已运行多久", msg is not None and "已运行" in msg, str(msg))
    check("给出 API Key 修复指引", msg is not None and "API Key" in msg, str(msg))
    close_server(server4)

    print("\n7) 无关 Set-Cookie 不会被采纳")
    state7 = SessionStash(max_age=60.0)
    state7.extra_cookies = ["other=1"]
    server7, port7 = serve(state7)
    api7 = StashAPI(conn_for(port7))
    api7.call(AUTH_PROBE_QUERY)
    check("只认 session Cookie", api7.cookie_name == "session", api7.cookie_name)
    check("值来自 session 而不是别的 Cookie",
          api7.cookie_value.startswith("refresh-") and api7.cookie_value != "1",
          api7.cookie_value)
    close_server(server7)

    print("\n8) 认证失效时立刻停任务（不再逐条刷「写回失败」）")
    state8 = SessionStash(max_age=60.0)
    state8.mutations_401 = True
    state8.seed("scene", [
        {"title": "Auth Dead One", "custom_fields": {}},
        {"title": "Auth Dead Two", "custom_fields": {}},
        {"title": "Auth Dead Three", "custom_fields": {}},
    ])
    server8, port8 = serve(state8)
    pipe.install_stub(pipe.StubEngine())
    code, result, err = run_plugin(conn_for(port8), {"mode": "scene"})
    check("退出码为 1", code == 1, str(code))
    check("错误里说明了 401", "401" in (result.get("error") or ""), str(result))
    check("不再逐条刷「写回失败」", err.count("写回失败") == 0, err[-200:])
    check("读请求确实走通了（所以不是一开始就挂）", state8.authed >= 1, str(state8.authed))
    close_server(server8)

    print("\n9) 所有引擎连续失败 → 停任务（fail_fast_after 默认 3）")
    state9 = SessionStash(max_age=60.0)
    state9.seed("scene", [{"title": "Dead %d" % i, "custom_fields": {}} for i in range(1, 7)])
    server9, port9 = serve(state9)
    dead = DeadRouter()
    plugin.make_router = lambda settings: dead
    code, result, err = run_plugin(conn_for(port9), {"mode": "scene"})
    check("退出码为 1", code == 1, str(code))
    check("只试了 3 次就停下", dead.calls == 3, str(dead.calls))
    check("报错带上最近一次失败原因",
          "所有引擎均失败" in (result.get("error") or ""), str(result))
    check("日志说明已停止任务", "已停止任务" in err, err[-200:])
    check("没有写回任何东西", state9.data.db["scene"][0].get("title") == "Dead 1")

    print("\n10) fail_fast_after 可调：1 = 立刻停，0 = 关闭保护")
    dead1 = DeadRouter()
    plugin.make_router = lambda settings: dead1
    code, result, err = run_plugin(conn_for(port9),
                                   {"mode": "scene", "fail_fast_after": "1"})
    check("fail_fast_after=1 只试 1 次", dead1.calls == 1, str(dead1.calls))

    dead0 = DeadRouter()
    plugin.make_router = lambda settings: dead0
    code, result, err = run_plugin(conn_for(port9),
                                   {"mode": "scene", "fail_fast_after": "0"})
    check("fail_fast_after=0 关闭保护（跑完全部 6 条）", dead0.calls == 6, str(dead0.calls))
    check("关闭保护时任务正常结束（退出码 0）", code == 0, "%s / %s" % (code, result))
    close_server(server9)

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
