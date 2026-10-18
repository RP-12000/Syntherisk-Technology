"""中英文切换：后端产出的文字默认是中文，请求里要英文时，在返回之前换成英文。

怎么用（前端）
    请求里带查询参数 ?lang=en，或者请求头 X-Language: en，返回里的标签、原因、进度说明、报错就是英文。
    不带（或者是 zh）就是中文，和以前完全一样。进度推送（SSE）同样认 ?lang=en。

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

本文件里有什么
    LANGS、normalize_lang   支持的语言和怎么认请求里的语言
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
import re
from contextlib import contextmanager
from typing import Any, Iterator

# 支持的语言：zh 中文（默认），en 英文
LANGS = ("zh", "en")

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
    """把请求里写的语言变成 "zh" 或 "en"。en、en-US、EN 都算英文；其余（包括没写）都是中文。"""
    return "en" if value and value.strip().lower().startswith("en") else "zh"


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
    """
    lang = lang or current_lang()
    if lang != "en":
        return value
    if isinstance(value, str):
        return en_of(value) if _HAN.search(value) else value
    if isinstance(value, list):
        return [localize(item, lang) for item in value]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.endswith("_en") and key[:-3] in value:
                continue
            english = value.get(f"{key}_en") if isinstance(key, str) else None
            out[key] = english if english else localize(item, lang)
        return out
    return value
