# -*- coding: utf-8 -*-
"""翻译引擎：EDGE、Google、百度、腾讯云 TMT、阿里云机器翻译、LibreTranslate。

只依赖 Python 标准库（urllib / hashlib / hmac / json / time / uuid），无需 pip 安装任何东西。

设计要点：
  * 每个引擎暴露统一的 translate_detailed(text, source, target) -> (译文, 检出语言)
    顺带返回检出语言，是为了让上层能把「引擎判定为中文」的文本丢弃，
    这是纯汉字日文场景下防止误翻的关键。
  * Router 按优先级串联多个引擎，前一个失败自动降级到下一个。
  * 统一节流（rate_limit_ms），避免打爆接口或触发限流。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import detect


class EngineError(Exception):
    """引擎调用失败。上层会据此切换到下一个引擎。"""


# --------------------------------------------------------------------------- #
# 语言代码映射：插件内部统一用 zh-CN / zh-TW / en / ja / ko / ru 这类规范写法，
# 各引擎自己的写法在这里翻译。
# --------------------------------------------------------------------------- #
LANG_MAP = {
    "zh-CN": {"edge": "zh-Hans", "google": "zh-CN", "baidu": "zh", "tencent": "zh", "alibaba": "zh", "libretranslate": "zh", "deepl": "ZH", "mymemory": "zh-CN", "lingva": "zh", "openai": "zh-CN"},
    "zh": {"edge": "zh-Hans", "google": "zh-CN", "baidu": "zh", "tencent": "zh", "alibaba": "zh", "libretranslate": "zh", "deepl": "ZH", "mymemory": "zh-CN", "lingva": "zh", "openai": "zh-CN"},
    "zh-Hans": {"edge": "zh-Hans", "google": "zh-CN", "baidu": "zh", "tencent": "zh", "alibaba": "zh", "libretranslate": "zh", "deepl": "ZH", "mymemory": "zh-CN", "lingva": "zh", "openai": "zh-CN"},
    "zh-TW": {"edge": "zh-Hant", "google": "zh-TW", "baidu": "cht", "tencent": "zh-TW", "alibaba": "zh-tw", "libretranslate": "zt", "deepl": "ZH", "mymemory": "zh-TW", "lingva": "zh_HANT", "openai": "zh-TW"},
    "zh-Hant": {"edge": "zh-Hant", "google": "zh-TW", "baidu": "cht", "tencent": "zh-TW", "alibaba": "zh-tw", "libretranslate": "zt", "deepl": "ZH", "mymemory": "zh-TW", "lingva": "zh_HANT", "openai": "zh-TW"},
    "en": {"edge": "en", "google": "en", "baidu": "en", "tencent": "en", "alibaba": "en", "libretranslate": "en", "deepl": "EN", "mymemory": "en", "lingva": "en", "openai": "en"},
    "ja": {"edge": "ja", "google": "ja", "baidu": "jp", "tencent": "ja", "alibaba": "ja", "libretranslate": "ja", "deepl": "JA", "mymemory": "ja", "lingva": "ja", "openai": "ja"},
    "ko": {"edge": "ko", "google": "ko", "baidu": "kor", "tencent": "ko", "alibaba": "ko", "libretranslate": "ko", "deepl": "KO", "mymemory": "ko", "lingva": "ko", "openai": "ko"},
    "ru": {"edge": "ru", "google": "ru", "baidu": "ru", "tencent": "ru", "alibaba": "ru", "libretranslate": "ru", "deepl": "RU", "mymemory": "ru", "lingva": "ru", "openai": "ru"},
}

# 引擎返回的语言里，哪些算「中文」——用于丢弃「其实原文就是中文」的翻译结果
CHINESE_CODES = {
    "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "zh-chs", "zh-cht",
    "chs", "cht", "chi", "cmn", "yue", "wuu", "lzh", "zt",
}


def to_engine_lang(engine_name: str, lang: str) -> str:
    """把规范语言码转成指定引擎的写法；未知的按原样透传。"""
    lang = (lang or "").strip()
    if not lang:
        return lang
    table = LANG_MAP.get(lang)
    if table and engine_name in table:
        return table[engine_name]
    # 大小写/下划线容错
    norm = lang.replace("_", "-")
    for key, mapped in LANG_MAP.items():
        if key.lower() == norm.lower():
            return mapped.get(engine_name, lang)
    return lang


def is_chinese_code(code) -> bool:
    return bool(code) and str(code).strip().lower() in CHINESE_CODES


# --------------------------------------------------------------------------- #
# 代理地址规范化
#
# 实测教训：把代理写成 `http:192.168.3.96:7890`（少了两个斜杠）时，urllib 会把它
# 整体当成 authority，主机名变成 `http:192.168.3.96`，于是**每一个**引擎都报
# `[Errno -2] Name or service not known`，看起来像所有翻译接口同时挂掉。
# 这里统一收口，把常见写法都整理成 urllib 认识的样子。
# --------------------------------------------------------------------------- #
# 只写了一个冒号、没有斜杠的协议头（http:host:port）。
# 末尾的 (?=.*:) 很关键：必须后面还有冒号才认定这是「协议头 + host:port」，
# 否则 `proxy.lan:7890` 这种正常的 host:port 会被误伤成 `proxy.lan://7890`。
_PROXY_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+\-]*):(?!//)(?=.*:)")

_PRIVATE_V4_PREFIXES = ("10.", "127.", "192.168.", "169.254.")


def normalize_proxy(value):
    """把用户填的代理地址整理成 `scheme://host:port`。

    容忍 `http:host:port`、`host:port`、`http://host:port/`、带账号密码等写法。
    """
    raw = (value or "").strip().strip("\"'").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/")
    fixed = _PROXY_SCHEME_RE.sub(lambda m: m.group(1) + "://", raw)
    if "://" not in fixed:
        fixed = "http://" + fixed
    return fixed


def is_local_host(host):
    """本机 / 内网地址：这些请求不该绕代理。

    典型场景：LibreTranslate 跑在同一台 NAS 上（192.168.3.96:5353），
    把它的流量丢给代理是没必要的，代理规则不巧还会把它拦掉。
    """
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host == "::1":
        return True
    if "." not in host:          # 裸主机名（docker 服务名、NAS 名等）
        return True
    if any(host.startswith(prefix) for prefix in _PRIVATE_V4_PREFIXES):
        return True
    parts = host.split(".")
    if host.startswith("172.") and len(parts) >= 2 and parts[1].isdigit():
        return 16 <= int(parts[1]) <= 31
    if host.startswith("100.") and len(parts) >= 2 and parts[1].isdigit():
        return 64 <= int(parts[1]) <= 127     # 运营商 CGNAT 段
    return False


# 这些状态码是「服务端忙」，值得重试一次；其余 4xx 是「请求本身不对」，重试没意义
TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

_TAG_RE = re.compile(r"<[^>]{0,2000}?>")


def brief(text, limit=160):
    """把 HTML 报错页压成一行可读文字 —— 否则日志里全是 `<!DOCTYPE html>`。"""
    text = _TAG_RE.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


# --------------------------------------------------------------------------- #
# HTTP 基础
# --------------------------------------------------------------------------- #
def http_request(url, method="GET", headers=None, data=None, timeout=20, proxy="",
                 retries=0, backoff_ms=800):
    """发起一次 HTTP 请求，返回 (状态码, 响应字节)。

    不发送 Accept-Encoding: gzip，这样 urllib 不需要额外解压。
    """
    headers = dict(headers or {})
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)

    proxy = normalize_proxy(proxy)
    if proxy and proxy.split(":", 1)[0].lower().startswith("socks"):
        raise EngineError(
            "不支持 SOCKS 代理（Python 标准库限制）：%s。请改填 HTTP 代理端口 —— "
            "Clash 的混合端口（默认 7890）本身就同时支持 HTTP。" % proxy
        )

    host = urllib.parse.urlsplit(url).hostname or ""
    if proxy and is_local_host(host):
        proxy = ""      # 内网地址直连

    if proxy:
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    else:
        # 不传代理时沿用环境变量里的设置（保持默认行为）
        handlers = [urllib.request.ProxyHandler()]
    opener = urllib.request.build_opener(*handlers)

    attempts = max(1, int(retries) + 1)
    for attempt in range(attempts):
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = brief(exc.read().decode("utf-8", "replace"))
            except Exception:
                pass
            if exc.code in TRANSIENT_STATUS and attempt + 1 < attempts:
                time.sleep(backoff_ms * (attempt + 1) / 1000.0)
                continue
            suffix = "（服务端限流，稍后重试或改用其它引擎）" if exc.code == 429 else ""
            raise EngineError("HTTP %s %s%s" % (exc.code, detail, suffix)) from exc
        except urllib.error.URLError as exc:
            raise EngineError("网络错误: %s" % (exc.reason,)) from exc
        except Exception as exc:  # 超时等
            raise EngineError("请求失败: %s" % (exc,)) from exc

    raise EngineError("请求失败：重试次数已用尽")


def json_request(url, method="POST", headers=None, payload=None, timeout=20, proxy="",
                 retries=0, backoff_ms=800):
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw = http_request(url, method, headers, body, timeout, proxy, retries, backoff_ms)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise EngineError("响应不是合法 JSON: %s" % (raw[:200],)) from exc


class BaseEngine:
    """引擎基类：统一节流与语言映射。"""

    name = "base"

    def __init__(self, options=None):
        options = options or {}
        self.timeout = int(options.get("timeout_s") or 20)
        self.rate_limit_ms = int(options.get("rate_limit_ms") or 0)
        self.proxy = normalize_proxy(options.get("http_proxy"))
        self.retries = max(0, int(options.get("retry_times") or 0))
        self.backoff_ms = max(0, int(options.get("retry_backoff_ms") or 800))
        self._last_request = 0.0

    # -- 内部工具 ---------------------------------------------------------- #
    def _throttle(self):
        if self.rate_limit_ms > 0:
            now = time.time() * 1000.0
            wait = self._last_request + self.rate_limit_ms - now
            if wait > 0:
                time.sleep(wait / 1000.0)
        self._last_request = time.time() * 1000.0

    def lang(self, lang):
        return to_engine_lang(self.name, lang)

    def _request(self, url, method="GET", headers=None, payload=None, form=None):
        """节流 + 发请求。form 用于 application/x-www-form-urlencoded。"""
        self._throttle()
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            hdrs = dict(headers or {})
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            status, raw = http_request(url, method, hdrs, body, self.timeout, self.proxy,
                                       self.retries, self.backoff_ms)
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except ValueError as exc:
                raise EngineError("响应不是合法 JSON: %s" % (raw[:200],)) from exc
        return json_request(url, method, headers, payload, self.timeout, self.proxy,
                            self.retries, self.backoff_ms)

    # -- 对外接口 ---------------------------------------------------------- #
    def available(self):
        """依赖的凭证是否齐全。"""
        return True

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        """返回 (译文, 检出语言)。必须由子类实现。"""
        raise NotImplementedError

    def translate(self, text, source="auto", target="zh-CN"):
        return self.translate_detailed(text, source, target)[0]

    def requires_credentials(self):
        return []


# --------------------------------------------------------------------------- #
# 1. EDGE —— 微软 Edge 浏览器内置翻译接口（免费、无需 Key）
#    POST https://edge.microsoft.com/translate/translatetext?from=en&to=zh-CHS&api-version=3.0
#    免认证，靠 Origin/Referer 伪装微软官网。
#
#    ⚠️ 2026-09 实测两条路径：
#       * 免 token 旧端点（api-edge.cognitive.microsofttranslator.com）需先取 JWT，
#         而 edge.microsoft.com/translate/auth 已下线（404）；
#       * 免认证 translatetext 端点真实存在，但在大陆网络下直连被 RST、
#         本机代理同样握手失败。是否可用完全取决于部署机的网络路径
#         —— Stash 在 NAS 上时，请用「测试翻译引擎」任务实测。
#       因此该引擎不进默认链，仍保留手动选择。
# --------------------------------------------------------------------------- #
class EdgeEngine(BaseEngine):
    name = "edge"
    API_URL = "https://edge.microsoft.com/translate/translatetext"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")
    BLOCKED_HINT = (
        "edge.microsoft.com 免认证端点连接失败：大陆网络下该域名常被重置，"
        "需要代理且代理规则不得把微软域名放直连；请用其它引擎或调整代理规则。"
    )

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        params = {"api-version": "3.0", "to": self.lang(target)}
        if source and source != "auto":
            params["from"] = self.lang(source)
        url = self.API_URL + "?" + urllib.parse.urlencode(params)

        data = self._request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.UA,
                "Origin": "https://www.microsoft.com",
                "Referer": "https://www.microsoft.com/",
                "Accept": "*/*",
            },
            payload=[text],
        )
        try:
            item = data[0] if isinstance(data, list) else data
            translated = item["translations"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EngineError("EDGE 响应结构异常: %s" % (json.dumps(data, ensure_ascii=False)[:200],)) from exc

        detected = None
        dl = (item or {}).get("detectedLanguage") or {}
        detected = dl.get("language")
        return translated, detected

    # 免认证端点的连接失败要带上可读提示，而不是干巴巴的 "Connection reset"
    def _request(self, url, method="POST", headers=None, payload=None, form=None):
        try:
            return super()._request(url, method, headers, payload, form)
        except EngineError as exc:
            message = str(exc)
            if "10054" in message or "reset" in message.lower() or "EOF" in message:
                raise EngineError("%s（底层错误：%s）" % (self.BLOCKED_HINT, message)) from exc
            raise


# --------------------------------------------------------------------------- #
# 2. Google —— translate_a/single 免费端点（无需 Key，但国内通常需要代理）
# --------------------------------------------------------------------------- #
class GoogleEngine(BaseEngine):
    name = "google"
    API_URL = "https://translate.googleapis.com/translate_a/single"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        params = {
            "client": "gtx",
            "sl": self.lang(source) if source and source != "auto" else "auto",
            "tl": self.lang(target),
            "dt": "t",
            "q": text,
        }
        url = self.API_URL + "?" + urllib.parse.urlencode(params)
        self._throttle()
        status, raw = http_request(
            url,
            headers={"User-Agent": self.UA, "Accept": "*/*"},
            timeout=self.timeout,
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )
        if status != 200:
            raise EngineError("Google 返回 HTTP %s" % status)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EngineError("Google 响应不是 JSON: %s" % (raw[:200],)) from exc

        # 结构：[[[译文, 原文, ...], ...], ... , "en", ...]
        translated = ""
        if isinstance(data, list) and data and isinstance(data[0], list):
            for segment in data[0]:
                if isinstance(segment, list) and segment and segment[0]:
                    translated += segment[0]
        if not translated:
            raise EngineError("Google 未返回译文: %s" % (json.dumps(data, ensure_ascii=False)[:200],))

        detected = None
        if isinstance(data, list) and len(data) > 2 and isinstance(data[2], str):
            detected = data[2]
        return translated, detected


# --------------------------------------------------------------------------- #
# 3. 百度翻译开放平台（需 AppID + 密钥，sign = md5(appid + q + salt + key)）
# --------------------------------------------------------------------------- #
class BaiduEngine(BaseEngine):
    name = "baidu"
    API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        self.appid = (options.get("baidu_appid") or "").strip()
        self.key = (options.get("baidu_key") or "").strip()

    def available(self):
        return bool(self.appid and self.key)

    def requires_credentials(self):
        return ["baidu_appid", "baidu_key"]

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if not self.available():
            raise EngineError("百度翻译未配置 AppID / 密钥")

        salt = str(int(time.time() * 1000))
        sign = hashlib.md5(
            (self.appid + text + salt + self.key).encode("utf-8")
        ).hexdigest()

        data = self._request(
            self.API_URL,
            method="POST",
            form={
                "q": text,
                "from": self.lang(source) if source and source != "auto" else "auto",
                "to": self.lang(target),
                "appid": self.appid,
                "salt": salt,
                "sign": sign,
            },
        )

        if data.get("error_code"):
            raise EngineError("百度翻译报错 %s: %s" % (data.get("error_code"), data.get("error_msg")))

        results = data.get("trans_result") or []
        if not results:
            raise EngineError("百度翻译未返回结果: %s" % (json.dumps(data, ensure_ascii=False)[:200],))

        # 百度按换行拆分成多条，按顺序拼回去
        translated = "\n".join(item.get("dst", "") for item in results)
        return translated, data.get("from")


# --------------------------------------------------------------------------- #
# 4. 腾讯云机器翻译 TMT（TC3-HMAC-SHA256 签名）
#
#    tencent_region 这个键历史上有两种填法：真正的「地域」（ap-guangzhou），
#    以及接口地址（https://tmt.tencentcloudapi.com）。旧插件用的是后者，用户
#    很容易照抄。两种都得认 —— 否则接口地址会被塞进 X-TC-Region，服务端直接
#    回 InvalidParameterValue: The value specified in `X-TC-Region` is invalid。
# --------------------------------------------------------------------------- #
TENCENT_DEFAULT_HOST = "tmt.tencentcloudapi.com"
TENCENT_DEFAULT_REGION = "ap-guangzhou"


def normalize_tencent_endpoint(value):
    """把用户填的腾讯云地域 / 地址整理成 (host, region)。"""
    raw = (value or "").strip()
    if not raw:
        return TENCENT_DEFAULT_HOST, TENCENT_DEFAULT_REGION

    if "//" in raw:
        parsed = urllib.parse.urlsplit(raw if raw.startswith("http") else "https://" + raw)
        host = parsed.netloc or parsed.path
    elif "." in raw:
        host = raw
    else:
        # 裸地域，如 ap-guangzhou
        return "tmt.%s.tencentcloudapi.com" % raw, raw

    host = host.strip("/").lower()
    prefix, suffix = "tmt.", ".tencentcloudapi.com"
    if host.startswith(prefix) and host.endswith(suffix):
        region = host[len(prefix):-len(suffix)]
        if region:
            return host, region
    return host, TENCENT_DEFAULT_REGION


class TencentEngine(BaseEngine):
    name = "tencent"
    HOST = TENCENT_DEFAULT_HOST
    SERVICE = "tmt"
    VERSION = "2018-03-21"
    ACTION = "TextTranslate"
    ALGORITHM = "TC3-HMAC-SHA256"
    CONTENT_TYPE = "application/json; charset=utf-8"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        self.secret_id = (options.get("tencent_secret_id") or "").strip()
        self.secret_key = (options.get("tencent_secret_key") or "").strip()
        self.host, self.region = normalize_tencent_endpoint(options.get("tencent_region"))

    def available(self):
        return bool(self.secret_id and self.secret_key)

    def requires_credentials(self):
        return ["tencent_secret_id", "tencent_secret_key"]

    @staticmethod
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _sign(self, payload_str, timestamp):
        date = time.strftime("%Y-%m-%d", time.gmtime(timestamp))
        credential_scope = "%s/%s/tc3_request" % (date, self.SERVICE)

        hashed_payload = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
        canonical_headers = "content-type:%s\nhost:%s\n" % (self.CONTENT_TYPE, self.host)
        signed_headers = "content-type;host"
        canonical_request = "\n".join([
            "POST", "/", "", canonical_headers, signed_headers, hashed_payload,
        ])

        string_to_sign = "\n".join([
            self.ALGORITHM,
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        secret_date = self._hmac(("TC3" + self.secret_key).encode("utf-8"), date)
        secret_service = self._hmac(secret_date, self.SERVICE)
        secret_signing = self._hmac(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return (
            "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
            % (self.ALGORITHM, self.secret_id, credential_scope, signed_headers, signature)
        )

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if not self.available():
            raise EngineError("腾讯云未配置 SecretId / SecretKey")

        payload = {
            "SourceText": text,
            "Source": self.lang(source) if source and source != "auto" else "auto",
            "Target": self.lang(target),
            "ProjectId": 0,
        }
        payload_str = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())

        headers = {
            "Authorization": self._sign(payload_str, timestamp),
            "Content-Type": self.CONTENT_TYPE,
            "Host": self.host,
            "X-TC-Action": self.ACTION,
            "X-TC-Version": self.VERSION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": self.region,
        }

        self._throttle()
        status, raw = http_request(
            "https://" + self.host,
            method="POST",
            headers=headers,
            data=payload_str.encode("utf-8"),
            timeout=self.timeout,
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EngineError("腾讯云响应不是 JSON: %s" % (raw[:200],)) from exc

        resp = data.get("Response") or {}
        if resp.get("Error"):
            err = resp["Error"]
            raise EngineError("腾讯云报错 %s: %s" % (err.get("Code"), err.get("Message")))

        translated = resp.get("TargetText")
        if not translated:
            raise EngineError("腾讯云未返回译文: %s" % (json.dumps(data, ensure_ascii=False)[:200],))
        return translated, resp.get("Source")


# --------------------------------------------------------------------------- #
# 5. 阿里云机器翻译（通用版）—— RPC 协议 + HMAC-SHA1 签名
#
#    与腾讯云不同，阿里云走的是老式 RPC 风格：所有参数（含公共参数）放在
#    query/form 里，签名是对「排序后的参数串」做 HMAC-SHA1 再 Base64。
#    流程：
#      1. 收集公共参数（Format / Version / AccessKeyId / SignatureMethod ...）
#      2. 按参数名做字典序排序，逐个 RFC3986 编码后用 & 拼成规范串
#      3. StringToSign = POST & %2F & percentEncode(规范串)
#      4. Signature = Base64(HMAC-SHA1(AccessKeySecret + "&", StringToSign))
#    文档：https://help.aliyun.com/zh/machine-translation/
# --------------------------------------------------------------------------- #
class AlibabaEngine(BaseEngine):
    name = "alibaba"
    ACTION = "TranslateGeneral"
    DETECT_ACTION = "GetDetectLanguage"
    VERSION = "2018-10-12"
    SCENE = "general"          # general=通用场景；电商场景可改 goods
    FORMAT_TYPE = "text"       # 实测必传，缺了会报 FormatType is mandatory for this action
    DEFAULT_HOST = "mt.aliyuncs.com"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        self.access_key = (options.get("alibaba_access_key") or "").strip()
        self.access_secret = (options.get("alibaba_access_secret") or "").strip()
        self.region_id = (options.get("alibaba_region_id") or "").strip()

        # 兼容三种填法：完整地址 / 裸域名 / 地域 ID
        #   https://mt.cn-hangzhou.aliyuncs.com  -> 取 host
        #   mt.aliyuncs.com                      -> 原样使用
        #   cn-hangzhou                          -> 补成 mt.cn-hangzhou.aliyuncs.com
        raw = (options.get("alibaba_region") or options.get("alibaba_endpoint") or "").strip()
        host = ""
        if raw:
            if "//" in raw:
                parsed = urllib.parse.urlparse(raw if raw.startswith("http") else "https://" + raw)
                host = parsed.netloc or parsed.path
            elif "." in raw:
                host = raw
            else:
                host = "mt.%s.aliyuncs.com" % raw
        self.host = (host or self.DEFAULT_HOST).strip("/")
        self.endpoint = "https://" + self.host + "/"
        # 从 mt.cn-hangzhou.aliyuncs.com 里抠出 cn-hangzhou 作为 RegionId
        if not self.region_id and self.host.startswith("mt.") and self.host.endswith(".aliyuncs.com"):
            self.region_id = self.host[len("mt."):-len(".aliyuncs.com")] or ""

    def available(self):
        return bool(self.access_key and self.access_secret)

    def requires_credentials(self):
        return ["alibaba_access_key", "alibaba_access_secret"]

    # -- 签名 -------------------------------------------------------------- #
    @staticmethod
    def _pe(value):
        """阿里云要求的百分号编码：空格->%20，斜杠、加号等全部转义。"""
        return urllib.parse.quote(str(value), safe="")

    def _sign(self, params, method="POST"):
        canonical = "&".join(
            "%s=%s" % (self._pe(k), self._pe(v)) for k, v in sorted(params.items())
        )
        # 注意：规范串本身还要再整体编码一次，所以 & 与 = 在这里会变成 %26 / %3D，
        # 这是阿里云规定的做法，不是笔误。
        string_to_sign = "&".join([method, self._pe("/"), self._pe(canonical)])
        digest = hmac.new(
            (self.access_secret + "&").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1,
        ).digest()
        return base64.b64encode(digest).decode("ascii")

    def _invoke(self, action, params):
        payload = {
            "Format": "JSON",
            "Version": self.VERSION,
            "AccessKeyId": self.access_key,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureVersion": "1.0",
            "SignatureNonce": str(uuid.uuid4()),
            "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "Action": action,
        }
        if self.region_id:
            payload["RegionId"] = self.region_id
        payload.update(params)
        payload["Signature"] = self._sign(payload)

        self._throttle()
        # quote_via=quote 让空格编码成 %20（而不是 urlencode 默认的 +），
        # 与服务端重算规范串时使用的编码保持一致。
        body = urllib.parse.urlencode(
            {k: str(v) for k, v in payload.items()}, quote_via=urllib.parse.quote
        ).encode("utf-8")
        status, raw = http_request(
            self.endpoint,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=body,
            timeout=self.timeout,
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EngineError("阿里云响应不是 JSON: %s" % (raw[:200],)) from exc

        # 业务错误时 HTTP 仍是 200，靠 Code 判断（Code 是字符串 "200"）
        code = str(data.get("Code") or "")
        if code and code != "200":
            raise EngineError("阿里云报错 %s: %s" % (code, data.get("Message")))
        return data

    def detect(self, text):
        data = self._invoke(self.DETECT_ACTION, {"SourceText": text})
        return data.get("DetectedLanguage") or (data.get("Data") or {}).get("DetectedLanguage")

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if not self.available():
            raise EngineError("阿里云未配置 AccessKeyId / AccessKeySecret")

        target_lang = self.lang(target)
        requested = self.lang(source) if source and source != "auto" else "auto"

        def call(source_lang):
            return self._invoke(self.ACTION, {
                "SourceLanguage": source_lang,
                "TargetLanguage": target_lang,
                "SourceText": text,
                "Scene": self.SCENE,
                "FormatType": self.FORMAT_TYPE,
            })

        try:
            data = call(requested)
        except EngineError:
            # 通用版对 auto 的支持视账号/接口版本而定：失败就先检测语言再重试一次
            if requested != "auto":
                raise
            detected_lang = self.detect(text)
            if not detected_lang:
                raise
            data = call(detected_lang)

        payload = data.get("Data") or {}
        translated = payload.get("Translated")
        if not translated:
            raise EngineError("阿里云未返回译文: %s" % (json.dumps(data, ensure_ascii=False)[:200],))
        return translated, payload.get("DetectedLanguage") or payload.get("Source")


# --------------------------------------------------------------------------- #
# 6. LibreTranslate（自托管或公共实例）
# --------------------------------------------------------------------------- #
class LibreTranslateEngine(BaseEngine):
    name = "libretranslate"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        base = (options.get("libretranslate_url") or "http://localhost:5000").rstrip("/")
        # 兼容两种写法：填服务根地址（http://host:5000）或直接填接口地址
        # （http://host:5000/translate）。后者在别的插件里很常见，这里都接受。
        if base.endswith("/translate"):
            base = base[: -len("/translate")]
        self.base_url = base
        self.api_key = (options.get("libretranslate_api_key") or "").strip()

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        payload = {
            "q": text,
            "source": self.lang(source) if source and source != "auto" else "auto",
            "target": self.lang(target),
            "format": "text",
        }
        if self.api_key:
            payload["api_key"] = self.api_key

        data = self._request(
            self.base_url + "/translate",
            method="POST",
            headers={"Content-Type": "application/json"},
            payload=payload,
        )
        translated = data.get("translatedText")
        if translated is None:
            errors = data.get("error")
            raise EngineError("LibreTranslate 未返回译文: %s" % (errors or json.dumps(data, ensure_ascii=False)[:200],))
        return translated, data.get("detectedLanguage", {}).get("language")

    def detect(self, text):
        payload = {"q": text}
        if self.api_key:
            payload["api_key"] = self.api_key
        data = self._request(
            self.base_url + "/detect",
            method="POST",
            headers={"Content-Type": "application/json"},
            payload=payload,
        )
        if isinstance(data, list) and data:
            return data[0].get("language")
        results = (data or {}).get("results") or []
        return results[0].get("language") if results else None


# --------------------------------------------------------------------------- #
# 7. DeepL API Free —— 免费档每月 50 万字符，需在 deepl.com 注册免费 Key
#    （Key 以 ":fx" 结尾的是 Free 档，走 api-free.deepl.com；Pro Key 走 api.deepl.com）
# --------------------------------------------------------------------------- #
class DeepLEngine(BaseEngine):
    name = "deepl"
    DEFAULT_URL = "https://api-free.deepl.com/v2"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        self.auth_key = (options.get("deepl_api_key") or "").strip()
        # 按官方规则自动识别：":fx" 结尾的 Key 属于免费档
        base = (options.get("deepl_api_url") or "").strip().rstrip("/")
        if not base:
            base = self.DEFAULT_URL if self.auth_key.endswith(":fx") else "https://api.deepl.com/v2"
        self.api_url = base + "/translate"

    def available(self):
        return bool(self.auth_key)

    def requires_credentials(self):
        return ["deepl_api_key"]

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if not self.available():
            raise EngineError("DeepL 未配置 API Key（deepl.com 免费注册，Key 以 :fx 结尾）")

        form = {
            "text": text,
            # DeepL 的简体中文目标码就是 ZH（繁体暂不支持，会落到简体）
            "target_lang": self.lang(target),
        }
        if source and source != "auto":
            form["source_lang"] = self.lang(source)

        self._throttle()
        status, raw = http_request(
            self.api_url,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                # DeepL v2 只认请求头鉴权，表单里传 auth_key 是旧版写法、会被 403 拒绝
                "Authorization": "DeepL-Auth-Key " + self.auth_key,
            },
            data=urllib.parse.urlencode(form).encode("utf-8"),
            timeout=self.timeout,
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EngineError("DeepL 响应不是 JSON: %s" % (raw[:200],)) from exc

        if status == 403:
            raise EngineError("DeepL 认证失败 (HTTP 403)：Key 无效或填错档位"
                              "（免费 Key 必须走 api-free.deepl.com）")
        if status == 456:
            raise EngineError("DeepL 配额已用完 (HTTP 456)：本月免费额度耗尽，"
                              "下月恢复或改用其它引擎")
        if status != 200:
            raise EngineError("DeepL HTTP %s: %s" % (status, data.get("message") or brief(raw.decode("utf-8", "replace"))))

        translations = data.get("translations") or []
        if not translations or not translations[0].get("text"):
            raise EngineError("DeepL 未返回译文: %s" % (json.dumps(data, ensure_ascii=False)[:200],))
        return translations[0]["text"], translations[0].get("detected_source_language")


# --------------------------------------------------------------------------- #
# 8. MyMemory —— 匿名免费（约 1000 词/天/IP），无需任何注册
#    填 mymemory_email（任意邮箱）可提升到约 5 万词/天。
#    限制：单次请求原文不超过 500 字节，超长文本请交给其它引擎。
# --------------------------------------------------------------------------- #
class MyMemoryEngine(BaseEngine):
    name = "mymemory"
    API_URL = "https://api.mymemory.translated.net/get"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        self.email = (options.get("mymemory_email") or "").strip()

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if len(text.encode("utf-8")) > 500:
            raise EngineError("原文超过 MyMemory 单次 500 字节上限，请换其它引擎")

        params = {
            "q": text,
            # MyMemory 接受 Autodetect 作为源语言
            "langpair": "%s|%s" % (
                self.lang(source) if source and source != "auto" else "Autodetect",
                self.lang(target),
            ),
        }
        if self.email:
            params["de"] = self.email

        url = self.API_URL + "?" + urllib.parse.urlencode(params)
        self._throttle()
        status, raw = http_request(
            url,
            headers={"Accept": "application/json"},
            timeout=self.timeout,
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise EngineError("MyMemory 响应不是 JSON: %s" % (raw[:200],)) from exc

        detail = str(data.get("responseDetails") or "")
        status_code = str(data.get("responseStatus") or "")
        if status_code not in ("200", "200 ") and status_code.strip() != "200":
            raise EngineError("MyMemory 报错 %s: %s" % (status_code, detail or data))
        translated = (data.get("responseData") or {}).get("translatedText")
        # 拒绝 MyMemory 塞在译文位置的配额告警文本
        if not translated or "MYMEMORY WARNING" in translated.upper() \
                or "QUERY LENGTH LIMIT" in translated.upper():
            raise EngineError("MyMemory 未返回有效译文: %s" % (translated or detail or data))
        return translated, None


# --------------------------------------------------------------------------- #
# 9. Lingva —— Google 翻译的开源前端镜像，无需 Key
#    ⚠️ 2026-09 实测：所有公共实例基本已死（本机与部署机两侧验证）——
#       lingva.ml / translate.plausibility.cloud 被 Cloudflare 盾拦截（403 Just a moment），
#       lingva.lunar.icu / lingva.esmailelbob.xyz 404，lingva.garudalinux.org 时通时不通。
#       引擎实现保留：自托管（docker run ghcr.io/tadashi-aikawa/lingva 之类镜像）
#       或临时可用的社区实例仍可在 lingva_instance 里指定。链里放 lingva 时，
#       公共实例失败会由熔断机制跳过，不会拖慢整体。
# --------------------------------------------------------------------------- #
class LingvaEngine(BaseEngine):
    name = "lingva"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        base = (options.get("lingva_instance") or "https://lingva.ml").strip().rstrip("/")
        if base.endswith("/api"):
            base = base[: -len("/api")]
        self.base_url = base

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        source_lang = self.lang(source) if source and source != "auto" else "auto"
        url = "%s/api/v1/%s/%s/%s" % (
            self.base_url, source_lang, self.lang(target), urllib.parse.quote(text, safe=""),
        )
        self._throttle()
        status, raw = http_request(
            url,
            headers={"Accept": "application/json", "User-Agent": self.UA},
            timeout=self.timeout,
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )
        body = raw.decode("utf-8", "replace")
        if status == 403 and ("Just a moment" in body or "cloudflare" in body.lower()):
            raise EngineError(
                "实例 %s 被 Cloudflare 人机验证拦截（403），浏览器都过不去、程序更不行。"
                "公共 Lingva 实例大多已死，请在 lingva_instance 里换自托管或临时可用的社区实例，"
                "或直接换 google / edge / mymemory 引擎" % self.base_url
            )
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise EngineError("Lingva 响应不是 JSON (HTTP %s): %s" % (status, body[:200],)) from exc

        translated = data.get("translation")
        if not translated:
            raise EngineError("Lingva 未返回译文: %s" % (json.dumps(data, ensure_ascii=False)[:200],))
        info = data.get("info") or {}
        return translated, info.get("detectedSource")


# --------------------------------------------------------------------------- #
# 10. AI 翻译（OpenAI 兼容接口）—— 一个配置通吃所有兼容端点：
#     OpenAI / DeepSeek / 智谱 / Kimi / 通义 / OpenRouter / Ollama / LM Studio ...
#     只要把「接口地址 + API Key + 模型名」填对即可。LLM 翻译质量通常
#     远好于传统机翻，且能理解上下文、保留专有名词。
# --------------------------------------------------------------------------- #
class OpenAIEngine(BaseEngine):
    name = "openai"
    DEFAULT_BASE = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o-mini"

    # 提示词里的目标语言名称（比语言代码更不容易被模型理解错）
    LANG_PROMPT = {
        "zh-CN": "简体中文", "zh": "简体中文", "zh-Hans": "简体中文",
        "zh-TW": "繁体中文（台湾用语）", "zh-Hant": "繁体中文（台湾用语）",
        "en": "English", "ja": "日本語", "ko": "한국어", "ru": "Русский",
    }

    DEFAULT_PROMPT = (
        "You are a professional subtitle/metadata translator. "
        "Translate the user's text into {lang}. Requirements:\n"
        "1. Return ONLY the translation, with no explanations, no quotes, no prefix.\n"
        "2. Keep numbers, version markers (e.g. #456), URLs and file extensions unchanged.\n"
        "3. Person names may stay in Latin letters if translating them looks awkward.\n"
        "4. Match the tone of the original (colloquial stays colloquial)."
    )

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        base = (options.get("openai_base_url") or "").strip().strip("\"'").strip()
        # 记录用户到底配没配（available 判断要用；base_url 本身永远有值便于容错）
        self._has_base = bool(base)
        # 容错：用户可能直接填到 /chat/completions，或者不带 /v1
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        base = base.rstrip("/")
        # 路径为空（如 https://api.deepseek.com 或 http://localhost:11434）补 /v1；
        # 用户自定义了路径（如 https://gw.example.com/api/openai）则尊重原样。
        if not base:
            base = self.DEFAULT_BASE
        elif urllib.parse.urlsplit(base).path.strip("/") == "":
            base = base + "/v1"
        self.base_url = base
        self.api_key = (options.get("openai_api_key") or "").strip()
        self.model = (options.get("openai_model") or "").strip() or self.DEFAULT_MODEL
        self.custom_prompt = (options.get("openai_prompt") or "").strip()

    def available(self):
        # Ollama / LM Studio 这类本地端点不需要 Key，填了地址即可用
        return self._has_base or bool(self.api_key)

    def requires_credentials(self):
        return ["openai_base_url（或 openai_api_key）"]

    def _system_prompt(self, target):
        if self.custom_prompt:
            return self.custom_prompt
        lang = self.LANG_PROMPT.get(self.lang(target), self.lang(target))
        return self.DEFAULT_PROMPT.format(lang=lang)

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if not self.available():
            raise EngineError("AI 翻译未配置：请填接口地址（openai_base_url）或 API Key（openai_api_key）")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key

        payload = {
            "model": self.model,
            "temperature": 0.2,
            "stream": False,
            "messages": [
                {"role": "system", "content": self._system_prompt(target)},
                {"role": "user", "content": text},
            ],
        }

        self._throttle()
        # LLM 比传统机翻慢得多（本地模型尤甚），超时下限放到 60 秒
        data = json_request(
            self.base_url + "/chat/completions",
            method="POST",
            headers=headers,
            payload=payload,
            timeout=max(self.timeout, 60),
            proxy=self.proxy,
            retries=self.retries,
            backoff_ms=self.backoff_ms,
        )

        if data.get("error"):
            err = data["error"]
            raise EngineError("AI 翻译报错: %s" % (
                err.get("message") if isinstance(err, dict) else err))
        choices = data.get("choices") or []
        if not choices:
            raise EngineError("AI 翻译未返回结果: %s" % (json.dumps(data, ensure_ascii=False)[:200],))
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        # 有些模型喜欢给译文套引号，去掉一层对称引号
        if len(content) >= 2 and content[0] in "\"'" and content[0] == content[-1]:
            content = content[1:-1].strip()
        if not content:
            raise EngineError("AI 翻译返回了空内容")
        return content, None


# --------------------------------------------------------------------------- #
# 引擎注册表
# --------------------------------------------------------------------------- #
ENGINE_CLASSES = {
    EdgeEngine.name: EdgeEngine,
    GoogleEngine.name: GoogleEngine,
    BaiduEngine.name: BaiduEngine,
    TencentEngine.name: TencentEngine,
    AlibabaEngine.name: AlibabaEngine,
    LibreTranslateEngine.name: LibreTranslateEngine,
    DeepLEngine.name: DeepLEngine,
    MyMemoryEngine.name: MyMemoryEngine,
    LingvaEngine.name: LingvaEngine,
    OpenAIEngine.name: OpenAIEngine,
}

ENGINE_LABELS = {
    "edge": "EDGE（免认证，大陆网络需代理）",
    "google": "Google（免费）",
    "baidu": "百度翻译",
    "tencent": "腾讯云 TMT",
    "alibaba": "阿里云机器翻译",
    "libretranslate": "LibreTranslate",
    "deepl": "DeepL（免费档，需 API Key）",
    "mymemory": "MyMemory（匿名免费）",
    "lingva": "Lingva（Google 免费镜像，公共实例大多被 Cloudflare 拦截）",
    "openai": "AI 翻译（OpenAI 兼容）",
}

# 默认链只留 Google —— 它不需要凭证、大陆网络下走代理即可用。
# EDGE 免认证端点在大陆网络下常被重置（是否可用取决于部署机网络路径），不进默认链。
DEFAULT_CHAIN = ["google"]


class TranslateResult:
    """一次翻译的结果。"""

    __slots__ = ("text", "engine", "detected", "source_text", "from_cache", "attempts")

    def __init__(self, text, engine, detected=None, source_text="", from_cache=False, attempts=None):
        self.text = text
        self.engine = engine
        self.detected = detected
        self.source_text = source_text
        self.from_cache = from_cache
        self.attempts = attempts or []

    def is_unchanged(self):
        return self.text == self.source_text


class Router:
    """按优先级串联多个引擎，失败自动降级。

    另外带一个「熔断」：某个引擎连续失败若干次后，本轮就不再试它。
    批量任务动辄上百条，如果链首引擎是死的，不熔断就会每条都白等一次超时。
    """

    def __init__(self, engine_names, options=None):
        options = options or {}
        self.options = options
        self.engines = []
        self.missing = []
        self.skip_after = max(0, int(options.get("engine_skip_after") or 0))
        self._fail_streak = {}
        self._benched = []

        for name in engine_names:
            name = (name or "").strip().lower()
            if not name or name not in ENGINE_CLASSES:
                continue
            engine = ENGINE_CLASSES[name](options)
            if not engine.available():
                self.missing.append((name, engine.requires_credentials()))
                continue
            self.engines.append(engine)

    def has_engine(self):
        return bool(self.engines)

    def chain_names(self):
        return [e.name for e in self.engines]

    def benched_names(self):
        """本轮被熔断跳过的引擎。"""
        return list(self._benched)

    # -- 熔断计数 ---------------------------------------------------------- #
    def _note_failure(self, name):
        streak = self._fail_streak.get(name, 0) + 1
        self._fail_streak[name] = streak
        if self.skip_after and streak >= self.skip_after and name not in self._benched:
            self._benched.append(name)
        return streak

    def _note_success(self, name):
        self._fail_streak[name] = 0

    def translate(self, text, source="auto", target="zh-CN"):
        """依次尝试各引擎，返回 TranslateResult；全部失败抛 EngineError。"""
        if not self.engines:
            detail = ""
            if self.missing:
                detail = "；缺少凭证: " + ", ".join(
                    "%s(%s)" % (n, "/".join(keys)) for n, keys in self.missing
                )
            raise EngineError("没有可用的翻译引擎" + detail)

        errors = []
        for engine in self.engines:
            if engine.name in self._benched:
                continue
            try:
                translated, detected = engine.translate_detailed(text, source, target)
            except EngineError as exc:
                self._note_failure(engine.name)
                errors.append("%s: %s" % (engine.name, exc))
                continue
            except Exception as exc:  # 兜底，绝不让单引擎异常打断整条链
                self._note_failure(engine.name)
                errors.append("%s: 意外错误 %s" % (engine.name, exc))
                continue

            if translated is None:
                self._note_failure(engine.name)
                errors.append("%s: 返回空译文" % engine.name)
                continue

            # 引擎「成功」但吐的是退化内容（同一字符反复重复）—— 当作失败降级，
            # 否则垃圾译文会被原样写进数据库。
            if translated != text and detect.looks_degenerate(translated):
                self._note_failure(engine.name)
                errors.append("%s: 译文退化（字符反复重复），已丢弃: %s"
                              % (engine.name, brief(translated, 40)))
                continue

            self._note_success(engine.name)
            result = TranslateResult(
                text=translated.strip(),
                engine=engine.name,
                detected=detected,
                source_text=text,
                attempts=errors,
            )
            # 引擎自己说原文就是中文 —— 说明不需要翻译，直接返回原文
            if source in ("", "auto", None) and is_chinese_code(detected):
                result.text = text
            return result

        raise EngineError(self.failure_message(errors))

    # -- 失败时的完整交代 -------------------------------------------------- #
    def _proxy_display(self):
        raw = (self.options.get("http_proxy") or "").strip()
        if not raw:
            return "未设置"
        fixed = normalize_proxy(raw)
        return "%s（已按 %s 使用）" % (raw, fixed) if fixed != raw else fixed

    def _hint(self, errors):
        """全部引擎失败时，若失败原因高度雷同就点一句可能的病因。"""
        if not errors:
            return ""
        joined = " ".join(errors)
        if "Name or service not known" in joined or "getaddrinfo failed" in joined:
            return ("。提示：所有引擎都解析不了域名，通常是 http_proxy 写错或代理不可达"
                    "（当前值: %s）" % self._proxy_display())
        if len(errors) > 1 and all(("网络错误" in e or "请求失败" in e) for e in errors):
            return ("。提示：全部引擎都是网络错误，请检查 http_proxy 与出网连通性"
                    "（当前值: %s）" % self._proxy_display())
        return ""

    def failure_message(self, errors):
        """把失败原因拼成一条能直接看出问题的日志。

        要交待三件事：谁失败了、为什么、谁根本没上（缺凭证 / 被熔断）。
        只报「所有引擎均失败 -> edge: ... | google: ...」最容易让人误判。
        """
        parts = list(errors)
        for name in self._benched:
            parts.append("%s: 本轮已连续失败 %d 次，暂时跳过"
                         % (name, self._fail_streak.get(name, 0)))
        for name, keys in self.missing:
            parts.append("%s: 未配置（需要 %s）" % (name, " / ".join(keys)))
        if not parts:
            parts.append("没有可尝试的引擎")
        return "所有引擎均失败 -> " + " | ".join(parts) + self._hint(errors)

    def test_all(self, sample="Hello, this is a translation test."):
        """逐个测试已配置的引擎，返回 [(label, ok, detail, elapsed_ms), ...]。"""
        report = []
        for engine in self.engines:
            started = time.time()
            try:
                translated, detected = engine.translate_detailed(sample, "auto", "zh-CN")
                elapsed = int((time.time() - started) * 1000)
                report.append((ENGINE_LABELS.get(engine.name, engine.name), True,
                               "%s（检出 %s）" % (translated, detected or "?"), elapsed))
            except Exception as exc:
                elapsed = int((time.time() - started) * 1000)
                report.append((ENGINE_LABELS.get(engine.name, engine.name), False, str(exc), elapsed))

        for name, keys in self.missing:
            report.append((ENGINE_LABELS.get(name, name), False,
                           "未配置: " + " / ".join(keys), 0))
        return report
