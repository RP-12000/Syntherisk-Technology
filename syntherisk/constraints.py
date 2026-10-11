"""约束与偏好（新建分析第 2 步，按钮 A5、A7–A15）。

Constraints 是前端提交的原样；EffectiveConstraints 是把档位、勾选、高级参数和
一句话要求（已经由 M2 解析成标签、用户确认过）合并之后，流水线真正执行的那一份。

这个文件不属于哪一个编号的模块：它定义“约束”这份数据长什么样，M0 的接口、M2、M10 和流水线都要用。

数据怎么走：
    前端的约束页
      → POST /api/analyses 请求体里的 constraints 字段（一个 Constraints，Pydantic 负责校验）
      → 原样存进 Analysis.constraints
      → 流水线第 1 阶段调 merge_constraints()，合并成 EffectiveConstraints，同时生成 R2 的约束标签
      → 第 2 阶段（M3）用它定搜索的步数上限和原料来源；第 7 阶段（M10）用它排除路线（硬约束）和扣分（软偏好）
    EffectiveConstraints 不存数据库：结果页放宽条件（B6）、重新排序、换权重时，都是从存着的
    Constraints 现合并一份。合并只查内存里的参考数据，不读写数据库，不联网。

硬约束和软偏好：
    硬约束（hard）  违反了，整条路线被排除。被排除的路线仍然留在结果里并写明原因，B6 可以临时放宽
    软偏好（soft）  违反了不排除，只在那一步的分数上扣分，每命中一次扣 data/scoring.toml 里
                    soft_preference.penalty 那么多分

谁调用它：
    api/schemas.py     AnalysisCreate（开始分析的请求体）里嵌着 Constraints
    api/core.py        保存约束预设（A15）时用 Constraints 校验并补全默认值；GET /api/meta/options 用两张文字表
    api/views.py、api/analyses.py、api/reports.py   读出 Analysis.constraints，需要时调 merge_constraints
    pipeline.py        AnalysisRunner.run（第 1 阶段）和 rescore
    modules/m02_constraints.py   用 RequirementTag 生成一句话要求的标签
    modules/m10_scoring.py       读 EffectiveConstraints 的各个字段；用 EVIDENCE_ORDER 比证据等级的高低
    modules/m13_reports.py       用 SCALE_LABELS
它依赖：m01_structure（SMILES → key14 和规范写法）、reference（约束词表、复杂度三档的定义、试剂名称）。

本文件里有什么：
    ConditionKey、EVIDENCE_ORDER、SCALE_LABELS、STOCK_LABELS   几个常量
    SafetyRequirements     A11 安全要求（三个勾选框）
    AdvancedParameters     A13 高级参数
    StartingMaterial       A5 指定起始骨架 / 必须使用的原料
    RequirementTag         A14 一句话要求解析出来的一个标签
    Constraints            上面这些合在一起：前端提交的整份约束
    EffectiveConstraints   合并以后，流水线真正执行的那一份
    _substance_entry       SMILES → (key14, 显示名)
    merge_constraints      Constraints → EffectiveConstraints
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from syntherisk.modules import m01_structure as chem
from syntherisk.reference import ReferenceData

# A10“做不了的条件”可以勾的五项的编号：加压气体、无水无氧、手套箱、低温、光化学或电化学。
# Literal 是类型标注，意思是“值只能是列出来的这几个字符串之一”；Pydantic 按它校验，传别的值接口返回 422。
# 这五个编号要和 data/vocabulary.toml 的 [condition.*]、data/condition_rules.toml 里规则的 constraint 一栏对得上：
# M7 给某一步打上带 constraint 的操作标签，M10 再拿标签上的编号和这里勾选的比。
ConditionKey = Literal["pressurized_gas", "anhydrous_anaerobic", "glovebox", "cryogenic", "photo_electro"]
# 证据等级从强到弱的顺序：E1 文献直接报道、E2 高相似先例、E3 低相似先例、E4 仅模型预测。
# 比两个等级谁强谁弱，就比它们在这个列表里的位置（m10_scoring.exclusion_reasons）。
EVIDENCE_ORDER = ["E1", "E2", "E3", "E4"]
# A12 规模三档的显示文字，键是 Constraints.scale 的取值。每一档按多少毫克目标产量算，在 data/scoring.toml 的 [scale]。
SCALE_LABELS = {"mg": "mg 级", "hundred_mg": "百 mg 级", "g": "g 级"}
# A8 原料来源三档的显示文字，键是 Constraints.stock_source 的取值。
STOCK_LABELS = {"inventory_only": "仅用库存", "inventory_first": "库存优先 + 可购买", "any": "不限来源"}


class SafetyRequirements(BaseModel):
    """A11 安全要求。

    三个勾选框，默认都不勾。勾上的是硬约束：某一步用到这类物质（反应物、试剂或中间产物），整条路线被排除。
    目标分子自己的危害不算。

    - `exclude_acute_toxic`：排除剧毒物质（GHS 急性毒性 1–2 类，例如氟化氢）
    - `exclude_cmr`：排除致癌、致突变、生殖毒性物质（GHS 1 类；“疑似”的 2 类不排除）
    - `exclude_controlled`：排除易制毒 / 管制化学品。只排除管得严的那一级，一般管理的（丙酮、盐酸这类）只提示
    """

    # 下面三项在 merge_constraints 里变成 EffectiveConstraints.excluded_safety 里的
    # "acute_toxic"、"cmr"、"controlled"；M10（exclusion_reasons）拿它们对照 M6 给每个物质打的危害标记
    exclude_acute_toxic: bool = False  # 排除急性毒性 1–2 类（如 HF）
    exclude_cmr: bool = False  # 排除致癌、致突变、生殖毒性物质
    exclude_controlled: bool = False  # 排除易制毒 / 管制化学品


class AdvancedParameters(BaseModel):
    """A13 高级参数。预算和货期现在只记录，等有了供应商价格数据才能执行。

    都可以不填。

    - `max_steps`：路线最多几步（1–15）。不填时用复杂度档位自带的上限（各档是多少见 `GET /api/meta/options`）
    - `min_evidence_level`：每一步的证据等级不能低于它，取 `E1`、`E2` 或 `E3`。先例库没有数据时不起作用
    - `max_steps_without_precedent`：最多允许几步没有文献先例（证据等级 E4）。不填时用档位自带的值
    - `avoid_purifications`：尽量避免的纯化方式（软偏好，只扣分），编号见 `GET /api/meta/options` 的 `purifications`
    - `excluded_reagent_classes`：排除的试剂类别（硬约束），编号见 `GET /api/meta/options` 的 `reagent_classes`
    - `excluded_substances`：排除的具体物质（硬约束），每项是一个 SMILES
    - `budget`：预算上限。现在只显示成一个标签，不参与筛选
    - `lead_time_days`：货期上限（天）。现在只显示成一个标签，不参与筛选
    - `equipment`：设备列表。现在后端只是收下，没有任何地方用到它
    """

    # Field(default=None, ge=1, le=15)：默认不填；填了必须 ≥ 1 且 ≤ 15，否则接口返回 422
    max_steps: int | None = Field(default=None, ge=1, le=15)
    # 可选的值只有 E1–E3，最低的 E4 不在里面
    min_evidence_level: Literal["E1", "E2", "E3"] | None = None
    # 可以填 0（一步没有先例的都不许有）；None 才表示“用档位自带的值”
    max_steps_without_precedent: int | None = Field(default=None, ge=0)
    # default_factory=list：不填时默认是一个新的空列表。
    # 这里填的编号要在 data/vocabulary.toml 的 [purification.*] 里，不在的会被 merge_constraints 悄悄跳过
    avoid_purifications: list[str] = Field(default_factory=list)
    # 编号要在 data/vocabulary.toml 的 [reagent_class.*] 里，不在的同样被跳过
    excluded_reagent_classes: list[str] = Field(default_factory=list)
    # 读不出来的 SMILES 被跳过
    excluded_substances: list[str] = Field(default_factory=list)  # SMILES
    # 预算、货期：merge_constraints 只把它们做成 kind="info" 的标签
    budget: float | None = None
    lead_time_days: int | None = None
    # 整个后端没有别处读这个字段
    equipment: list[str] = Field(default_factory=list)


class StartingMaterial(BaseModel):
    """A5 指定起始骨架 / 必须使用的原料。

    硬约束：不满足的路线被排除。

    - `smiles`：结构。读不出来的话，开始分析的接口直接返回 422
    - `mode`：`scaffold`（默认）要求路线的起始原料里有一个含有这个结构（按子结构匹配）；
      `must_use` 要求起始原料里正好有这个物质
    """

    smiles: str
    # 两种模式怎么判断，见 m10_scoring.exclusion_reasons 里处理 starting_materials 的那一段
    mode: Literal["scaffold", "must_use"] = "scaffold"


class RequirementTag(BaseModel):
    """A14：一句话补充要求解析出来的一个约束标签。用户点了确认才会放进 Constraints.requirements。

    `POST /api/constraints/parse` 返回的 `tags` 里每一项就是这个结构；前端把用户确认过的那些原样放进
    开始分析时的 `constraints.requirements`。

    - `kind`：约束的类型，决定 `value` 填什么
        - `exclude_reagent_class`：一类试剂，`value` 是试剂类别的编号（如 `palladium`）
        - `exclude_substance`：一个具体物质，`value` 是它的名称，结构在 `smiles` 里
        - `exclude_condition`：一种操作条件，`value` 是条件的编号（如 `cryogenic`）
        - `avoid_purification`：一种纯化方式，`value` 是纯化方式的编号（如 `column_chromatography`）
        - `safety`：安全要求，`value` 是 `acute_toxic`、`cmr` 或 `controlled`
        - `max_steps`：步数上限，`value` 是数字写成的字符串（如 `"4"`）
        - `require_evidence`：最低证据等级，`value` 是 `E1`、`E2` 或 `E3`
    - `value`：见上
    - `hardness`：`hard` 硬约束（违反就排除路线），`soft` 软偏好（只扣分）。
      `safety`、`max_steps`、`require_evidence` 三种不分软硬，一律按硬约束执行
    - `label`：显示在界面上的文字，如“排除试剂：钯催化剂”
    - `source_text`：用户原话里对应的那一小段
    - `smiles`：只有 `exclude_substance` 用，被排除物质的结构
    """

    # 最后一种 require_evidence 目前只有前端自己构造才会出现：M2 的大模型输出格式和关键词规则都不产生它
    kind: Literal[
        "exclude_reagent_class", "exclude_substance", "exclude_condition",
        "avoid_purification", "safety", "max_steps", "require_evidence",
    ]
    # 编号要在 data/vocabulary.toml 对应的那一组里；不在的标签在 merge_constraints 里不生效
    value: str
    hardness: Literal["hard", "soft"] = "hard"
    # label、source_text 只用来显示：merge_constraints 不读它们，标签上的文字是按 kind 和 value 重新生成的
    label: str = ""
    source_text: str = ""
    smiles: str | None = None  # exclude_substance 用


class Constraints(BaseModel):
    """新建分析第 2 步“约束与偏好”整页的内容，随开始分析（A16）一起提交。每一项都有默认值，可以只传要改的。

    - `complexity`（A7 复杂度）：`concise` 简洁、`balanced` 平衡（默认）、`explore` 探索。
      决定步数上限、允许几步没有文献先例，以及低置信度的路线能不能排到前面
    - `stock_source`（A8 原料来源）：`inventory_only` 仅用库存、`inventory_first` 库存优先 + 可购买（默认）、`any` 不限来源
    - `unavailable_conditions`（A10 做不了的条件）：`pressurized_gas`、`anhydrous_anaerobic`、`glovebox`、
      `cryogenic`、`photo_electro` 里的若干个。需要这些条件的路线被排除
    - `safety`（A11 安全要求）：见 SafetyRequirements
    - `scale`（A12 规模）：`mg`（默认）、`hundred_mg`、`g`。只影响报告里物料清单的用量
    - `advanced`（A13 高级参数）：见 AdvancedParameters
    - `starting_materials`（A5）：指定的起始骨架或必须使用的原料，见 StartingMaterial
    - `requirement_text`（A14）：用户写的那句补充要求的原话，只是存着备查
    - `requirements`（A14）：这句话解析出来、并且用户确认过的约束标签，见 RequirementTag
    """

    # 三档各自的步数上限等数字在 data/scoring.toml 的 [complexity.*]
    complexity: Literal["concise", "balanced", "explore"] = "balanced"  # A7
    # 流水线把它原样交给 M3 当 stock_mode；M10 用它给可得性打分、判断原料算不算“拿不到”
    stock_source: Literal["inventory_only", "inventory_first", "any"] = "inventory_first"  # A8
    unavailable_conditions: list[ConditionKey] = Field(default_factory=list)  # A10
    safety: SafetyRequirements = Field(default_factory=SafetyRequirements)  # A11
    scale: Literal["mg", "hundred_mg", "g"] = "mg"  # A12
    advanced: AdvancedParameters = Field(default_factory=AdvancedParameters)  # A13
    starting_materials: list[StartingMaterial] = Field(default_factory=list)  # A5
    # merge_constraints 不读这个字段；起作用的是下面的 requirements
    requirement_text: str = ""  # A14 的原话
    requirements: list[RequirementTag] = Field(default_factory=list)  # A14 确认后的标签


@dataclass
class EffectiveConstraints:
    """合并以后、流水线真正执行的那一份约束。由 merge_constraints() 生成，不存数据库。

    名字里带 excluded_ 的是硬约束（碰到就排除路线），带 soft_ 的是软偏好（碰到只扣分）。
    这是普通的 dataclass，不是 Pydantic 模型：它不出现在接口上，只在后端内部传递。
    谁在读它：pipeline.AnalysisRunner._search（步数上限、原料来源）；
              m10_scoring 的 exclusion_reasons（硬约束）、_soft_hits（软偏好）、
              _availability_dimension（原料来源）、score_result（复杂度档位）。
    """

    # 复杂度档位的编号：concise / balanced / explore。M10 排序时看它：explore 档只按分数排
    complexity: str
    # 档位的显示名（简洁 / 平衡 / 探索），写排除原因时用
    complexity_label: str
    # 原料来源：inventory_only / inventory_first / any
    stock_source: str
    # 规模：mg / hundred_mg / g。这里只是带着；出报告时读的是 Constraints.scale
    scale: str
    # 路线最多几步。先取高级参数里填的，没填用档位自带的；一句话要求里有步数上限时再取较小的那个
    max_steps: int
    # 最多允许几步没有文献先例（证据等级 E4）。高级参数里填了就用填的，否则用档位自带的
    max_steps_without_precedent: int
    # 每一步的证据等级不能低于它（E1 / E2 / E3）；None 表示不限
    min_evidence_level: str | None
    # 做不了的条件（硬约束），元素是 ConditionKey 那几个编号。来自 A10 的勾选和一句话要求里的硬约束
    excluded_conditions: set[str] = field(default_factory=set)
    # 尽量避免的条件（软偏好），只能来自一句话要求
    soft_conditions: set[str] = field(default_factory=set)
    # 安全上的硬约束：要排除哪几类危险物质。来自 A11 的勾选和一句话要求
    excluded_safety: set[str] = field(default_factory=set)  # acute_toxic / cmr / controlled
    # 排除的试剂类别（硬约束），元素是 data/vocabulary.toml 里 reagent_class 的编号
    excluded_reagent_classes: set[str] = field(default_factory=set)
    # 少用的试剂类别（软偏好）
    soft_reagent_classes: set[str] = field(default_factory=set)
    # 排除的具体物质（硬约束）。来自 A13 的 excluded_substances 和一句话要求里的硬约束
    excluded_substances: dict[str, str] = field(default_factory=dict)  # key14 -> 显示名
    # 少用的具体物质（软偏好），同样是 key14 -> 显示名
    soft_substances: dict[str, str] = field(default_factory=dict)
    # 不用的纯化方式（硬约束），只能来自一句话要求里的硬约束
    excluded_purifications: set[str] = field(default_factory=set)
    # 尽量避免的纯化方式（软偏好）。来自 A13 的 avoid_purifications 和一句话要求里的软偏好
    soft_purifications: set[str] = field(default_factory=set)
    # A5 指定的起始骨架 / 必须使用的原料，从 Constraints 原样带过来
    starting_materials: list[StartingMaterial] = field(default_factory=list)
    # 每个标签是一个字典：
    #   key       标签的编号，同一个编号只留一条，如 "complexity"、"condition:glovebox"、"substance:<key14>"
    #   label     显示的文字，如 “排除：手套箱”
    #   kind      tier 档位 / exclude 硬约束 / soft 软偏好 / info 只是说明、不执行
    #   hardness  "hard"、"soft" 或 None（档位和说明类的标签是 None）
    #   source    从哪来：tier 档位和规模 / checkbox 勾选框和起始原料 / advanced 高级参数 / ai 一句话要求
    tags: list[dict[str, Any]] = field(default_factory=list)  # R2 约束标签

    @property
    def tag_summary(self) -> str:
        """一句话概括合并结果，当作流水线第 1 阶段完成时的进度消息。

        例：“合并 9 条约束，其中 2 条来自一句话要求”；没有来自一句话要求的标签时只有前半句。
        @property 让它用起来像字段：写 effective.tag_summary，不加括号。
        谁在用：pipeline.AnalysisRunner.run。
        """
        # source 是 "ai" 的标签就是来自一句话要求（A14）的
        from_text = sum(1 for tag in self.tags if tag["source"] == "ai")
        suffix = f"，其中 {from_text} 条来自一句话要求" if from_text else ""
        return f"合并 {len(self.tags)} 条约束{suffix}"


def _substance_entry(reference: ReferenceData, smiles: str) -> tuple[str, str] | None:
    """把一个 SMILES 变成 (key14, 显示名)，约束里凡是指到具体物质的地方都用它。

    参数：reference  参考数据（用里面的内置试剂表查名字）；smiles  物质的结构。
    返回：(key14, 显示名)。显示名先用内置试剂表里的名字，表里没有就用规范 SMILES。
          SMILES 读不出来时返回 None，调用方就跳过这一项。
    例：氯仿 ClC(Cl)Cl → (它的 key14, "三氯甲烷")。
    谁在用：merge_constraints()（排除的物质、起始原料）。
    """
    key = chem.key14_of(smiles)
    if key is None:
        return None
    return key, reference.compounds.name_for(smiles) or chem.canonical_smiles(smiles)


def merge_constraints(constraints: Constraints, reference: ReferenceData) -> EffectiveConstraints:
    """流水线第 1 阶段“读入约束”：合并档位、勾选和一句话要求。

    参数：
        constraints  前端提交的约束（从 Analysis.constraints 读出来、经 Pydantic 校验过的）
        reference    参考数据：data/scoring.toml 里复杂度三档的定义、data/vocabulary.toml 的约束词表、内置试剂表
    返回：
        EffectiveConstraints。里面的 tags 就是界面 R2 显示的约束标签，顺序固定：
        复杂度档位 → 原料来源 → 做不了的条件 → 安全要求 → 高级参数 → 一句话要求 → 起始原料 → 规模。
    规则：
        · 步数上限：高级参数里填了就用它，没填用档位自带的；一句话要求里又有步数上限时，取两者中较小的。
        · 词表里没有的编号、读不出来的 SMILES，对应的那一项直接跳过，不报错。
        · 标签按 key 去重，先登记的留下。同一条约束既在勾选框里、又在一句话要求里出现时，
          界面上只显示一条，但两处的效果都会并进集合里。
        · 预算、货期只生成说明用的标签，不参与筛选。
    不读写数据库，不联网，也不改传进来的 constraints。
    例（tests/test_analysis_flow.py）：平衡档 + 勾了“加压气体”“手套箱”+ 勾了“排除剧毒”，得到 6 个标签：
        平衡 · ≤ 6 步、库存优先 + 可购买、排除：加压气体、排除：手套箱、排除：剧毒、mg 级
    谁在用：pipeline.AnalysisRunner.run（第 1 阶段）、pipeline.rescore（B6、B11、B12 之后重新过滤排序）；
            api/views.py 的 analysis_view（给前端现算约束标签）；api/analyses.py 的 relax_constraints（B6）；
            api/reports.py 的 _weighted_result（D1 换权重重算）。
    """
    # 这一档的定义，形如 {"label": "平衡", "max_steps": 6, "max_steps_without_precedent": 1}
    tier = reference.scoring["complexity"][constraints.complexity]
    advanced = constraints.advanced
    # 先把不需要判断的几项填好；各个集合和 tags 先是空的，下面逐类往里加
    effective = EffectiveConstraints(
        complexity=constraints.complexity,
        complexity_label=tier["label"],
        stock_source=constraints.stock_source,
        scale=constraints.scale,
        # 高级参数里的 max_steps 没填（None）时 or 取后面档位自带的值
        max_steps=int(advanced.max_steps or tier["max_steps"]),
        # 这一项不能用 or：填 0 是有意义的（一步没有先例的都不许有），所以明确判断是不是 None
        max_steps_without_precedent=int(
            tier["max_steps_without_precedent"]
            if advanced.max_steps_without_precedent is None
            else advanced.max_steps_without_precedent
        ),
        min_evidence_level=advanced.min_evidence_level,
        starting_materials=list(constraints.starting_materials),
    )
    # tags 和 effective.tags 是同一个列表，往 tags 里加就是往结果里加
    tags = effective.tags

    def tag(key: str, label: str, *, kind: str, source: str, hardness: str | None = None) -> None:
        """登记一个约束标签；同一个 key 已经登记过就不再加（先到的留下）。各项的含义见 EffectiveConstraints.tags。"""
        if not any(existing["key"] == key for existing in tags):
            tags.append({"key": key, "label": label, "kind": kind, "hardness": hardness, "source": source})

    # ---- A7 复杂度档位、A8 原料来源：各一个标签
    tag("complexity", f"{tier['label']} · ≤ {effective.max_steps} 步", kind="tier", source="tier")
    tag("stock", STOCK_LABELS[constraints.stock_source], kind="tier", source="tier")

    # ---- A10 做不了的条件：都是硬约束。label_for(..., short=True) 取词表里的短名，比如“加压气体”
    for key in constraints.unavailable_conditions:
        effective.excluded_conditions.add(key)
        label = reference.label_for("condition", key, short=True)
        tag(f"condition:{key}", f"排除：{label}", kind="exclude", source="checkbox", hardness="hard")

    # ---- A11 安全要求：三个勾选框，勾了哪个就加哪个
    for flag, enabled in (
        ("acute_toxic", constraints.safety.exclude_acute_toxic),
        ("cmr", constraints.safety.exclude_cmr),
        ("controlled", constraints.safety.exclude_controlled),
    ):
        if enabled:
            effective.excluded_safety.add(flag)
            label = reference.label_for("safety", flag, short=True)
            tag(f"safety:{flag}", f"排除：{label}", kind="exclude", source="checkbox", hardness="hard")

    # ---- A13 高级参数。步数上限和最低证据等级上面已经填进 effective 了，这里只补标签
    if advanced.max_steps:
        tag("max_steps", f"步数 ≤ {advanced.max_steps}", kind="exclude", source="advanced", hardness="hard")
    if advanced.min_evidence_level:
        tag("evidence", f"证据不低于 {advanced.min_evidence_level}", kind="exclude", source="advanced", hardness="hard")
    # 排除的试剂类别：编号要在词表里才算数
    for class_key in advanced.excluded_reagent_classes:
        if class_key in reference.vocabulary.get("reagent_class", {}):
            effective.excluded_reagent_classes.add(class_key)
            label = reference.label_for("reagent_class", class_key)
            tag(f"reagent:{class_key}", f"排除试剂：{label}", kind="exclude", source="advanced", hardness="hard")
    # 排除的具体物质：entry 是 (key14, 显示名)；SMILES 读不出来时是 None，跳过
    for smiles in advanced.excluded_substances:
        entry = _substance_entry(reference, smiles)
        if entry:
            effective.excluded_substances[entry[0]] = entry[1]
            tag(f"substance:{entry[0]}", f"排除试剂：{entry[1]}", kind="exclude", source="advanced", hardness="hard")
    # 高级参数里的“避免纯化方式”只算软偏好（扣分，不排除路线）
    for key in advanced.avoid_purifications:
        if key in reference.vocabulary.get("purification", {}):
            effective.soft_purifications.add(key)
            label = reference.label_for("purification", key)
            tag(f"purification:{key}", f"避免{label}", kind="soft", source="advanced", hardness="soft")
    # 预算、货期：只生成 kind="info" 的标签，effective 里没有对应的字段，后面没有任何模块执行它们。
    # 格式 :g 去掉多余的小数：500.0 写成 500
    if advanced.budget is not None:
        tag("budget", f"预算 ≤ {advanced.budget:g}（待有价格数据后生效）", kind="info", source="advanced")
    if advanced.lead_time_days is not None:
        tag("lead_time", f"货期 ≤ {advanced.lead_time_days} 天（待有货期数据后生效）", kind="info", source="advanced")

    # ---- A14 一句话要求：用户确认过的标签，逐个按 kind 处理。
    # 每个分支都先检查 value 在不在词表里（或者是不是合法的数字 / 等级），不合格的标签不生效也不显示。
    # 写法 (甲 if hard else 乙).add(value)：硬约束加进“排除”的集合，软偏好加进“少用 / 尽量避免”的集合
    for requirement in constraints.requirements:
        hard = requirement.hardness == "hard"
        kind = "exclude" if hard else "soft"
        value = requirement.value
        if requirement.kind == "exclude_reagent_class" and value in reference.vocabulary.get("reagent_class", {}):
            (effective.excluded_reagent_classes if hard else effective.soft_reagent_classes).add(value)
            label = reference.label_for("reagent_class", value)
            tag(f"reagent:{value}", f"{'排除试剂' if hard else '少用试剂'}：{label}", kind=kind, source="ai",
                hardness=requirement.hardness)
        elif requirement.kind == "exclude_substance" and requirement.smiles:
            # 具体物质认的是标签里带的 smiles，不是 value（value 只是名称）
            entry = _substance_entry(reference, requirement.smiles)
            if entry:
                (effective.excluded_substances if hard else effective.soft_substances)[entry[0]] = entry[1]
                tag(f"substance:{entry[0]}", f"{'排除试剂' if hard else '少用试剂'}：{entry[1]}", kind=kind,
                    source="ai", hardness=requirement.hardness)
        elif requirement.kind == "exclude_condition" and value in reference.vocabulary.get("condition", {}):
            (effective.excluded_conditions if hard else effective.soft_conditions).add(value)
            label = reference.label_for("condition", value, short=True)
            tag(f"condition:{value}", f"{'排除' if hard else '尽量避免'}：{label}", kind=kind, source="ai",
                hardness=requirement.hardness)
        elif requirement.kind == "avoid_purification" and value in reference.vocabulary.get("purification", {}):
            (effective.excluded_purifications if hard else effective.soft_purifications).add(value)
            label = reference.label_for("purification", value)
            tag(f"purification:{value}", f"{'不用' if hard else '避免'}{label}", kind=kind, source="ai",
                hardness=requirement.hardness)
        elif requirement.kind == "safety" and value in reference.vocabulary.get("safety", {}):
            # 安全、步数、证据等级这三种不看标签上写的 hardness，一律当硬约束
            effective.excluded_safety.add(value)
            label = reference.label_for("safety", value, short=True)
            tag(f"safety:{value}", f"排除：{label}", kind="exclude", source="ai", hardness="hard")
        elif requirement.kind == "max_steps" and value.isdigit():
            # 和已有的上限（高级参数或档位的）取较小的那个。
            # 标签的 key 和高级参数的是同一个 "max_steps"：高级参数里也填了步数时，这里的标签不会再加
            effective.max_steps = min(effective.max_steps, int(value))
            tag("max_steps", f"步数 ≤ {value}", kind="exclude", source="ai", hardness="hard")
        elif requirement.kind == "require_evidence" and value in EVIDENCE_ORDER[:3]:
            # EVIDENCE_ORDER[:3] 是 E1、E2、E3。直接覆盖高级参数里填的最低证据等级（不比较哪个更严）
            effective.min_evidence_level = value
            tag("evidence", f"证据不低于 {value}", kind="exclude", source="ai", hardness="hard")

    # ---- A5 起始原料：只在这里生成标签。真正的检查在 m10_scoring.exclusion_reasons，
    # 它读的是 effective.starting_materials（上面已经原样带过来了）
    for material in constraints.starting_materials:
        entry = _substance_entry(reference, material.smiles)
        if entry:
            prefix = "起始骨架" if material.mode == "scaffold" else "必须使用"
            tag(f"start:{entry[0]}", f"{prefix}：{entry[1]}", kind="exclude", source="checkbox", hardness="hard")

    # ---- A12 规模：排在最后的一个说明标签
    tag("scale", SCALE_LABELS[constraints.scale], kind="info", source="tier")
    return effective
