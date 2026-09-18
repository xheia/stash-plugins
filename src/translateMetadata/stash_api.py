# -*- coding: utf-8 -*-
"""Stash GraphQL 客户端。

插件通过 stdin 拿到 server_connection（含 Scheme / Host / Port / SessionCookie），
用其中的会话 Cookie 访问 Stash 的 /graphql 端点。

会话 Cookie 必须原样回传：它既是认证，也是 Stash 用来识别插件 Hook 循环的依据
（同一个插件 + 同一个 Hook 最多连锁 10 次）。

关于「跑到一半全变成 HTTP 401」（v1.3.0 修复）
------------------------------------------------
Stash 的会话 Cookie 有**硬性有效期**：`security.max_session_age`（config.yml 里的
max_session_age，默认 1 小时）。判定在 gorilla/securecookie 解码阶段完成 ——

    if s.maxAge != 0 && t1 < t2 - s.maxAge { return errTimestampExpired }

（t1 = Cookie 里签名的时间戳，t2 = 当前时间）一旦超龄，`session.Store.Get()` 返回
错误，`GetSessionUserID()` 把它当成「未登录」，接着 internal/api/authentication.go
看到 `userID == ""` 且路径是 /graphql，直接 `w.WriteHeader(401)`（无响应体）。

Stash 每次认证成功都会 `session.Save()` 一次，**在响应里塞一个时间戳刷新的新
Cookie**（滑动续期）。官方 JS 插件用的 pkg/plugin/util.NewClient 带 cookiejar，
会自动接住这个续期；Python 这边如果一直复用插件启动那一刻拿到的旧 Cookie，
连续跑满 1 小时后就会全线 401 —— 现象是「任务前半段正常，某个时刻起成批写回
失败」，而且往往在读请求（每页开头才一次）之后才暴露出来。

所以这里做了两件事：
  1. 跟随服务端续期：每个响应都吸收 Set-Cookie，后续请求用最新的 Cookie
     （等价于 JS 客户端的 cookiejar）。长任务因此可以一直跑下去。
  2. 提供 API Key 通道：`stash_api_key` 设置（或环境变量 STASH_API_KEY）填了
     就以 `ApiKey` 请求头发送。API Key 是长期有效的 JWT，不受会话过期影响，
     也能扛住 Stash 中途重启（重启会让会话签名密钥轮换，旧 Cookie 立刻作废）。
     注意 Stash 的判定是「带了 ApiKey 头就必须匹配」（不匹配直接 401，
     不会退回 Cookie），所以这个值必须与 设置 -> 安全 -> API Key 完全一致。
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

# 环境变量兜底：插件配置本身就存在 Stash 里，万一启动时拿到的会话已经失效，
# 设置页就读不出来了，这时只能靠容器环境变量喂一个 API Key 进来。
API_KEY_ENV = "STASH_API_KEY"

# 最便宜的一次探活：不碰任何业务数据，只走认证中间件 + 版本解析。
AUTH_PROBE_QUERY = "query AuthProbe { version { version } }"


class StashError(Exception):
    pass


class StashAuthError(StashError):
    """认证被拒（HTTP 401）。这是全局性问题，逐条重试没有意义，应立刻停止任务。"""


def _fmt_elapsed(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "%d 秒" % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "%d 分 %d 秒" % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return "%d 小时 %d 分" % (hours, minutes)


class StashAPI:
    def __init__(self, server_connection=None, api_key=None):
        sc = server_connection or {}

        scheme = sc.get("Scheme") or "http"
        host = sc.get("Host") or "127.0.0.1"
        port = sc.get("Port") or 9999

        cookie = sc.get("SessionCookie") or {}
        cookie_name = cookie.get("Name") or "session"
        cookie_value = cookie.get("Value") or ""

        # 容器里 Host 可能是 0.0.0.0，回环访问更稳妥
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"

        self.scheme = scheme
        self.host = host
        self.port = port
        self.endpoint = "%s://%s:%s/graphql" % (scheme, host, port)
        self.cookie_name = cookie_name
        self.cookie_value = cookie_value
        self.cookie = "%s=%s" % (cookie_name, cookie_value) if cookie_value else ""
        self.timeout = int(sc.get("TimeoutSeconds") or 60)

        # 设置页填的值优先，其次环境变量（插件配置读不到时的兜底）
        self.api_key = (api_key or os.environ.get(API_KEY_ENV) or "").strip()

        # 会话健康度诊断：401 时要把"插件跑了多久"打出来，
        # 这样用户一眼能对上 max_session_age（默认 1 小时）
        self.started_at = time.time()
        self.refreshed = 0
        self.calls = 0

    # -- 凭据 -------------------------------------------------------------- #
    def use_api_key(self, api_key):
        """设置页读到 API Key 后启用（留空保持现状，不覆盖环境变量）。"""
        api_key = (api_key or "").strip()
        if api_key:
            self.api_key = api_key
        return self

    def auth_label(self):
        """当前认证方式，写进日志方便排查（「测试翻译引擎」会打印）。"""
        if self.cookie and self.api_key:
            return "会话 Cookie + API Key"
        if self.cookie:
            return "仅会话 Cookie（未配 API Key）"
        if self.api_key:
            return "仅 API Key（未拿到会话 Cookie）"
        return "无凭据（Stash 未开启认证时属正常）"

    def verify(self):
        """探活：返回 Stash 版本号；认证失败抛 StashAuthError。"""
        data = self.call(AUTH_PROBE_QUERY)
        version = ((data or {}).get("version") or {}).get("version")
        return version or "未知版本"

    # -- 请求 -------------------------------------------------------------- #
    def call(self, query, variables=None):
        """执行一次 GraphQL 请求，返回 data。出错抛 StashError（认证失败抛 StashAuthError）。"""
        payload = json.dumps(
            {"query": query, "variables": variables or {}},
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(self.endpoint, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        if self.api_key:
            # Stash 的 session.Store.Authenticate 先看这个头：带上就必须匹配
            req.add_header("ApiKey", self.api_key)

        self.calls += 1

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self._absorb_set_cookie(resp)
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            # 错误响应也可能带着续期 Cookie（认证通过、后续环节才失败的场景），
            # 顺手吸收掉，下一次请求就有机会自愈
            self._absorb_set_cookie(exc)
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300].strip()
            except Exception:
                pass
            if exc.code == 401:
                raise StashAuthError(self._auth_error_text(detail)) from exc
            raise StashError("Stash GraphQL 返回 HTTP %s %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            raise StashError("无法连接 Stash（%s）: %s" % (self.endpoint, exc)) from exc

        if body.get("errors"):
            raise StashError("Stash GraphQL 错误: %s" % (body["errors"],))

        return body.get("data") or {}

    # -- 会话续期 ---------------------------------------------------------- #
    def _absorb_set_cookie(self, response):
        """吸收响应里的 Set-Cookie —— 等价于 JS 客户端的 cookiejar。

        没有这一步，插件就只有一个启动瞬间的 Cookie，跑满 max_session_age
        （默认 1 小时）之后所有请求都会被 401。
        """
        headers = getattr(response, "headers", None)
        if headers is None:
            try:
                headers = response.info()
            except Exception:
                return

        values = []
        try:
            values = headers.get_all("Set-Cookie") or []
        except AttributeError:
            single = headers.get("Set-Cookie")
            values = [single] if single else []

        for raw in values:
            name, value = self._parse_cookie(raw)
            if not name or not value:
                continue
            # Stash 只用 session 这一个 Cookie；不受理别的名字，免得被反向代理
            # 塞进来的无关 Cookie 顶掉真正的会话
            if self.cookie_name and name != self.cookie_name:
                continue
            if value == self.cookie_value:
                continue
            self.cookie_name = name
            self.cookie_value = value
            self.cookie = "%s=%s" % (name, value)
            self.refreshed += 1

    @staticmethod
    def _parse_cookie(raw):
        """从 "session=xxx; Path=/; Max-Age=3600" 里取出 (名字, 值)。"""
        pair = (raw or "").split(";", 1)[0].strip()
        if "=" not in pair:
            return "", ""
        name, _, value = pair.partition("=")
        return name.strip(), value.strip()

    # -- 错误说明 ---------------------------------------------------------- #
    def _auth_error_text(self, detail):
        elapsed = _fmt_elapsed(time.time() - self.started_at)
        if self.api_key:
            hint = ("已附带 API Key 仍被拒：请核对设置里的「Stash - API KEY」是否与"
                    "「设置 -> 安全 -> API Key」完全一致（带了 ApiKey 头就必须匹配，"
                    "Stash 不会退回会话 Cookie 认证）")
        else:
            hint = ("Stash 的会话 Cookie 有硬性有效期（security.max_session_age，默认 1 小时），"
                    "超龄即判为未登录。本插件已跟随服务端续期，若持续 401，请把"
                    "「设置 -> 安全 -> API Key」填到本插件的「Stash - API KEY」里 —— "
                    "API Key 不会过期，超长批量任务建议都配上")
        text = "Stash 认证被拒（HTTP 401），插件已运行 %s。%s" % (elapsed, hint)
        if detail:
            text += "。服务端返回：%s" % detail
        return text
