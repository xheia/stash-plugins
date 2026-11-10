# -*- coding: utf-8 -*-
"""语言判定：判断一段文本是否还需要翻译。

保守优先 —— 判不准的时候倾向于「不翻译」，这样既不会破坏已有中文，
也保证自动 Hook 不会无限循环（翻译结果本身是中文，第二次进入必然被跳过）。

判定顺序（classify：所有实体、所有字段共用，不做特例）：
  1. 空文本                 -> 不需要翻译
  2. 纯数字 / 日期 / 编号    -> 不需要翻译（1080、2024-01-01、01）
  3. 规格 / 编码 / 纯大写缩写 -> 不需要翻译（4K、1080p、x264、S01E02、BBC、POV）
  4. 含日文假名              -> 需要翻译（日文）
  5. 含韩文谚文 / 西里尔      -> 需要翻译
  6. 已是中文（汉字 >= 3 或占比过半） -> 不需要翻译
  7. 含拉丁字母的其余文本     -> 需要翻译
  8. 纯 CJK 汉字（短）        -> 歧义：由 ambiguous_cjk 决定跳过或交给引擎 detect
  9. 其余（纯符号 / emoji）   -> 不需要翻译

「数字类」与「名称类」在这里**统一按语义**判断：只看有没有可翻译的
自然语言内容，不看它长得像数字还是像专有名词 —— 避免按长度 / 字符类型
各搞一套规则，出现「4K 跳过但 BBC 被送出去」这种不一致。
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

# 拉丁单词（用于中文占比判断：按单词算，不按字母算）
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")

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


# 判定「已经是中文」时，汉字占全部字母的比例下限（短文本用）
_CJK_RATIO = 0.5

# 汉字达到这个数量就直接判定为中文，不再看拉丁字母占多少。
# 为什么需要这条：人名 / 厂牌名常被引擎保留成拉丁字母，例如
# 「Mika 的访谈」「John Smith 访谈」—— 按字面比例算会被判成英文，
# 于是每一轮都重新提交，白白烧额度。三个汉字已经足以说明是中文表达。
_CJK_STRONG = 3

# 纯数字 / 日期 / 编号 / 分隔符：没有自然语言语义
_PURE_NUMERIC = re.compile(
    r"^[\d\s.,:;\-–—~%/\\+×*#()\[\]&|'\"°]+$"
)

# 规格与编码类 token：4K / 1080P / FHD / HDR10 / x264 / S01E02 / Vol.3 / 60fps
# 这类内容既不该翻译，也不该因为「含字母」就当成英文送出去。
_SPEC_TOKEN = re.compile(
    r"^(?:"
    r"\d+(?:\.\d+)?[kp]|\d{3,4}[pi]|[248]k|"
    r"[hx]\.?26[45]|hevc|avc|av1|vp9|"
    r"hdr\d*|sdr|fhd|uhd|hd|sd|3d|2d|vr|ar|"
    r"\d+fps|fps|aac|ac3|dts|flac|"
    r"mp4|mkv|avi|wmv|webm|iso|"
    r"blu-?ray|web-?dl|webrip|hdrip|dvdrip|bdrip|remux|hdtv|"
    r"cd\d+|dvd\d*|part\d+|vol\.?\d+|no\.?\d+|"
    r"s\d{1,2}e\d{1,3}|ep\d+|\d+bit|\d+khz"
    r")$",
    re.IGNORECASE,
)

# 纯大写缩写（品牌 / 类型代号）：BBC、POV、JAV、VR、DIY。
# 与数字、规格同样看待 —— 它们都不是「需要翻译的自然语言」。
_ACRONYM = re.compile(r"^[A-Z0-9][A-Z0-9&\-\.\+/]{1,5}$")


def looks_translated(text: str) -> bool:
    """文本是否已经是中文 —— 用于跳过，保证幂等。

    规则：不含日文 / 韩文 / 西里尔特征，且满足下面任一条：
      1. 汉字数量 >= 3（中文表达已经成立，夹几个英文词不算外语）；
      2. 汉字数占「汉字 + 拉丁单词数」的一半以上（一两个汉字的短标题）。

    第 2 条特意用**拉丁单词数**而不是字母数：译文里保留人名很常见
    （「Mika 访谈」= 2 汉字 vs 1 个单词，判为中文；按字母算是 2/6，
    会被误判成英文，于是每一轮都重新提交）。数字/规格 token 不算单词。
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
    if cjk_count >= _CJK_STRONG:
        return True

    latin_words = len(_LATIN_WORD.findall(text))
    if latin_words == 0:
        return True
    if cjk_count < 2:
        return False
    return (float(cjk_count) / float(cjk_count + latin_words)) >= _CJK_RATIO


# 文本分类结果：调用方据此决定「翻译 / 跳过 / 交给引擎检测」
CATE_EMPTY = "empty"          # 空文本
CATE_NUMERIC = "numeric"      # 纯数字 / 日期 / 编号
CATE_CODE = "code"            # 规格 / 编码 / 纯大写缩写
CATE_CHINESE = "chinese"      # 已经是中文
CATE_FOREIGN = "foreign"      # 外语，需要翻译
CATE_AMBIGUOUS = "ambiguous"  # 纯汉字，可能是日文（交给引擎检测）
CATE_OTHER = "other"          # 纯符号 / emoji


def _is_numeric(text: str) -> bool:
    return bool(_PURE_NUMERIC.match(text))


def _is_code(text: str) -> bool:
    """是否整段都由规格 / 编码 / 缩写组成（这类内容一律不翻）。

    逐个 token 判断，而不是整串正则 —— 「1080p 60fps」「BBC POV」这种
    组合也要能认出来。超过 4 个 token 就不再当编码，避免把
    「Beautiful Nurse HD」这类真实名称误伤。
    """
    tokens = [t for t in re.split(r"[\s,/|]+", text) if t]
    if not tokens or len(tokens) > 4:
        return False
    for token in tokens:
        if _SPEC_TOKEN.match(token):
            continue
        if _ACRONYM.match(token) and any(ch.isalpha() for ch in token):
            continue
        return False
    return True


def classify(text: str, ambiguous_cjk: str = "skip") -> str:
    """把文本归入唯一类别 —— 所有字段共用这一套判断，不做实体特例。

    「数字类」与「名称类」在这里统一看待：判断依据是**有没有可翻译的
    自然语言内容**，而不是它长得像数字还是像名字。
      - 1080 / 2024-01-01 / 01        -> numeric（跳过）
      - 4K / 1080p / S01E02 / BBC     -> code（跳过）
      - 无码流出 / 中文字幕 4K 修复版 -> chinese（跳过）
      - Beautiful Nurse / Mika        -> foreign（翻译）
    """
    text = strip_text(text)
    if not text:
        return CATE_EMPTY
    if _is_numeric(text):
        return CATE_NUMERIC
    if _is_code(text):
        return CATE_CODE

    # 日文 / 韩文 / 西里尔特征优先，避免「纯汉字日文」被误判成中文
    if has_kana(text) or has_jp_mark(text) or has_jp_only_kanji(text):
        return CATE_FOREIGN
    if has_hangul(text) or _CYRILLIC.search(text):
        return CATE_FOREIGN

    cjk_count = len(_CJK.findall(text))
    latin_count = len(_LATIN.findall(text))

    if cjk_count and not latin_count:
        # 纯汉字：可能是中文，也可能是纯汉字日文
        if ambiguous_cjk == "detect":
            return CATE_AMBIGUOUS
        return CATE_CHINESE if looks_translated(text) else CATE_FOREIGN

    if cjk_count:
        return CATE_CHINESE if looks_translated(text) else CATE_FOREIGN

    if latin_count:
        return CATE_FOREIGN

    # 纯符号 / emoji
    return CATE_OTHER


def needs_translation(text: str, ambiguous_cjk: str = "skip") -> bool:
    """是否需要翻译。

    ambiguous_cjk = "skip"   : 纯汉字一律当作中文，跳过（默认，最保守）
    ambiguous_cjk = "detect" : 纯汉字返回 True，交给引擎去检测真实语言
    """
    return classify(text, ambiguous_cjk) in (CATE_FOREIGN, CATE_AMBIGUOUS)


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
