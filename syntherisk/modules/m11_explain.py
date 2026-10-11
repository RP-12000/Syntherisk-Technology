"""M11 解释（大模型的三处用途之二）：根据证据卡写说明，每句附出处。

大模型不打分、不生成路线、不编文献：它只读证据卡，输出的每一句都要列出来源编号。
后端拿到结果后再核一遍：
  - 来源编号必须是这张卡里真有的；
  - 句子里出现的数字必须能在卡里找到。
通不过的句子直接丢掉；一句都不剩，或者没有配置大模型、调用失败，就用模板根据同一张卡生成。

对应关系
    PPT 模块：M11 解释。
    流水线：第 8 阶段“写说明”（syntherisk/pipeline.py 的 AnalysisRunner._explain）。给排名前几条的路线
    （配置项 EXPLAIN_TOP_ROUTES，默认 3）各写一段推荐理由（explain_route），再给这些路线的每一步写一段说明
    （explain_step）。同一步出现在几条路线里、序号和分数都一样时只写一次。
    界面：
        B10 点击某一步，打开证据卡（R6）   GET /api/analyses/{id}/routes/{route_id}/steps/{n}。
            这一步的说明还没有写过时（路线不在前几名里，或者重新评分后分数变了、旧说明被删掉），
            接口当场调 explain_step() 补写，并存回分析结果。
        B4 路线卡片                        GET /api/analyses/{id}/routes/{route_id} 返回的 explanation
            就是 explain_route() 写的推荐理由。
        F3 生成报告                        这条路线还没有推荐理由时，api/reports.py 当场调 explain_route()（不存回去）。

数据从哪来、到哪去
    输入：证据卡（syntherisk/cards.py 的 build_card()）。先用 card_facts() 压成只含事实和来源编号的精简版，
          再转成 JSON 文本发给大模型。路线的推荐理由用的是 M10 写在 route 上的汇总字段（summary、weakest 等）。
    大模型：通过 syntherisk/llm 的 LLMService.complete_json() 调用，要求按 SENTENCES_SCHEMA 返回结构化的结果。
          LLMService 把每次调用的用量记进 llm_calls 表，模块名记作 "M11"。
    输出：{"sentences", "text", "generated_by", "provider", "model", "dropped_sentences", "note"}。
          调用方把一步的说明存到 route["explanations"][步骤编号]，把路线的推荐理由存到 route["explanation"]。
    本文件自己不读写数据库。

来源编号
    证据卡的 sources 列出这张卡里可以引用的出处（cards.build_sources()），编号有：
    ROUTE（逆合成路线）、P1 P2 …（先例）、RULE:<规则编号>（命中的操作规则；一条都没命中时是 RULE）、
    GHS、STOCK、CALC、SCORE，另外 MODEL 和 DEMO 只在有模型依据、用了演示数据时才有。
    cards.render_sentences() 把编号换成显示用的标记拼在每句话后面：“……[1][2]。……[规则 · 气氛]。”

本文件里有什么
    _NUMBER、_numbers()     从一段文字里找出所有数字
    SENTENCES_SCHEMA        要求大模型返回的 JSON 结构
    STEP_SYSTEM_PROMPT、ROUTE_SYSTEM_PROMPT   写步骤说明、写路线推荐理由用的两段系统提示词
    validate_sentences()    核对大模型写的句子：出处是不是卡里有的，数字是不是卡里有的（M12 也用它）
    Explainer               写说明的类
        explain_step()      一步的说明
        _package()          把句子包成统一的返回格式
        _step_template()    不用大模型时，按模板写一步的说明
        explain_route()     整条路线的推荐理由
"""

from __future__ import annotations

import json
import re
from typing import Any

from syntherisk.cards import card_facts, render_sentences
from syntherisk.llm import LLMError, LLMService
from syntherisk.modules.m06_safety import most_severe

# 找数字的正则表达式：\d+ 是一串数字；(?:\.\d+)? 是后面可以再跟一个小数点和一串数字
# （(?:...) 只分组、不单独取出，结尾的 ? 表示可有可无）。所以 "78"、"78.4" 都能匹配。
# 不认正负号：“−78 °C”取出来的是 78。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# 要求大模型返回的 JSON 结构（JSON Schema 写法）：
#     {"sentences": [{"text": "一句话", "sources": ["P1", "SCORE"]}, ...]}
# required 列出必须有的字段；additionalProperties 为 False 表示不许多出别的字段。
# 由 syntherisk/llm 里各厂商的适配层交给模型，返回的 response.data 就是这个结构的字典。
SENTENCES_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "sources"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentences"],
    "additionalProperties": False,
}

# 写一步说明时的系统提示词。整段原样发给大模型，改它就是改模型的行为。
# 用户消息是 card_facts() 得到的精简版证据卡（JSON 文本）。
STEP_SYSTEM_PROMPT = """你是 Syntherisk 的说明撰写助手，读者是做有机合成的研究人员。

用户会给你一张“证据卡”（JSON），描述合成路线里的一步反应：文献先例、反应条件、产率、毒性、原料和各维度的得分。
请用中文写 2 到 4 句话，说明：这一步做的是什么反应、有什么依据、主要风险在哪里、分数主要扣在哪一项。

必须遵守：
1. 只使用证据卡里的信息。卡里没有的事实、数字、文献、试剂和条件，一律不要写。
2. 每句话都要在 sources 里列出它依据的来源编号。编号只能从证据卡的 allowed_sources 里选；卡里每一项前面的 source 字段就是它的编号。
3. 不要自己打分，也不要改动卡里的任何数字。
4. 卡里缺少的信息（比如没有先例、没有产率记录）要如实说缺少，不要猜。
5. 每句话只讲一件事，不要写客套话，句子里不要出现方括号形式的引用标记。"""

# 写整条路线的推荐理由时的系统提示词。用户消息是 explain_route() 里拼的那份汇总信息（JSON 文本）。
ROUTE_SYSTEM_PROMPT = """你是 Syntherisk 的说明撰写助手，读者是做有机合成的研究人员。

用户会给你一条合成路线的汇总信息（JSON）。请用中文写 2 到 3 句话的推荐理由：这条路线的整体情况、最薄弱的环节、实验前要注意什么。

必须遵守：
1. 只使用给出的信息，不要补充任何没有给出的事实或数字。
2. 每句话都要在 sources 里列出它依据的来源编号，编号只能从 allowed_sources 里选。
3. 不要自己打分，也不要改动任何数字。
4. 句子里不要出现方括号形式的引用标记。"""


def _numbers(text: str) -> list[float]:
    """找出一段文字里的所有数字，按出现的先后返回（都转成 float）。

    例子：“文献报道 60–75%（3 个先例）” → [60.0, 75.0, 3.0]。
    谁在用：
        本文件的 validate_sentences()。
    """
    return [float(n) for n in _NUMBER.findall(text)]


def validate_sentences(raw: Any, allowed_sources: set[str], facts: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """只留下出处有效、数字有据的句子。返回 (保留的句子, 丢掉的句数)。

    大模型写的每一句话都要过这一关，这是“说明里不出现编造内容”的最后一道检查。

    参数：
        raw              大模型返回的 sentences，应当是 [{"text": ..., "sources": [...]}] 这样的列表。
                         不是列表时当作一句都没有。
        allowed_sources  可以引用的来源编号的集合（证据卡的 allowed_sources）。
        facts            发给大模型的那份事实（card_facts() 的结果，或者路线的汇总信息）。
                         句子里的数字要到这里面去找。
    返回：
        (kept, dropped)
        kept     通过检查的句子 [{"text": 去掉首尾空白的句子, "sources": 去掉重复的来源编号（保持原来的先后）}]
        dropped  没通过、被丢掉的句数
    一句话要同时满足下面几条才留下：
        1. 文字不是空的，并且不超过 400 个字符；
        2. sources 不是空的，并且每个编号都在 allowed_sources 里；
        3. 句子里的每个数字，都能在 facts 的 JSON 文本里找到一个相差不到 0.51 的数字
           （允许四舍五入：卡里是 78.4，句子写 78 可以；卡里没有 95，句子写 95% 就不行）。
    数字检查的范围：
        句子里的任何一串数字都要查（包括“第 4 步”里的 4、化合物名字里的位号）；facts 里任何位置出现过的
        数字都算“卡里有”（包括文献号、年份）。所以它拦得住卡里完全没有的数字，但不核对这个数字指的是不是同一件事。
    副作用：没有，不改 raw。
    谁在用：
        本文件的 Explainer.explain_step() 和 explain_route()；m12_qa.QuestionAnswerer.answer()。
        tests/test_llm.py 有逐条的例子。
    """
    # facts 整个转成 JSON 文本，再把里面所有的数字找出来，作为“卡里有的数字”。
    # ensure_ascii=False 让中文保持原样，不转成 \uXXXX（那样转义里的数字会被误当成数字）
    known = _numbers(json.dumps(facts, ensure_ascii=False))
    kept, dropped = [], 0
    for item in raw if isinstance(raw, list) else []:
        # 每一项应当是字典；不是字典就当成空句子，下面会被丢掉
        text = str(item.get("text", "")).strip() if isinstance(item, dict) else ""
        sources = [s for s in (item.get("sources") or [])] if isinstance(item, dict) else []
        # grounded：句子里的每个数字 n，都能在卡里找到一个相差不到 0.51 的数字 k。句子里没有数字时为 True
        grounded = all(any(abs(n - k) < 0.51 for k in known) for n in _numbers(text))
        if text and sources and all(s in allowed_sources for s in sources) and grounded and len(text) <= 400:
            # dict.fromkeys() 去掉重复的编号，同时保持原来的顺序
            kept.append({"text": text, "sources": list(dict.fromkeys(sources))})
        else:
            dropped += 1
    return kept, dropped


class Explainer:
    """写说明的类：一步的说明（explain_step）和整条路线的推荐理由（explain_route）。

    属性：
        llm  大模型调用层（syntherisk.llm.LLMService）。llm.enabled 为 False 表示没有配置大模型，
             这时所有说明都按模板生成。
    不保存别的状态，用的时候现建一个：Explainer(ctx.llm)。
    """

    def __init__(self, llm: LLMService):
        """参数 llm：大模型调用层，一般是 AppContext.llm。"""
        self.llm = llm

    # ------------------------------------------------------------ 一步

    def explain_step(self, card: dict[str, Any], *, org_id: str | None = None, analysis_id: str | None = None) -> dict[str, Any]:
        """给一步反应写说明（证据卡 R6 上的那段文字）。

        流程：
            1. card_facts(card)：把证据卡压成只有事实和来源编号的精简版（不带图片和界面用的字段）。
            2. 配了大模型：把精简版转成 JSON 发过去，要求按 SENTENCES_SCHEMA 返回若干句话，
               再用 validate_sentences() 核对。有句子留下来，就用这些句子。
            3. 没配大模型、调用失败（LLMError，模型拒答也算）、或者一句都没通过核对：
               改用 _step_template() 按模板写，并在 note 里写明原因。
        参数：
            card         证据卡（cards.build_card() 的返回值）。
            org_id       课题组 id，只用来记大模型用量，可以是 None。
            analysis_id  分析 id，只用来记大模型用量，可以是 None。
        返回：
            字典，字段见 _package()：sentences、text、generated_by（"llm" 或 "template"）、
            provider、model、dropped_sentences、note。
        副作用：
            配了大模型时发一次网络请求；LLMService 把这次调用（成功或失败）记进 llm_calls 表。
            不改 card，也不把说明存回分析结果，存不存由调用方决定。
        会抛的错：
            LLMError 在这里接住，不会抛出去。
        谁在用：
            syntherisk/pipeline.py 的 AnalysisRunner._explain()（流水线第 8 阶段）；
            api/analyses.py 的 get_step_card()（B10：这一步的说明还没写过时补写）。
        """
        facts = card_facts(card)
        note = "未配置大模型，按模板生成"
        if self.llm.enabled:
            try:
                # complete_json()：发一次请求，要求模型按 schema 返回 JSON；失败时抛 LLMError。
                # module="M11" 和两个 id 只用来记用量；schema_name 是给这个结构起的名字，有的厂商接口要求提供
                response = self.llm.complete_json(
                    module="M11",
                    system=STEP_SYSTEM_PROMPT,
                    user=json.dumps(facts, ensure_ascii=False),
                    schema=SENTENCES_SCHEMA,
                    schema_name="step_explanation",
                    org_id=org_id,
                    analysis_id=analysis_id,
                )
                # 核对模型写的句子：出处必须是这张卡里有的，数字必须是卡里有的
                sentences, dropped = validate_sentences(
                    response.data.get("sentences"), set(facts["allowed_sources"]), facts
                )
                if sentences:
                    return self._package(sentences, card["sources"], generated_by="llm", provider=response.provider,
                                         model=response.model, dropped=dropped)
                note = "大模型写的句子没有通过出处核对，改用模板"
            except LLMError as exc:
                note = f"大模型没有调通（{exc}），改用模板"
        # 走到这里说明没有可用的大模型结果：按模板写
        return self._package(self._step_template(card), card["sources"], generated_by="template", note=note)

    @staticmethod
    def _package(sentences, sources, *, generated_by, provider=None, model=None, dropped=0, note="") -> dict[str, Any]:
        """把句子包成统一的返回格式。

        参数：
            sentences     [{"text": 句子, "sources": [来源编号]}]。
            sources       这张卡（或这条路线）可以引用的出处 [{"id", "type", "label", ...}]；
                          拼文字时用它把来源编号换成显示用的标记。
            generated_by  "llm"（大模型写的）或 "template"（模板写的）。
            provider      大模型的厂商名；模板生成时是 None。
            model         大模型的型号；模板生成时是 None。
            dropped       大模型写的句子里，有几句没通过核对被丢掉了。
            note          补充说明，比如为什么退回了模板；没有时是空字符串。
        返回（字典）：
            sentences          同参数
            text               拼好的一段文字，每句后面带出处标记，例如“……[路线]。……[规则 · 气氛]。”
            generated_by       同参数
            provider、model    同参数
            dropped_sentences  同 dropped
            note               同参数
        谁在用：
            本类的 explain_step() 和 explain_route()。
        """
        return {
            "sentences": sentences,
            "text": render_sentences(sentences, sources),
            "generated_by": generated_by,  # llm / template
            "provider": provider,
            "model": model,
            "dropped_sentences": dropped,
            "note": note,
        }

    @staticmethod
    def _step_template(card: dict[str, Any]) -> list[dict[str, Any]]:
        """不用大模型时，按固定的模板根据证据卡写一步的说明。

        参数：
            card  证据卡。
        返回：
            句子列表 [{"text", "sources"}]，最多 6 句，按下面的顺序：
            1. 这一步是什么反应、由什么得到什么                              出处 ROUTE
            2. 文献依据，按证据等级有四种说法                                出处是先例编号；没有先例时是 SCORE 或 MODEL
            3. 操作上需要什么（扣分最多的那个操作标签）；没有标签就没有这一句   出处 RULE:<规则编号>
            4. 安全方面要注意哪个物质（危害最重的那个）；没有就没有这一句       出处 GHS
            5. 操作或安全没有评估时，如实说出来                              出处 SCORE
            6. 综合得分、扣分最多的维度；分数依据不足（thin）时再补半句        出处 SCORE
        模板句子的内容全部取自卡片，不再经过 validate_sentences()。
        谁在用：
            本类的 explain_step()。
        """
        sentences: list[dict[str, Any]] = []
        # 第 1 句：反应名、反应物、产物。没有名字的分子用分子式
        product = card["product"]["name"] or card["product"]["formula"]
        reactants = "、".join(m["name"] or m["formula"] for m in card["reactants"])
        sentences.append({"text": f"这一步是{card['name']}，由{reactants}得到{product}", "sources": ["ROUTE"]})

        # 第 2 句：文献依据。refs 是这张卡上列出的全部先例的编号（P1、P2……）
        evidence, yield_info = card["evidence"], card["yield"]
        refs = [p["ref"] for p in card["precedents"]]
        if evidence["level"] is None:
            # 等级是 None：先例库还没有导入，没有评估
            sentences.append({"text": "先例库还没有导入数据，这一步的文献依据暂时没有评估", "sources": ["SCORE"]})
        elif evidence["level"] == "E1":
            # E1：库里有同一个反应的报道。有文献报道的产率就接在后面；出处优先写同一反应的那几条先例
            exact = [p["ref"] for p in card["precedents"] if p["exact"]]
            text = f"先例库里有 {evidence['n_exact']} 个先例报道了同一个反应"
            if yield_info.get("source") == "literature":
                text += f"，{yield_info['label']}"
            sentences.append({"text": text, "sources": exact or refs})
        elif evidence["level"] in ("E2", "E3"):
            # E2 / E3：只有相似先例。直接引用 M5 写好的依据（evidence["basis"]），出处最多写三条先例
            text = f"没有找到同一个反应的报道，{evidence['basis']}"
            sentences.append({"text": text, "sources": refs[:3]})
        else:
            # E4：没有可参考的先例。卡上有逆合成模型的依据时出处写 MODEL，否则写 SCORE
            sentences.append({"text": "先例库里没有找到可参考的先例，这一步只有模型依据",
                              "sources": ["MODEL"] if card.get("model") else ["SCORE"]})

        # 第 3 句：操作。只提扣分最多的那个标签
        tags = card["conditions"].get("tags") or []
        if tags:
            main = max(tags, key=lambda t: t["penalty"])
            sentences.append({"text": f"操作上需要{main['label']}：{main['reason']}", "sources": [f"RULE:{main['rule_id']}"]})

        # 第 4 句：安全。most_severe()（m06_safety）在这一步用到的物质里挑危害最重的一个；
        # 这一步的产物自己不算。没有带危害类别的物质时返回 None，就没有这一句
        worst = most_severe([h for h in card["hazards"] if h["key14"] != card["product"]["key14"]])
        if worst:
            sentences.append({"text": f"安全方面要注意{worst['name']}：{worst['statement']}", "sources": ["GHS"]})

        # 没评估的项目要说出来，免得读的人以为“没提就是没问题”
        by_key = {d["key"]: d for d in card["dimensions"]}
        missing = []
        if not tags and by_key["operation"]["score"] is None:
            missing.append("不知道这一步用什么试剂和条件，操作复杂度没有评估")
        if by_key["safety"]["score"] is None:
            missing.append("用到的物质都没有危害数据，安全性没有评估")
        if missing:
            sentences.append({"text": "；".join(missing), "sources": ["SCORE"]})

        # 最后一句：综合得分，以及损失最大的维度（损失 = 权重 × (100 − 维度分)，只在有分数的维度里比）
        scored = [d for d in card["dimensions"] if d["score"] is not None]
        if scored and card["score"] is not None:
            lowest = max(scored, key=lambda d: d["weight"] * (100 - d["score"]))
            # :g 去掉多余的小数位（78.0 写成 78）
            text = f"这一步综合得分 {card['score']:g}，扣分主要在{lowest['short']}一项"
            if card.get("thin"):
                # 依据不足的步骤：把实际算进去的维度列出来，提醒这个分数参考价值有限
                text += f"；这个分数只算了{'、'.join(d['short'] for d in scored)}，参考价值有限"
            sentences.append({"text": text, "sources": ["SCORE"]})
        return sentences[:6]

    # ------------------------------------------------------------ 整条路线（报告封面的推荐理由）

    def explain_route(self, route: dict[str, Any], *, org_id: str | None = None, analysis_id: str | None = None) -> dict[str, Any]:
        """给整条路线写推荐理由（路线详情里的 explanation，报告“一、推荐路线”开头的那段话）。

        流程和 explain_step() 一样，只是输入不是证据卡，而是 M10 写在这条路线上的汇总信息。
        参数：
            route        评过分的路线；读它的 name、steps、score、confidence_label、confidence_reason、
                         summary（汇总条）和 weakest（最薄弱环节，可以没有）。
            org_id       课题组 id，只用来记大模型用量，可以是 None。
            analysis_id  分析 id，只用来记大模型用量，可以是 None。
        返回：
            同 explain_step()。可以引用的出处固定是五个：SCORE、STOCK、RULE、GHS、ROUTE。
        模板生成时的句子（没配大模型、调用失败、或者一句都没通过核对时）：
            1. 路线名、步数、综合得分、置信度和理由        出处 SCORE
            2. 最薄弱的环节和建议（有 weakest 才有）       出处 SCORE
            3. 需要的特殊操作（有才有）                    出处 RULE
            4. 所需物料里库存已有几样（有物料才有）        出处 STOCK
        副作用：
            配了大模型时发一次网络请求，用量记进 llm_calls 表。不改 route，存不存由调用方决定。
        会抛的错：
            LLMError 在这里接住，不会抛出去。
        谁在用：
            syntherisk/pipeline.py 的 AnalysisRunner._explain()（第 8 阶段，给排名靠前的几条路线写）；
            api/reports.py 的 create_report()（F3：这条路线还没有推荐理由时现写，不存回去）。
        """
        summary, weakest = route["summary"], route.get("weakest")
        # 这段话可以引用的出处。路线的推荐理由不引用具体的先例，所以只有这五个固定的编号
        sources = [
            {"id": "SCORE", "type": "score", "label": "评分规则"},
            {"id": "STOCK", "type": "stock", "label": "库存与在售目录"},
            {"id": "RULE", "type": "rule", "label": "条件规则"},
            {"id": "GHS", "type": "ghs", "label": "GHS 危害分类"},
            {"id": "ROUTE", "type": "route", "label": "逆合成路线"},
        ]
        # 发给大模型的事实。每一项都带 source，告诉模型引用这一项时该写哪个来源编号
        facts = {
            # 路线名和各步的反应名
            "route": {"source": "ROUTE", "name": route["name"], "steps": [s["name"] for s in route["steps"]]},
            # 路线分和置信度
            "score": {"source": "SCORE", "value": route["score"], "confidence": route["confidence_label"],
                      "confidence_reason": route["confidence_reason"]},
            # 最薄弱的一步；没有时是 None
            "weakest_step": {"source": "SCORE", "step": weakest["step_index"], "name": weakest["name"],
                             "score": weakest["score"], "why": weakest["text"],
                             "suggestion": weakest["suggestion"]} if weakest else None,
            # 库存覆盖：百分数和“1/9”这样的文字
            "inventory": {"source": "STOCK", "coverage_percent": summary["inventory_coverage"],
                          "count": summary["inventory_count"]},
            # 特殊操作，形如 ["无水无氧操作 1 步"]
            "special_operations": {"source": "RULE", "items": [op["text"] for op in summary["special_operations"]]},
            # 最重的危害；**summary["max_hazard"] 把那个字典的 flag、label、substances 三项展开放进来
            "max_hazard": {"source": "GHS", **summary["max_hazard"]} if summary["max_hazard"] else None,
            # 文献覆盖（百分数）
            "literature_coverage_percent": {"source": "SCORE", "value": summary["literature_coverage"]},
            "allowed_sources": [s["id"] for s in sources],
        }
        note = "未配置大模型，按模板生成"
        if self.llm.enabled:
            try:
                # 和 explain_step() 一样：调模型、核对句子，有句子留下来就用
                response = self.llm.complete_json(
                    module="M11", system=ROUTE_SYSTEM_PROMPT, user=json.dumps(facts, ensure_ascii=False),
                    schema=SENTENCES_SCHEMA, schema_name="route_summary", org_id=org_id, analysis_id=analysis_id,
                )
                sentences, dropped = validate_sentences(response.data.get("sentences"), set(facts["allowed_sources"]), facts)
                if sentences:
                    return self._package(sentences, sources, generated_by="llm", provider=response.provider,
                                         model=response.model, dropped=dropped)
                note = "大模型写的句子没有通过出处核对，改用模板"
            except LLMError as exc:
                note = f"大模型没有调通（{exc}），改用模板"

        # 模板：第 1 句总是有，后面三句有相应的内容才写
        # 路线分可能是 None：每一步的六个维度全都没有数据时算不出分数。这时照实写，不能拿 None 去套数字格式
        scored = "综合得分 {:g}".format(route["score"]) if route["score"] is not None else "各项都没有数据，没有算出综合得分"
        sentences = [{
            "text": f"“{route['name']}”路线共 {summary['n_steps']} 步，{scored}，"
                    f"置信度{route['confidence_label']}（{route['confidence_reason']}）",
            "sources": ["SCORE"],
        }]
        if weakest:
            # weakest 只从有分数的步骤里挑（m10 的 _weakest_link），所以这里的 score 一定是数字
            sentences.append({
                "text": f"最薄弱的环节是第 {weakest['step_index']} 步{weakest['name']}（{weakest['score']:g} 分）；"
                        f"{weakest['suggestion']}",
                "sources": ["SCORE"],
            })
        if summary["special_operations"]:
            operations = "、".join(op["text"] for op in summary["special_operations"])
            sentences.append({"text": f"需要的特殊操作：{operations}", "sources": ["RULE"]})
        if summary["inventory_count"]:
            sentences.append({"text": f"所需物料里库存已有 {summary['inventory_count']}", "sources": ["STOCK"]})
        return self._package(sentences, sources, generated_by="template", note=note)
