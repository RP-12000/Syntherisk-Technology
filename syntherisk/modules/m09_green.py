"""M9 绿色化学：原子经济性和溶剂评级。

溶剂评级取自 CHEM21 溶剂选择指南（Prat 等，Green Chem. 2016, 18, 288），
分四档：recommended / problematic / hazardous / highly_hazardous，记在 data/compounds.csv。
E 因子需要实际投料量和废弃物量，现在没有这些数据，所以不算。

对应关系
    PPT 模块：M9 绿色化学。
    流水线：第 6 阶段“条件、产率、绿色化学”。syntherisk/pipeline.py 的 AnalysisRunner._conditions()
    对每一步调用一次 evaluate_green()，把返回的字典存进 reaction["green"]。
    界面：没有专门的按钮。结果出现在证据卡（R6）的绿色化学一栏和六个维度里的“绿色化学”；
    结果页排序“最绿色”（B1，GET /api/analyses/{id}/result?sort=green）和对比页的雷达图（R9）
    用的是一条路线各步绿色化学分的平均值；报告（M13）的证据卡表格里也有“绿色化学”一行。

数据从哪来、到哪去
    输入：这一步的反应物、产物、试剂的 SMILES（流水线从 result["molecules"] 里取）；
          data/scoring.toml 的 [green]（两项的权重、四档溶剂各多少分）；
          data/compounds.csv 的 roles 和 solvent_class 两列（哪些物质是溶剂、属于哪一档）。
          后两样在服务启动时读进 ReferenceData（syntherisk/reference.py）。
    输出：reaction["green"]。之后 m10_scoring.base_dimensions() 取它的 score 和 basis 当作“绿色化学”
          这一维（权重见 scoring.toml 的 [weights]，现在是 0.05）；cards.build_card() 把整个字典放进证据卡。
    本文件不读写数据库，不联网，不改传进来的对象。

本文件里有什么
    SOLVENT_CLASS_LABELS  四档溶剂评级的中文名
    _WORST_FIRST          四档从差到好的顺序
    atom_economy()        算一步反应的原子经济性
    evaluate_green()      把原子经济性和溶剂评级合成绿色化学分，连同依据一起返回
"""

from __future__ import annotations

from typing import Any

from syntherisk.modules import m01_structure as chem
from syntherisk.reference import ReferenceData

# CHEM21 四档评级的中文名。键是 data/compounds.csv 里 solvent_class 一列的取值。
# 用在证据卡的 green.solvents[].class_label 和依据文字（basis）里。
SOLVENT_CLASS_LABELS = {
    "recommended": "推荐",
    "problematic": "有问题",
    "hazardous": "有害",
    "highly_hazardous": "高度有害",
}
# 四档从最差到最好的顺序。一步用到几种溶剂时，按其中最差的一种计分。
_WORST_FIRST = ["highly_hazardous", "hazardous", "problematic", "recommended"]


def atom_economy(reactants: list[str], product: str) -> float | None:
    """产物分子量 / 反应物分子量之和。反应物不全时会超过 100%，这时返回 None。

    原子经济性衡量“投进去的原子有多少留在了产物里”，越高越好。
    这里只算反应物，不算试剂、溶剂和催化剂。

    参数：
        reactants  这一步各个反应物的 SMILES。
        product    产物的 SMILES。
    返回：
        0–100 的百分数，保留一位小数。下面几种情况返回 None：
        产物或任何一个反应物的 SMILES 读不出来；反应物列表是空的；反应物分子量之和不大于 0；
        算出来超过 100.5%（说明反应物没给全，比如逆合成引擎漏掉了一个片段）。
    例子：
        水杨酸（138.12）+ 乙酸酐（102.09）→ 阿司匹林（180.16）：180.16 ÷ 240.21 = 75.0%。
    副作用：没有。
    谁在用：
        本文件的 evaluate_green()；tests/test_structure.py 用上面这个例子验证它。
    """
    # mol_record() 解析 SMILES，返回一条带分子量的记录（mol_weight：RDKit 算的平均分子量，保留两位小数）；
    # SMILES 读不出来时返回 None
    product_record = chem.mol_record(product)
    records = [chem.mol_record(s) for s in reactants]
    # 任何一个分子读不出来，或者根本没有反应物，就没法算
    if product_record is None or not records or any(r is None for r in records):
        return None
    total = sum(r.mol_weight for r in records)
    if total <= 0:
        return None
    value = 100 * product_record.mol_weight / total
    # 分子量四舍五入会让加成反应略超 100%，容许半个点的误差
    return min(100.0, round(value, 1)) if value <= 100.5 else None


def evaluate_green(reference: ReferenceData, reactants: list[str], product: str, reagents: list[str]) -> dict[str, Any]:
    """给一步反应算绿色化学这一项：原子经济性加溶剂评级，合成一个 0–100 的分数。

    参数：
        reference  参考数据。这里用到 reference.scoring["green"]（data/scoring.toml 的 [green]）
                   和 reference.compounds（data/compounds.csv，用来认出哪些试剂是溶剂、属于哪一档）。
        reactants  反应物的 SMILES 列表。
        product    产物的 SMILES。
        reagents   试剂的 SMILES 列表（溶剂、催化剂都在这里面）。不知道这一步用什么试剂时是空列表。
    返回（字典，流水线原样存进 reaction["green"]）：
        atom_economy   原子经济性，0–100；算不出来是 None
        solvents       认出来的溶剂，每项是 {"name": 显示名, "class": 评级的英文键, "class_label": 评级的中文名}
        solvent_class  这些溶剂里最差的一档（英文键）；一个溶剂都没认出来时是 None
        score          绿色化学分，0–100，保留一位小数；两项都没有数据时是 None（M10 就不计这一维）
        basis          给人看的依据，格式是“原子经济性 75%；溶剂 名字1、名字2（最差一档的中文名）”，
                       哪一项没有数据就不写哪一项；两项都没有时是“反应物或溶剂信息不全，无法评估”
    算法：
        score = (0.6 × 原子经济性 + 0.4 × 最差溶剂那一档的分数) ÷ 有数据的那几项的权重之和。
        0.6 和 0.4 是 [green] 的 atom_economy_weight 和 solvent_weight；
        各档的分数是 [green.solvent_scores]：推荐 100、有问题 60、有害 30、高度有害 0。
        只有一项有数据时，分数就等于那一项，而不是把缺的那一项当成 0 分。
    注意：
        只在 reagents 里找溶剂，不看反应物；内置试剂表里没有的溶剂认不出来，不参与评分。
    副作用：没有。
    会抛的错：compounds.csv 里的 solvent_class 如果不是那四档之一，这里会抛 KeyError；
        tests/test_reference.py 检查了表里只有这四档。
    谁在用：
        syntherisk/pipeline.py 的 AnalysisRunner._conditions()（流水线第 6 阶段，每一步调一次）。
    """
    # [green] 一节：atom_economy_weight、solvent_weight 两个权重和 solvent_scores（每一档溶剂多少分）
    config = reference.scoring["green"]
    economy = atom_economy(reactants, product)

    # 在内置试剂表里按结构找每个试剂（find_by_smiles 比的是 InChIKey 的前 14 位）。
    # 找到了、角色（roles）里有 solvent、并且填了评级，才算认出一个溶剂
    solvents = []
    for smiles in reagents:
        compound = reference.compounds.find_by_smiles(smiles)
        if compound and "solvent" in compound.roles and compound.solvent_class:
            solvents.append({"name": compound.display_name, "class": compound.solvent_class,
                             "class_label": SOLVENT_CLASS_LABELS[compound.solvent_class]})
    # _WORST_FIRST.index 给每一档一个序号，越差序号越小，所以 min 取到的是最差的一档；
    # 没有溶剂时走 default，得到 None
    worst = min((s["class"] for s in solvents), key=_WORST_FIRST.index, default=None)

    # (权重, 分数) 的对子，只放有数据的项，再做加权平均：Σ(权重 × 分数) ÷ Σ权重
    parts: list[tuple[float, float]] = []
    if economy is not None:
        parts.append((float(config["atom_economy_weight"]), economy))
    if worst is not None:
        parts.append((float(config["solvent_weight"]), float(config["solvent_scores"][worst])))
    score = round(sum(w * v for w, v in parts) / sum(w for w, _ in parts), 1) if parts else None

    # 依据文字：证据卡和报告里直接显示。{economy:g} 去掉多余的小数位（75.0 显示成 75）
    basis = []
    if economy is not None:
        basis.append(f"原子经济性 {economy:g}%")
    if worst is not None:
        # 列出全部认出来的溶剂的名字，括号里写最差那一档的中文名
        names = "、".join(s["name"] for s in solvents)
        basis.append(f"溶剂 {names}（{SOLVENT_CLASS_LABELS[worst]}）")
    return {
        "atom_economy": economy,
        "solvents": solvents,
        "solvent_class": worst,
        "score": score,
        "basis": "；".join(basis) or "反应物或溶剂信息不全，无法评估",
    }
