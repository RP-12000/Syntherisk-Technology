"""中英法切换：后端产出的文字默认是中文，请求里要英文或法文时，在返回之前换过去。

怎么用（前端）
    请求里带查询参数 ?lang=en，或者请求头 X-Language: en，返回里的标签、原因、进度说明、报错就是英文；
    lang=fr 是法文。不带（或者是 zh）就是中文，和以前完全一样。进度推送（SSE）同样认 ?lang=。

设计：存的始终是中文，另外记一本“中文 → 英文”的对照
    分析结果（analyses.result）里的文字是流水线跑的时候就拼好存下来的，而用户随时可能切换语言。
    所以不按语言存两份结果，而是：
      1. 代码里每一处产出文字的地方都写成 tr("中文模板", "English template", 参数…)。
         tr() 返回中文（存进结果的仍然是中文，所有既有的逻辑和测试不受影响），
         同时把“这句中文 → 对应的英文”记进当前的对照表。
      2. 流水线把这张对照表存在 result["_en"] 里，跟着结果一起加密保存。
      3. 接口返回之前（api 层的中间件），如果请求要英文，就把返回的 JSON 整个走一遍：
         凡是和对照表里某句中文完全相同的字符串，换成它的英文（localize()）。
    固定不变的文字（没有参数的标签、data/ 下各个表里的名称）记在进程级的 _STATIC 里，不用每次存。

    模板里的参数如果本身是一句有对照的中文（比如物质名、标签），英文句子里会用它的英文。
    大模型写的句子不走对照表：它按分析时（或提问时）的语言直接写成那种语言。

法文：在英文的基础上再翻一次
    不另记“中文 → 法文”：先按上面的办法得到英文，再查法文对照表 syntherisk/locales/fr.json（fr_of）。
    那张表是 {英文: 法文}，键就是代码里 tr() 的英文模板（和 data/ 下各表的英文名称），参数照样写 {name}。
    整句查得到就直接换；查不到时按模板认：把英文模板变成正则去匹配这句英文，取出各个参数，
    参数自己再翻一次（它们往往是名称或者半句话），填进法文模板。都认不出来就留英文。
    这样存好的分析结果、对照表一样都不用改，以前跑的分析在法文界面上也能显示。
    tests/test_i18n.py 的最后一个测试会查：代码里每一句 tr() 的英文在表里都有法文。

本文件里有什么
    LANGS、normalize_lang   支持的语言和怎么认请求里的语言
    fr_of                   查一句英文的法文
    tr                      产出一句文字，并记下它的英文
    register                登记一条固定的对照（数据文件里的名称用）
    en_of                   查一句中文的英文
    collecting              在一段代码执行期间，把对照记进指定的表（流水线、请求各用一次）
    add_memory              把一张存好的对照表（result["_en"]）接进当前请求
    current_lang            当前请求要的语言
    localize                把要返回的数据换成指定的语言
"""

from __future__ import annotations

import contextvars
import functools
import json
import re
import string
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# 支持的语言：zh 中文（默认），en 英文，fr 法文
LANGS = ("zh", "en", "fr")

# 进程级的固定对照：中文 → 英文。没有参数的 tr()、register() 都记在这里
_STATIC: dict[str, str] = {}

# 当前这段执行（一个请求，或者一次后台任务）的状态：
#   lang     这次请求要的语言
#   writes   tr() 要把对照写进去的表（可以有好几张：请求自己的一张，加上涉及到的分析结果的 _en）
#   reads    localize() 和 en_of() 查对照时看的表
_state: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("syntherisk_i18n", default=None)

# 判断一个字符串里有没有汉字（没有汉字的字符串不用查对照）
_HAN = re.compile(r"[一-鿿]")


def normalize_lang(value: str | None) -> str:
    """把请求里写的语言变成 "zh"、"en" 或 "fr"。en、en-US、EN 都算英文，fr、fr-CA 算法文；其余（包括没写）都是中文。"""
    value = (value or "").strip().lower()
    if value.startswith("en"):
        return "en"
    if value.startswith("fr"):
        return "fr"
    return "zh"


def en_of(text: Any) -> Any:
    """查一句中文的英文；查不到（或者不是字符串）原样返回。先查当前请求的对照表，再查固定对照。"""
    if not isinstance(text, str):
        return text
    state = _state.get()
    for memory in (state["reads"] if state else ()):
        if text in memory:
            return memory[text]
    return _STATIC.get(text, text)


def register(zh: str, en: str) -> None:
    """登记一条固定的对照。reference.py 读 data/ 下的表时，用它登记每个名称的英文。空的不登记。"""
    if zh and en and zh != en:
        _STATIC[zh] = en


def tr(zh: str, en: str, /, **params: Any) -> str:
    """产出一句文字：返回中文，并记下它对应的英文。

    参数：
        zh      中文模板，用 str.format 的写法留参数，比如 "第 {n} 步用到{label}：{names}"
        en      英文模板，参数名和中文模板一样
        params  模板的参数。参数是一句有对照的中文时，英文句子里用它的英文
    返回：
        填好参数的中文句子。
    副作用：
        把“中文句子 → 英文句子”记进对照表：没有参数的记进固定对照；有参数的记进当前生效的表
        （见 collecting()），当前没有表时不记。
    """
    state = _state.get()
    if not params:
        register(zh, en)
        # 固定的句子也记进当前的对照表：它可能被存进分析结果，而读结果的可能是另一个进程
        # （Worker 算、API 读，或者重启以后），那个进程不一定执行过这一行
        pair(zh, en)
        return zh
    text = zh.format(**params)
    if state and state["writes"]:
        pair(text, en.format(**{name: en_of(value) for name, value in params.items()}))
    return text


def pair(zh: str, en: str) -> None:
    """把一对已经拼好的“中文 → 英文”记进当前生效的对照表（当前没有表时什么都不做）。

    tr() 和 join() 都靠它；自己拼好了两种语言的文字（比如去掉句尾标点的句子）时也可以直接调。
    """
    state = _state.get()
    if state and zh != en:
        for memory in state["writes"]:
            memory[zh] = en


def join(items: list[str], zh_sep: str = "、", en_sep: str = ", ") -> str:
    """把几段文字连成一句：中文用顿号，英文用逗号。各段有对照的话，英文那句用它们的英文。"""
    text = zh_sep.join(items)
    state = _state.get()
    if state and state["writes"] and len(items) > 1:
        english = en_sep.join(str(en_of(item)) for item in items)
        for memory in state["writes"]:
            memory[text] = english
    return text


@contextmanager
def collecting(memory: dict[str, str], lang: str = "zh") -> Iterator[dict[str, Any]]:
    """在 with 块执行期间，tr() 把对照记进 memory，localize() 也从它查。

    流水线跑一次分析时用一次（memory 最后存成 result["_en"]）；每个请求由中间件用一次。
    lang 是这段执行要的语言（current_lang() 返回它）。
    """
    state = {"lang": lang, "writes": [memory], "reads": [memory]}
    token = _state.set(state)
    try:
        yield state
    finally:
        _state.reset(token)


def add_memory(memory: dict[str, str] | None) -> None:
    """把一张存好的对照表接进当前请求：之后 tr() 也往它里面记，localize() 也从它查。

    接口取出一次分析以后调它，传 result["_en"]（那个字典本身，不是拷贝）：这样重新评分时新拼出来的句子
    会记进结果自己的对照表，随结果一起保存。当前没有在请求里（比如测试直接调函数）时什么都不做。
    """
    state = _state.get()
    if state is not None and memory is not None and not any(memory is known for known in state["reads"]):
        state["writes"].append(memory)
        state["reads"].append(memory)


def current_lang() -> str:
    """当前请求要的语言；不在请求里时是 "zh"。"""
    state = _state.get()
    return state["lang"] if state else "zh"


def localize(value: Any, lang: str | None = None) -> Any:
    """把要返回的数据换成指定的语言，返回新的数据，不改传进来的。

    lang 是 "zh"（或者当前请求要的就是中文）时原样返回。是 "en" 时把整个结构走一遍：
      - 字符串：和对照表里某句中文完全相同的，换成英文；
      - 字典：有 "x_en" 这样的键并且值不是空的，就用它的值顶替 "x"（进度说明是这样存的），"x_en" 自己去掉；
      - 列表：逐项处理。
    是 "fr" 时先照英文那样换，换成的英文再查一次法文（fr_of）。没有汉字的字符串（用户输入、结构、编号、
    大模型直接写的英文或法文）两种语言下都原样不动。
    """
    lang = lang or current_lang()
    if lang == "zh":
        return value
    if isinstance(value, str):
        if not _HAN.search(value):
            return value
        english = en_of(value)
        return fr_of(english) if lang == "fr" else english
    if isinstance(value, list):
        return [localize(item, lang) for item in value]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.endswith("_en") and key[:-3] in value:
                continue
            english = value.get(f"{key}_en") if isinstance(key, str) else None
            if english:
                out[key] = fr_of(english) if lang == "fr" and isinstance(english, str) else english
            else:
                out[key] = localize(item, lang)
        return out
    return value


# ---------------------------------------------------------------- 法文

# 法文对照表：{英文句子或英文模板: 法文}。第一次用到时才读（_french()）
_FR_PATH = Path(__file__).parent / "locales" / "fr.json"
# 列表连成一句时用的分隔符（join() 的英文分隔符）：整句认不出来时，按它拆开逐项翻
_LIST_SEPARATORS = ("; ", ", ")
# 像是数字的参数：步号列表 “2, 3”、范围 “60–75”、百分数、分数 “1/9”
_NUMERIC = re.compile(r"^[\d\s.,%–\-+/]+$")
# 用逗号分千位的整数（12,345）：法文里逗号是小数点，换成不断行的空格
_GROUPED = re.compile(r"^\d{1,3}(,\d{3})+$")
# 不断行的空格（法文的千位分隔）
_NBSP = "\u00a0"
# 带英文单词的文字：参数或者拆出来的一段是这样、却没翻出来，就算这种翻法有缺口（见 fr_of）
_WORDY = re.compile(r"[A-Za-z]{3,}")
# 一句英文按一个模板最多试多少种切法（见 _best_split）
_MAX_SPLITS = 64


@dataclass
class _Template:
    """法文对照表里的一个英文模板。

    pattern  匹配英文句子的正则（参数是“尽量短的任意文字”），用来快速筛掉对不上的模板
    parts    模板拆成的片段：("text", 一段字) 或 ("field", 参数名)，按顺序
    french   法文模板
    """

    pattern: re.Pattern[str]
    parts: list[tuple[str, str]]
    french: str


@functools.lru_cache(maxsize=1)
def _french() -> tuple[dict[str, str], dict[str, list[_Template]]]:
    """读法文对照表，分成两部分：(整句对照, 模板)。

    整句对照：英文里没有 {参数} 的，{英文: 法文}。
    模板：{英文模板开头第一个字（模板以参数开头时是空字符串）: [_Template, …]}，
    同一组里字多的模板排在前面（越具体越先试）。只有参数、没有一个字的模板认不出什么，不收。
    文件不存在时两部分都是空的：法文界面上就全是英文。
    """
    table = json.loads(_FR_PATH.read_text(encoding="utf-8")) if _FR_PATH.exists() else {}
    exact: dict[str, str] = {}
    templates: dict[str, list[tuple[int, _Template]]] = {}
    for english, french in table.items():
        parsed = list(string.Formatter().parse(english))
        if all(field is None for _, field, _, _ in parsed):
            exact[english] = french
            continue
        literal = "".join(text for text, _, _, _ in parsed)
        if not literal.strip() or any(field is not None and not field.isidentifier() for _, field, _, _ in parsed):
            continue
        # 英文模板变正则：字原样（转义），参数变成“尽量短的任意文字”；同名参数第二次出现时要和第一次一样
        pattern, seen, parts = "", set(), []
        for text, field, _, _ in parsed:
            pattern += re.escape(text)
            if text:
                parts.append(("text", text))
            if field is not None:
                pattern += f"(?P={field})" if field in seen else f"(?P<{field}>.+?)"
                seen.add(field)
                parts.append(("field", field))
        template = _Template(re.compile(pattern + r"\Z", re.S), parts, french)
        templates.setdefault(parsed[0][0][:1], []).append((len(literal), template))
    ordered = {first: [t for _, t in sorted(group, key=lambda item: -item[0])] for first, group in templates.items()}
    return exact, ordered


def _splits(parts: list[tuple[str, str]], text: str, start: int, values: dict[str, str]) -> Iterator[dict[str, str]]:
    """把一句英文按模板的片段切开的所有切法，每种切法是 {参数名: 那一段英文}。参数短的切法先出来。"""
    if not parts:
        if start == len(text):
            yield dict(values)
        return
    kind, value = parts[0]
    if kind == "text":
        if text.startswith(value, start):
            yield from _splits(parts[1:], text, start + len(value), values)
        return
    if value in values:  # 同名参数第二次出现：必须和第一次一样
        if text.startswith(values[value], start):
            yield from _splits(parts[1:], text, start + len(values[value]), values)
        return
    following = parts[1] if len(parts) > 1 else None
    if following is None:
        ends: Iterator[int] | list[int] = [len(text)] if len(text) > start else []
    elif following[0] == "text":
        # 参数到下一段字为止：下一段字在后面每出现一次，就是一种切法
        ends = (m.start() for m in re.finditer(re.escape(following[1]), text[start + 1:]))
        ends = [start + 1 + offset for offset in ends]
    else:
        ends = range(start + 1, len(text))
    for end in ends:
        values[value] = text[start:end]
        yield from _splits(parts[1:], text, end, values)
        del values[value]


def _best_split(template: _Template, english: str, exact: dict[str, str]) -> dict[str, str] | None:
    """一句英文按一个模板最可能的切法。

    参数挨在一起时（比如 "{word} {numbers} {verb} no precedent" 对 "Steps 2, 3 have no precedent"），
    正则那种“参数尽量短”的切法会切错（numbers 只拿到 "2,"）。所以把各种切法都列出来打分：
    参数整个在整句对照里（Steps、have 这种按个数选的词）3 分，像数字 2 分，其余 0 分；取分最高的，
    同分取参数短的那种。对不上这个模板时返回 None。
    """
    best, best_score = None, -1
    for count, values in enumerate(_splits(template.parts, english, 0, {})):
        if count >= _MAX_SPLITS:
            break
        score = sum(3 if v in exact else 2 if _NUMERIC.match(v) else 0 for v in values.values())
        if score > best_score:
            best, best_score = values, score
    return best


def _fill(template: str, values: dict[str, str]) -> str:
    """把参数填进法文模板。参数已经是排好的文字，所以模板里的格式（{score:g} 的 :g）不再起作用。"""
    out = []
    for text, field, _, _ in string.Formatter().parse(template):
        out.append(text)
        if field is not None:
            out.append(values.get(field, "{" + field + "}"))
    return "".join(out)


def fr_of(english: str) -> str:
    """查一句英文的法文；查不到原样返回英文。做法见 _fr()。"""
    return _fr(english)[0]


@functools.lru_cache(maxsize=50000)
def _fr(english: str) -> tuple[str, int]:
    """fr_of() 的实现。返回 (法文, 没翻出来的字数)：没翻出来的字数是还留着英文单词的那几段一共多长，0 就是全翻出来了。

    依次试：整句对照；千位分隔的数字；"Value error, " 开头的（校验报错，前缀留着给前端去掉）；
    英文模板（各个参数再各自翻）；在句号后面切成前后两段（"{head}{tail}" 这种只有参数的模板认不出来的长句）；
    按 "; "、", " 拆成几段（join() 连起来的列表；相邻几段合起来才是一句对照的，也合起来试）。
    哪种翻法全翻出来了就用哪种；都有没翻出来的，用没翻出来的字最少的那种（同样多时取先试到的）。
    "{label}: {reason}" 这种很宽的模板几乎什么句子都对得上，所以不能对上就用，要和别的翻法比。
    同一句英文翻过一次就记住（lru_cache）。
    """
    exact, templates = _french()
    if english in exact:
        return exact[english], 0
    if _GROUPED.match(english):
        return english.replace(",", _NBSP), 0
    if english.startswith("Value error, "):
        text, left = _fr(english[len("Value error, "):])
        return "Value error, " + text, left
    untranslated = (english, len(english) if _WORDY.search(english) else 0)
    if not untranslated[1]:
        return untranslated  # 只有数字、符号、编号：不用翻
    candidates = [untranslated]
    for template in templates.get(english[:1], []) + templates.get("", []):
        if not template.pattern.match(english):
            continue
        values = _best_split(template, english, exact)
        if values is None:
            continue
        parts = {name: _fr(value) for name, value in values.items()}
        candidate = (_fill(template.french, {name: text for name, (text, _) in parts.items()}),
                     sum(left for _, left in parts.values()))
        if not candidate[1]:
            return candidate
        candidates.append(candidate)
    for match in re.finditer(r"(?<=\.) (?=.)", english, re.S):
        head, tail = _fr(english[:match.end()]), _fr(english[match.end():])
        candidates.append((head[0] + tail[0], head[1] + tail[1]))
    for separator in _LIST_SEPARATORS:
        if separator in english:
            candidates.append(_segments(english.split(separator), separator))
    return min(candidates, key=lambda item: item[1])


def _segments(pieces: list[str], separator: str) -> tuple[str, int]:
    """按分隔符拆开的几段，怎么分组翻没翻出来的字最少（相邻几段可以合起来当一句翻），返回 (法文, 没翻出来的字数)。

    不会把全部几段合成一组：那就是原来那句，交给调用方。
    """
    # best[i]：前 i 段最好的翻法 (法文的几组, 没翻出来的字数)
    best: list[tuple[list[str], int] | None] = [([], 0)] + [None] * len(pieces)
    for end in range(1, len(pieces) + 1):
        for start in range(end):
            if best[start] is None or (start == 0 and end == len(pieces)):
                continue
            text, left = _fr(separator.join(pieces[start:end]))
            total = best[start][1] + left
            if best[end] is None or total < best[end][1]:
                best[end] = (best[start][0] + [text], total)
    groups, left = best[len(pieces)]
    return separator.join(groups), left
