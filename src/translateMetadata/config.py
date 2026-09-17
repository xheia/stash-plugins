# -*- coding: utf-8 -*-
"""插件配置：从 Stash 的设置页读取，并做类型规范化。

Stash v0.31 起，插件在 yml 里声明的 settings 可以通过
    configuration { plugins(include: ["translateMetadata"]) }
读到实际填写的值（早期版本读不到，只能写死在 defaultArgs 里）。
所以所有可调项都放在设置页，改完即刻生效，不需要重载插件。

优先级：任务/钩子传入的 args（defaultArgs）> 设置页 > 内置默认值。
"""
from __future__ import annotations

import os

PLUGIN_ID = "translateMetadata"

SETTINGS_QUERY = """
query PluginSettings($include: [ID!]) {
  configuration {
    plugins(include: $include)
  }
}
"""

DEFAULTS = {
    # 引擎
    # EDGE 的免费接口已被微软下线（2026-09），所以默认链只留 Google ——
    # 它是唯一还活着且不需要凭证的引擎。有云端凭证的建议把 tencent 提到第一位。
    "engine": "google",
    "engine_fallback": "",
    "target_lang": "zh-CN",
    "source_lang": "auto",
    # 自动翻译开关（按实体）
    "auto_scene": True,
    "auto_performer": True,
    "auto_studio": True,
    "auto_tag": True,
    # 字段开关
    "translate_title": True,
    "translate_details": True,
    "translate_names": False,
    "tag_name_mode": "alias",
    # 行为
    "skip_existing": True,
    "ambiguous_cjk": "skip",
    "keep_original": True,
    "rate_limit_ms": 300,
    "timeout_s": 20,
    "retry_times": 1,
    "retry_backoff_ms": 800,
    "engine_skip_after": 3,
    "batch_size": 100,
    "max_items": 0,
    "cache_enabled": True,
    "http_proxy": "",
    # 凭证
    "baidu_appid": "",
    "baidu_key": "",
    "tencent_secret_id": "",
    "tencent_secret_key": "",
    "tencent_region": "ap-guangzhou",
    "alibaba_access_key": "",
    "alibaba_access_secret": "",
    "alibaba_region": "mt.aliyuncs.com",
    "libretranslate_url": "http://localhost:5000",
    "libretranslate_api_key": "",
    # DeepL / MyMemory / Lingva
    "deepl_api_key": "",
    "deepl_api_url": "",
    "mymemory_email": "",
    "lingva_instance": "https://lingva.ml",
    # AI 翻译（OpenAI 兼容）
    "openai_base_url": "",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "openai_prompt": "",
}

# --------------------------------------------------------------------------- #
# 旧键名兼容
#
# 不少中文 Stash 翻译插件用的是 translateXxx 驼峰命名，用户手里往往已经存了
# 一套凭证。这里做一层只读映射：设置页里没填本插件自己的键时，顺手看一眼旧键。
#
# 注意：故意不映射 translateTencentRegion —— 那个键在旧插件里存的是接口地址
# （https://tmt.tencentcloudapi.com），而本插件的 tencent_region 要的是地域
# （ap-guangzhou），张冠李戴会直接把请求打到错误的地域上。
# --------------------------------------------------------------------------- #
KEY_ALIASES = {
    "baidu_appid": ("translateBaiduAppid", "translateBaiduAppId"),
    "baidu_key": ("translateBaiduKey", "translateBaiduSecretKey"),
    "tencent_secret_id": ("translateTencentSecretId",),
    "tencent_secret_key": ("translateTencentSecretKey",),
    "alibaba_access_key": ("translateAlibabaAccessKey",),
    "alibaba_access_secret": ("translateAlibabaAccessSecret",),
    "alibaba_region": ("translateAlibabaRegion", "translateAlibabaEndpoint"),
    "libretranslate_url": ("translateLibretranslateUrl",),
    "libretranslate_api_key": ("translateLibretranslateApiKey",),
}

BOOL_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, bool)}
INT_KEYS = {k for k, v in DEFAULTS.items() if isinstance(v, int) and not isinstance(v, bool)}

TRUTHY = ("1", "true", "yes", "on", "y", "t")
FALSY = ("0", "false", "no", "off", "n", "f", "")


def to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return default


def to_int(value, default=0):
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_str(value, default=""):
    if value is None:
        return default
    return str(value).strip()


class Settings:
    """插件运行配置。"""

    def __init__(self, values=None, source="defaults", read_keys=None):
        self._values = dict(DEFAULTS)
        if values:
            self._values.update(values)
        # 配置是从哪儿来的，用于「改完没生效」这类排查（「测试翻译引擎」任务会打印）
        self.source = source
        self.read_keys = list(read_keys or [])

    def __getitem__(self, key):
        return self._values.get(key, DEFAULTS.get(key))

    def get(self, key, default=None):
        value = self._values.get(key)
        return DEFAULTS.get(key, default) if value is None else value

    def as_dict(self):
        return dict(self._values)

    # -- 派生属性 ---------------------------------------------------------- #
    @property
    def engine_chain(self):
        """引擎优先级列表，去重并保序。"""
        chain = []
        primary = to_str(self._values.get("engine"), DEFAULTS["engine"]).lower()
        if primary:
            chain.append(primary)
        raw_fallback = to_str(self._values.get("engine_fallback"), DEFAULTS["engine_fallback"])
        for item in raw_fallback.replace(";", ",").replace(" ", ",").split(","):
            item = item.strip().lower()
            if item and item not in chain:
                chain.append(item)
        return chain

    @property
    def tag_name_mode(self):
        mode = to_str(self._values.get("tag_name_mode"), "alias").lower()
        return mode if mode in ("alias", "rename") else "alias"

    @property
    def ambiguous_cjk(self):
        mode = to_str(self._values.get("ambiguous_cjk"), "skip").lower()
        return mode if mode in ("skip", "detect") else "skip"

    @property
    def target_lang(self):
        return to_str(self._values.get("target_lang"), "zh-CN") or "zh-CN"

    @property
    def source_lang(self):
        return to_str(self._values.get("source_lang"), "auto") or "auto"

    def engine_options(self):
        """传给引擎构造函数的参数。"""
        return {
            "timeout_s": max(5, to_int(self._values.get("timeout_s"), 20)),
            "rate_limit_ms": max(0, to_int(self._values.get("rate_limit_ms"), 300)),
            "retry_times": max(0, to_int(self._values.get("retry_times"), 1)),
            "retry_backoff_ms": max(0, to_int(self._values.get("retry_backoff_ms"), 800)),
            "engine_skip_after": max(0, to_int(self._values.get("engine_skip_after"), 3)),
            "http_proxy": to_str(self._values.get("http_proxy")),
            "baidu_appid": to_str(self._values.get("baidu_appid")),
            "baidu_key": to_str(self._values.get("baidu_key")),
            "tencent_secret_id": to_str(self._values.get("tencent_secret_id")),
            "tencent_secret_key": to_str(self._values.get("tencent_secret_key")),
            "tencent_region": to_str(self._values.get("tencent_region"), "ap-guangzhou"),
            "alibaba_access_key": to_str(self._values.get("alibaba_access_key")),
            "alibaba_access_secret": to_str(self._values.get("alibaba_access_secret")),
            "alibaba_region": to_str(self._values.get("alibaba_region"), DEFAULTS["alibaba_region"]),
            "libretranslate_url": to_str(self._values.get("libretranslate_url"), "http://localhost:5000"),
            "libretranslate_api_key": to_str(self._values.get("libretranslate_api_key")),
            "deepl_api_key": to_str(self._values.get("deepl_api_key")),
            "deepl_api_url": to_str(self._values.get("deepl_api_url")),
            "mymemory_email": to_str(self._values.get("mymemory_email")),
            "lingva_instance": to_str(self._values.get("lingva_instance"), "https://lingva.ml"),
            "openai_base_url": to_str(self._values.get("openai_base_url")),
            "openai_api_key": to_str(self._values.get("openai_api_key")),
            "openai_model": to_str(self._values.get("openai_model")),
            "openai_prompt": to_str(self._values.get("openai_prompt")),
        }

    def auto_enabled(self, entity):
        return to_bool(self._values.get("auto_" + entity), DEFAULTS.get("auto_" + entity, True))

    def field_enabled(self, gate):
        return to_bool(self._values.get(gate), DEFAULTS.get(gate, True))


def load_settings(api, args=None):
    """先读设置页的值，再用任务 / 钩子的 args 覆盖。

    读设置失败不致命 —— 退回到默认值继续跑，只在日志里记一笔。
    """
    args = args or {}
    overrides = {}
    from_settings = []
    from_alias = []

    raw = None
    try:
        data = api.call(SETTINGS_QUERY, {"include": [PLUGIN_ID]})
        raw = ((data.get("configuration") or {}).get("plugins") or {}).get(PLUGIN_ID) or {}
    except Exception:
        raw = None

    for key in DEFAULTS:
        # 设置页的值
        if raw and key in raw and raw[key] is not None and raw[key] != "":
            value = raw[key]
            from_settings.append(key)
        elif key in args and args[key] is not None:
            value = args[key]
        else:
            # 本插件自己的键没填，看看有没有旧插件命名的同义键
            alias_value = None
            for alias in KEY_ALIASES.get(key, ()):
                for src in (raw, args):
                    if src and src.get(alias) not in (None, ""):
                        alias_value = src[alias]
                        from_alias.append("%s<-%s" % (key, alias))
                        break
                if alias_value is not None:
                    break
            if alias_value is None:
                continue
            value = alias_value

        if key in BOOL_KEYS:
            overrides[key] = to_bool(value, DEFAULTS[key])
        elif key in INT_KEYS:
            overrides[key] = to_int(value, DEFAULTS[key])
        else:
            overrides[key] = to_str(value, DEFAULTS[key])

    # bool/int 类型一律按默认值同类型收口，避免字符串混进来
    for key in BOOL_KEYS:
        if key in overrides:
            overrides[key] = to_bool(overrides[key], DEFAULTS[key])
    for key in INT_KEYS:
        if key in overrides:
            overrides[key] = to_int(overrides[key], DEFAULTS[key])

    if raw is None:
        source = "unreadable"
    elif from_settings or from_alias:
        source = "settings"
    else:
        source = "empty"

    return Settings(overrides, source=source, read_keys=from_settings + from_alias)


def cache_path(plugin_dir, server_dir=""):
    """缓存文件位置：优先插件目录（稳定且可预期）。"""
    base = plugin_dir or server_dir or os.getcwd()
    return os.path.join(base, "translate-cache.sqlite3")
