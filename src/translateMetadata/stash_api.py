# -*- coding: utf-8 -*-
"""Stash GraphQL 客户端。

插件通过 stdin 拿到 server_connection（含 Scheme / Host / Port / SessionCookie），
用其中的会话 Cookie 访问 Stash 的 /graphql 端点。

会话 Cookie 必须原样回传：它既是认证，也是 Stash 用来识别插件 Hook 循环的依据
（同一个插件 + 同一个 Hook 最多连锁 10 次）。丢掉它会让循环检测失效。
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request


class StashError(Exception):
    pass


class StashAPI:
    def __init__(self, server_connection=None):
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
        self.cookie = "%s=%s" % (cookie_name, cookie_value) if cookie_value else ""
        self.timeout = int(sc.get("TimeoutSeconds") or 60)

    def call(self, query, variables=None):
        """执行一次 GraphQL 请求，返回 data。出错抛 StashError。"""
        payload = json.dumps(
            {"query": query, "variables": variables or {}},
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(self.endpoint, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        if self.cookie:
            req.add_header("Cookie", self.cookie)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise StashError("Stash GraphQL 返回 HTTP %s %s" % (exc.code, detail)) from exc
        except (urllib.error.URLError, socket.timeout) as exc:
            raise StashError("无法连接 Stash（%s）: %s" % (self.endpoint, exc)) from exc

        if body.get("errors"):
            raise StashError("Stash GraphQL 错误: %s" % (body["errors"],))

        return body.get("data") or {}
