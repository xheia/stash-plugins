# -*- coding: utf-8 -*-
"""语言判定：判断一段文本是否还需要翻译。

保守优先 —— 判不准的时候倾向于「不翻译」，这样既不会破坏已有中文，
也保证自动 Hook 不会无限循环（翻译结果本身是中文，第二次进入必然被跳过）。

判定顺序：
  1. 空文本            -> 不需要翻译
  2. 含日文假名        -> 需要翻译（日文）
  3. 含韩文谚文        -> 需要翻译（韩文）
  4. 含西里尔 / 泰文等 -> 需要翻译
  5. 含拉丁字母        -> 需要翻译（英文等）
  6. 纯 CJK 汉字       -> 歧义：可能是中文，也可能是纯汉字日文
                        由 ambiguous_cjk 决定：skip（默认）或交给引擎 detect
  7. 其余（纯符号数字）-> 不需要翻译
"""
from __future__ import annotations

import re
from collections import Counter

# CJK 统一表意文字（含扩展 A、兼容区）
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df]")

# 日文假名（平假名 + 片假名 + 片假名语音扩展）
_KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff\u31f0-\u31ff\uff66-\uff9d]")

# 典型日文记号：重复符号、小写假名记号、叠字点等
_JP_MARK = re.compile(r"[\u3005\u3006\u30f6\u309d\u309e\u30fd\u30fe\u301c\u30fb\u3001\u3002]")

# 韩文谚文
_HANGUL = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")

# 西里尔字母（俄语等）
_CYRILLIC = re.compile(r"[\u0400-\u04ff]")

# 拉丁字母
_LATIN = re.compile(r"[A-Za-z]")

# 汉字排除集：这些字只出现在日文里，出现即可判定为日文
_JP_ONLY_KANJI = set("働込畑峠辻匂噂枠腺栃埼笹麿糀鰯鱈")


def has_cjk(text: str) -> bool:
    return bool(text) and bool(_CJK.search(text))


def has_kana(text: str) -> bool:
    return bool(text) and bool(_KANA.search(text))


def has_jp_mark(text: str) -> bool:
    return bool(text) and bool(_JP_MARK.search(text))


def has_hangul(text: str) -> bool:
    return bool(text) and bool(_HANGUL.search(text))


def has_latin(text: str) -> bool:
    return bool(text) and bool(_LATIN.search(text))


def has_jp_only_kanji(text: str) -> bool:
    return bool(text) and any(ch in _JP_ONLY_KANJI for ch in text)


# 判定「已经是中文」时，汉字占全部字母的比例下限
_CJK_RATIO = 0.5


def looks_translated(text: str) -> bool:
    """文本是否已经是中文 —— 用于跳过，保证幂等。

    规则：不含日文 / 韩文 / 西里尔特征，且汉字占字母总数的一半以上。
    用比例而不是「含拉丁就否决」，是因为中文标题里夹带英文单词很常见
    （例如「中出 中文版」「4K 修复版」），这类不该被反复送去翻译。
    """
    if not text or not text.strip():
        return False
    if not has_cjk(text):
        return False
    if has_kana(text) or has_jp_mark(text) or has_jp_only_kanji(text):
        return False
    if has_hangul(text) or _CYRILLIC.search(text):
        return False

    cjk_count = len(_CJK.findall(text))
    latin_count = len(_LATIN.findall(text))
    total = cjk_count + latin_count
    if total == 0:
        return True
    return (float(cjk_count) / float(total)) >= _CJK_RATIO


def needs_translation(text: str, ambiguous_cjk: str = "skip") -> bool:
    """是否需要翻译。

    ambiguous_cjk = "skip"   : 纯汉字一律当作中文，跳过（默认，最保守）
    ambiguous_cjk = "detect" : 纯汉字返回 True，交给引擎去检测真实语言
    """
    if not text or not text.strip():
        return False

    # 日文特征优先，避免「纯汉字日文」被误判成中文
    if has_kana(text) or has_jp_mark(text) or has_jp_only_kanji(text):
        return True

    if has_hangul(text):
        return True

    if has_cjk(text):
        # 走到这里说明是纯汉字（无假名、无拉丁）
        if ambiguous_cjk == "detect":
            return True
        return not looks_translated(text)

    # 没有汉字
    if has_latin(text) or _CYRILLIC.search(text):
        return True

    # 纯数字、纯符号、纯 emoji 之类，无需翻译
    return False


def strip_text(value) -> str:
    """把任意输入规整成去空白的字符串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


# --------------------------------------------------------------------------- #
# 退化输出识别
# --------------------------------------------------------------------------- #
# 同一字符连续出现这么多次，就当作引擎抽搐了（正常译文里几乎不可能）
_MAX_CHAR_RUN = 8


def looks_degenerate(text) -> bool:
    """译文是否明显是引擎的退化输出。

    LibreTranslate 处理人名串时会吐出「相相相相相相相相…」这种结果 ——
    这种内容一旦写进数据库，比翻译失败还难收拾（还得回头找出来删掉）。
    判定为退化时上层会丢弃该译文并换下一个引擎。

    两条判据（命中任一即算）：
      1. 同一字符连续出现 8 次以上；
      2. 单个字符占了全部有效字符的一半以上。
    两者都只看有内容的字符（字母 / 数字 / 汉字），标点与空格不参与 ——
    否则「----」这种分隔线会被误判。正常译文两条都碰不到，
    连「哈哈哈哈」这种正常叠字也放行。
    """
    text = strip_text(text)
    if len(text) < _MAX_CHAR_RUN:
        return False

    meaningful = [ch for ch in text if ch.isalnum()]
    if len(meaningful) < _MAX_CHAR_RUN:
        return False

    run = 1
    for i in range(1, len(meaningful)):
        if meaningful[i] == meaningful[i - 1]:
            run += 1
            if run >= _MAX_CHAR_RUN:
                return True
        else:
            run = 1

    if len(meaningful) >= 12:
        top_count = max(Counter(meaningful).values())
        if top_count >= 0.5 * len(meaningful):
            return True
    return False
