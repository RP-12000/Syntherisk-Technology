"""M2 约束解析（大模型的三处用途之一）：把“一句话补充要求”变成约束标签（A14 → R2）。

例：“不要用钯催化剂，尽量不过柱色谱”
    → 排除试剂：钯催化剂 · 硬约束
    → 纯化偏好：避免柱色谱 · 软偏好

模型只能在 data/vocabulary.toml 的词表里选，选不出来的原话会放进 unsupported 如实告诉用户，
这样不会出现“界面上显示了约束、流水线却执行不了”的情况。解析结果要用户点确认才生效。
没有配置大模型时，用同一张词表做关键词匹配。

对应 PPT 的 M2、按钮 A14。接口是 POST /api/constraints/parse（api/structures.py 的 parse_requirement）。
这个模块不在“开始分析”之后的八个阶段里：它在用户填约束页的时候被调用，只负责产出标签。
用户确认过的标签随 Constraints.requirements 一起提交，由 constraints.merge_constraints 并进约束，最后由 M10 执行。

两条解析路线，返回的格式完全一样：
    大模型      配了大模型时用。提示词里带着词表，要求模型按固定的 JSON 格式（SCHEMA）回答；
                回来的每个标签再由后端检查一遍（_validate、_make_tag），词表里没有的归到 unsupported。
                调用失败（超时、拒答、输出被截断……）时自动退回关键词规则，并在返回的 note 里说明。
    关键词规则  没配大模型、或者大模型没调通时用（_parse_with_rules）。把句子按标点切成小句，
                在每个小句里找：表示“硬 / 软”的措辞、词表里的同义词、内置试剂表里的物质名称、“不超过 N 步”这类写法。

它依赖：
    reference.ReferenceData     约束词表 data/vocabulary.toml（reference.vocabulary）和内置试剂表（reference.compounds）
    llm.LLMService              大模型调用层。每调一次，它会往 llm_calls 表里记一行用量（记账是它做的，不是这个文件）
    m01_resolver.NameResolver   把物质名称变成结构；内置试剂表里没有的名称可能要查 PubChem、OPSIN
    constraints.RequirementTag  一个标签的数据结构
这个文件自己不读写数据库，不写文件。

本文件里有什么：
    _CLAUSE_SPLIT、_MAX_STEPS、_KIND_BY_GROUP   关键词规则用的两个正则和一张对照表
    SYSTEM_PROMPT、SCHEMA                       给大模型的提示词，和要求它输出的格式
    ConstraintParser
        parse              入口：一句话 → {tags, unsupported, generated_by, note}
        _system_prompt     把词表拼到提示词后面
        _validate          检查大模型的输出
        _make_tag          检查一个标签能不能执行，并生成显示用的文字（两条路线共用）
        _hardness          从措辞判断是硬约束还是软偏好
        _parse_with_rules  关键词规则解析
"""

from __future__ import annotations

import re
from typing import Any

from syntherisk.constraints import RequirementTag
from syntherisk.llm import LLMError, LLMService
from syntherisk.modules.m01_resolver import NameResolver
from syntherisk.reference import ReferenceData, normalize_name

# 关键词规则把一句话切成小句用的分隔符：
#   [，,；;。.！!\n]+   方括号里任意一个字符（中英文的逗号、分号、句号、感叹号，还有换行），+ 表示连着一个或多个
#   并且|而且|同时|另外   这几个连接词
# 例：“不要用钯催化剂，尽量不过柱色谱” 切成 “不要用钯催化剂”“尽量不过柱色谱” 两个小句，各自判断
_CLAUSE_SPLIT = re.compile(r"[，,；;。.！!\n]+|并且|而且|同时|另外")
# 认“步数上限”的两种写法，中间用 | 隔开：
#   (?:不超过|最多|≤|<=|少于|控制在)\s*(\d{1,2})\s*步    “不超过 4 步”“最多6步”：数字在第 1 个捕获组
#   (\d{1,2})\s*步(?:以内|之内|以下)                    “5 步以内”：数字在第 2 个捕获组
# (?:…) 是只分组、不记下内容的括号；(\d{1,2}) 是一到两位数字，会被记下来；\s* 是可有可无的空白
_MAX_STEPS = re.compile(r"(?:不超过|最多|≤|<=|少于|控制在)\s*(\d{1,2})\s*步|(\d{1,2})\s*步(?:以内|之内|以下)")
# 词表（data/vocabulary.toml）里的四个组各对应哪一种标签（RequirementTag.kind）。
# 另外两种标签不靠词表：exclude_substance（具体物质，查试剂表）和 max_steps（步数，靠上面的正则）
_KIND_BY_GROUP = {
    "reagent_class": "exclude_reagent_class",
    "condition": "exclude_condition",
    "purification": "avoid_purification",
    "safety": "safety",
}

# 给大模型的系统提示词的前半段。_system_prompt() 会在它后面接上词表：每种 kind 可以填哪些编号。
# 这是发给模型的原文，改这里的字就是改模型的行为
SYSTEM_PROMPT = """你是 Syntherisk 的约束解析助手。用户会用一句话补充他们对合成路线的要求，你要把它拆成约束标签。

每个标签有四项：
- kind：约束的类型，只能是下面列出的几种
- value：必须是下面词表里的编号；kind 为 exclude_substance 时填用户提到的具体物质名称；kind 为 max_steps 时填步数（数字）
- hardness：hard 表示硬约束（“不要”“不能”“禁止”“排除”这类措辞），soft 表示软偏好（“尽量”“最好”“优先”“避免”这类措辞）
- source_text：用户原话里对应的那一小段

规则：
1. 只解析用户明确说出来的要求，不要自己添加。
2. 词表里找不到对应项、又不是某个具体物质的要求，把原话放进 unsupported，不要勉强归类。
3. 用户提到的是一类试剂（比如“钯催化剂”）用 exclude_reagent_class；提到的是一个具体物质（比如“氯仿”）用 exclude_substance。
4. safety 类的要求一律是硬约束。

kind 的取值和对应的词表：
"""

# 要求大模型返回的 JSON 的形状（JSON Schema）。和提示词一起交给 LLMService.complete_json，
# 由大模型调用层按厂商的方式让模型照这个格式输出。模型的回答形如：
#   {"tags": [{"kind": "exclude_reagent_class", "value": "palladium", "hardness": "hard",
#              "source_text": "不要用钯催化剂"}],
#    "unsupported": ["希望便宜一点"]}
# "required" 列出必须有的键；"additionalProperties": False 是不许多出别的键；"enum" 是只能取这几个值。
# kind 的可选值比 RequirementTag 少一种：这里没有 require_evidence。
SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["exclude_reagent_class", "exclude_substance", "exclude_condition",
                                 "avoid_purification", "safety", "max_steps"],
                    },
                    "value": {"type": "string"},
                    "hardness": {"type": "string", "enum": ["hard", "soft"]},
                    "source_text": {"type": "string"},
                },
                "required": ["kind", "value", "hardness", "source_text"],
                "additionalProperties": False,
            },
        },
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tags", "unsupported"],
    "additionalProperties": False,
}


class ConstraintParser:
    """M2 的解析器：一句话补充要求 → 约束标签。

    对象不保存状态，每次请求现建一个（api/structures.py 的 parse_requirement），用完就丢。
    """

    def __init__(self, reference: ReferenceData, llm: LLMService, resolver: NameResolver | None = None):
        """参数：
            reference  参考数据：约束词表和内置试剂表
            llm        大模型调用层。没有配置大模型时 llm.enabled 是 False，parse 直接走关键词规则
            resolver   名称转结构用的 NameResolver。可以不给：那样只认内置试剂表里有的物质名称
        """
        self.reference = reference
        self.llm = llm
        self.resolver = resolver

    def parse(self, text: str, *, org_id: str | None = None) -> dict[str, Any]:
        """把一句话补充要求解析成约束标签（A14 → R2）。这是本模块对外的唯一入口。

        参数：
            text    用户写的那句话，比如“不要用钯催化剂，尽量不过柱色谱”
            org_id  课题组的编号，只用来给大模型的用量记账；走关键词规则时用不到
        返回一个字典（接口原样返回给前端）：
            tags          标签的列表。每个标签是 RequirementTag 转成的字典：
                          kind、value、hardness、label、source_text、smiles（含义见 constraints.RequirementTag）
            unsupported   原话里没法变成约束的部分（字符串的列表），界面上如实告诉用户“这些暂不支持”
            generated_by  "llm"（大模型解析的）或 "rules"（关键词规则解析的）
            provider、model  只有 generated_by 是 "llm" 时才有：哪家厂商、哪个模型
            note          说明文字。正常由大模型解析时是空的；
                          否则是“未配置大模型，用关键词规则解析”或“大模型没有调通（原因），这次用关键词规则解析”
        text 是空的时候直接返回空结果，不调模型。
        例（tests/test_llm.py，没有配置大模型）：“不要用钯催化剂，尽量不过柱色谱” 得到两个标签：
            exclude_reagent_class / palladium / hard / “排除试剂：钯催化剂”
            avoid_purification / column_chromatography / soft / “纯化偏好：避免柱色谱”
        副作用：配了大模型时发一次模型请求（LLMService 会往 llm_calls 表里记一行用量）；
                标签里点名的物质不在内置试剂表里时，会通过 resolver 查 PubChem / OPSIN。
        大模型调用层的错误（LLMError）在这里接住，不往外抛。
        谁在用：api/structures.py 的 parse_requirement（POST /api/constraints/parse）。
        """
        text = (text or "").strip()
        if not text:
            return {"tags": [], "unsupported": [], "generated_by": "rules", "note": ""}
        if self.llm.enabled:
            try:
                # module="M2" 是记账时标的模块名；schema_name 是这种输出格式的名字，有的厂商接口要求给一个
                response = self.llm.complete_json(
                    module="M2", system=self._system_prompt(), user=text, schema=SCHEMA,
                    schema_name="constraint_tags", org_id=org_id,
                )
                # 模型的回答不能直接信：逐个标签检查，词表里没有的、物质认不出来的都挪到 unsupported
                tags, unsupported = self._validate(response.data)
                return {"tags": tags, "unsupported": unsupported, "generated_by": "llm",
                        "provider": response.provider, "model": response.model, "note": ""}
            except LLMError as exc:
                # 超时、拒答、返回的不是合格的 JSON 等：不让请求失败，记下原因，往下走关键词规则
                note = f"大模型没有调通（{exc}），这次用关键词规则解析"
        else:
            note = "未配置大模型，用关键词规则解析"
        tags, unsupported = self._parse_with_rules(text)
        return {"tags": tags, "unsupported": unsupported, "generated_by": "rules", "note": note}

    # ------------------------------------------------------------ 大模型

    def _system_prompt(self) -> str:
        """拼出发给大模型的完整系统提示词：SYSTEM_PROMPT 后面接上词表。

        接上去的每一行是一种 kind 和它可以填的编号，形如
            - exclude_reagent_class：palladium（钯催化剂）；precious_metal（贵金属催化剂）；……
        最后两行说明 exclude_substance 和 max_steps 的 value 怎么填。
        词表每次现读（reference.vocabulary），所以在 data/vocabulary.toml 里加一个类别，重启以后提示词里就有了。
        谁在用：parse()。
        """
        vocabulary = self.reference.vocabulary
        lines = [SYSTEM_PROMPT]
        for group, kind in _KIND_BY_GROUP.items():
            # 这一组里的每一项写成 “编号（显示名）”，用分号连起来
            entries = "；".join(f"{key}（{spec['label']}）" for key, spec in vocabulary.get(group, {}).items())
            lines.append(f"- {kind}：{entries}")
        lines.append("- exclude_substance：value 填具体物质的名称")
        lines.append("- max_steps：value 填数字")
        return "\n".join(lines)

    def _validate(self, data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """检查大模型返回的内容，只留下后端真能执行的标签。

        参数：data  模型返回的 JSON（已经解析成字典），形状见 SCHEMA。
        返回：(标签列表, 不支持的原话列表)。
            · 模型自己放进 unsupported 的原话照收（空白的不要）。
            · 每个标签交给 _make_tag 检查。通不过的（编号不在词表里、物质认不出来……），
              把它的 source_text（没有就用 value）加到“不支持”里。
            · kind 和 value 都相同的标签只留第一个。
        例（tests/test_llm.py）：模型给了四个标签——试剂类别 tin 和 plutonium、物质“独角兽粉”、条件 cryogenic。
            词表里没有 plutonium，“独角兽粉”认不出结构，所以只留下两个标签（有机锡试剂、低温），
            “不用钚”“不用独角兽粉”这两段原话进了不支持。
        谁在用：parse()。
        """
        tags: list[dict[str, Any]] = []
        # “data.get(...) or []”：键不存在或者值是 None 时当成空列表
        unsupported = [str(item) for item in data.get("unsupported") or [] if str(item).strip()]
        for raw in data.get("tags") or []:
            tag = self._make_tag(raw.get("kind"), str(raw.get("value", "")).strip(), raw.get("hardness"),
                                 str(raw.get("source_text", "")))
            if tag is None:
                unsupported.append(raw.get("source_text") or str(raw.get("value")))
            elif not any(t["kind"] == tag["kind"] and t["value"] == tag["value"] for t in tags):
                tags.append(tag)
        return tags, unsupported

    def _make_tag(self, kind: str | None, value: str, hardness: str | None, source_text: str) -> dict[str, Any] | None:
        """检查一个标签能不能真的被执行，并由后端自己生成显示用的文字。

        大模型和关键词规则两条路线都走这里，所以两边出来的标签格式、文字完全一样。
        参数：
            kind         标签类型，RequirementTag.kind 的取值之一；别的值一律不通过
            value        词表里的编号；kind 是 exclude_substance 时是物质名称；是 max_steps 时是数字字符串
            hardness     "hard" 或 "soft"；别的值（包括 None）当成 "hard"
            source_text  用户原话里对应的那一段，原样放进标签
        返回：RequirementTag 转成的字典（键：kind、value、hardness、label、source_text、smiles）；
              通不过检查时返回 None，调用方把这段原话归到“不支持”。
        各种 kind 怎么才算通过，以及 label 的写法：
            exclude_reagent_class  value 在词表的 reagent_class 里    排除试剂：钯催化剂 / 少用试剂：钯催化剂
            exclude_condition      value 在词表的 condition 里        排除：低温 / 尽量避免：低温
            avoid_purification     value 在词表的 purification 里     纯化偏好：不用柱色谱 / 纯化偏好：避免柱色谱
            safety                 value 在词表的 safety 里           排除：剧毒（一律硬约束）
            max_steps              value 是 1–15 的整数               步数 ≤ 4（一律硬约束）
            exclude_substance      能找到这个名称对应的结构           排除试剂：三氯甲烷 / 少用试剂：三氯甲烷
        （斜杠前面是硬约束时的文字，后面是软偏好时的。）
        require_evidence 不在上面，所以这里不会产生这种标签。
        副作用：exclude_substance 的名称不在内置试剂表里、又给了 resolver 时，会联网查 PubChem / OPSIN。
        谁在用：_validate()（大模型路线）、_parse_with_rules()（关键词规则路线）。
        """
        vocabulary = self.reference.vocabulary
        hardness = hardness if hardness in ("hard", "soft") else "hard"
        smiles = None
        # 下面一串 if / elif：每个分支既是检查（条件不成立就落到最后的 else，返回 None），也负责写 label
        if kind == "exclude_reagent_class" and value in vocabulary.get("reagent_class", {}):
            label = f"{'排除试剂' if hardness == 'hard' else '少用试剂'}：{vocabulary['reagent_class'][value]['label']}"
        elif kind == "exclude_condition" and value in vocabulary.get("condition", {}):
            label = f"{'排除' if hardness == 'hard' else '尽量避免'}：{self.reference.label_for('condition', value, short=True)}"
        elif kind == "avoid_purification" and value in vocabulary.get("purification", {}):
            label = f"纯化偏好：{'不用' if hardness == 'hard' else '避免'}{vocabulary['purification'][value]['label']}"
        elif kind == "safety" and value in vocabulary.get("safety", {}):
            # 安全要求不分软硬：不管传进来的 hardness 是什么，都改成硬约束
            hardness = "hard"
            label = f"排除：{self.reference.label_for('safety', value, short=True)}"
        elif kind == "max_steps" and value.isdigit() and 1 <= int(value) <= 15:
            # 步数上限同样一律是硬约束；1–15 和 constraints.AdvancedParameters.max_steps 的范围一致
            hardness = "hard"
            label = f"步数 ≤ {int(value)}"
        elif kind == "exclude_substance" and value:
            # 具体物质：要能找到结构才算数（M10 是按结构去认“这一步用没用到它”的）。
            # 先查内置试剂表（不联网）；查不到、又给了 resolver 时，再用 A3 那一套去查，只要第一个候选
            compound = self.reference.compounds.find_by_name(value)
            name = compound.display_name if compound else value
            smiles = compound.smiles if compound else None
            if smiles is None and self.resolver is not None:
                candidates, _ = self.resolver.lookup(value, limit=1, with_svg=False)
                if candidates:
                    smiles, name = candidates[0].smiles, candidates[0].name
            if smiles is None:
                return None
            # 显示用查到的正式名称（比如用户写“氯仿”，这里显示“三氯甲烷”）；value 仍是传进来的那个名称
            label = f"{'排除试剂' if hardness == 'hard' else '少用试剂'}：{name}"
        else:
            return None
        # 用 Pydantic 模型建一次（顺便按 RequirementTag 的定义校验），再用 model_dump() 转成普通字典
        tag = RequirementTag(kind=kind, value=value, hardness=hardness, label=label, source_text=source_text, smiles=smiles)
        return tag.model_dump()

    # ------------------------------------------------------------ 关键词规则

    def _hardness(self, clause: str) -> str | None:
        """软偏好的措辞优先（“尽量不…”算软偏好）；两类措辞都没有，就看不出用户是要排除还是在陈述。

        参数：clause  一个小句。
        返回："soft"、"hard" 或 None。
        两类措辞列在 data/vocabulary.toml 的 [hardness] 里：soft 是“尽量”“最好”“避免”“prefer”这些，
        hard 是“不要”“不能”“禁止”“without”这些。比较前把小句转成小写；只要小句里含有其中一个词就算。
        例：“尽量不要用钯”里两类词都有，先查软的，所以是 "soft"；“我们有手套箱”两类都没有，是 None。
        谁在用：_parse_with_rules()。
        """
        words = self.reference.vocabulary.get("hardness", {})
        lowered = clause.lower()
        if any(word in lowered for word in words.get("soft", [])):
            return "soft"
        if any(word in lowered for word in words.get("hard", [])):
            return "hard"
        return None

    def _parse_with_rules(self, text: str) -> tuple[list[dict[str, Any]], list[str]]:
        """不用大模型，靠词表做关键词匹配，把一句话解析成标签。

        参数：text  用户的原话（已经去掉首尾空白，不是空的）。
        返回：(标签列表, 不支持的小句列表)，格式和 _validate 的一样。
        做法：按 _CLAUSE_SPLIT 切成小句，每个小句单独处理：
            1. 用 _MAX_STEPS 找步数上限。找到就生成 max_steps 标签，不需要“不要”“尽量”这类措辞。
            2. 用 _hardness 判断这个小句是硬约束、软偏好，还是都看不出来。
               看不出来的小句不再往下找（只是提到某个词，不等于要排除它）；
               要是第 1 步也没有找到步数，这个小句就进“不支持”。
            3. 在小句里找词表四个组的同义词（synonyms），找到一个生成一个标签。长的同义词先找。
            4. 再找内置试剂表里的物质名称，找到一个生成一个 exclude_substance 标签。
            5. 第 1、3、4 步一个标签都没生成：这个小句进“不支持”。
        例（tests/test_llm.py）：“不能用氯仿；不超过 4 步；我们有手套箱；希望便宜一点”
            不能用氯仿    → 排除试剂：三氯甲烷（硬）
            不超过 4 步   → 步数 ≤ 4
            我们有手套箱  → 不支持（没有“不要”“尽量”这类措辞，只是陈述）
            希望便宜一点  → 不支持（“希望”是软偏好的措辞，但词表里没有对得上的内容）
        不联网：物质名称只在内置试剂表里找（传给 _make_tag 的是表里的正式名称，一定查得到）。
        谁在用：parse()。
        """
        vocabulary = self.reference.vocabulary
        tags: list[dict[str, Any]] = []
        unsupported: list[str] = []

        def add(tag: dict[str, Any] | None) -> bool:
            """收下一个标签。tag 是 None（没通过检查）返回 False；否则返回 True，kind 和 value 都重复的不再加。"""
            if tag is None:
                return False
            if not any(t["kind"] == tag["kind"] and t["value"] == tag["value"] for t in tags):
                tags.append(tag)
            return True

        for clause in (c.strip() for c in _CLAUSE_SPLIT.split(text)):
            if not clause:
                continue
            hardness = self._hardness(clause)
            # remaining：这个小句转成小写、去掉空格和标点以后的样子（reference.normalize_name）。
            # 下面每匹配到一个词，就把它从 remaining 里换成空格，剩下的部分接着找
            remaining = normalize_name(clause)
            found = False

            # 第 1 步：步数上限。两种写法的数字分别在第 1、第 2 个捕获组里，哪个有值用哪个。
            # “found |= …” 的意思是：这次 add 返回 True 的话，found 就变成 True
            steps = _MAX_STEPS.search(clause)
            if steps:
                found |= add(self._make_tag("max_steps", steps.group(1) or steps.group(2), "hard", clause))
            # 第 2 步：看不出硬还是软，就到此为止
            if hardness is None:
                if not found:
                    unsupported.append(clause)
                continue

            # 第 3 步
            # 先匹配词表里的类别，长的同义词优先；匹配到的部分从句子里挖掉，避免后面重复匹配
            # candidates：词表四个组里所有的同义词，每个是 (处理成和 remaining 同样写法的同义词, 组名, 编号)，
            # 比如 ("钯催化剂", "reagent_class", "palladium")。
            # 长的优先是因为“钯催化剂”“钯催化”“钯”都是 palladium 的同义词：先认最长的并把它挖掉，短的就不会再中一次
            candidates = [
                (normalize_name(synonym), group, key)
                for group in _KIND_BY_GROUP
                for key, spec in vocabulary.get(group, {}).items()
                for synonym in spec.get("synonyms", [])
            ]
            # key=lambda item: -len(item[0])：按同义词的长度从长到短排
            for synonym, group, key in sorted(candidates, key=lambda item: -len(item[0])):
                if synonym and synonym in remaining:
                    remaining = remaining.replace(synonym, " ")
                    found |= add(self._make_tag(_KIND_BY_GROUP[group], key, hardness, clause))

            # 第 4 步
            # 再看有没有点名具体物质。单个汉字或两个字母的名字太容易误伤，不匹配
            # compounds.by_name 的键是内置试剂表里所有的名称和别名（已经处理成小写、去掉空格和标点的写法）。
            # 同样从长到短找；只含 ASCII 字符的名字（英文名、缩写）至少 3 个字符、含中文的名字至少 2 个字才算
            names = sorted(self.reference.compounds.by_name, key=len, reverse=True)
            for name in names:
                minimum = 3 if name.isascii() else 2
                if len(name) >= minimum and name in remaining:
                    remaining = remaining.replace(name, " ")
                    compound = self.reference.compounds.by_name[name]
                    found |= add(self._make_tag("exclude_substance", compound.display_name, hardness, clause))

            # 第 5 步：这个小句什么都没认出来
            if not found:
                unsupported.append(clause)
        return tags, unsupported
