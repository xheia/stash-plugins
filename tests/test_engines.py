# -*- coding: utf-8 -*-
"""引擎层单元测试（不触网）。

覆盖：
  * 阿里云 RPC 签名的 HMAC-SHA1 实现 —— 用官方文档给出的签名测试向量逐字节比对
  * 各引擎的语言码映射覆盖度
  * 阿里云接入地址的三种写法归一化
  * LibreTranslate 地址带 /translate 后缀时的兼容
  * Router 的降级、缺凭证提示、未知引擎名容忍

用法：python tests/test_engines.py
"""
from __future__ import annotations

import os
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "translateMetadata"))

import engines  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


def section(title):
    print("\n%s" % title)


# --------------------------------------------------------------------------- #
# 1. 阿里云签名：官方文档测试向量
# --------------------------------------------------------------------------- #
section("1) 阿里云 RPC 签名 —— 官方文档测试向量")

DOC_PARAMS = {
    "AccessKeyId": "testid",
    "Action": "DescribeRegions",
    "Format": "XML",
    "SignatureMethod": "HMAC-SHA1",
    "SignatureNonce": "3ee8c1b8-83d3-44af-a94f-4e0ad82fd6cf",
    "SignatureVersion": "1.0",
    "Timestamp": "2016-02-23T12:46:24Z",
    "Version": "2014-05-26",
}
DOC_EXPECTED = "OLeaidS1JvxuMvnyHOwuJ+uX5qY="

engine = engines.AlibabaEngine({"alibaba_access_key": "testid",
                                "alibaba_access_secret": "testsecret"})
check("官方向量签名一致", engine._sign(DOC_PARAMS, method="GET") == DOC_EXPECTED,
      "得到 %s" % engine._sign(DOC_PARAMS, method="GET"))

# 参数顺序不应影响结果（内部会排序）
shuffled = dict(reversed(list(DOC_PARAMS.items())))
check("参数顺序无关", engine._sign(shuffled, method="GET") == DOC_EXPECTED)

# 改一个字符就必须变（防止签名函数退化成常量）
wrong = dict(DOC_PARAMS)
wrong["Timestamp"] = "2016-02-23T12:46:25Z"
check("参数变更后签名变化", engine._sign(wrong, method="GET") != DOC_EXPECTED)

# 百分号编码细节：空格必须编成 %20 而非 +
check("百分号编码：空格 -> %20", engine._pe("a b") == "a%20b")
check("百分号编码：斜杠 -> %2F", engine._pe("/") == "%2F")
check("百分号编码：星号 -> %2A", engine._pe("*") == "%2A")
check("百分号编码：波浪线保留", engine._pe("~") == "~")
check("百分号编码：中文转 UTF-8", engine._pe("中") == "%E4%B8%AD")


# --------------------------------------------------------------------------- #
# 2. 阿里云接入地址归一化
# --------------------------------------------------------------------------- #
section("2) 阿里云接入地址归一化")


def host_of(value):
    return engines.AlibabaEngine({"alibaba_access_key": "k",
                                  "alibaba_access_secret": "s",
                                  "alibaba_region": value}).host


check("完整地址 -> 取 host", host_of("https://mt.cn-hangzhou.aliyuncs.com") == "mt.cn-hangzhou.aliyuncs.com")
check("带路径的完整地址 -> 取 host", host_of("https://mt.aliyuncs.com/api") == "mt.aliyuncs.com")
check("裸域名原样使用", host_of("mt.cn-hangzhou.aliyuncs.com") == "mt.cn-hangzhou.aliyuncs.com")
check("地域 ID -> 补全域名", host_of("cn-hangzhou") == "mt.cn-hangzhou.aliyuncs.com")
check("留空 -> 用默认中心接入点", host_of("") == "mt.aliyuncs.com")
check("endpoint 未带 scheme 也能解析",
      engines.AlibabaEngine({"alibaba_access_key": "k", "alibaba_access_secret": "s",
                             "alibaba_region": "mt.aliyuncs.com"}).endpoint == "https://mt.aliyuncs.com/")

# RegionId 自动从 host 推导
e = engines.AlibabaEngine({"alibaba_access_key": "k", "alibaba_access_secret": "s",
                           "alibaba_region": "https://mt.cn-hangzhou.aliyuncs.com"})
check("RegionId 自动推导", e.region_id == "cn-hangzhou", "得到 %r" % e.region_id)


# --------------------------------------------------------------------------- #
# 3. LibreTranslate 地址兼容
# --------------------------------------------------------------------------- #
section("3) LibreTranslate 地址兼容")


def libre_base(value):
    return engines.LibreTranslateEngine({"libretranslate_url": value}).base_url


check("根地址原样", libre_base("http://localhost:5000") == "http://localhost:5000")
check("结尾斜杠被去掉", libre_base("http://localhost:5000/") == "http://localhost:5000")
check("带 /translate 后缀被剥离",
      libre_base("http://192.168.3.96:5353/translate") == "http://192.168.3.96:5353")
check("带 /translate 且结尾斜杠也剥离",
      libre_base("http://192.168.3.96:5353/translate/") == "http://192.168.3.96:5353")
check("留空回退到默认值", libre_base("") == "http://localhost:5000")


# --------------------------------------------------------------------------- #
# 4. 语言码映射
# --------------------------------------------------------------------------- #
section("4) 语言码映射")

check("阿里云简体 zh-CN -> zh", engines.to_engine_lang("alibaba", "zh-CN") == "zh")
check("阿里云繁体 zh-TW -> zh-tw", engines.to_engine_lang("alibaba", "zh-TW") == "zh-tw")
check("腾讯云简体 -> zh", engines.to_engine_lang("tencent", "zh-CN") == "zh")
check("百度日语 -> jp", engines.to_engine_lang("baidu", "ja") == "jp")
check("百度韩语 -> kor", engines.to_engine_lang("baidu", "ko") == "kor")
check("EDGE 简体 -> zh-Hans", engines.to_engine_lang("edge", "zh-CN") == "zh-Hans")
check("下划线写法容错", engines.to_engine_lang("google", "zh_CN") == "zh-CN")
check("未知语言原样透传", engines.to_engine_lang("google", "xx-YY") == "xx-YY")
check("空值不炸", engines.to_engine_lang("google", "") == "")

# 每个 LANG_MAP 条目都应覆盖全部已注册引擎，否则会静默退回原码
missing_engine_map = []
for lang, table in engines.LANG_MAP.items():
    for name in engines.ENGINE_CLASSES:
        if name not in table:
            missing_engine_map.append("%s/%s" % (lang, name))
check("LANG_MAP 覆盖全部引擎", not missing_engine_map, "缺少: %s" % missing_engine_map)


# --------------------------------------------------------------------------- #
# 5. 引擎注册与凭证门禁
# --------------------------------------------------------------------------- #
section("5) 引擎注册与凭证门禁")

for name in ("edge", "google", "baidu", "tencent", "alibaba", "libretranslate"):
    check("引擎已注册: %s" % name, name in engines.ENGINE_CLASSES)
    check("引擎有中文标签: %s" % name, name in engines.ENGINE_LABELS)

check("无凭证时阿里云不可用",
      not engines.AlibabaEngine({}).available())
check("有凭证时阿里云可用",
      engines.AlibabaEngine({"alibaba_access_key": "a", "alibaba_access_secret": "b"}).available())
check("只给一半凭证仍不可用",
      not engines.AlibabaEngine({"alibaba_access_key": "a"}).available())
check("免费引擎（EDGE）无需凭证", engines.EdgeEngine({}).available())
check("腾讯云缺凭证正确声明",
      engines.TencentEngine({}).requires_credentials() == ["tencent_secret_id", "tencent_secret_key"])


# --------------------------------------------------------------------------- #
# 6. Router 组装
# --------------------------------------------------------------------------- #
section("6) Router 组装")

router = engines.Router(["edge", "google"], {})
check("免费引擎可直接进链", router.chain_names() == ["edge", "google"])
check("有可用引擎", router.has_engine())

router = engines.Router(["tencent", "alibaba"], {})
check("缺凭证的引擎被剔除", not router.has_engine())
check("缺凭证被记录下来", len(router.missing) == 2)

router = engines.Router(["alibaba", "tencent"], {
    "alibaba_access_key": "a", "alibaba_access_secret": "b",
})
check("有凭证的留存、缺凭证的剔除",
      router.chain_names() == ["alibaba"] and len(router.missing) == 1)

router = engines.Router(["不存在的引擎", "edge"], {})
check("未知引擎名被静默跳过", router.chain_names() == ["edge"])

router = engines.Router([], {})
try:
    router.translate("hi")
    check("空链应抛错", False)
except engines.EngineError as exc:
    check("空链抛出可读错误", "没有可用的翻译引擎" in str(exc), str(exc))


# --------------------------------------------------------------------------- #
# 7. 中文语言码判定
# --------------------------------------------------------------------------- #
section("7) 中文语言码判定")

for code in ("zh", "zh-CN", "zh-TW", "zh-Hans", "ZH-cn", "cht", "zt"):
    check("识别为中文: %s" % code, engines.is_chinese_code(code))
for code in ("en", "ja", "ko", "ru", "", None):
    check("不误判为中文: %s" % (code,), not engines.is_chinese_code(code))


# --------------------------------------------------------------------------- #
# 8. 阿里云 auto 源语言的降级重试
# --------------------------------------------------------------------------- #
section("8) 阿里云 auto 源语言降级重试")


class _FakeAlibaba(engines.AlibabaEngine):
    """记录调用序列。fail_sources 里的源语言会被桩成「接口不支持」。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []
        self.fail_sources = {"auto"}

    def _invoke(self, action, params):
        self.calls.append((action, params.get("SourceLanguage")))
        if action == self.DETECT_ACTION:
            return {"DetectedLanguage": "ja"}
        if params.get("SourceLanguage") in self.fail_sources:
            raise engines.EngineError("阿里云报错 NotSupported: 该源语言不被支持")
        return {"Code": "200", "Data": {"Translated": "测试译文", "DetectedLanguage": "ja"}}


fake = _FakeAlibaba({"alibaba_access_key": "a", "alibaba_access_secret": "b"})
text, detected = fake.translate_detailed("こんにちは", "auto", "zh-CN")
check("auto 失败后检出语言重试成功", text == "测试译文")
check("重试用的源语言来自检测结果", fake.calls[-1] == ("TranslateGeneral", "ja"),
      "调用序列 %s" % (fake.calls,))

fake2 = _FakeAlibaba({"alibaba_access_key": "a", "alibaba_access_secret": "b"})
fake2.fail_sources = set()
text2, _ = fake2.translate_detailed("hello", "auto", "zh-CN")
check("auto 直接可用时不多发请求",
      [c[0] for c in fake2.calls] == ["TranslateGeneral"], "调用序列 %s" % (fake2.calls,))

fake3 = _FakeAlibaba({"alibaba_access_key": "a", "alibaba_access_secret": "b"})
fake3.fail_sources = {"en"}
try:
    fake3.translate_detailed("hello", "en", "zh-CN")
    check("显式源语言失败时不重试", False, "竟然没抛错")
except engines.EngineError:
    check("显式源语言失败时不重试",
          [c[0] for c in fake3.calls] == ["TranslateGeneral"], "调用序列 %s" % (fake3.calls,))


# --------------------------------------------------------------------------- #
# 9. 代理地址归一化
# --------------------------------------------------------------------------- #
section("9) 代理地址归一化")

# ★ 实测撞到过：`http:192.168.3.96:7890` 少了两个斜杠，urllib 会把整个串当 authority，
#   主机名变成 "http:192.168.3.96"，于是**每个**引擎都报 DNS 解析失败。
check("少两个斜杠 -> 自动补回来",
      engines.normalize_proxy("http:192.168.3.96:7890") == "http://192.168.3.96:7890",
      engines.normalize_proxy("http:192.168.3.96:7890"))
check("只写 host:port -> 补 http://",
      engines.normalize_proxy("192.168.3.96:7890") == "http://192.168.3.96:7890")
check("正常写法原样保留",
      engines.normalize_proxy("http://192.168.3.96:7890") == "http://192.168.3.96:7890")
check("结尾斜杠去掉",
      engines.normalize_proxy("http://192.168.3.96:7890/") == "http://192.168.3.96:7890")
check("https 代理保留协议",
      engines.normalize_proxy("https://proxy.lan:8443") == "https://proxy.lan:8443")
check("带账号密码保留",
      engines.normalize_proxy("http://u:p@10.0.0.1:8080") == "http://u:p@10.0.0.1:8080")
check("带引号的写法也认", engines.normalize_proxy('"http://a.lan:1"') == "http://a.lan:1")
check("裸域名带端口不被误伤",
      engines.normalize_proxy("proxy.lan:7890") == "http://proxy.lan:7890",
      engines.normalize_proxy("proxy.lan:7890"))
check("纯主机名带端口不被误伤",
      engines.normalize_proxy("myproxy:8080") == "http://myproxy:8080")
check("IPv6 字面量能处理",
      engines.normalize_proxy("[::1]:8080") == "http://[::1]:8080")
check("空白 -> 空", engines.normalize_proxy("   ") == "")
check("None -> 空", engines.normalize_proxy(None) == "")

# SOCKS 代理要给出可操作的报错，而不是抛 urllib 的 unknown url type
try:
    engines.http_request("http://example.invalid/", proxy="socks5://127.0.0.1:1080")
    check("SOCKS 代理应报错", False, "竟然没抛错")
except engines.EngineError as exc:
    check("SOCKS 代理给出可操作提示", "不支持 SOCKS" in str(exc), str(exc))


# --------------------------------------------------------------------------- #
# 10. 内网地址识别（决定要不要绕开代理）
# --------------------------------------------------------------------------- #
section("10) 内网地址识别")

for host in ("localhost", "127.0.0.1", "192.168.3.96", "10.0.0.5",
             "172.16.0.1", "172.31.255.254", "169.254.1.1", "nas", "::1"):
    check("算内网: %s" % host, engines.is_local_host(host))
for host in ("translate.googleapis.com", "edge.microsoft.com", "8.8.8.8",
             "172.15.0.1", "172.32.0.1", "100.63.0.1", ""):
    check("不算内网: %s" % (host or "(空)",), not engines.is_local_host(host))
check("CGNAT 段算内网", engines.is_local_host("100.64.0.1"))


# --------------------------------------------------------------------------- #
# 11. 腾讯云地域 / 接入地址归一化
# --------------------------------------------------------------------------- #
section("11) 腾讯云地域归一化")


def tencent_endpoint(value):
    e = engines.TencentEngine({"tencent_secret_id": "i", "tencent_secret_key": "k",
                               "tencent_region": value})
    return e.host, e.region


check("裸地域 -> 补全接入点",
      tencent_endpoint("ap-guangzhou") == ("tmt.ap-guangzhou.tencentcloudapi.com", "ap-guangzhou"))
# ★ 实测撞到过：用户填接口地址，被原样塞进 X-TC-Region，服务端报 InvalidParameterValue
# ★ v1.2.9 收口：主域名（不带地域段）一律换算成地域域名再请求 —— 腾讯网关对
#   「主域名 + X-TC-Region」组合也报 region invalid（v1.2.8 种子触发过）
check("完整接口地址 -> 换算成地域域名",
      tencent_endpoint("https://tmt.tencentcloudapi.com") == ("tmt.ap-guangzhou.tencentcloudapi.com", "ap-guangzhou"))
check("带地域的完整地址 -> 提取地域",
      tencent_endpoint("https://tmt.ap-shanghai.tencentcloudapi.com")
      == ("tmt.ap-shanghai.tencentcloudapi.com", "ap-shanghai"))
check("裸域名 -> 提取地域",
      tencent_endpoint("tmt.ap-beijing.tencentcloudapi.com")
      == ("tmt.ap-beijing.tencentcloudapi.com", "ap-beijing"))
check("留空 -> 默认（地域域名，v1.2.9 收口后与旧版行为一致）",
      tencent_endpoint("") == ("tmt.ap-guangzhou.tencentcloudapi.com", "ap-guangzhou"))
check("自定义内网接入点保留 host",
      tencent_endpoint("https://tmt.internal.corp")[0] == "tmt.internal.corp")

_tc_gz = engines.TencentEngine({"tencent_secret_id": "i", "tencent_secret_key": "k",
                                "tencent_region": "ap-guangzhou"})
_tc_sh = engines.TencentEngine({"tencent_secret_id": "i", "tencent_secret_key": "k",
                                "tencent_region": "ap-shanghai"})
check("签名里的 host 随接入点变化",
      _tc_gz._sign("{}", 1700000000) != _tc_sh._sign("{}", 1700000000))


# --------------------------------------------------------------------------- #
# 12. 阿里云 TranslateGeneral 的必填参数
# --------------------------------------------------------------------------- #
section("12) 阿里云 TranslateGeneral 必填参数")


class _CaptureAlibaba(engines.AlibabaEngine):
    """记录实际发出的参数，验证必填项一个不少。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen = []

    def _invoke(self, action, params):
        self.seen.append((action, dict(params)))
        return {"Code": "200", "Data": {"Translated": "你好", "DetectedLanguage": "en"}}


capture = _CaptureAlibaba({"alibaba_access_key": "a", "alibaba_access_secret": "b"})
capture.translate_detailed("hello", "en", "zh-CN")
_action, _params = capture.seen[-1]
# ★ 实测撞到过：缺 FormatType，服务端报 "FormatType is mandatory for this action."
check("TranslateGeneral 必须带 FormatType",
      _params.get("FormatType") == "text", str(_params))
check("TranslateGeneral 带 Scene", _params.get("Scene") == "general")
check("目标语言已映射成引擎写法", _params.get("TargetLanguage") == "zh")
check("显式源语言已映射成引擎写法", _params.get("SourceLanguage") == "en")
check("原文带上", _params.get("SourceText") == "hello")


# --------------------------------------------------------------------------- #
# 13. 退化译文拦截
# --------------------------------------------------------------------------- #
section("13) 退化译文拦截")

import detect as detect_mod  # noqa: E402

# ★ 实测撞到过：LibreTranslate 把人名串翻成「相相相相相相相相…」，还被当成成功写进了库
check("识别单字长串重复", detect_mod.looks_degenerate("相" * 12))
check("识别拉丁字符长串重复", detect_mod.looks_degenerate("a" * 20))
check("识别单字占比过半的文本", detect_mod.looks_degenerate("相相生相相相相相相生" * 3))
check("识别两字符交替刷屏", detect_mod.looks_degenerate("相生" * 10))
check("正常译文放行", not detect_mod.looks_degenerate("美丽的护士秋穗"))
check("正常长句放行",
      not detect_mod.looks_degenerate("这是一段正常的场景简介，包含足够多的不同字符用于通过判定。"))
check("正常叠字放行（未达阈值）", not detect_mod.looks_degenerate("哈哈哈哈"))
check("常见字多次出现但不刷屏，放行",
      not detect_mod.looks_degenerate("他一个人一个人地走过去，一个人一个人地回来。"))
check("带英文与编号的标题放行",
      not detect_mod.looks_degenerate("Cospuri #461: Ria Kurumi"))
check("中文夹英文的标题放行",
      not detect_mod.looks_degenerate("4K 修复版 中文字幕，全长 120 分钟"))
check("空串放行", not detect_mod.looks_degenerate(""))
check("纯标点放行", not detect_mod.looks_degenerate("--------------"))


def _make_stub(name, reply=None, error=None, counter=None):
    """造一个可编排的桩引擎并注册进注册表。"""
    class _Stub(engines.BaseEngine):
        pass

    _Stub.name = name

    def translate_detailed(self, text, source="auto", target="zh-CN"):
        if counter is not None:
            counter.append(name)
        if error is not None:
            raise error if isinstance(error, engines.EngineError) else engines.EngineError(error)
        return reply, "en"

    _Stub.translate_detailed = translate_detailed
    engines.ENGINE_CLASSES[name] = _Stub
    return _Stub


def _drop_stubs():
    for name in [n for n in engines.ENGINE_CLASSES if n.startswith("_")]:
        del engines.ENGINE_CLASSES[name]


stub_calls = []
_make_stub("_degen", reply="相" * 30, counter=stub_calls)
_make_stub("_good", reply="正常译文", counter=stub_calls)
_router = engines.Router(["_degen", "_good"], {})
_result = _router.translate("hello")
check("退化译文被丢弃并降级到下一个引擎",
      _result.engine == "_good" and _result.text == "正常译文",
      "%s / %s" % (_result.engine, _result.text))
check("降级过程记录在 attempts 里",
      any("退化" in a for a in _result.attempts), str(_result.attempts))

_make_stub("_degen_only", reply="x" * 40)
try:
    engines.Router(["_degen_only"], {}).translate("hello")
    check("全部退化时抛错", False, "竟然没抛错")
except engines.EngineError as exc:
    check("全部退化时抛错并说明原因", "退化" in str(exc), str(exc))


# --------------------------------------------------------------------------- #
# 14. 引擎熔断
# --------------------------------------------------------------------------- #
section("14) 引擎熔断")

bench_calls = []
_make_stub("_dead", error="接口挂了", counter=bench_calls)
_make_stub("_alive", reply="好的", counter=bench_calls)
_router = engines.Router(["_dead", "_alive"], {"engine_skip_after": 2})
for _ in range(6):
    _router.translate("hello")
check("连续失败达阈值后不再尝试该引擎",
      bench_calls.count("_dead") == 2, "实际调用 %d 次" % bench_calls.count("_dead"))
check("熔断名单可查", _router.benched_names() == ["_dead"], str(_router.benched_names()))
check("健康引擎不受影响", _router.translate("hi").engine == "_alive")

off_calls = []
_make_stub("_dead0", error="接口挂了", counter=off_calls)
_make_stub("_alive0", reply="好的", counter=off_calls)
_router = engines.Router(["_dead0", "_alive0"], {"engine_skip_after": 0})
for _ in range(4):
    _router.translate("hello")
check("阈值 0 表示关闭熔断",
      off_calls.count("_dead0") == 4, "实际调用 %d 次" % off_calls.count("_dead0"))

_ok_calls = []
_make_stub("_flaky", reply="好的", counter=_ok_calls)
_router = engines.Router(["_flaky"], {"engine_skip_after": 1})
for _ in range(3):
    _router.translate("hello")
check("成功会清零失败计数", not _router.benched_names(), str(_router.benched_names()))


# --------------------------------------------------------------------------- #
# 15. 失败报告要说清楚「谁没上」
# --------------------------------------------------------------------------- #
section("15) 失败报告")

_make_stub("_dead_a", error="HTTP 404")
try:
    engines.Router(["_dead_a", "tencent"], {"engine_skip_after": 1}).translate("hello")
    check("应抛错", False, "竟然没抛错")
except engines.EngineError as exc:
    message = str(exc)
check("报告含失败引擎与原因", "_dead_a: HTTP 404" in message, message)
check("报告含被熔断的引擎", "暂时跳过" in message, message)
check("报告含缺凭证的引擎", "tencent" in message and "未配置" in message, message)

_make_stub("_dead_b", error="网络错误: [Errno -2] Name or service not known")
try:
    engines.Router(["_dead_b"], {"http_proxy": "http:192.168.3.96:7890"}).translate("hi")
    check("应抛错", False, "竟然没抛错")
except engines.EngineError as exc:
    hint = str(exc)
check("域名解析失败时点出代理问题", "http_proxy" in hint, hint)
check("顺带展示规范化后的代理地址", "http://192.168.3.96:7890" in hint, hint)

_drop_stubs()


# --------------------------------------------------------------------------- #
# 16. 报错文本压缩
# --------------------------------------------------------------------------- #
section("16) 报错文本压缩")

_html = ('<html><head><title>Sorry...</title></head><body><div><p>Sorry, your computer '
         'or network may be sending automated queries.</p></div></body></html>')
check("HTML 标签被剥掉", "<" not in engines.brief(_html), engines.brief(_html))
check("保留可读文字", "automated queries" in engines.brief(_html))
check("超长被截断", engines.brief("x" * 500).endswith("…"))
check("换行被压平", "\n" not in engines.brief("a\n\nb"))
check("空输入不炸", engines.brief("") == "")
check("None 不炸", engines.brief(None) == "")


# --------------------------------------------------------------------------- #
# 17. DeepL 引擎（Free / Pro 档位与响应解析）
# --------------------------------------------------------------------------- #
section("17) DeepL 引擎")

import json as _json

_real_http_request = engines.http_request
_real_json_request = engines.json_request


def _stub_http(status, payload):
    raw = _json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def fake(url, method="GET", headers=None, data=None, timeout=20, proxy="",
             retries=0, backoff_ms=800):
        fake.last_url = url
        fake.last_headers = dict(headers or {})
        fake.last_body = (data or b"").decode("utf-8")
        fake.calls += 1
        return status, raw

    fake.calls = 0

    return fake


def _stub_http_error(message):
    def fake(*args, **kwargs):
        raise engines.EngineError(message)
    return fake


deepl = engines.DeepLEngine({"deepl_api_key": "abc123:fx"})
check("免费档 Key 走 api-free", deepl.api_url.startswith("https://api-free.deepl.com/v2/translate"),
      deepl.api_url)
deepl_pro = engines.DeepLEngine({"deepl_api_key": "abc123"})
check("专业档 Key 走 api.deepl.com", deepl_pro.api_url.startswith("https://api.deepl.com/v2/translate"),
      deepl_pro.api_url)
deepl_manual = engines.DeepLEngine({"deepl_api_key": "abc123:fx",
                                    "deepl_api_url": "https://api.deepl.com/v2"})
check("显式接口地址优先", deepl_manual.api_url.startswith("https://api.deepl.com/v2/translate"),
      deepl_manual.api_url)
check("语言码映射 zh-CN -> ZH", engines.to_engine_lang("deepl", "zh-CN") == "ZH")

engines.http_request = _stub_http(200, {"translations": [
    {"text": "你好，世界", "detected_source_language": "EN"}]})
out = deepl.translate_detailed("Hello, world")
check("DeepL 正常解析", out == ("你好，世界", "EN"), str(out))
check("DeepL 请求打到 translate 端点", "/v2/translate" in deepl_translate_url if (deepl_translate_url := getattr(engines.http_request, "last_url", "")) else False,
      str(getattr(engines.http_request, "last_url", "")))
check("DeepL 用 Authorization 头鉴权（表单不再带 auth_key）",
      "auth_key=" not in engines.http_request.last_body
      and engines.http_request.last_headers.get("Authorization") == "DeepL-Auth-Key abc123:fx",
      "headers=%s body=%s" % (engines.http_request.last_headers, engines.http_request.last_body[:80]))

engines.http_request = _stub_http(403, {"message": "Wrong key"})
try:
    deepl.translate_detailed("hi")
    check("DeepL 403 应抛错", False)
except engines.EngineError as exc:
    check("DeepL 403 提示档位问题", "403" in str(exc) and "api-free" in str(exc), str(exc))

engines.http_request = _stub_http(456, {"message": "Quota exceeded"})
try:
    deepl.translate_detailed("hi")
    check("DeepL 456 应抛错", False)
except engines.EngineError as exc:
    check("DeepL 456 提示配额", "配额" in str(exc), str(exc))

engines.http_request = _real_http_request


# --------------------------------------------------------------------------- #
# 18. 失败分类：额度 / 凭证 / 普通（决定要不要继续在这个引擎上花时间）
#
# 背景：线上出现过「全库几千条，每条都在同一个死引擎上白打一次请求」——
# 账号欠费、Key 填错这类错误重试一万次也一样，必须一次定性、本轮停用它。
# --------------------------------------------------------------------------- #
section("18) 失败分类（额度 / 凭证）")

quota_samples = [
    "腾讯云报错 FailedOperation.NoFreeAmount: 免费额度已用完",
    "腾讯云报错 FailedOperation.InsufficientBalance: 账户余额不足",
    "百度翻译报错 54004: 账户余额不足",
    "百度翻译报错 58002: 服务当前已关闭",
    "阿里云报错 InvalidAccountStatus: 未开通服务",
    "DeepL 配额已用完 (HTTP 456)：本月免费额度耗尽",
]
for sample in quota_samples:
    check("额度类: %s" % sample[:24], engines.looks_like_quota(sample), sample)

cred_samples = [
    "DeepL 认证失败 (HTTP 403)：Key 无效或填错档位",
    "腾讯云报错 AuthFailure.SignatureFailure: 签名错误",
    "阿里云报错 InvalidAccessKeyId.NotFound: 无效的 AccessKeyId",
    "百度翻译报错 52003: 未授权用户",
]
for sample in cred_samples:
    check("凭证类: %s" % sample[:24], engines.looks_like_credentials(sample), sample)

check("限流不算额度类（429 值得重试）",
      not engines.looks_like_quota("HTTP 429 Too Many Requests"))
check("网络错误不算额度类",
      not engines.looks_like_quota("网络错误: [SSL: UNEXPECTED_EOF_WHILE_READING]"))
check("额度类抛 EngineQuotaError",
      isinstance(engines.api_error("余额不足"), engines.EngineQuotaError))
check("凭证类抛 EngineCredentialsError",
      isinstance(engines.api_error("HTTP 403 forbidden"), engines.EngineCredentialsError))
check("普通错误仍是 EngineError",
      type(engines.api_error("网络错误: timeout")) is engines.EngineError)

# 全链停用 -> 任务该停：额度用尽给"无额度"，凭证问题给"凭证"提示
engines.http_request = _stub_http(200, {"error_code": 54004, "error_msg": "账户余额不足"})
router_quota = engines.Router(["baidu"], {"baidu_appid": "a", "baidu_key": "b"})
try:
    router_quota.translate("hello")
    check("额度用尽应抛错", False)
except engines.EngineQuotaError as exc:
    check("额度用尽抛 EngineQuotaError", "额度" in str(exc) and "baidu" in str(exc), str(exc))
check("额度用尽的引擎被停用", router_quota.unusable_names() == ["baidu"],
      str(router_quota.unusable_names()))
check("全链停用时给出停止信号", router_quota.stop_exception() is not None)
check("停用信号里写明是额度问题",
      "额度" in str(router_quota.stop_exception()), str(router_quota.stop_exception()))

# 停用的引擎在后续文本上不再被打扰（请求计数是关键）
engines.http_request.last_url = None
calls_before = engines.http_request.calls
try:
    router_quota.translate("world")
except engines.EngineError:
    pass
check("已被停用的引擎不会再被调用",
      engines.http_request.calls == calls_before, str(engines.http_request.calls))

# 凭证类同理
engines.http_request = _stub_http(200, {"error_code": 52003, "error_msg": "未授权用户"})
router_cred = engines.Router(["baidu"], {"baidu_appid": "a", "baidu_key": "b"})
try:
    router_cred.translate("hello")
    check("凭证无效应抛错", False)
except engines.EngineCredentialsError as exc:
    check("凭证无效抛 EngineCredentialsError", "未授权" in str(exc), str(exc))
check("凭证类停用后给出停止信号",
      isinstance(router_cred.stop_exception(), engines.EngineCredentialsError))

engines.http_request = _real_http_request


# --------------------------------------------------------------------------- #
# 19. 请求次数不重复：一段文本、一个引擎，最多 retries+1 次 HTTP
#
# 背景：重试与"网络异常换链路"曾经各自计数，最坏一次翻译打出 4 个请求
# —— 额度掉得莫名其妙，还查不出原因。现在两者共用同一份预算。
# --------------------------------------------------------------------------- #
section("19) 请求预算（不重复调用）")


class _CountingOpener:
    """只数请求次数的 opener：不发真请求，按配置抛网络错误或 5xx。"""

    def __init__(self, state):
        self.state = state

    def open(self, req, timeout=None):
        self.state["opens"] += 1
        if self.state.get("http_status"):
            raise urllib.error.HTTPError(
                req.full_url, self.state["http_status"], "boom", {}, None)
        raise urllib.error.URLError("boom")


_real_build_opener = engines._build_opener
_real_routes = engines._routes
_real_cache = dict(engines._ROUTE_CACHE)

state = {"opens": 0}
engines._ROUTE_CACHE.clear()
engines._build_opener = lambda route: _CountingOpener(state)
engines._routes = lambda proxy, host: [("代理 %s" % proxy, proxy), ("直连", "")]
try:
    engines.http_request("https://example.invalid/x", proxy="http://127.0.0.1:7890", retries=1)
    check("网络异常最终应抛错", False)
except engines.EngineError as exc:
    check("网络异常最终抛 EngineError", "网络错误" in str(exc), str(exc))
check("重试与换链路共用预算：retries=1 -> 最多 2 次请求",
      state["opens"] == 2, "实际 %d 次" % state["opens"])

state["opens"] = 0
try:
    engines.http_request("https://example.invalid/x", proxy="http://127.0.0.1:7890", retries=0)
except engines.EngineError:
    pass
check("retries=0 -> 只发 1 次请求", state["opens"] == 1, "实际 %d 次" % state["opens"])

# 没配代理且环境里也没有代理时只有一条链路：网络错误不在这条链路上反复试
state["opens"] = 0
engines._routes = lambda proxy, host: [("直连", "")]
try:
    engines.http_request("https://example.invalid/x", retries=1)
except engines.EngineError:
    pass
check("单链路上的网络错误只发 1 次（交给上层换引擎）",
      state["opens"] == 1, "实际 %d 次" % state["opens"])

# 单链路上的 5xx 仍然按预算原地重试
state["opens"] = 0
state["http_status"] = 503
try:
    engines.http_request("https://example.invalid/x", retries=1, backoff_ms=1)
except engines.EngineError as exc:
    check("5xx 最终抛错", "503" in str(exc), str(exc))
check("单链路上的 503 重试 1 次（共 2 次）", state["opens"] == 2, "实际 %d 次" % state["opens"])
state.pop("http_status")

engines._build_opener = _real_build_opener
engines._routes = _real_routes
engines._ROUTE_CACHE.clear()
engines._ROUTE_CACHE.update(_real_cache)


# --------------------------------------------------------------------------- #
# 19. AI 翻译（OpenAI 兼容）引擎
# --------------------------------------------------------------------------- #
section("20) AI 翻译（OpenAI 兼容）引擎")


def openai_base(options):
    return engines.OpenAIEngine(options).base_url


check("只填 Key -> OpenAI 官方地址",
      openai_base({"openai_api_key": "sk-x"}) == "https://api.openai.com/v1",
      openai_base({"openai_api_key": "sk-x"}))
check("DeepSeek 域名补 /v1",
      openai_base({"openai_base_url": "https://api.deepseek.com"}) == "https://api.deepseek.com/v1",
      openai_base({"openai_base_url": "https://api.deepseek.com"}))
check("Ollama 补 /v1",
      openai_base({"openai_base_url": "http://localhost:11434"}) == "http://localhost:11434/v1",
      openai_base({"openai_base_url": "http://localhost:11434"}))
check("自定义路径尊重原样",
      openai_base({"openai_base_url": "https://gw.example.com/api/openai"}) == "https://gw.example.com/api/openai",
      openai_base({"openai_base_url": "https://gw.example.com/api/openai"}))
check("/chat/completions 后缀被剥离",
      openai_base({"openai_base_url": "https://api.deepseek.com/v1/chat/completions"}) == "https://api.deepseek.com/v1",
      openai_base({"openai_base_url": "https://api.deepseek.com/v1/chat/completions"}))
check("只填地址（本地服务）即可用", engines.OpenAIEngine({"openai_base_url": "http://localhost:11434"}).available())
check("只填 Key 即可用", engines.OpenAIEngine({"openai_api_key": "sk-x"}).available())
check("两者都不填不可用", not engines.OpenAIEngine({}).available())
check("默认模型 gpt-4o-mini",
      engines.OpenAIEngine({"openai_api_key": "sk"}).model == "gpt-4o-mini")
check("自定义模型生效",
      engines.OpenAIEngine({"openai_api_key": "sk", "openai_model": "deepseek-chat"}).model == "deepseek-chat")

ai = engines.OpenAIEngine({"openai_base_url": "https://api.deepseek.com",
                           "openai_api_key": "sk-test", "openai_model": "deepseek-chat"})
captured = {}


def _stub_json(url, method="POST", headers=None, payload=None, timeout=20, proxy="",
               retries=0, backoff_ms=800):
    captured.update({"url": url, "headers": dict(headers or {}), "payload": payload})
    return {"choices": [{"message": {"content": "  \"你好世界\"  "}}]}


engines.json_request = _stub_json
out = ai.translate_detailed("Hello world")
check("AI 正常解析并去引号", out[0] == "你好世界", str(out))
check("AI 请求打到 chat/completions",
      captured["url"] == "https://api.deepseek.com/v1/chat/completions", captured["url"])
check("AI 带模型名", captured["payload"]["model"] == "deepseek-chat")
check("AI 带 Bearer 头", captured["headers"].get("Authorization") == "Bearer sk-test")
check("AI 提示词含目标语言", "简体中文" in captured["payload"]["messages"][0]["content"])
check("AI 用户消息是原文", captured["payload"]["messages"][1]["content"] == "Hello world")

engines.json_request = lambda *a, **k: {"error": {"message": "Incorrect API key"}}
try:
    ai.translate_detailed("hi")
    check("AI 报错应抛错", False)
except engines.EngineError as exc:
    check("AI 错误透出 message", "Incorrect API key" in str(exc), str(exc))

engines.json_request = _real_json_request


# --------------------------------------------------------------------------- #
# 21. EDGE 免认证端点（translatetext）
# --------------------------------------------------------------------------- #
section("21) EDGE 免认证端点")

edge = engines.EdgeEngine({"timeout_s": 20})
engines.http_request = _stub_http(200, [
    {"detectedLanguage": {"language": "en"},
     "translations": [{"text": "你好，世界", "to": "zh-Hans"}]}])
out = edge.translate_detailed("Hello, world", "auto", "zh-CN")
check("EDGE 正常解析", out == ("你好，世界", "en"), str(out))
check("EDGE 打到免认证端点", "edge.microsoft.com/translate/translatetext" in engines.http_request.last_url,
      engines.http_request.last_url)
check("EDGE 带 Origin 伪装头", engines.http_request.last_headers.get("Origin") == "https://www.microsoft.com")
check("EDGE 不带 Authorization", "Authorization" not in engines.http_request.last_headers)

engines.http_request = _stub_http_error("网络错误: [WinError 10054] 远程主机强迫关闭了一个现有的连接")
try:
    edge.translate_detailed("hi")
    check("EDGE 连接被重置应抛错", False)
except engines.EngineError as exc:
    check("EDGE 被重置时给出网络路径提示", "代理" in str(exc) or "重置" in str(exc), str(exc))

engines.http_request = _real_http_request


# --------------------------------------------------------------------------- #
# 22. 各翻译服务的自定义接口地址（v1.2.7）
#     留空 = 官方地址；填了就用填的（换镜像站 / 自建反代 / 内网网关）。
# --------------------------------------------------------------------------- #
section("22) 自定义接口地址")

check("Google 默认官方地址",
      engines.GoogleEngine({}).api_url == "https://translate.googleapis.com/translate_a/single",
      engines.GoogleEngine({}).api_url)
check("百度默认官方地址",
      engines.BaiduEngine({}).api_url == "https://fanyi-api.baidu.com/api/trans/vip/translate")
check("EDGE 默认官方地址",
      engines.EdgeEngine({}).api_url == "https://edge.microsoft.com/translate/translatetext")

check("Google 用自定义地址",
      engines.GoogleEngine({"google_url": "https://mt.example.com/g"}).api_url
      == "https://mt.example.com/g")
check("百度用自定义地址",
      engines.BaiduEngine({"baidu_url": "https://baidu.example.com/api"}).api_url
      == "https://baidu.example.com/api")
check("EDGE 用自定义地址（顺手去空格）",
      engines.EdgeEngine({"edge_url": "  https://edge.example.com/tt  "}).api_url
      == "https://edge.example.com/tt")

# 光"读到了"不算数，得确认请求真的打到自定义地址上
engines.http_request = _stub_http(200, [
    {"detectedLanguage": {"language": "en"},
     "translations": [{"text": "你好", "to": "zh-Hans"}]}])
engines.EdgeEngine({"edge_url": "https://edge.example.com/tt"}).translate_detailed("hi")
check("EDGE 请求确实打到自定义地址",
      engines.http_request.last_url.startswith("https://edge.example.com/tt"),
      engines.http_request.last_url)

engines.http_request = _stub_http(200, {"Response": {"TargetText": "你好", "Source": "en"}})
engines.TencentEngine({"tencent_secret_id": "i", "tencent_secret_key": "k",
                       "tencent_url": "https://tmt.ap-shanghai.tencentcloudapi.com"}
                      ).translate_detailed("hi")
check("腾讯云请求打到自定义地址",
      engines.http_request.last_url == "https://tmt.ap-shanghai.tencentcloudapi.com",
      engines.http_request.last_url)

# 腾讯云的自定义地址与地域要配合好：官方域名能解析出地域，
# 换成内网网关这类非标准域名时则按 tencent_region 走
tc_official = engines.TencentEngine({"tencent_url": "https://tmt.ap-shanghai.tencentcloudapi.com"})
check("腾讯云：官方地域域名自动解析出地域",
      (tc_official.host, tc_official.region) == ("tmt.ap-shanghai.tencentcloudapi.com", "ap-shanghai"),
      "%s / %s" % (tc_official.host, tc_official.region))
tc_mirror = engines.TencentEngine({"tencent_url": "https://tmt.internal.corp",
                                   "tencent_region": "ap-beijing"})
check("腾讯云：内网网关这类域名按 tencent_region 定地域",
      (tc_mirror.host, tc_mirror.region) == ("tmt.internal.corp", "ap-beijing"),
      "%s / %s" % (tc_mirror.host, tc_mirror.region))
# v1.2.9 回归：默认值初始化把 tencent_url 种成了官方主域名，直接请求会报
# X-TC-Region invalid —— 必须换算成地域域名
tc_seeded = engines.TencentEngine({"tencent_url": "https://tmt.tencentcloudapi.com"})
check("腾讯云：官方主域名（种子值）换算成地域域名",
      (tc_seeded.host, tc_seeded.region) == ("tmt.ap-guangzhou.tencentcloudapi.com", "ap-guangzhou"),
      "%s / %s" % (tc_seeded.host, tc_seeded.region))
tc_legacy = engines.TencentEngine({"tencent_region": "https://tmt.tencentcloudapi.com"})
check("腾讯云：region 填完整接口地址（旧插件残留）同样收口",
      (tc_legacy.host, tc_legacy.region) == ("tmt.ap-guangzhou.tencentcloudapi.com", "ap-guangzhou"),
      "%s / %s" % (tc_legacy.host, tc_legacy.region))

check("阿里云用自定义地址（完整域名）",
      engines.AlibabaEngine({"alibaba_url": "https://mt.cn-hangzhou.aliyuncs.com"}).host
      == "mt.cn-hangzhou.aliyuncs.com")
check("阿里云用自定义地址（只填地域）",
      engines.AlibabaEngine({"alibaba_url": "cn-hangzhou"}).host == "mt.cn-hangzhou.aliyuncs.com")
check("阿里云老键名 alibaba_region 仍兼容",
      engines.AlibabaEngine({"alibaba_region": "mt.ap-southeast-1.aliyuncs.com"}).host
      == "mt.ap-southeast-1.aliyuncs.com")
check("阿里云默认官方地址", engines.AlibabaEngine({}).host == "mt.aliyuncs.com")

# 超时 / 重试：设置页已撤掉，改由各引擎自己兜底
check("机翻引擎默认 20s / 重试 1 次",
      (engines.EdgeEngine({}).timeout, engines.EdgeEngine({}).retries) == (20, 1),
      str((engines.EdgeEngine({}).timeout, engines.EdgeEngine({}).retries)))
check("AI 引擎默认超时 120s",
      engines.OpenAIEngine({}).timeout == 120, str(engines.OpenAIEngine({}).timeout))
check("任务参数显式指定时优先生效",
      (engines.EdgeEngine({"timeout_s": 45}).timeout,
       engines.EdgeEngine({"retry_times": 0}).retries,
       engines.EdgeEngine({"retry_backoff_ms": 100}).backoff_ms) == (45, 0, 100))
check("retry_times=0 表示「不重试」而不是「用默认」",
      engines.EdgeEngine({"retry_times": 0}).retries == 0)

engines.http_request = _real_http_request


# --------------------------------------------------------------------------- #
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
for name, detail in FAILED:
    print("  失败: %s  %s" % (name, detail))
sys.exit(1 if FAILED else 0)
