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

# --------------------------------------------------------------------------- #
# 默认引擎链
#
# v1.2.6 起只有一个「翻译引擎链」设置，原先那个单独的「主翻译引擎」已取消 ——
# 两处配置容易互相打架（改了一个忘了另一个，表现就是"我改了怎么没生效"）。
#
# Stash 的插件清单不支持声明设置的默认值（pkg/plugin/config.go 里 SettingConfig
# 只有 type / displayName / description），所以默认链只能写在这儿：设置页留空即生效。
#
# 顺序 = 优先级，从左到右。没配凭证的引擎由 Router 自动跳过（进 missing 列表并
# 在「测试翻译引擎」里列出），不会白白浪费一次请求，所以链可以写长一点。
# 取舍：付费云服务（质量稳、有额度）→ DeepL / AI → 免费兜底。
# --------------------------------------------------------------------------- #
DEFAULT_ENGINE_CHAIN = ("tencent", "alibaba", "baidu", "deepl", "openai",
                        "google")

DEFAULTS = {
    # 引擎
    # 这里故意留空串：空 = 用户没填 = 用 DEFAULT_ENGINE_CHAIN。
    # 若把默认链直接写在这里，Settings 一构造就把它铺进 _values，
    # 就再也分不清"用户填了默认链"和"用户根本没填"了（旧版主引擎键的兼容逻辑依赖这个区分）。
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
    # 跳过策略：一个键管两件事（整合前是 skip_existing + ambiguous_cjk 两个键）
    "skip_policy": "smart",
    "keep_original": True,
    "rate_limit_ms": 300,
    "engine_skip_after": 3,
    "batch_size": 100,
    "cache_enabled": True,
    "http_proxy": "",
    # Stash 自身的 API Key（可选）：会话 Cookie 有硬性有效期（默认 1 小时），
    # 超长批量任务建议填上，见 stash_api.py 顶部的说明。留空 = 只用会话 Cookie。
    "stash_api_key": "",
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
    # DeepL
    "deepl_api_key": "",
    "deepl_api_url": "",
    # AI 翻译（OpenAI 兼容）
    "openai_base_url": "",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    "openai_prompt": "",
    # 各翻译服务的接口地址（留空 = 用引擎内置的官方地址，见 engines.py 里的 API_URL）
    # 用途：换镜像站、走自建反代、内网网关。填根地址或完整接口地址都行。
    "google_url": "",
    "edge_url": "",
    "baidu_url": "",
    "tencent_url": "",
    "alibaba_url": "",
}

# --------------------------------------------------------------------------- #
# 只能从任务参数传的键
#
# v1.2.7 起前三项从设置页撤掉了：超时与重试按「各引擎自己的默认值」走
# （机翻 20s / 1 次，AI 120s / 1 次，见 engines.py），单次实际翻译上限默认不限。
# 偶尔仍需要临时收口时（比如先试跑 20 条），在任务的 defaultArgs 里传即可：
#   {"max_items": "20"} / {"timeout_s": "45"} / {"retry_times": "0"}
#   {"fail_fast_after": "1"}  —— 连续失败几次就停（默认 3，0 = 关闭该保护）
# 它们不在 DEFAULTS 里，所以不会被设置页的空值覆盖，只有显式传入才生效。
# --------------------------------------------------------------------------- #
ARG_ONLY_KEYS = ("max_items", "timeout_s", "retry_times", "retry_backoff_ms",
                 "fail_fast_after")

# 连续多少次「所有引擎都失败」就停下任务。
#
# 引擎链整条走完仍然失败，说明是凭证 / 额度 / 代理这类全局问题，再往下扫库
# 只会把日志刷满、把时间耗光（线上真跑过：全库 6000 多条的每一条都在报同样的错）。
# 设成 3 是给瞬时抖动留一点余地；填 0 表示关闭这个保护（任务参数 fail_fast_after）。
FAIL_FAST_AFTER_DEFAULT = 3


def fail_fast_threshold(settings):
    """读出连续失败阈值：None/空 = 没填（用默认 3），0 = 显式关闭。

    必须把"没填"和"填 0"分开，所以不能写 `value or 默认值`（0 会被吞掉）。
    """
    value = settings.get("fail_fast_after")
    if value in (None, ""):
        return FAIL_FAST_AFTER_DEFAULT
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return FAIL_FAST_AFTER_DEFAULT

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
        """引擎优先级列表，去重并保序。

        设置页留空 → 用内置默认链 DEFAULT_ENGINE_CHAIN。

        兼容旧配置：老版本还有个单独的 `engine`（主翻译引擎）键，它已不再出现在
        设置页里；只有在「引擎链」也留空时才会被当作链首读一次，免得老用户升级后
        行为突变。链一旦填了，它就被忽略。
        """
        raw = to_str(self._values.get("engine_fallback"))
        if not raw:
            legacy = to_str(self._values.get("engine")).lower()
            if legacy:
                return [legacy]
            return list(DEFAULT_ENGINE_CHAIN)

        chain = []
        for item in raw.replace(";", ",").replace("，", ",").replace(" ", ",").split(","):
            item = item.strip().lower()
            if item and item not in chain:
                chain.append(item)
        return chain or list(DEFAULT_ENGINE_CHAIN)

    @property
    def tag_name_mode(self):
        mode = to_str(self._values.get("tag_name_mode"), "alias").lower()
        return mode if mode in ("alias", "rename") else "alias"

    @property
    def skip_policy(self):
        """跳过策略：一个键管两件事（v1.2.7 把 skip_existing + ambiguous_cjk 整合进来）。

          smart  —— 已是中文跳过；纯汉字（可能是日文）也跳过。最保守，默认
          detect —— 已是中文跳过；纯汉字交给引擎检测真实语言
          none   —— 一律翻译，连已是中文的也送引擎

        旧配置的两个键在 load_settings 里就被换算成这里的取值了，属性只做归一化。
        填错一律按 smart：宁可少翻，也不要把中文再翻一遍。
        """
        mode = to_str(self._values.get("skip_policy"), "smart").lower()
        return mode if mode in ("smart", "detect", "none") else "smart"

    @property
    def skip_existing(self):
        """是否跳过已是中文的文本（由 skip_policy 派生，保留旧属性名给下游用）。"""
        return self.skip_policy != "none"

    @property
    def ambiguous_cjk(self):
        """纯汉字文本怎么处理（由 skip_policy 派生）。"""
        return "detect" if self.skip_policy == "detect" else "skip"

    @property
    def target_lang(self):
        return to_str(self._values.get("target_lang"), "zh-CN") or "zh-CN"

    @property
    def source_lang(self):
        return to_str(self._values.get("source_lang"), "auto") or "auto"

    def engine_options(self):
        """传给引擎构造函数的参数。

        注意两个刻意的"不传"：
          * 超时 / 重试次数 / 重试等待 —— 设置页已取消（v1.2.7），各引擎用自己的
            类默认值（engines.py 的 default_timeout_s 等）。只有任务参数显式给了才传。
          * 各服务的接口地址 —— 留空表示"没自定义"，引擎会退回内置官方地址。
        """
        options = {
            "rate_limit_ms": max(0, to_int(self._values.get("rate_limit_ms"), 300)),
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
            "openai_base_url": to_str(self._values.get("openai_base_url")),
            "openai_api_key": to_str(self._values.get("openai_api_key")),
            "openai_model": to_str(self._values.get("openai_model")),
            "openai_prompt": to_str(self._values.get("openai_prompt")),
            # 各服务的接口地址（空串 = 没自定义，引擎退回内置官方地址）
            "google_url": to_str(self._values.get("google_url")),
            "edge_url": to_str(self._values.get("edge_url")),
            "baidu_url": to_str(self._values.get("baidu_url")),
            "tencent_url": to_str(self._values.get("tencent_url")),
            "alibaba_url": to_str(self._values.get("alibaba_url")),
        }

        # 超时 / 重试这三个键设置页里没有了，只有任务参数显式给了才往下传，
        # 否则引擎用自己的默认值（不传 = 让引擎决定）。
        for key in ("timeout_s", "retry_times", "retry_backoff_ms"):
            if self._values.get(key) not in (None, ""):
                options[key] = max(0, to_int(self._values.get(key), 0))
        return options

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
    from_args = []

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

    # 只能走任务参数的键（设置页已取消这三项）：显式传了才生效，
    # 并且不算作"设置来源是设置页"，免得日志里误导人。
    for key in ARG_ONLY_KEYS:
        if key in args and args[key] not in (None, ""):
            overrides[key] = max(0, to_int(args[key], 0))
            from_args.append("%s(任务参数)" % key)

    # 兼容 v1.2.6 之前的老配置：那时还有个单独的「主翻译引擎」键（engine）。
    # 它已经不在 DEFAULTS / 设置页里，所以上面那个循环不会碰它，这里单独读一次。
    # 只有在「翻译引擎链」也留空时才会真正生效（见 Settings.engine_chain）。
    if "engine" not in overrides:
        for src in (raw, args):
            legacy_engine = (src or {}).get("engine")
            if legacy_engine not in (None, ""):
                overrides["engine"] = to_str(legacy_engine, "")
                from_alias.append("engine_fallback<-engine(旧版主引擎键)")
                break

    # 兼容 v1.2.7 之前的两个键：skip_existing + ambiguous_cjk 已合并成 skip_policy。
    # 老配置里改过这两项的人不少（比如把「跳过已是中文」关掉），不换算的话升级后
    # 会静默变回默认策略。只在用户没填 skip_policy 时才做这层换算。
    if "skip_policy" not in overrides:
        legacy_skip = {}
        for legacy_key in ("skip_existing", "ambiguous_cjk"):
            for src in (raw, args):
                legacy_value = (src or {}).get(legacy_key)
                if legacy_value not in (None, ""):
                    legacy_skip[legacy_key] = legacy_value
                    break
        if legacy_skip:
            if not to_bool(legacy_skip.get("skip_existing"), True):
                mode = "none"
            elif to_str(legacy_skip.get("ambiguous_cjk")).lower() == "detect":
                mode = "detect"
            else:
                mode = "smart"
            overrides["skip_policy"] = mode
            from_alias.append("skip_policy<-%s(旧版键)" % ",".join(sorted(legacy_skip)))

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

    return Settings(overrides, source=source, read_keys=from_settings + from_alias + from_args)


def describe_engine_chain(settings):
    """引擎链来源说明，供「测试翻译引擎」任务打印。

    v1.2.6 起默认链写在代码里（DEFAULT_ENGINE_CHAIN），设置页留空即生效，
    所以"我的链到底是从哪来的"必须一眼能看出来 —— 否则又是一轮来回排查。
    """
    configured = to_str(settings.get("engine_fallback"))
    if configured:
        return "设置页：%s" % configured
    legacy = to_str(settings.get("engine"))
    if legacy:
        return "设置页留空，沿用旧版「主翻译引擎」：%s" % legacy
    return "设置页留空，用内置默认：%s" % ",".join(DEFAULT_ENGINE_CHAIN)


# --------------------------------------------------------------------------- #
# 设置页默认值初始化
#
# Stash 的插件清单声明不了设置项的默认值（PluginSetting 只有 name/display_name/
# description/type），所以设置页里没填过的键显示为空（NUMBER 甚至渲染成 0），
# 看起来像"默认值没生效"。这里在服务端把默认值补写进插件配置（configurePlugin），
# UI 上就能看到真实的默认值了。
#
# 规则：只补空缺（raw 里没有 / 空串 / None 的键），绝不覆盖用户已填的值。
# configurePlugin 是整体替换语义（stash 源码 Config.set(PluginsSettingPrefix+id, v)），
# 所以必须先读全量再合并写回，顺带保留 raw 里的旧版兼容键（engine 等）。
# 注意 engine_fallback 只在「它自己空 且 旧版主引擎键也没有」时才补种子 ——
# 否则老用户配置里的 engine 会被默认链悄悄顶掉。
# --------------------------------------------------------------------------- #
CONFIGURE_PLUGIN_MUTATION = """
mutation ConfigurePlugin($plugin_id: ID!, $input: Map!) {
  configurePlugin(plugin_id: $plugin_id, input: $input)
}
"""


def _init_defaults():
    """要种进设置页的默认值（只列「显示出来有意义」的键）。

    NUMBER/STRING 以字符串写入（与设置页保存行为一致），BOOLEAN 用真布尔 ——
    Stash 前端的复选框对字符串 "false" 也按真值渲染，传布尔才不会骗人。
    接口地址从各引擎类的 API_URL 取，避免和 engines.py 双处维护。
    凭证类（baidu/tencent/alibaba/openai 的 key）、隐藏键（engine_skip_after 等）
    不种 —— 空着就该空着。
    """
    import engines

    return {
        # 引擎链：填默认链，让"当前用的什么链"在 UI 一眼可见、可改
        "engine_fallback": ",".join(DEFAULT_ENGINE_CHAIN),
        # 翻译参数
        "source_lang": "auto",
        "target_lang": "zh-CN",
        "rate_limit_ms": "300",
        "batch_size": "100",
        # 范围开关
        "auto_scene": True,
        "auto_performer": True,
        "auto_studio": True,
        "auto_tag": True,
        "translate_title": True,
        "translate_details": True,
        "translate_names": False,
        "tag_name_mode": "alias",
        # 内容识别
        "skip_policy": "smart",
        "keep_original": True,
        # 接口地址（官方地址，想换镜像/反代时直接改这里）
        "google_url": engines.GoogleEngine.API_URL,
        "edge_url": engines.EdgeEngine.API_URL,
        "baidu_url": engines.BaiduEngine.API_URL,
        "tencent_url": "https://" + engines.TENCENT_DEFAULT_HOST,
        "alibaba_url": "https://" + engines.AlibabaEngine.DEFAULT_HOST,
        # 注意：libretranslate_url 故意不种 —— 它没有公共实例，"默认" localhost:5000
        # 种进配置会顶掉用户从旧插件键迁移来的真实地址（v1.2.8 就这么坑过一回）。
        # AI
        "openai_model": DEFAULTS["openai_model"],
    }


def missing_init_seeds(raw):
    """算出需要补种的键：INIT 默认值里有、而 raw 里空缺的。

    「空缺」还包括"本键没填但旧插件别名键填了"的情况 —— load_settings 的别名
    兼容还在给它供值，这时候种默认值会把真实值顶掉（v1.2.8 的教训）。
    """
    raw = raw or {}
    seeds = {}
    for key, value in _init_defaults().items():
        if raw.get(key) in (None, ""):
            aliased = any(raw.get(a) not in (None, "")
                          for a in KEY_ALIASES.get(key, ()))
            if not aliased:
                seeds[key] = value
    if "engine_fallback" in seeds and raw.get("engine") not in (None, ""):
        # 旧版「主翻译引擎」还在生效中，不动引擎链，兼容逻辑继续走
        del seeds["engine_fallback"]
    return seeds


def initialize_default_settings(api, log=None):
    """把默认值补写进插件配置。返回实际写入的键值；无需写入时返回空 dict。

    读不到配置（接口异常）就放弃 —— 初始化失败不该影响翻译任务本身。
    """
    try:
        data = api.call(SETTINGS_QUERY, {"include": [PLUGIN_ID]})
        raw = ((data.get("configuration") or {}).get("plugins") or {}).get(PLUGIN_ID) or {}
    except Exception as exc:
        if log:
            log.debug("默认值初始化跳过：读不到插件配置（%s）" % exc)
        return {}

    seeds = missing_init_seeds(raw)

    merged = dict(raw)
    merged.update(seeds)

    # 一次性迁移（v1.2.8 事故善后）：libretranslate_url 被种成 localhost:5000、
    # 而旧插件键里存着真实地址的用户，把真值找回来。
    if (merged.get("libretranslate_url") or "").strip().rstrip("/").lower() \
            in ("http://localhost:5000", "localhost:5000"):
        for alias in KEY_ALIASES.get("libretranslate_url", ()):
            legacy = (raw or {}).get(alias)
            if legacy not in (None, ""):
                merged["libretranslate_url"] = legacy
                seeds["libretranslate_url"] = "%s（从旧版键 %s 迁回）" % (legacy, alias)
                break

    if not seeds:
        return {}
    api.call(CONFIGURE_PLUGIN_MUTATION,
             {"plugin_id": PLUGIN_ID, "input": merged})
    if log:
        log.info("已把 %d 项默认值写入设置页（只补空缺，不覆盖已填的值）：%s"
                 % (len(seeds), ", ".join(sorted(seeds))))
    return seeds


def cache_path(plugin_dir, server_dir=""):
    """缓存文件位置：优先插件目录（稳定且可预期）。"""
    base = plugin_dir or server_dir or os.getcwd()
    return os.path.join(base, "translate-cache.sqlite3")
