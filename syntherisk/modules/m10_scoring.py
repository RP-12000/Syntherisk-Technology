"""M10 评分与排序：六个维度加权，最弱一步决定上限。

    步骤分 = Σ（维度分 × 权重），没有数据的维度不计入，其余权重按比例放大；再减去软偏好扣分
    路线分 = 0.6 × 各步平均分 + 0.4 × 最弱一步
    置信度（高 / 中 / 低）单独给；低置信度路线不排在高置信度路线前面，除非用户选了“探索”档

这里的函数只读已经算好的评估结果，不查库、不联网，所以重新过滤（B6 放宽条件）、
重新排序（B11 排除分支）、换权重（D1）都是秒级。前端按同一个公式也能自己重算，
公式参数由 GET /api/scoring/config 给出。

对应关系
    PPT 模块：M10 评分与排序。
    流水线：第 7 阶段“评分与排序”（syntherisk/pipeline.py 的 AnalysisRunner._score 调 score_result）。
    第 6 阶段结尾还会调 base_dimensions()，把每一步里和路线无关的五个维度先算好，存进 reaction["dimensions"]。
    界面：
        A7  复杂度三档        步数上限、允许几步没有先例；“探索”档排序时不把低置信度路线往后放
        A8  原料来源三档      影响可得性这一维的分数，以及“原料不在库存里 / 买不到”的排除
        A5 A10 A11 A13 A14    起始骨架、做不了的条件、安全要求、高级参数、一句话要求：硬约束排除路线，软偏好扣分
        B1 B4 B9、R4 R7 R8    排序、路线卡片、最薄弱环节、路线列表、汇总条、被排除的路线：都读这里写回 route 的字段
        B6  放宽条件          POST /api/analyses/{id}/relax
        B11 排除分支或试剂、B12 标记为我有     改完以后由 pipeline.rescore() 调 score_result() 重新过滤和排序
        D1 D4 D5              权重滑杆、导出对比、差异步骤：POST /api/analyses/{id}/compare 和 …/compare/export

评分规则（数字都在 data/scoring.toml。那个文件开头写明：都是初稿，要用历史案例回测和化学家盲评来校准，
改完重启服务生效。给前端重算用的公式另见 docs/API.md “评分公式”）

  1. 六个维度，每个 0–100 分，越高越好。权重是 [weights]，加起来是 1：
         literature    文献依据      0.30   M5 按证据等级给：E1 90–100、E2 70–90、E3 40–70、E4 0–40（[evidence.levels]）
         operation     操作复杂度    0.20   M7：100 减去命中的操作规则的扣分
         safety        毒性与安全    0.20   本文件调 m06_safety.safety_score()：100 减去危害扣分（[safety.penalties]）
         availability  可得性与成本  0.15   本文件 _availability_dimension()：库存 100、能买到 70 或 100、买不到 0
         yield         产率          0.10   同一反应或高相似先例报道的产率的中位数，直接当分数
         green         绿色化学      0.05   M9：原子经济性和溶剂评级

  2. 没有数据的维度是 None，不是 0 分，也不是 100 分。None 的意思是“没评估”：
         literature    先例库还没有导入
         operation     不知道这一步用什么试剂和条件，又没有命中任何操作规则
         safety        投进去的物质一个都查不到危害数据
         availability  这一步没有需要准备的物料（或者物料的状态都不知道）
         yield         没有文献（或高相似先例）报道的产率
         green         原子经济性和溶剂评级都算不出来

  3. 步骤分 = Σ(维度分 × 权重) ÷ Σ(有数据的维度的权重) − 软偏好扣分，不低于 0，保留一位小数。
     只在有数据的维度之间加权平均，缺的维度的权重按比例分给其余各项。
     软偏好（“尽量不过柱色谱”这类）每命中一条扣 [soft_preference] 的 penalty，现在是 8 分。

  4. coverage 和 thin：coverage 是有数据的维度占了多少权重（0–1）。coverage 低于 [confidence] 的
     min_assessed_weight（现在是 0.3）时这一步标 thin = True：分数只是拿几个小项凑出来的。
     thin 的步骤算低置信度，并且不计入路线分（除非整条路线每一步都是 thin）。

  5. 路线分 = 0.6 × 各步平均分 + 0.4 × 最弱一步，保留一位小数。系数是 [route] 的 mean_coefficient 和
     weakest_coefficient。例子（PPT 附录 A，tests/test_scoring.py）：四步 90、88、80、48，
     平均 76.5，路线分 = 0.6 × 76.5 + 0.4 × 48 = 65.1。步数不在公式里，路线长短不直接影响路线分。

  6. 置信度和分数分开给。
     每一步：E1 高；E2 且最高相似度 ≥ 0.85（e2_high_similarity）高，否则中；E3 或等级未评估 中；E4 低；thin 低。
     整条路线（[confidence]）：
         低置信步骤 ≥ 2 个（low_steps_for_low）或占比 > 0.34（low_fraction_for_low）      → 低
         有低置信步骤但没到上面的程度                                                  → 中
         每一步的证据等级都没评估（先例库没导入）                                       → 中
         中置信步骤 ≥ 2 个（medium_steps_for_medium）或占比 > 0.34（medium_fraction_for_medium） → 中
         其余                                                                          → 高

  7. 硬约束决定排除，和分数无关：违反任何一条硬约束的路线标 excluded = True，但仍然留在结果里并写明原因
     （exclusion_reasons）。每条原因带一个 constraint 编号，B6 放宽条件时把编号传回来即可。

  8. 排序：没被排除的路线按路线分从高到低；除“探索”档外，低置信度路线不排在高置信度路线前面。

写回的数据（score_result 直接改传进来的 result，要不要存库由调用方决定）
    每条路线 route：steps、score、dims_avg、confidence、confidence_label、confidence_reason、min_stars、
                    weakest、summary、availability_details、exclusion_reasons、excluded、relaxed、rank
    result：        ranking（没被排除的路线 id，按名次）、excluded（被排除的路线 id）、weights（这次用的权重）

依赖
    constraints.EffectiveConstraints（合并后的约束）、reference.ReferenceData（scoring.toml 和词表）、
    m06_safety（安全分、危害排序）、m04_availability（“待询价”这个常量）、m01_structure（结构比较）。

本文件里有什么
    DIMENSIONS、CONFIDENCE_LABELS、FLAG_LABELS   六个维度的键；置信度和危害类别的中文名
    weighted_score()          六个维度按权重合成步骤分
    assessed_share()          有数据的维度占了多少权重（coverage）
    _mean()                   忽略 None 的平均值
    route_score()             由各步的分数算路线分
    base_dimensions()         一步里和路线无关的五个维度：分数、依据、出处
    _step_substances()        一步的反应物和试剂
    step_materials()          一步要准备的物料
    _availability_dimension() 可得性这一维
    _soft_hits()              一步命中了哪些软偏好
    step_confidence()         一步的置信度
    _route_confidence()       整条路线的置信度和理由
    _weakest_link()           最薄弱的一步
    _summary()                汇总条（R7）
    evaluate_route()          算一条路线的全部分数，写回 route
    exclusion_reasons()       一条路线违反了哪些硬约束
    rank_routes()             排名
    score_result()            总入口：评分、排除、排名
    compare_routes()          对比页（R9）的数据
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from syntherisk.constraints import EVIDENCE_ORDER, EffectiveConstraints
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m04_availability import PRICE_PENDING
from syntherisk.modules.m06_safety import safety_score, statements_behind, top_hazard
from syntherisk.reference import ReferenceData

# 六个维度的键，顺序固定：文献依据、操作复杂度、毒性与安全、可得性与成本、产率、绿色化学。
# 中文名在 data/scoring.toml 的 [dimension_labels]。本文件之外，syntherisk/cards.py（证据卡）、
# api/core.py（GET /api/scoring/config、权重预设的校验）和 api/reports.py（对比时校验权重）也用它。
DIMENSIONS = ["literature", "operation", "safety", "availability", "yield", "green"]
# 置信度的中文名。本文件的 evaluate_route() 和 syntherisk/cards.py 用。
CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}


def weighted_score(dimensions: dict[str, float | None], weights: dict[str, float]) -> float | None:
    """把一步反应六个维度的分数按权重合成一个分数（还没有减软偏好扣分）。

    参数：
        dimensions  每个维度的分数（0–100）。值是 None 表示“这一项没有评估”，不是 0 分。
        weights     每个维度的权重。默认是 data/scoring.toml 的 [weights]（加起来是 1）；
                    D1 拖权重滑杆时是用户给的一组数，不要求加起来是 1。
    返回：
        0–100 的分数，不四舍五入；有数据而且权重大于 0 的维度一个都没有时返回 None。
    算法：
        只在有数据的维度之间加权平均：Σ(分数 × 权重) ÷ Σ(有数据的维度的权重)。
        这样缺一项数据时，其余各项的权重按比例放大，而不是把缺的那一项当成 0 分。
    例子（tests/test_scoring.py）：
        六项是 100、78、85、70、68、60 时 = 30 + 15.6 + 17 + 10.5 + 6.8 + 3 = 82.9；
        产率没有数据时 = (30 + 15.6 + 17 + 10.5 + 3) ÷ 0.9。
    谁在用：
        本文件的 evaluate_route()；前端也按同一个公式重算，见 docs/API.md “评分公式”。
    """
    # 只留下有数据、并且权重大于 0 的维度
    used = [d for d in DIMENSIONS if dimensions.get(d) is not None and float(weights.get(d, 0)) > 0]
    # 分母：这些维度的权重之和。等于 0 说明没有任何能用的维度
    total = sum(float(weights[d]) for d in used)
    if total <= 0:
        return None
    return sum(float(weights[d]) * float(dimensions[d]) for d in used) / total


def assessed_share(dimensions: dict[str, float | None], weights: dict[str, float]) -> float:
    """这个分数用到了百分之多少的权重（0–1）。其余的维度没有数据，没算进去。

    参数：
        dimensions  每个维度的分数，None 表示没有数据。
        weights     每个维度的权重。
    返回：
        有数据的维度的权重之和 ÷ 六个维度的权重之和。比如只有可得性（0.15）和绿色化学（0.05）有数据时是 0.2。
        六个权重加起来不大于 0 时返回 0.0。
    谁在用：
        本文件的 evaluate_route()：结果存成 step["coverage"]，并且和 [confidence] 的 min_assessed_weight
        比较，得出 step["thin"]。
    """
    total = sum(float(weights.get(d, 0)) for d in DIMENSIONS)
    used = sum(float(weights.get(d, 0)) for d in DIMENSIONS if dimensions.get(d) is not None)
    return used / total if total > 0 else 0.0


def _mean(values: list[float | None]) -> float | None:
    """忽略 None 的平均值，保留一位小数；全是 None（或列表是空的）时返回 None。

    谁在用：
        本文件的 evaluate_route()，用它算 route["dims_avg"]（一条路线各步在某个维度上的平均分）。
    """
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 1) if known else None


def route_score(step_scores: list[float], coefficients: dict[str, float]) -> float | None:
    """由各步的分数算路线分：平均分和最弱一步各占一部分。

    参数：
        step_scores   各步的步骤分（0–100），不能有 None。
        coefficients  data/scoring.toml 的 [route]：mean_coefficient（现在是 0.6）和 weakest_coefficient（0.4）。
    返回：
        mean_coefficient × 各步平均分 + weakest_coefficient × 最低的一步，不四舍五入；
        step_scores 是空列表时返回 None。
    文件头说的“最弱一步决定上限”就是后一项：分数最低的一步单独占 weakest_coefficient 的分量，
    其余各步再好，也只能通过平均分那一项把路线分拉高。
    例子（PPT 附录 A，tests/test_scoring.py）：
        [90, 88, 80, 48] → 0.6 × 76.5 + 0.4 × 48 = 65.1。
    谁在用：
        本文件的 evaluate_route()；前端按同一个公式重算。
    """
    if not step_scores:
        return None
    mean = sum(step_scores) / len(step_scores)
    return float(coefficients["mean_coefficient"]) * mean + float(coefficients["weakest_coefficient"]) * min(step_scores)


# ---------------------------------------------------------------- 每一步里和路线无关的维度


def base_dimensions(reaction: dict[str, Any], target_key: str, scoring: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """文献、操作、安全、产率、绿色化学五项。可得性取决于这一步在哪条路线里，另算。

    做什么：
        把一步反应已经评估好的结果（M5 的证据、M6 的危害、M7 的条件、M9 的绿色化学）整理成
        “维度 -> {分数, 依据, 出处}”的统一格式。五项里只有安全分是在这里现算的，其余都照搬上游算好的分数。
    参数：
        reaction    result["reactions"] 里的一步。读它的 evidence、conditions、yield、green、hazards、model。
        target_key  目标分子的 key14（InChIKey 前 14 位）。目标分子自己的危害不计入安全分。
        scoring     reference.scoring，也就是 data/scoring.toml 的全部内容；这里只用 [safety.penalties]。
    返回：
        字典，键是 "literature"、"operation"、"safety"、"yield"、"green"，每个值是：
            score    0–100 的分数；None 表示这一项没有评估（不是 0 分）
            basis    给人看的依据，一句话
            sources  出处编号的列表：先例是 "P1"、"P2"……，规则是 "RULE:<规则编号>"（没有命中规则时是 "RULE"），
                     另有 "GHS"、"CALC"、"MODEL"、"SCORE"。证据卡用这些编号给每句话标出处
        safety 另外带 deductions：扣分明细，每项 {"flag": 危害类别, "substances": [物质名], "penalty": 实际扣的分}。
    各项什么时候是 None：
        literature  先例库还没有导入（M5 给的 literature_score 是 None）
        operation   不知道试剂和条件、又没有命中任何规则（M7 给的 operation_score 是 None）
        safety      投进去的物质（反应物和试剂）一个都查不到危害数据，并且没有任何扣分
        yield       没有同一反应或高相似先例报道的产率
        green       原子经济性和溶剂评级都算不出来（M9 给的 score 是 None）
    副作用：没有，不改 reaction。
    谁在用：
        syntherisk/pipeline.py 的 AnalysisRunner._conditions()：第 6 阶段给每一步算一次，存进 reaction["dimensions"]；
        本文件的 evaluate_route()：reaction 里没有存 dimensions 时现算。
    """
    # 上游各模块写在这一步上的结果。还没有时换成空字典，下面的 .get() 就都得到 None
    evidence = reaction.get("evidence") or {}
    conditions = reaction.get("conditions") or {}
    yield_info = reaction.get("yield") or {}
    green = reaction.get("green") or {}

    tags = conditions.get("tags") or []
    # 目标分子自己的危害对所有路线都一样，不计入这一步的安全分
    hazards = [h for h in reaction.get("hazards") or [] if h["key14"] != target_key]
    # safety_score()（m06_safety）：从 100 分往下扣。按危害类别扣，最重的一类全额扣，第二类扣一半，
    # 第三类扣四分之一，依次减半；每一类扣多少在 [safety.penalties]。deductions 是扣分明细
    safety, deductions = safety_score(hazards, scoring["safety"]["penalties"])
    # 查不到危害数据的物质（data_source 是 "none"）：在依据里点名，免得被读成“安全”
    no_data = [h["name"] for h in hazards if h["data_source"] == "none"]
    # 依据文字：最多列三类扣分，每类写“物质名：危害类别的中文名”，用分号隔开
    safety_basis = "；".join(
        f"{'、'.join(d['substances'])}：{FLAG_LABELS.get(d['flag'], d['flag'])}" for d in deductions[:3]
    ) or "没有查到需要扣分的危害"
    if no_data:
        safety_basis += f"（{'、'.join(no_data[:3])} 没有危害数据）"
    # 投进去的物质 = 反应物和试剂（role 不是 "product" 的）
    inputs = [h for h in hazards if h.get("role") != "product"]
    if inputs and all(h["data_source"] == "none" for h in inputs) and not deductions:
        # 投进去的东西一个都查不到危害数据：不能当成“没有危害”给满分
        safety, safety_basis = None, "这一步用到的物质都没有危害数据，安全这一项没有评估"

    # 操作这一维的依据：把命中的规则逐条列成“标签（理由）”。一条都没有时分两种情况说明：
    # M7 给了分数，说明确实没有命中规则；M7 没给分数，说明根本不知道条件
    operation_basis = "、".join(f"{t['label']}（{t['reason']}）" for t in tags)
    if not operation_basis:
        operation_basis = (
            "没有命中需要特殊操作的规则" if conditions.get("operation_score") is not None
            else "不知道这一步用什么试剂和条件，操作复杂度没有评估"
        )

    # 产率只认文献报道的：literature 是同一反应的报道值，similar_literature 是高相似先例的报道值
    reported = yield_info.get("source") in ("literature", "similar_literature")
    return {
        "literature": {
            "score": evidence.get("literature_score"),
            "basis": evidence.get("basis", "未评估"),
            # 出处：列出来的先例的编号。一条先例都没有时，有逆合成模型的依据写 MODEL，否则写 SCORE
            "sources": [p["ref"] for p in evidence.get("precedents") or []]
            or (["MODEL"] if reaction.get("model") else ["SCORE"]),
        },
        "operation": {
            "score": conditions.get("operation_score"),
            "basis": operation_basis,
            "sources": [f"RULE:{t['rule_id']}" for t in tags] or ["RULE"],
        },
        "safety": {"score": safety, "basis": safety_basis, "sources": ["GHS"], "deductions": deductions},
        "yield": {
            # 产率的中位数（0–100 的百分数）直接当作这一维的分数
            "score": yield_info.get("median") if reported else None,
            "basis": yield_info.get("label", "无产率记录"),
            "sources": yield_info.get("refs") or [],
        },
        "green": {"score": green.get("score"), "basis": green.get("basis", "未评估"), "sources": ["CALC"]},
    }


# 危害类别（m06_safety 里的 flag）-> 中文名，写安全这一维的依据时用（base_dimensions）。
# 前 14 个是 GHS 危险性说明归出来的类别，最后两个是管制清单给的。
# 扣分只看 [safety.penalties] 里有的类别；表里查不到的类别，依据里直接显示英文键。
FLAG_LABELS = {
    "acute_toxic_12": "剧毒（急性毒性 1–2 类）",
    "acute_toxic_3": "有毒（急性毒性 3 类）",
    "cmr_1": "致癌、致突变或生殖毒性",
    "cmr_2": "疑似致癌、致突变或生殖毒性",
    "explosive": "易爆",
    "pyrophoric": "遇空气自燃",
    "extremely_flammable": "极易燃",
    "highly_flammable": "高度易燃",
    "flammable": "易燃",
    "water_reactive": "遇水放出易燃气体",
    "corrosive": "腐蚀",
    "oxidizer": "氧化剂",
    "organ_toxicity": "损害器官",
    "sensitizer": "致敏",
    "controlled_strict": "管制化学品",
    "controlled_basic": "管制化学品（一般管理）",
}


# ---------------------------------------------------------------- 一条路线


def _step_substances(result: dict[str, Any], reaction: dict[str, Any]) -> list[dict[str, Any]]:
    """这一步的反应物和试剂在 molecules 表里的条目（不含产物）。

    参数：
        result    分析结果；result["molecules"] 是 key14 -> 分子 的表。
        reaction  一步反应；reaction["reactants"] 和 reaction["reagents"] 是 key14 的列表。
    返回：
        分子字典的列表，先反应物后试剂。
    谁在用：
        本文件的 _soft_hits() 和 exclusion_reasons()。
    """
    # (*a, *b) 把两个列表接成一个元组
    return [result["molecules"][key] for key in (*reaction["reactants"], *reaction["reagents"])]


def step_materials(result: dict[str, Any], route: dict[str, Any], reaction: dict[str, Any]) -> list[dict[str, Any]]:
    """这一步要准备的物料：作为起始原料的反应物，加上已知的试剂。

    中间体（由这条路线里别的步骤做出来的反应物）不算；内置试剂表里带 ubiquitous 角色的物质
    （随手就有，现在 data/compounds.csv 里只有水）也不算。

    参数：
        result    分析结果（用它的 molecules 表）。
        route     这一步所在的路线（用它的 leaf_keys：这条路线的起始原料）。
        reaction  这一步。
    返回：
        列表，每项是一个物料：
            key14   结构编号
            name    显示名；没有名字时用分子式
            smiles  结构
            role    "reactant"（起始原料）或 "reagent"（试剂）
            status  "inventory"（库存里有）、"purchasable"（能买到）、"unavailable"（买不到）或 "unknown"（不知道）
            label   状态的中文名（库存 / 可购买 / 买不到）；status 是 unknown 时是“未知”
    为什么和路线有关：
        同一个反应物，在一条路线里是买来的起始原料，在另一条路线里可能是前一步做出来的中间体。
    谁在用：
        本文件的 evaluate_route()。结果存进 step["materials"]，可得性分数、汇总条的库存覆盖、
        证据卡里的物料列表都从那里读。
    """
    leaves = set(route["leaf_keys"])
    materials = []
    for role, keys in (("reactant", reaction["reactants"]), ("reagent", reaction["reagents"])):
        for key in keys:
            # 反应物只算起始原料；不在 leaf_keys 里的是中间体，跳过
            if role == "reactant" and key not in leaves:
                continue
            molecule = result["molecules"][key]
            # availability 是 M4 写在分子上的可得性结果：status、label、ubiquitous 等
            availability = molecule.get("availability") or {}
            if availability.get("ubiquitous"):
                continue
            status = availability.get("status", "unknown")
            if role == "reagent" and status == "unavailable":
                status = "unknown"  # 试剂不在目录里，只说明目录不全，不能断定买不到
            materials.append(
                {
                    "key14": key,
                    "name": molecule.get("name") or molecule["formula"],
                    "smiles": molecule["smiles"],
                    "role": role,
                    "status": status,
                    "label": availability.get("label") if status != "unknown" else "未知",
                }
            )
    return materials


def _availability_dimension(materials: list[dict[str, Any]], stock_source: str, scoring: dict[str, Any]) -> dict[str, Any]:
    """可得性这一维：这一步要准备的物料平均有多好拿。

    参数：
        materials     step_materials() 的返回值。
        stock_source  A8 原料来源档位：inventory_only / inventory_first / any。
        scoring       reference.scoring；这里用 [availability]。
    返回：
        {"score", "basis", "sources"}，格式和 base_dimensions() 里的一项相同。
        score    各物料分数的平均值，保留一位小数。每个物料的分数来自 [availability]：
                 库存里有 100；能买到的，“不限来源”（any）档 100，另外两档 70；买不到 0。
                 状态是 unknown 的物料不参与平均。没有可以算的物料时是 None
        basis    前五个物料的“名字（状态）”；没有可以算的物料时是“这一步没有需要准备的起始原料”
        sources  固定是 ["STOCK"]
    例子：
        演示路线里的格氏羧基化要准备干冰、镁屑、乙醚，三样都是“可购买”，
        “库存优先”档下可得性是 70 分（tests/test_analysis_flow.py）。
    谁在用：
        本文件的 evaluate_route()。
    """
    config = scoring["availability"]
    # 三种状态各值多少分。能买到的值多少分，看用户选的原料来源档位
    values = {
        "inventory": float(config["inventory"]),
        "purchasable": float(config["purchasable_any" if stock_source == "any" else "purchasable_inventory_first"]),
        "unavailable": float(config["unavailable"]),
    }
    # 只算状态明确的物料；unknown 的不算
    known = [m for m in materials if m["status"] in values]
    if not known:
        return {"score": None, "basis": "这一步没有需要准备的起始原料", "sources": ["STOCK"]}
    score = sum(values[m["status"]] for m in known) / len(known)
    basis = "、".join(f"{m['name']}（{m['label']}）" for m in known[:5])
    return {"score": round(score, 1), "basis": basis, "sources": ["STOCK"]}


def _soft_hits(result: dict[str, Any], reaction: dict[str, Any], effective: EffectiveConstraints, reference: ReferenceData) -> list[str]:
    """这一步碰到了哪些“软偏好”（尽量避免，但不排除）。每碰到一条，步骤分扣一次分。

    软偏好来自 A13 的“避免某种纯化方式”，和 A14 一句话要求里措辞是“尽量、最好”的那些
    （怎么分硬和软，见 syntherisk/constraints.py 的 merge_constraints）。

    参数：
        result     分析结果（用 molecules 表查这一步的反应物和试剂）。
        reaction   这一步。
        effective  合并后的约束；这里读 soft_conditions、soft_purifications、soft_substances、soft_reagent_classes。
        reference  参考数据；用它判断一个物质属不属于某个试剂类别，以及取类别的中文名。
    返回：
        命中的说明文字，去掉重复并排好序，例如 ["偏好：避免柱色谱纯化"]。没有命中是空列表。
        evaluate_route() 按“条数 × [soft_preference] 的 penalty（现在是 8）”扣分。
    谁在用：
        本文件的 evaluate_route()。
    """
    hits = []
    # M7 给这一步打的操作标签：标签的 constraint / purification 在软偏好里，就算命中
    for tag in (reaction.get("conditions") or {}).get("tags") or []:
        if tag.get("constraint") in effective.soft_conditions:
            hits.append(f"尽量避免：{tag['label']}")
        if tag.get("purification") in effective.soft_purifications:
            hits.append(f"偏好：避免{tag['label']}")
    # 这一步的反应物和试剂：点名要少用的物质，或者属于要少用的试剂类别
    for molecule in _step_substances(result, reaction):
        if molecule["key14"] in effective.soft_substances:
            hits.append(f"少用试剂：{effective.soft_substances[molecule['key14']]}")
        for class_key in effective.soft_reagent_classes:
            # reagent_class_matches()：按 data/vocabulary.toml 里这个类别的物质清单和 SMARTS 判断
            if reference.reagent_class_matches(class_key, molecule["smiles"]):
                hits.append(f"少用试剂：{reference.label_for('reagent_class', class_key)}")
    # set() 去掉重复（同一类试剂在这一步出现两次只算一条），sorted() 让顺序固定
    return sorted(set(hits))


def step_confidence(reaction: dict[str, Any], scoring: dict[str, Any]) -> str:
    """一步的置信度，只看证据等级。返回 "high"、"medium" 或 "low"。

        E1（文献直接报道）                              high
        E2（高相似先例），最高相似度 ≥ 0.85              high；不到 0.85 是 medium
        E3（低相似先例），或者先例库没导入、等级未评估     medium
        E4（仅模型预测）                                low
    0.85 是 data/scoring.toml [confidence] 的 e2_high_similarity。

    参数：
        reaction  一步反应；读它的 evidence["level"] 和 evidence["best_similarity"]。
        scoring   reference.scoring。
    注意：
        evaluate_route() 里，依据太少（thin）的步骤不管证据等级，直接算 low，不调这个函数。
    谁在用：
        本文件的 evaluate_route()。
    """
    evidence = reaction.get("evidence") or {}
    level = evidence.get("level")
    if level == "E1":
        return "high"
    if level == "E2":
        # best_similarity：同类反应的先例里，底物（产物结构）最像的那一条的相似度（0–1）
        threshold = float(scoring["confidence"]["e2_high_similarity"])
        return "high" if (evidence.get("best_similarity") or 0) >= threshold else "medium"
    if level == "E4":
        return "low"
    return "medium"  # E3，或者先例库没导入、等级未评估


def _route_confidence(steps: list[dict[str, Any]], scoring: dict[str, Any]) -> tuple[str, str]:
    """整条路线的置信度和一句理由。

    参数：
        steps    evaluate_route() 里拼好的步骤列表；这里只读每一步的 index、confidence、evidence_level、thin。
        scoring  reference.scoring；用 [confidence]。
    返回：
        (置信度, 理由)。置信度是 "high"、"medium" 或 "low"；理由是给人看的一句话。
    规则（数字都在 data/scoring.toml 的 [confidence]，按下面的先后判断）：
        1. 有低置信度的步骤：
               低置信步骤 ≥ 2 个（low_steps_for_low），或者占比 > 0.34（low_fraction_for_low）  → low
               否则                                                                         → medium
           理由写明是哪几步、为什么：“第 2 步无文献先例”（E4），“第 1 步多数评分项没有数据”（thin）。
        2. 所有步骤的证据等级都是 None（先例库没导入）                                        → medium
        3. 中置信步骤 ≥ 2 个（medium_steps_for_medium），或者占比 > 0.34（medium_fraction_for_medium）
                                                           → medium，理由“第 N 步只有相似先例”
        4. 其余                                             → high，理由“每一步都有文献依据”
    占比的门槛是给短路线用的：三步里有一步（0.333）不超过 0.34，两步里有一步（0.5）就超过了。
    谁在用：
        本文件的 evaluate_route()；tests/test_scoring.py 直接测了几种组合。
    """
    config = scoring["confidence"]

    def numbers(items: list[dict[str, Any]]) -> str:
        """把几步的序号拼成“1、3”这样的文字，写理由时用。"""
        return "、".join(str(s["index"]) for s in items)

    low = [s for s in steps if s["confidence"] == "low"]
    medium = [s for s in steps if s["confidence"] == "medium"]
    if low:
        # 低置信度有两种来历，理由分开写：没有文献先例（E4），或者依据太少（thin）。同一步可能两种都占
        no_precedent = [s for s in low if s["evidence_level"] == "E4"]
        thin = [s for s in low if s["thin"]]
        reasons = [f"第 {numbers(no_precedent)} 步无文献先例"] if no_precedent else []
        reasons += [f"第 {numbers(thin)} 步多数评分项没有数据"] if thin else []
        # 个数够多，或者在路线里占的比例够大，整条路线才算“低”；否则算“中”
        too_many = len(low) >= int(config["low_steps_for_low"]) or len(low) / len(steps) > float(config["low_fraction_for_low"])
        return ("low" if too_many else "medium"), "；".join(reasons)
    if all(step["evidence_level"] is None for step in steps):
        return "medium", "先例库还没有导入数据，证据等级未评估"
    # 只有相似先例的步骤够多，或者在短路线里占的比例够大（一步的路线只有一个低相似先例，不能算“高”）
    medium_share = len(medium) / len(steps)
    if len(medium) >= int(config["medium_steps_for_medium"]) or medium_share > float(config.get("medium_fraction_for_medium", 1)):
        return "medium", f"第 {numbers(medium)} 步只有相似先例"
    return "high", "每一步都有文献依据"


def _weakest_link(steps, route_dimensions, weights, scoring) -> dict[str, Any] | None:
    """找出路线里最薄弱的一步（B9 高亮最薄弱环节），并说明分数主要扣在哪一项。

    参数：
        steps             evaluate_route() 里拼好的步骤列表。
        route_dimensions  步骤编号 -> 六个维度的详情（score / basis / sources），用来取依据文字。
        weights           这次评分用的权重。
        scoring           reference.scoring；用 [dimension_short_labels] 和 [weakest_link] 的 suggestion。
    返回：
        字典；没有任何一步有分数时返回 None。
            step_index      第几步（从 1 数）
            step_key        步骤编号
            name            反应名
            score           这一步的分数
            main_dimension  损失最大的维度（英文键）；损失 = 权重 × (100 − 维度分)
            text            说明文字，格式是“文献、可得性两项接近满分，扣分主要在操作：<操作这一维的依据>。”
                            “接近满分”指维度分 ≥ 95，最多提两项
            suggestion      建议，[weakest_link] 的 suggestion 填上步骤序号：“建议先做 50 mg 小试验证第 4 步”
    怎么选：
        在有分数的步骤里取分数最低的（并列时取靠前的一步）；依据不足（thin）的步骤先不考虑，除非每一步都是 thin。
    谁在用：
        本文件的 evaluate_route()，结果存进 route["weakest"]。
    """
    scored = [s for s in steps if s["score"] is not None]
    # 先在不是 thin 的步骤里找；去掉 thin 以后一步不剩（空列表算“假”），就退回全部有分数的步骤
    scored = [s for s in scored if not s["thin"]] or scored
    if not scored:
        return None
    weakest = min(scored, key=lambda s: s["score"])
    labels = scoring["dimension_short_labels"]
    details = route_dimensions[weakest["key"]]
    # 每个有数据的维度让这一步损失了多少：权重 × (100 − 维度分)
    losses = {
        d: float(weights.get(d, 0)) * (100 - value)
        for d, value in weakest["dimensions"].items()
        if value is not None
    }
    main = max(losses, key=losses.get) if losses else None
    # 接近满分（≥ 95）的其他维度，最多提两项，放在句子前半截
    full = [labels[d] for d, value in weakest["dimensions"].items() if value is not None and value >= 95 and d != main][:2]
    text = f"{'、'.join(full)}{'两项' if len(full) == 2 else '一项'}接近满分，" if full else ""
    if main:
        text += f"扣分主要在{labels[main]}：{details[main]['basis']}。"
    return {
        "step_index": weakest["index"],
        "step_key": weakest["key"],
        "name": weakest["name"],
        "score": weakest["score"],
        "main_dimension": main,
        "text": text,
        "suggestion": scoring["weakest_link"]["suggestion"].format(step=weakest["index"]),
    }


def _summary(result, route, steps) -> dict[str, Any]:
    """R7 底部汇总条。

    参数：
        result  分析结果（用 reactions 表和目标分子的 key14）。
        route   这条路线。这个函数里没有用到它，需要的内容都在 steps 里。
        steps   evaluate_route() 里拼好的步骤列表。
    返回（字典，存进 route["summary"]）：
        n_steps              步数
        overall_yield        总产率（百分数）：各步产率中位数连乘；有任何一步没有产率记录就是 None
        yield_known_steps    有产率记录的步数
        material_cost        原料成本，现在没有价格数据，固定是 None
        material_cost_label  固定是“待询价”
        max_hazard           这条路线上最重的危害：{"flag": 类别, "label": 中文名, "substances": [物质名]}；
                             没有任何 GHS 危害类别时是 None（只在管制清单上的不算）。目标分子自己不算
        literature_coverage  文献覆盖：不是 E4 的步骤占百分之几（整数）；有步骤的等级未评估时是 None
        inventory_coverage   库存覆盖：要准备的物料里库存已有的占百分之几（整数）；没有物料时是 None
        inventory_count      同上，写成“1/9”这样的文字；没有物料时是 None
        special_operations   特殊操作，按出现的步数从多到少：
                             [{"key": 条件编号, "label": 标签名, "steps": 几步, "text": "无水无氧操作 1 步"}]
    谁在用：
        本文件的 evaluate_route()。读它的有 api/views.py 的 route_card()（结果页卡片）、本文件的
        compare_routes()、m11_explain（推荐理由）和 m13_reports（报告）。
    """
    reactions = result["reactions"]
    target_key = result["target"]["key14"]

    # 总产率：每一步取产率的中位数（0–100），全都有才连乘，缺一步就不给总产率
    yields = [(reactions[s["key"]].get("yield") or {}).get("median") for s in steps]
    overall = None
    if yields and all(y is not None for y in yields):
        overall = 100.0
        for value in yields:
            overall *= value / 100
        overall = round(overall, 1)

    # 危害类别 -> 这条路线上属于这一类的物质名（不重复）。目标分子自己不算
    flags: dict[str, list[str]] = {}
    for step in steps:
        for hazard in reactions[step["key"]].get("hazards") or []:
            if hazard["key14"] == target_key:
                continue
            for flag in hazard["flags"]:
                # setdefault：这个类别第一次出现时先放一个空列表，然后拿到这个列表往里加
                names = flags.setdefault(flag, [])
                if hazard["name"] not in names:
                    names.append(hazard["name"])
    # top_hazard()（m06_safety）：按从重到轻的固定顺序，取这些类别里最重的一个，返回 (类别, 中文名)
    worst = top_hazard(list(flags))

    # 文献覆盖：每一步都评过等级才算；E4 是“仅模型预测”，其余都算有文献依据
    levels = [s["evidence_level"] for s in steps]
    coverage = None
    if all(level is not None for level in levels):
        coverage = round(100 * sum(level != "E4" for level in levels) / len(levels))

    # 这条路线要准备的全部物料（同一个物料在几步里出现只算一次），其中多少在库存里
    materials: dict[str, str] = {}
    for step in steps:
        for material in step["materials"]:
            materials.setdefault(material["key14"], material["status"])
    in_inventory = sum(status == "inventory" for status in materials.values())

    # 特殊操作：只数带 constraint 的标签（对应 A10 “做不了的条件”的那几种），按条件编号计步数
    operations = Counter()
    labels = {}
    for step in steps:
        for tag in (reactions[step["key"]].get("conditions") or {}).get("tags") or []:
            if tag.get("constraint"):
                operations[tag["constraint"]] += 1
                labels[tag["constraint"]] = tag["label"]
    return {
        "n_steps": len(steps),
        "overall_yield": overall,
        "yield_known_steps": sum(y is not None for y in yields),
        "material_cost": None,
        "material_cost_label": PRICE_PENDING,
        "max_hazard": {"flag": worst[0], "label": worst[1], "substances": flags[worst[0]]} if worst else None,
        "literature_coverage": coverage,
        "inventory_coverage": round(100 * in_inventory / len(materials)) if materials else None,
        "inventory_count": f"{in_inventory}/{len(materials)}" if materials else None,
        # most_common()：Counter 按次数从多到少给出 (条件编号, 步数)
        "special_operations": [
            {"key": key, "label": labels[key], "steps": count, "text": f"{labels[key]} {count} 步"}
            for key, count in operations.most_common()
        ],
    }


def evaluate_route(
    result: dict[str, Any],
    route: dict[str, Any],
    effective: EffectiveConstraints,
    reference: ReferenceData,
    weights: dict[str, float],
) -> None:
    """算一条路线的步骤分、路线分、置信度、最薄弱环节和汇总条，写回 route。

    参数：
        result     分析结果（molecules、reactions、target）。
        route      要算的路线；读它的 step_keys（按实验顺序排好的步骤编号）和 leaf_keys（起始原料）。
        effective  合并后的约束；用 stock_source（可得性怎么打分）和几组软偏好。
        reference  参考数据；用 reference.scoring。
        weights    这次用的六个权重。
    返回：没有（None）。结果通过 route.update() 写回 route：
        steps                 步骤列表，每一步的字段见下
        availability_details  步骤编号 -> 可得性这一维的 {score, basis, sources}（另外五维存在 reaction["dimensions"]）
        score                 路线分，保留一位小数；没有任何一步有分数时是 None
        dims_avg              每个维度在各步上的平均分（None 不参与），雷达图和“最安全”之类的排序用
        confidence            "high" / "medium" / "low"
        confidence_label      高 / 中 / 低
        confidence_reason     理由
        min_stars             各步证据星级里最低的；有步骤没评估时是 None
        weakest               最薄弱的一步，见 _weakest_link()
        summary               汇总条，见 _summary()
    steps 里每一步的字段：
        index            第几步（从 1 数，第 1 步最先做）
        key              步骤编号（result["reactions"] 的键）
        name             反应名
        dimensions       六个维度的分数，没有数据的是 None
        soft_hits        命中的软偏好（文字列表）
        soft_penalty     软偏好一共扣多少分
        score            步骤分；六个维度都没有数据时是 None
        coverage         有数据的维度占了多少权重（0–1，两位小数）
        thin             coverage 低于 [confidence] 的 min_assessed_weight 时为 True
        confidence       这一步的置信度；thin 的步骤固定是 "low"
        evidence_level   E1–E4；没评估是 None
        stars            证据星级 0–3；没评估是 None
        operation_label  操作标签，例如“操作 中等 · 需无水”
        materials        要准备的物料，见 step_materials()
        experiment       实验回填的摘要（M14 写在 reaction 上的）；没做过是 None
    副作用：只改 route；不改 result 里的 molecules 和 reactions。
    谁在用：
        本文件的 score_result()。
    """
    scoring = reference.scoring
    # 软偏好每命中一条扣多少分（[soft_preference] 的 penalty）
    penalty_per_hit = float(scoring["soft_preference"]["penalty"])
    target_key = result["target"]["key14"]
    steps: list[dict[str, Any]] = []
    # 步骤编号 -> 六个维度的详情（分数、依据、出处）。写最薄弱环节的说明文字时要用依据
    route_dimensions: dict[str, dict[str, Any]] = {}

    # enumerate(..., 1)：序号从 1 数起，和界面上的“第 1 步”一致
    for index, step_key in enumerate(route["step_keys"], 1):
        reaction = result["reactions"][step_key]
        materials = step_materials(result, route, reaction)
        # 和路线无关的五个维度，流水线第 6 阶段已经算好存在 reaction["dimensions"] 里；没有就现算。
        # 外面套一层 dict() 是复制一份：下面要往里加 availability，不能改到 reaction 上那一份，
        # 因为同一步反应可能出现在几条路线里，各条路线的可得性不一样
        details = dict(reaction.get("dimensions") or base_dimensions(reaction, target_key, scoring))
        details["availability"] = _availability_dimension(materials, effective.stock_source, scoring)
        route_dimensions[step_key] = details
        # 只留分数，形如 {"literature": 92.0, "operation": 78.0, "safety": None, ...}
        dimensions = {d: details[d]["score"] for d in DIMENSIONS}
        base = weighted_score(dimensions, weights)
        soft = _soft_hits(result, reaction, effective, reference)
        evidence = reaction.get("evidence") or {}
        # 有数据的维度加起来不到这么多权重，分数就只是凑出来的（比如只算了可得性和绿色化学）：按低置信度处理
        coverage = assessed_share(dimensions, weights)
        thin = coverage < float(scoring["confidence"].get("min_assessed_weight", 0))
        steps.append(
            {
                "index": index,
                "key": step_key,
                "name": reaction["name"],
                "dimensions": dimensions,
                "soft_hits": soft,
                "soft_penalty": penalty_per_hit * len(soft),
                # 步骤分 = 加权分 − 软偏好扣分，不低于 0，保留一位小数
                "score": None if base is None else round(max(0.0, base - penalty_per_hit * len(soft)), 1),
                "coverage": round(coverage, 2),
                "thin": thin,
                "confidence": "low" if thin else step_confidence(reaction, scoring),
                "evidence_level": evidence.get("level"),
                "stars": evidence.get("stars"),
                "operation_label": (reaction.get("conditions") or {}).get("operation_label"),
                "materials": materials,
                "experiment": reaction.get("experiment"),
            }
        )

    scored = [s for s in steps if s["score"] is not None]
    # 依据太少的步骤，分数没有意义（常常是虚高的满分），不让它抬高路线分
    # 写法 [...] or scored：去掉 thin 以后一步不剩（空列表算“假”）时，退回用全部有分数的步骤
    solid = [s for s in scored if not s["thin"]] or scored
    # 路线分用的是上面已经四舍五入到一位小数的步骤分
    total = route_score([s["score"] for s in solid], scoring["route"])
    confidence, reason = _route_confidence(steps, scoring)
    stars = [s["stars"] for s in steps]
    route.update(
        {
            "steps": steps,
            # 字典推导式：每一步只取 availability 这一项
            "availability_details": {k: v["availability"] for k, v in route_dimensions.items()},
            "score": None if total is None else round(total, 1),
            "dims_avg": {d: _mean([s["dimensions"][d] for s in steps]) for d in DIMENSIONS},
            "confidence": confidence,
            "confidence_label": CONFIDENCE_LABELS[confidence],
            "confidence_reason": reason,
            "min_stars": min(stars) if all(s is not None for s in stars) else None,
            "weakest": _weakest_link(steps, route_dimensions, weights, scoring),
            "summary": _summary(result, route, steps),
        }
    )


# ---------------------------------------------------------------- 硬约束：哪些路线被排除、为什么


def exclusion_reasons(
    result: dict[str, Any],
    route: dict[str, Any],
    effective: EffectiveConstraints,
    reference: ReferenceData,
    user_exclusions: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """每条原因都带 constraint 编号；B6 放宽条件时，把要放宽的编号传回来就行。

    做什么：
        检查一条路线违反了哪些硬约束，一条一条列出来。列表是空的，这条路线就可选；
        不是空的，就被排除（除非这些约束都被临时放宽了，见 score_result）。排除只看硬约束，不看分数。
    参数：
        result           分析结果。
        route            要检查的路线。
        effective        合并后的约束（档位、勾选、高级参数、一句话要求合在一起）。
        reference        参考数据（试剂类别的判断、物质的名字）。
        user_exclusions  用户在路线树上右键排除的分支和试剂（B11），每项形如
                         {"id", "type": "branch", "step_key", "name"} 或
                         {"id", "type": "substance", "key14", "smiles", "name"}。
    返回：
        原因列表，每项是字典：
            constraint  约束编号，见下表
            label       给人看的原因，例如“第 2 步用到剧毒物质 氯乙酸乙酯（H310 皮肤接触致命）”
            user        是不是用户自己排除的（B11）。True 的原因在“放宽挡住这条路线的全部约束”时不会被放宽
            step_index  第几步（针对某一步的原因才有）
            substance   物质名（安全类的原因才有）
    constraint 编号和检查的内容（按代码里的先后）：
        max_steps               步数超过上限（A7 档位的上限；A13 填了步数就用 A13 的；A14 的要求只会让它更小）
        precedent               没有文献先例（E4）的步数，超过档位允许的数量（A13 可以改）。先例库没导入时不检查
        evidence                有步骤的证据等级低于要求的最低等级（A13 / A14）。先例库没导入时不检查
        condition:<条件编号>     某一步的操作标签属于“做不了的条件”（A10，或 A14 里的硬性要求）
        purification:<纯化方式>  某一步要用被排除的纯化方式（A14 里的硬性要求）
        safety:acute_toxic      某一步用到剧毒物质（急性毒性 1–2 类）
        safety:cmr              某一步用到致癌、致突变或生殖毒性物质（1 类）
        safety:controlled       某一步用到严格管制的化学品（一般管理的不排除）
        substance:<key14>       某一步用到用户排除的物质
        reagent:<类别编号>       某一步用到用户排除的试剂类别（比如钯催化剂）
        stock                   “仅用库存”档：有起始原料不在库存里；其他档：有起始原料买不到
        start:<key14>           没有用到指定的原料（must_use），或者不是从指定的骨架出发（scaffold）
        user:<排除记录的 id>     用到了用户右键排除的分支或试剂（B11）
    同一个编号可以出现多次（比如两步都要加压，就有两条 condition:pressurized_gas，文字里的步骤序号不同）。
    目标分子自己的危害不算；安全类只检查这一步的 hazards（反应物、试剂、产物）。
    副作用：没有。
    谁在用：
        本文件的 score_result()。
    """
    reasons: list[dict[str, Any]] = []
    molecules, reactions = result["molecules"], result["reactions"]
    target_key = result["target"]["key14"]
    # [(第几步, 这一步的反应), ...]，序号从 1 数起
    steps = [(index, reactions[key]) for index, key in enumerate(route["step_keys"], 1)]

    def add(constraint: str, label: str, **extra) -> None:
        """记一条排除原因。编号和文字都相同的原因已经有了就不再加。

        参数：
            constraint  约束编号。
            label       原因文字。
            **extra     附带的字段（step_index、substance），原样并进这条原因。
        副作用：往外层的 reasons 列表里加一项。
        """
        if not any(r["constraint"] == constraint and r["label"] == label for r in reasons):
            reasons.append({"constraint": constraint, "label": label, "user": False, **extra})

    # ---- 步数上限（effective.max_steps：档位的上限，被 A13 / A14 覆盖或收紧以后的数）
    if len(steps) > effective.max_steps:
        add("max_steps", f"共 {len(steps)} 步，超过“{effective.complexity_label}”档的 {effective.max_steps} 步上限")

    # ---- 证据等级。只有每一步都评过等级才检查：先例库没导入时等级是 None，不能当成“没有先例”
    levels = [(index, (reaction.get("evidence") or {}).get("level")) for index, reaction in steps]
    if all(level is not None for _, level in levels):
        # 没有先例（E4）的步骤太多
        without = [index for index, level in levels if level == "E4"]
        if len(without) > effective.max_steps_without_precedent:
            numbers = "、".join(str(i) for i in without)
            add("precedent", f"第 {numbers} 步没有文献先例，“{effective.complexity_label}”档最多允许 "
                             f"{effective.max_steps_without_precedent} 步")
        if effective.min_evidence_level:
            # EVIDENCE_ORDER 是 ["E1", "E2", "E3", "E4"]，序号越大证据越弱。序号比要求的大，就是低于要求
            limit = EVIDENCE_ORDER.index(effective.min_evidence_level)
            weak = [index for index, level in levels if EVIDENCE_ORDER.index(level) > limit]
            if weak:
                numbers = "、".join(str(i) for i in weak)
                add("evidence", f"第 {numbers} 步的证据等级低于 {effective.min_evidence_level}")

    for index, reaction in steps:
        # ---- 操作条件和纯化方式：看 M7 给这一步打的标签
        for tag in (reaction.get("conditions") or {}).get("tags") or []:
            if tag.get("constraint") in effective.excluded_conditions:
                add(f"condition:{tag['constraint']}", f"第 {index} 步需要{tag['label']}", step_index=index)
            if tag.get("purification") in effective.excluded_purifications:
                add(f"purification:{tag['purification']}", f"第 {index} 步需要{tag['label']}", step_index=index)

        # ---- 安全要求（A11）：看 M6 给这一步每个物质标的危害类别（flags）
        for hazard in reaction.get("hazards") or []:
            if hazard["key14"] == target_key:
                continue
            # statements_behind()（m06_safety）：这个物质凭哪几条危险性说明归到这一类，写进原因里
            if "acute_toxic" in effective.excluded_safety and "acute_toxic_12" in hazard["flags"]:
                add("safety:acute_toxic",
                    f"第 {index} 步用到剧毒物质 {hazard['name']}（{statements_behind(hazard, 'acute_toxic_12')}）",
                    step_index=index, substance=hazard["name"])
            if "cmr" in effective.excluded_safety and "cmr_1" in hazard["flags"]:
                add("safety:cmr",
                    f"第 {index} 步用到致癌、致突变或生殖毒性物质 {hazard['name']}（{statements_behind(hazard, 'cmr_1')}）",
                    step_index=index, substance=hazard["name"])
            if "controlled" in effective.excluded_safety and "controlled_strict" in hazard["flags"]:
                # 列出它在哪些清单上属于严格管制，形如“CN 第二类、US List I”；集合去重，排序后顺序固定
                lists = "、".join(sorted({f"{c['jurisdiction']} {c['category']}" for c in hazard["controlled"]
                                         if c["severity"] == "strict"}))
                add("safety:controlled", f"第 {index} 步用到管制化学品 {hazard['name']}（{lists}）",
                    step_index=index, substance=hazard["name"])

        # ---- 用户排除的物质和试剂类别（A13、A14）：看这一步的反应物和试剂
        for molecule in _step_substances(result, reaction):
            name = molecule.get("name") or molecule["formula"]
            if molecule["key14"] in effective.excluded_substances:
                add(f"substance:{molecule['key14']}", f"第 {index} 步用到你排除的试剂 {name}", step_index=index)
            for class_key in effective.excluded_reagent_classes:
                if reference.reagent_class_matches(class_key, molecule["smiles"]):
                    label = reference.label_for("reagent_class", class_key)
                    add(f"reagent:{class_key}", f"第 {index} 步用到{label}：{name}", step_index=index)

    # ---- 原料来源（A8）：只看这条路线的起始原料（叶子），不看试剂；ubiquitous 的（水）不用准备
    leaves = [molecules[key] for key in route["leaf_keys"]]
    statuses = {m["key14"]: (m.get("availability") or {}) for m in leaves}
    needed = [m for m in leaves if not statuses[m["key14"]].get("ubiquitous")]
    if effective.stock_source == "inventory_only":
        # “仅用库存”：只要有一个起始原料不在库存里，就排除
        missing = [m for m in needed if statuses[m["key14"]].get("status") != "inventory"]
        if missing:
            names = "、".join(m.get("name") or m["formula"] for m in missing[:5])
            add("stock", f"原料不在库存里：{names}")
    else:
        # 另外两档：库存里没有可以买，只有买不到的才排除
        missing = [m for m in needed if statuses[m["key14"]].get("status") == "unavailable"]
        if missing:
            names = "、".join(m.get("name") or m["formula"] for m in missing[:5])
            add("stock", f"原料买不到：{names}")

    # ---- 指定的起始原料或骨架（A5）
    for material in effective.starting_materials:
        wanted = chem.key14_of(material.smiles)
        # parse_query()：把用户给的结构读成子结构查询（先按 SMILES 读，读不了再按 SMARTS 读）
        pattern = chem.parse_query(material.smiles)
        if material.mode == "must_use":
            # 必须使用：这个结构本身要是起始原料之一（按 key14 比，不分立体异构）
            satisfied = wanted in route["leaf_keys"]
        else:
            # 起始骨架：至少有一个起始原料含有这个子结构
            satisfied = any(pattern is not None and chem.has_substructure(m["smiles"], pattern) for m in leaves)
        if not satisfied:
            name = reference.compounds.name_for(material.smiles) or material.smiles
            what = "没有用到指定的原料" if material.mode == "must_use" else "不是从指定的骨架出发"
            add(f"start:{wanted}", f"{what}：{name}")

    # ---- 用户在路线树上右键排除的（B11）。这些原因的 user 是 True，编号是 user:<排除记录的 id>
    for exclusion in user_exclusions:
        if exclusion["type"] == "branch" and exclusion["step_key"] in route["step_keys"]:
            # 排除分支：这条路线里有被排除的那一步
            index = route["step_keys"].index(exclusion["step_key"]) + 1
            reasons.append({"constraint": f"user:{exclusion['id']}", "label": f"你排除了这个分支：第 {index} 步 {exclusion['name']}",
                            "user": True, "step_index": index})
        elif exclusion["type"] == "substance":
            # 排除试剂：这条路线有任何一步把它当反应物或试剂
            used = any(exclusion["key14"] in (*r["reactants"], *r["reagents"]) for _, r in steps)
            if used:
                reasons.append({"constraint": f"user:{exclusion['id']}", "label": f"你排除了试剂：{exclusion['name']}",
                                "user": True})
    return reasons


# ---------------------------------------------------------------- 排序


def rank_routes(routes: list[dict[str, Any]], complexity: str) -> list[dict[str, Any]]:
    """给没被排除的路线排名，把名次写回每条路线的 rank。

    规则：
        1. 按路线分从高到低；没有分数的排在最后；分数相同的保持原来的先后。
        2. 除“探索”档（explore）外，低置信度路线不排在高置信度路线前面：找到排在最后的那条高置信度路线，
           它前面的低置信度路线都挪到它后面（挪过去的几条之间先后不变）。中置信度的路线不动，
           本来就排在它后面的路线也不动。
    参数：
        routes      result["routes"]；每条要已经有 excluded、score、confidence。
        complexity  A7 档位：concise / balanced / explore。
    返回：
        排好名次的、没被排除的路线列表。
    副作用：
        给每条路线写 rank：1、2、3……；被排除的路线是 None。
    例子（tests/test_scoring.py）：
        低置信 90 分、中置信 85 分、高置信 80 分 → 排成 中、高、低；“探索”档是 低、中、高。
    谁在用：
        本文件的 score_result()。
    """
    # 排序的键取负数就是从高到低；没有分数的当作 −1 分，排在所有有分数的后面
    eligible = sorted((r for r in routes if not r["excluded"]), key=lambda r: -(r["score"] if r["score"] is not None else -1))
    if complexity != "explore":
        # 高置信度路线现在排在哪些位置
        high_positions = [i for i, r in enumerate(eligible) if r["confidence"] == "high"]
        if high_positions:
            last_high = high_positions[-1]
            # head：从第一名到最后一条高置信度路线为止的这一段
            head = eligible[: last_high + 1]
            demoted = [r for r in head if r["confidence"] == "low"]
            # 新顺序 = 这一段里不是低置信的 + 这一段里低置信的 + 后面原样不动的
            eligible = [r for r in head if r["confidence"] != "low"] + demoted + eligible[last_high + 1 :]
    for position, route in enumerate(eligible, 1):
        route["rank"] = position
    for route in routes:
        if route["excluded"]:
            route["rank"] = None
    return eligible


def score_result(
    result: dict[str, Any],
    effective: EffectiveConstraints,
    reference: ReferenceData,
    *,
    user_exclusions: Iterable[dict[str, Any]] = (),
    relaxed: Iterable[str] = (),
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """流水线第 7 阶段，也是 B6、B11、B12 之后重新过滤和排序时调用的入口。直接改写 result。

    做什么（对 result["routes"] 里的每条路线）：
        1. evaluate_route()：算步骤分、路线分、置信度、最薄弱环节、汇总条，写回 route；
        2. exclusion_reasons()：列出它违反的硬约束；
        3. 去掉这次放宽的约束以后还有剩下的原因，这条路线就是“被排除”。
       最后 rank_routes() 给没被排除的路线排名。
    参数：
        result           分析结果（Analysis.result 那个字典），要已经跑完流水线第 6 阶段。
        effective        合并后的约束（constraints.merge_constraints 的返回值）。
        reference        参考数据（权重、阈值、词表都在里面）。
        user_exclusions  用户在路线树上右键排除的分支和试剂（Analysis.user_exclusions），B11。
        relaxed          这次临时放宽的约束编号（exclusion_reasons() 里的 constraint），B6。
        weights          六个维度的权重；不给（或给空的）就用 data/scoring.toml 的 [weights]。D1 换权重时传。
    写回 result 的内容：
        每条路线：evaluate_route() 写的那些字段，再加上
            exclusion_reasons  全部排除原因（包括已经放宽的）
            excluded           还有没被放宽的原因时为 True
            relaxed            有排除原因、但全都被放宽了时为 True（界面上标“放宽后”）
            rank               名次；被排除的是 None
        result["ranking"]   没被排除的路线 id，按名次排
        result["excluded"]  被排除的路线 id
        result["weights"]   这次用的权重
    返回：
        result 本身（同一个对象，不是副本）。
    副作用：
        只改传进来的 result，不写数据库。要不要保存由调用方决定。
    谁在用：
        pipeline.AnalysisRunner._score()        流水线第 7 阶段，算完由它存库
        pipeline.rescore()                      B11 排除 / 撤销排除、B12 标记为我有之后（api/analyses.py）：
                                                改的是存着的那份结果，接口随后提交保存
        api/analyses.py 的 relax_constraints()   B6：在结果的副本上算，不保存
        api/reports.py 的 _weighted_result()     D1 / D4：换了权重时在副本上算，不保存
    """
    weights = weights or reference.scoring["weights"]
    # relaxed 转成 set，方便判断“在不在里面”。user_exclusions 的类型是 Iterable（有的只能遍历一次），
    # 先转成 list，因为下面每条路线都要把它完整地用一遍
    relaxed = set(relaxed)
    exclusions = list(user_exclusions)
    for route in result["routes"]:
        evaluate_route(result, route, effective, reference, weights)
        reasons = exclusion_reasons(result, route, effective, reference, exclusions)
        # active：没有被这次放宽掉的原因
        active = [r for r in reasons if r["constraint"] not in relaxed]
        route["exclusion_reasons"] = reasons
        route["excluded"] = bool(active)
        # “放宽后”：本来会被排除，放宽了某些条件才进入候选
        route["relaxed"] = bool(reasons) and not active
    ranked = rank_routes(result["routes"], effective.complexity)
    result["ranking"] = [route["id"] for route in ranked]
    result["excluded"] = [route["id"] for route in result["routes"] if route["excluded"]]
    result["weights"] = dict(weights)
    return result


def compare_routes(result: dict[str, Any], route_ids: list[str], scoring: dict[str, Any]) -> dict[str, Any]:
    """R9 对比页：指标表、雷达图数据、差异步骤（D5：共同步骤灰显）。

    参数：
        result     已经评过分的分析结果（每条路线要有 summary、dims_avg、steps 等字段）。
        route_ids  要对比的路线 id；不存在的 id 直接跳过。
        scoring    reference.scoring；这里只用 [dimension_labels]。
    返回：
        {"dimension_labels": 六个维度的中文名, "routes": [...]}，routes 按 route_ids 的顺序，每项：
            route_id、name、score        路线的 id、名字、路线分
            n_steps                      步数
            literature_coverage          文献覆盖（百分数，可能是 None）
            inventory_coverage           库存覆盖（百分数，可能是 None）
            special_operations           特殊操作，拼成一句话，例如“无水无氧操作 1 步”；没有时是“无”
            green                        绿色化学这一维的各步平均分
            material_cost                原料成本的文字，现在固定是“待询价”
            confidence                   置信度的中文名（高 / 中 / 低）
            radar                        六个维度的各步平均分（雷达图用），就是 route["dims_avg"]
            steps                        [{"index", "key", "name", "shared"}]；shared 为 True 表示这一步
                                         在所有被对比的路线里都有（按步骤编号比），前端据此把共同步骤灰显
        一条路线都没找到时 routes 是空列表。
    副作用：没有。
    谁在用：
        api/reports.py 的 compare()（POST /api/analyses/{id}/compare，D1）和
        export_comparison()（POST /api/analyses/{id}/compare/export，D4；返回值再交给 m13_reports 出表格）。
    """
    by_id = {route["id"]: route for route in result["routes"]}
    chosen = [by_id[route_id] for route_id in route_ids if route_id in by_id]
    # 共同步骤 = 各条路线步骤编号集合的交集。set.intersection(*集合们) 一次求出几个集合共有的元素
    shared = set.intersection(*(set(route["step_keys"]) for route in chosen)) if chosen else set()
    rows = []
    for route in chosen:
        summary = route["summary"]
        rows.append(
            {
                "route_id": route["id"],
                "name": route["name"],
                "score": route["score"],
                "n_steps": summary["n_steps"],
                "literature_coverage": summary["literature_coverage"],
                "inventory_coverage": summary["inventory_coverage"],
                "special_operations": "、".join(op["text"] for op in summary["special_operations"]) or "无",
                "green": route["dims_avg"]["green"],
                "material_cost": summary["material_cost_label"],
                "confidence": route["confidence_label"],
                "radar": route["dims_avg"],
                "steps": [
                    {"index": s["index"], "key": s["key"], "name": s["name"], "shared": s["key"] in shared}
                    for s in route["steps"]
                ],
            }
        )
    return {"dimension_labels": scoring["dimension_labels"], "routes": rows}
