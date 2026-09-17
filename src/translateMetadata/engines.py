# -*- coding: utf-8 -*-
"""翻译引擎：EDGE、Google、百度、腾讯云 TMT、LibreTranslate。

只依赖 Python 标准库（urllib / hashlib / hmac / json / time），无需 pip 安装任何东西。

设计要点：
  * 每个引擎暴露统一的 translate_detailed(text, source, target) -> (译文, 检出语言)
    顺带返回检出语言，是为了让上层能把「引擎判定为中文」的文本丢弃，
    这是纯汉字日文场景下防止误翻的关键。
  * Router 按优先级串联多个引擎，前一个失败自动降级到下一个。
  * 统一节流（rate_limit_ms），避免打爆接口或触发限流。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class EngineError(Exception):
    """引擎调用失败。上层会据此切换到下一个引擎。"""


# --------------------------------------------------------------------------- #
# 语言代码映射：插件内部统一用 zh-CN / zh-TW / en / ja / ko / ru 这类规范写法，
# 各引擎自己的写法在这里翻译。
# --------------------------------------------------------------------------- #
LANG_MAP = {
    "zh-CN": {"edge": "zh-Hans", "google": "zh-CN", "baidu": "zh", "tencent": "zh", "libretranslate": "zh"},
    "zh": {"edge": "zh-Hans", "google": "zh-CN", "baidu": "zh", "tencent": "zh", "libretranslate": "zh"},
    "zh-Hans": {"edge": "zh-Hans", "google": "zh-CN", "baidu": "zh", "tencent": "zh", "libretranslate": "zh"},
    "zh-TW": {"edge": "zh-Hant", "google": "zh-TW", "baidu": "cht", "tencent": "zh-TW", "libretranslate": "zt"},
    "zh-Hant": {"edge": "zh-Hant", "google": "zh-TW", "baidu": "cht", "tencent": "zh-TW", "libretranslate": "zt"},
    "en": {"edge": "en", "google": "en", "baidu": "en", "tencent": "en", "libretranslate": "en"},
    "ja": {"edge": "ja", "google": "ja", "baidu": "jp", "tencent": "ja", "libretranslate": "ja"},
    "ko": {"edge": "ko", "google": "ko", "baidu": "kor", "tencent": "ko", "libretranslate": "ko"},
    "ru": {"edge": "ru", "google": "ru", "baidu": "ru", "tencent": "ru", "libretranslate": "ru"},
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
# HTTP 基础
# --------------------------------------------------------------------------- #
def http_request(url, method="GET", headers=None, data=None, timeout=20, proxy=""):
    """发起一次 HTTP 请求，返回 (状态码, 响应字节)。

    不发送 Accept-Encoding: gzip，这样 urllib 不需要额外解压。
    """
    headers = dict(headers or {})
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式使用环境变量里的代理设置（默认行为），这里保持默认即可
        handlers.append(urllib.request.ProxyHandler())
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise EngineError("HTTP %s %s" % (exc.code, detail)) from exc
    except urllib.error.URLError as exc:
        raise EngineError("网络错误: %s" % (exc.reason,)) from exc
    except Exception as exc:  # 超时等
        raise EngineError("请求失败: %s" % (exc,)) from exc


def json_request(url, method="POST", headers=None, payload=None, timeout=20, proxy=""):
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw = http_request(url, method, headers, body, timeout, proxy)
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
        self.proxy = (options.get("http_proxy") or "").strip()
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
            status, raw = http_request(url, method, hdrs, body, self.timeout, self.proxy)
            try:
                return json.loads(raw.decode("utf-8", "replace"))
            except ValueError as exc:
                raise EngineError("响应不是合法 JSON: %s" % (raw[:200],)) from exc
        return json_request(url, method, headers, payload, self.timeout, self.proxy)

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
#    GET  https://edge.microsoft.com/translate/auth        取短期 JWT
#    POST https://api-edge.cognitive.microsofttranslator.com/translate
# --------------------------------------------------------------------------- #
class EdgeEngine(BaseEngine):
    name = "edge"
    AUTH_URL = "https://edge.microsoft.com/translate/auth"
    API_URL = "https://api-edge.cognitive.microsofttranslator.com/translate"
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0")

    def __init__(self, options=None):
        super().__init__(options)
        self._token = None
        self._token_at = 0.0

    def _get_token(self):
        # token 有效期约 10 分钟，留 60 秒余量
        if self._token and (time.time() - self._token_at) < 540:
            return self._token
        self._throttle()
        status, raw = http_request(
            self.AUTH_URL,
            headers={"User-Agent": self.UA, "Accept": "*/*"},
            timeout=self.timeout,
            proxy=self.proxy,
        )
        token = raw.decode("utf-8", "replace").strip()
        if status != 200 or not token or len(token) < 20:
            raise EngineError("EDGE 取 token 失败 (HTTP %s): %s" % (status, token[:200]))
        self._token = token
        self._token_at = time.time()
        return token

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        token = self._get_token()
        params = {"api-version": "3.0", "to": self.lang(target)}
        if source and source != "auto":
            params["from"] = self.lang(source)
        url = self.API_URL + "?" + urllib.parse.urlencode(params)

        data = self._request(
            url,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "User-Agent": self.UA,
                "Accept": "*/*",
            },
            payload=[{"Text": text}],
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
# --------------------------------------------------------------------------- #
class TencentEngine(BaseEngine):
    name = "tencent"
    HOST = "tmt.tencentcloudapi.com"
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
        self.region = (options.get("tencent_region") or "ap-guangzhou").strip()

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
        canonical_headers = "content-type:%s\nhost:%s\n" % (self.CONTENT_TYPE, self.HOST)
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
            "Host": self.HOST,
            "X-TC-Action": self.ACTION,
            "X-TC-Version": self.VERSION,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": self.region,
        }

        self._throttle()
        status, raw = http_request(
            "https://" + self.HOST,
            method="POST",
            headers=headers,
            data=payload_str.encode("utf-8"),
            timeout=self.timeout,
            proxy=self.proxy,
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
# 5. LibreTranslate（自托管或公共实例）
# --------------------------------------------------------------------------- #
class LibreTranslateEngine(BaseEngine):
    name = "libretranslate"

    def __init__(self, options=None):
        super().__init__(options)
        options = options or {}
        self.base_url = (options.get("libretranslate_url") or "http://localhost:5000").rstrip("/")
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
# 引擎注册表
# --------------------------------------------------------------------------- #
ENGINE_CLASSES = {
    EdgeEngine.name: EdgeEngine,
    GoogleEngine.name: GoogleEngine,
    BaiduEngine.name: BaiduEngine,
    TencentEngine.name: TencentEngine,
    LibreTranslateEngine.name: LibreTranslateEngine,
}

ENGINE_LABELS = {
    "edge": "EDGE（免费）",
    "google": "Google（免费）",
    "baidu": "百度翻译",
    "tencent": "腾讯云 TMT",
    "libretranslate": "LibreTranslate",
}

DEFAULT_CHAIN = ["edge", "google"]


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
    """按优先级串联多个引擎，失败自动降级。"""

    def __init__(self, engine_names, options=None):
        options = options or {}
        self.options = options
        self.engines = []
        self.missing = []

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
            try:
                translated, detected = engine.translate_detailed(text, source, target)
            except EngineError as exc:
                errors.append("%s: %s" % (engine.name, exc))
                continue
            except Exception as exc:  # 兜底，绝不让单引擎异常打断整条链
                errors.append("%s: 意外错误 %s" % (engine.name, exc))
                continue

            if translated is None:
                errors.append("%s: 返回空译文" % engine.name)
                continue

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

        raise EngineError("所有引擎均失败 -> " + " | ".join(errors))

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
