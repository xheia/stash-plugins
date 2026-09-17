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
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
for name, detail in FAILED:
    print("  失败: %s  %s" % (name, detail))
sys.exit(1 if FAILED else 0)
