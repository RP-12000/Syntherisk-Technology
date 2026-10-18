"""把逆合成引擎返回的路线树整理成后端自己的结构。

整理后的结果（存在 Analysis.result 里）分三张表，互相用编号引用，避免同一步在多条路线里存多份：

    molecules   key14 -> 分子（目标、中间体、原料、试剂）
    reactions   步骤编号 -> 一步反应及其评估结果（文献、条件、毒性、产率、绿色化学）
    routes      路线：路线树、按实验顺序排好的步骤编号、叶子原料

步骤的实验顺序和界面一致：第 1 步是最先做的那一步，最后一步得到目标分子。

在整个流程里的位置
    属于 PPT 的 M3（逆合成搜索）的收尾。逆合成引擎（AiZynthFinder，或者内置的演示路线，见
    modules/m03_retrosynthesis.py）只负责搜索，交回来的是 AiZynthFinder 格式的路线树；
    这个文件把它变成后面所有模块（M4–M14）和接口都在读写的那份结果。
      - 流水线第 2 阶段（pipeline.py 的 AnalysisRunner._search）调用 build_skeleton()，返回的字典
        存进 Analysis.result。这时还没有分数，但前端已经可以“先看初步结果”
        （按钮 A18，GET /api/analyses/{id}/preliminary）。
      - 结果页（GET /api/analyses/{id}/result，以及放宽条件、排除分支、标记为我有之后返回的结果）
        每次都调用 build_graph()，给 R5 路线树画布上的分叉标签（按钮 B8）用。
      - 生成报告（M13）时用 iter_reaction_nodes()、iter_mol_nodes() 遍历路线树。

依赖
    modules/m01_structure.py（解析结构、算反应编号和反应中心）、modules/m07_conditions.py 的
    name_reaction（按官能团变化猜反应名）、reference.py（内置试剂表里的名字）。
    这个文件不查数据库、不联网、不写文件，只在内存里整理字典。

Analysis.result 的完整形状（带 ★ 的键是本文件写的，其余是后面的阶段补上去的）

    schema ★          结果结构的版本号，现在是 1
    stage ★           结果做到了哪一步："preliminary" → "scored"（第 7 阶段评完分）
                      → "final"（第 8 阶段写完说明，或者一条路线都没找到）
    engine ★          {name, version, stats}：这次用的逆合成引擎
    target ★          目标分子，内容和 molecules 里它那一条相同
    molecules ★       {key14: 分子}。每个键的含义见 _Builder.molecule
    reactions ★       {步骤编号: 一步反应}。每个键的含义见 _Builder._reaction
    routes ★          [路线, ...]。每个键的含义见 build_skeleton
    warnings ★        [给用户看的提示, ...]
    constraint_tags   这次分析生效的约束标签（第 2 阶段，pipeline.py）
    ranking           可选路线的编号，按名次排（第 7 阶段，m10_scoring.score_result）
    excluded          被排除的路线的编号（同上）
    weights           评分用的六个权重（同上）
    scoring_version   评分配置 data/scoring.toml 的版本（第 7 阶段，pipeline.py）

    后面的阶段往三张表里补的键（含义见写它们的模块）：
      分子  availability（第 3 阶段，M4）；没有名字的分子，第 5 阶段可能补上 PubChem 上的名字
      反应  evidence、yield（第 4 阶段，M5）；hazards（第 5 阶段，M6）；
            conditions、green、dimensions（第 6 阶段，M7、M9、M10）；experiment（回填实验结果时，M14）。
            第 4 阶段借用先例的试剂时，还会改写 reagents、reaction_smiles，并加上 reagents_source
      路线  steps、availability_details、score、dims_avg、confidence、confidence_label、
            confidence_reason、min_stars、weakest、summary、exclusion_reasons、excluded、relaxed、rank
            （第 7 阶段，M10）；explanation、explanations（第 8 阶段，M11）

    前端拿到的不是这份原样的结果：接口先用 api/views.py 把它整理成界面各个位置要的样子，
    证据卡由 cards.py 现拼。

本文件里有什么
    UNNAMED_CONVERSION、UNNAMED_COUPLING  认不出反应类型时用的两个通用名字
    RouteBuildError        一条路线树整理不了时抛的错误
    _is_reaction           引擎路线树里的一个节点是不是反应节点
    _clean_classification  去掉 AiZynthFinder 反应类别前面的编号
    _Builder               把引擎的路线树转成后端的结构，同时登记分子表和反应表
        molecule           登记一个分子
        convert            转换一个分子节点和它下面的整棵子树
        _reaction          登记一步反应
    iter_reaction_nodes    按实验顺序遍历路线树里的反应节点
    iter_mol_nodes         遍历路线树里的分子节点
    leaf_keys              路线树的叶子（起始原料）
    build_skeleton         入口一：搜索结果 → 还没评分的分析结果
    build_graph            入口二：分析结果 → 画布用的分叉视图
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterator

# chem：后端里和化学结构有关的计算都在 m01_structure 里，各个文件都给它起这个别名
from syntherisk import i18n
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m03_retrosynthesis import SearchResult
from syntherisk.modules.m07_conditions import name_reaction
from syntherisk.reference import ReferenceData


# 认不出反应类型时的名字：至少说清楚是一个分子自己变，还是两个片段接起来
# UNNAMED_CONVERSION 用在只有一个反应物的步骤上，UNNAMED_COUPLING 用在有两个或更多反应物的步骤上
# （见 _Builder._reaction）。tests/test_reference.py 用它们检查演示路线的每一步都有正经名字
# i18n.tr 返回的就是中文，同时登记了英文（请求带 ?lang=en 时换成英文，见 syntherisk/i18n.py）
UNNAMED_CONVERSION = i18n.tr("官能团转化", "Functional group interconversion")
UNNAMED_COUPLING = i18n.tr("片段连接", "Fragment coupling")


class RouteBuildError(ValueError):
    """引擎给的一条路线树整理不了：有结构读不出来、某一步没有反应物，或者算不出反应编号。

    只在本文件里用。_Builder 抛出，build_skeleton 接住后跳过那一条路线、在 warnings 里记一句，
    其余路线照常处理，不会让整次分析失败。
    例外：目标分子自己读不出来时没有人接，分析会失败；不过提交分析时接口已经校验过目标结构。
    """

    pass


# ---------------------------------------------------------------- 引擎的路线树 → 后端的结构


def _is_reaction(node: dict[str, Any]) -> bool:
    """引擎路线树里的这个节点是不是“反应”节点（不是的话就是分子节点）。

    引擎的路线树是分子节点和反应节点一层隔一层：分子节点的 children 里是得到它的那一步反应，
    反应节点的 children 里是这一步的各个反应物。
    AiZynthFinder 输出的反应节点同时带 "type": "reaction" 和 "is_reaction": true，
    内置演示路线（data/demo_routes/*.json）只写了前一个，所以两个里有一个就算。

    参数：
        node  引擎路线树里的一个节点（字典）。
    谁在用：
        本文件的 _Builder.convert。
    """
    return node.get("type") == "reaction" or bool(node.get("is_reaction"))


def _clean_classification(text: str | None) -> str | None:
    """AiZynthFinder 的反应类别形如 “2.1.2 Carboxylic acid + amine condensation”，去掉前面的编号。

    参数：
        text  反应节点 metadata 里的 classification。演示路线没有这一项，传进来的是 None。
    返回：
        去掉编号以后的类别名（英文），比如 “1.7.9 O-Acylation” 得到 “O-Acylation”。
        没给、去掉编号后是空的、或者类别是 “0.0 Unrecognized”（这一步没有归到任何已知类别）时返回 None，
        这时反应名改用别的办法取，见 _Builder._reaction。
    谁在用：
        本文件的 _Builder._reaction。
    """
    if not text:
        return None
    # 在第一个空格处切成两段：["2.1.2", "Carboxylic acid + amine condensation"]
    parts = text.strip().split(" ", 1)
    # 第一段去掉小数点以后全是数字，才认为它是编号并丢掉；否则整段原样保留
    if parts[0].replace(".", "").isdigit():
        text = parts[1] if len(parts) > 1 else ""
    # “Unrecognized” 不能当名字显示，按没有类别处理
    return None if not text or "unrecognized" in text.lower() else text.strip()


class _Builder:
    """把引擎的路线树一条一条地转成后端的结构，边转边往分子表、反应表里登记。

    一次分析只建一个 _Builder，所有路线共用：同一个分子、同一步反应在几条路线里出现时只登记一次，
    路线树里只留编号（分子的 key14、反应的 step_key）。

    属性：
        reference  参考数据；这里只用内置试剂表给分子取名字。
        molecules  分子表 {key14: 分子字典}，每个键的含义见 molecule()。
        reactions  反应表 {step_key: 反应字典}，每个键的含义见 _reaction()。
    谁在用：
        本文件的 build_skeleton。
    """

    def __init__(self, reference: ReferenceData):
        """从两张空表开始。reference 是启动时读进内存的参考数据（syntherisk/reference.py）。"""
        self.reference = reference
        # 分子表：{key14: 分子字典}
        self.molecules: dict[str, dict[str, Any]] = {}
        # 反应表：{step_key: 反应字典}
        self.reactions: dict[str, dict[str, Any]] = {}

    def molecule(self, smiles: str, name: str | None = None, in_stock: bool | None = None) -> dict[str, Any]:
        """登记一个分子（已经登记过就直接取出来），返回它在分子表里的那个字典。

        参数：
            smiles    分子的 SMILES，任何合法写法都行，这里会转成规范写法。
            name      引擎或演示数据给的名字，可以没有。内置试剂表里有名字时用表里的。
            in_stock  逆合成引擎自己的在售清单里有没有它：True / False；
                      引擎没说就是 None（演示路线里的分子、试剂、目标分子）。
        返回：
            分子表里的字典（不是拷贝，改它就是改表）。七个键的含义见函数体里的注释。
        副作用：
            第一次遇到的分子加进 self.molecules。已经登记过的分子，这次 in_stock 为真而原来不是时，
            把 engine_in_stock 改成 True（几处出现里只要有一处说有货，就算有货），其余内容不动。
        抛出：
            RouteBuildError  SMILES 读不出来。
        谁在用：
            本文件的 convert()、_reaction()（登记试剂）、build_skeleton（登记目标分子）。
        """
        # mol_record 用 RDKit 解析 SMILES，给出规范 SMILES、InChIKey、分子式、分子量；读不出来返回 None
        record = chem.mol_record(smiles)
        if record is None:
            raise RouteBuildError(i18n.tr("路线里有读不出来的结构：{smiles}",
                                          "The route contains a structure that could not be read: {smiles}", smiles=smiles))
        # 用 key14 认分子：同一个分子的不同 SMILES 写法、不同的立体异构体都落到同一条上，
        # 表里留的是先登记的那个写法
        entry = self.molecules.get(record.key14)
        if entry is None:
            entry = {
                # 分子编号：InChIKey 的前 14 位，也是它在分子表里的键。只看原子怎么连，不分立体异构
                "key14": record.key14,
                # RDKit 的规范 SMILES（同一个分子不管原来怎么写，转出来都一样）
                "smiles": record.smiles,
                # 完整的 InChIKey（27 个字符）。个别生成不了 InChI 的金属有机物，
                # 用 SMILES 的哈希顶替，格式相同、以 X 开头（见 m01_structure.inchikey_of）
                "inchikey": record.inchikey,
                # 显示用的名字：内置试剂表（data/compounds.csv）里有就用表里的，没有才用引擎或
                # 演示数据给的；都没有是 None，后端要显示名字的地方这时改用分子式。
                # 第 5 阶段查到 PubChem 上的名字时会补上
                "name": self.reference.compounds.name_for(record.smiles) or name,
                # 分子式，比如 C13H18O2
                "formula": record.formula,
                # 分子量（g/mol），保留两位小数
                "mol_weight": record.mol_weight,
                # 逆合成引擎自己的在售清单里有没有它：True / False / None（引擎没说）。
                # 第 3 阶段 M4 判断可得性时会参考：我们自己的目录里没有、但引擎说有货的，也算可购买
                "engine_in_stock": in_stock,
            }
            self.molecules[record.key14] = entry
        # 以前登记过：只在“这一处说有货、而原来记的不是有货”时把标记改成 True
        elif in_stock and not entry["engine_in_stock"]:
            entry["engine_in_stock"] = True
        return entry

    def convert(self, node: dict[str, Any]) -> dict[str, Any]:
        """把引擎路线树的一个分子节点（连同它下面的整棵子树）转成后端的路线树节点。递归函数。

        参数：
            node  引擎给的分子节点。用到的键：smiles（必有）、name、in_stock、children。
                  children 里是得到这个分子的反应节点（反应节点的 metadata 见 _reaction()），
                  反应节点的 children 又是各个反应物的分子节点，如此一层层往下。
        返回：
            后端的分子节点：{"type": "mol", "key14": 分子编号, "children": [...]}。
            叶子（起始原料）的 children 是空列表；否则里面恰好有一个反应节点：
            {"type": "reaction", "step_key": 步骤编号, "children": [各反应物的分子节点]}。
            树里只放编号，分子和反应的内容分别到分子表、反应表里取。
            （build_skeleton 之后还会给每个反应节点加上 "step_index"：这一步在这条路线里是第几步。）
        副作用：
            途中遇到的分子和反应都登记进 self.molecules、self.reactions。
            一棵树转到一半出错时，已经登记的那些不会撤回。
        抛出：
            RouteBuildError  有结构读不出来、有一步没有反应物，或者算不出反应编号。
            KeyError         节点里没有 smiles。
        谁在用：
            本文件的 build_skeleton（对每条路线的根节点调用一次），以及它自己（递归）。
        """
        # 先登记这个分子，拿到它的编号；out 是要返回的后端节点，先按叶子（没有 children）建好
        entry = self.molecule(node["smiles"], node.get("name"), node.get("in_stock"))
        out = {"type": "mol", "key14": entry["key14"], "children": []}
        # 这个分子下面挂着的反应节点。一个都没有，就是叶子（起始原料），到此为止
        reaction_nodes = [child for child in node.get("children") or [] if _is_reaction(child)]
        if not reaction_nodes:
            return out
        # 一条路线里一个分子只有一种做法（AiZynthFinder 的路线树里分子节点下面最多一个反应节点），取第一个
        reaction_node = reaction_nodes[0]
        # 先把各个反应物的子树转好（递归），再登记这一步反应
        children = [self.convert(child) for child in reaction_node.get("children") or [] if not _is_reaction(child)]
        if not children:
            raise RouteBuildError(i18n.tr("路线里有一步没有反应物", "A step in the route has no reactants"))
        step_key = self._reaction(entry, children, reaction_node.get("metadata") or {})
        out["children"] = [{"type": "reaction", "step_key": step_key, "children": children}]
        return out

    def _reaction(self, product: dict[str, Any], children: list[dict[str, Any]], meta: dict[str, Any]) -> str:
        """登记一步反应（已经登记过就不重复登记），返回它的步骤编号 step_key。

        参数：
            product   产物在分子表里的字典。
            children  已经转换好的各个反应物节点（convert 的返回值），各带一个 key14。
            meta      引擎路线树里这个反应节点的 metadata。两种来源给的东西不一样：
                      AiZynthFinder 给 classification、template_hash、policy_probability、
                      policy_probability_rank、policy_name、library_occurence、mapped_reaction_smiles；
                      内置演示路线给 name、reagents、conditions。
        返回：
            step_key：16 位十六进制字符串，反应表的键。
        副作用：
            往 self.reactions 里加一条（十一个键的含义见函数体里的注释）；
            meta 里列出的试剂也会登记进 self.molecules。
            同一步在别的路线里已经登记过时什么都不加，表里留的是第一次登记时的名字、模型信息和条件说明。
        抛出：
            RouteBuildError  试剂的结构读不出来，或者算不出反应编号。
        谁在用：
            本文件的 convert()。
        """
        reactant_keys = [child["key14"] for child in children]
        reactant_smiles = [self.molecules[key]["smiles"] for key in reactant_keys]
        # 试剂只有演示路线会给（SMILES 列表）；AiZynthFinder 只给反应物和产物，这里就是空列表
        reagent_keys = [self.molecule(smiles)["key14"] for smiles in meta.get("reagents") or []]
        # “同一反应”的编号：同一批反应物（盐只看最大的那个片段，不分先后）得到同一个产物，编号就相同。
        # 先例库的 reaction_key 一列是同一个函数算的；M5 找“文献直接报道”（E1）时比的就是这个编号
        rxn_key = chem.reaction_key(reactant_smiles, product["smiles"])
        if rxn_key is None:
            raise RouteBuildError(i18n.tr("路线里有一步算不出反应编号", "A reaction key could not be computed for a step in the route"))
        # 同样的反应物和产物、试剂不同（比如 AlCl3 和 HF 催化的傅-克反应），算两步不同的做法
        signature = rxn_key + "|" + ",".join(sorted(reagent_keys))
        # 步骤编号取 SHA-1 的前 16 位十六进制字符。它只和反应物、产物、试剂有关，
        # 所以同一步出现在几条路线里编号相同，只登记一次、后面也只评估一次
        step_key = hashlib.sha1(signature.encode("ascii")).hexdigest()[:16]
        if step_key in self.reactions:
            return step_key

        classification = _clean_classification(meta.get("classification"))
        probability = meta.get("policy_probability")
        mapped = meta.get("mapped_reaction_smiles")  # 引擎按“产物>>反应物”给的、带原子映射的这一步
        # 这三项引擎信息只要有一项，就认为这一步是模型提出来的；演示路线一项都没有，model 就是 None
        has_model = any(k in meta for k in ("template_hash", "policy_probability", "library_occurence"))
        self.reactions[step_key] = {
            # 步骤编号，和这条记录在反应表里的键相同。路线的 step_keys、路线树的反应节点、
            # 排除分支、实验回填（ExperimentRecord.step_key）都用它指这一步
            "key": step_key,
            # “同一反应”的编号（40 位十六进制，不含试剂）。存着备查，后面的模块没有读它（M5 自己重新算）
            "rxn_key": rxn_key,
            # 显示用的反应名，按这个先后取：演示数据写的名字 → AiZynthFinder 的反应类别 →
            # 按官能团变化猜的名字（m07_conditions.name_reaction）→ 都没有就用文件开头那两个通用名字
            "name": meta.get("name") or classification or name_reaction(reactant_smiles, product["smiles"])
            or (UNNAMED_COUPLING if len(reactant_keys) > 1 else UNNAMED_CONVERSION),
            # AiZynthFinder 给的反应类别（去掉了编号，英文）；演示路线和没有归类的步骤是 None。
            # 除了上面取名，后面的模块目前没有读它
            "classification": classification,
            # 产物的 key14
            "product": product["key14"],
            # 各个反应物的 key14，顺序和引擎给的一样
            "reactants": reactant_keys,
            # 试剂、溶剂、催化剂的 key14。演示路线才有；AiZynthFinder 的步骤先是空列表，
            # 第 4 阶段找到“文献直接报道”或“高相似先例”时，会借用最好的那条先例的试剂填进来
            "reagents": reagent_keys,
            # “反应物>试剂>产物”形式的反应 SMILES（不带原子映射），每一组里用 . 隔开
            "reaction_smiles": chem.build_reaction_smiles(
                reactant_smiles, product["smiles"], [self.molecules[k]["smiles"] for k in reagent_keys]
            ),
            # 这一步到底改了哪些键（有原子映射才算得出来）。M5 拿它和先例比，判断是不是同类反应
            # 形如 “F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1”，写法见 m01_structure.reaction_center；演示路线是 None
            "center": chem.retro_reaction_center(mapped) if isinstance(mapped, str) and ">>" in mapped else None,
            # 逆合成模型自己给的依据：可以当“模型依据”，但不是文献证据
            "model": {
                # 这一步用的反应模板在引擎模板库里的编号
                "template_hash": meta.get("template_hash"),
                # 引擎模板库里记的这个模板的出现次数。AiZynthFinder 的字段名少一个 r
                # （library_occurence），两种拼法都认
                "library_occurrence": meta.get("library_occurence", meta.get("library_occurrence")),
                # 单步模型给这个模板的概率（0–1）。找不到先例（E4）时，M5 用它在 E4 的分数段里定文献分
                "policy_probability": float(probability) if probability is not None else None,
                # 这个模板在单步模型给这个分子提出的候选里排第几，0 是概率最高的
                "policy_rank": meta.get("policy_probability_rank"),
                # 提出这一步的单步模型的名字（公开模型是 "uspto"）
                "policy_name": meta.get("policy_name"),
            } if has_model else None,
            # 演示数据里写的条件说明（给人看的一句话）；AiZynthFinder 不给条件，是 None。
            # 第 6 阶段把它当作这一步的条件说明，来源标成 DEMO
            "given_conditions": meta.get("conditions"),
        }
        return step_key


# ---------------------------------------------------------------- 遍历整理好的路线树


def iter_reaction_nodes(tree: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """按实验顺序（先做的在前，得到目标分子的那一步在最后）遍历路线树里的反应。

    参数：
        tree  整理好的路线树的一个分子节点（通常是根，也就是目标分子），结构见 _Builder.convert。
    产出：
        反应节点 {"type": "reaction", "step_key": ..., "children": [...]}（build_skeleton 编过号以后
        还有 "step_index"）。给出的是树里的原对象，调用方往上面写东西就是直接改树。
    顺序：
        先走完一个反应的所有反应物下面的步骤，再轮到这个反应自己（后序遍历）。
        所以越靠近原料的步骤越先出来，根节点下面那一步（得到目标分子）最后出来。
    谁在用：
        本文件的 build_skeleton（给步骤编号）；modules/m13_reports.py 的 bill_of_materials（物料清单）。
    """
    # 分子节点的 children 里是得到它的那一步反应（最多一个）；叶子的 children 是空的，循环不执行
    for reaction in tree.get("children") or []:
        for child in reaction["children"]:
            # yield from：把下一层生成器给出的东西原样接着往外交
            yield from iter_reaction_nodes(child)
        yield reaction


def iter_mol_nodes(tree: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """遍历路线树里的全部分子节点：先给出当前节点，再依次进入它下面每个反应物的子树。

    参数：
        tree  整理好的路线树的一个分子节点。
    产出：
        分子节点 {"type": "mol", "key14": ..., "children": [...]}，第一个就是传进来的 tree 自己。
        同一个分子在树里出现几次就给出几次，要不重复的话调用方自己去重。
    谁在用：
        本文件的 leaf_keys；modules/m13_reports.py 的 export_structures_zip（导出 SDF 时列出路线上的分子）。
    """
    # 先交出当前这个分子节点，再沿着它下面的反应（最多一个）进入每个反应物的子树
    yield tree
    for reaction in tree.get("children") or []:
        for child in reaction["children"]:
            yield from iter_mol_nodes(child)


def leaf_keys(tree: dict[str, Any]) -> list[str]:
    """路线树的叶子，也就是这条路线的起始原料：返回它们的 key14，不重复，按在树里出现的先后。

    “叶子”只表示这条路线没有再往下拆它，不代表它一定买得到：能不能拿到由 M4 在第 3 阶段判断。
    试剂不在路线树里，所以也不在这个列表里。

    参数：
        tree  整理好的路线树的根节点（目标分子）。
    返回：
        key14 的列表；只有目标分子、一步都没拆的树，返回的就是目标分子自己的 key14。
    谁在用：
        本文件的 build_skeleton，结果存在每条路线的 leaf_keys 里。
    """
    keys: list[str] = []
    for node in iter_mol_nodes(tree):
        # children 是空的分子节点就是叶子。用列表而不用集合，是为了保留出现的先后
        if not node.get("children") and node["key14"] not in keys:
            keys.append(node["key14"])
    return keys


# ---------------------------------------------------------------- 对外的两个入口


def build_skeleton(search: SearchResult, target_smiles: str, reference: ReferenceData) -> dict[str, Any]:
    """流水线第 2 阶段结束时调用：得到还没评分的“初步路线树”（A18 先看初步结果）。

    参数：
        search         逆合成引擎的搜索结果（m03_retrosynthesis.SearchResult）。search.routes 的每一项是
                       {"tree": AiZynthFinder 格式的路线树, "score": 引擎自己的分数,
                        "solved": 是否拆到了在售原料, "name": 路线名}，后三项可能没有。
        target_smiles  目标分子的 SMILES（提交分析时已经校验并转成规范写法）。
        reference      参考数据，用来给分子取名字。
    返回：
        分析结果的骨架，一个可以直接存成 JSON 的字典。八个键见函数末尾 return 处的注释，
        其中 routes 的每一项有七个键，见循环里的注释。
    处理规则：
        - 整理不了的路线跳过，并在 warnings 里记一句；终点不是目标分子的路线也一样。
        - 一步都没拆的“路线”（只有目标分子本身）不算路线，直接丢掉，不记提示。
        - 用到的步骤完全相同的路线只留第一条（同一组反应、只是引擎用的模板不同，算同一条），不记提示。
        - 路线编号 r1、r2……按留下来的先后编，也就是引擎返回的先后。
        - 被跳过或丢掉的路线在转换途中登记的分子和反应仍然留在两张表里。
    副作用：
        没有。不改 search，不碰数据库。
    抛出：
        RouteBuildError  目标分子的 SMILES 读不出来（正常情况下不会发生）。
    谁在用：
        pipeline.py 的 AnalysisRunner._search；tests/test_integrations.py、tests/test_reference.py。
    """
    builder = _Builder(reference)
    # 先登记目标分子，后面拿它的 key14 核对每条路线树的根节点
    target = builder.molecule(target_smiles)
    routes: list[dict[str, Any]] = []
    # 拷一份再往里加，不改引擎返回的那个列表
    warnings = list(search.warnings)
    # 已经收下的路线各自“用到了哪些步骤”，去重用
    seen: set[tuple[str, ...]] = set()

    for raw in search.routes:
        try:
            tree = builder.convert(raw["tree"])
        except (RouteBuildError, KeyError) as exc:
            # KeyError：字典里缺 "tree"、"smiles" 这类必有的键。坏的只是这一条，其余路线照常处理
            # 原因先转成字符串再当参数：它是上面 RouteBuildError 里的一句（有英文对照），英文句子里会自动换上
            warnings.append(i18n.tr("跳过了一条读不出来的路线：{reason}",
                                    "Skipped a route that could not be read: {reason}", reason=str(exc)))
            continue
        if tree["key14"] != target["key14"]:
            warnings.append(i18n.tr("跳过了一条终点不是目标分子的路线",
                                    "Skipped a route that does not end at the target molecule"))
            continue
        # 给这条路线的每一步编上实验顺序的序号（从 1 开始），写在路线树的反应节点上，
        # 同时按这个顺序收集步骤编号。iter_reaction_nodes 给出的就是“先原料、后产物”的顺序
        step_keys = []
        for number, reaction in enumerate(iter_reaction_nodes(tree), 1):
            reaction["step_index"] = number
            step_keys.append(reaction["step_key"])
        # 去重看的是“用到了哪些步骤”，和先后无关，所以排好序再比
        signature = tuple(sorted(step_keys))
        if not step_keys or signature in seen:
            continue
        seen.add(signature)
        route_id = f"r{len(routes) + 1}"
        # 最后一步就是得到目标分子的那一步；引擎没给路线起名字时借它的反应名
        final_step = builder.reactions[step_keys[-1]]
        routes.append(
            {
                # 路线编号 "r1"、"r2"……，接口路径里的 {route_id} 就是它
                "id": route_id,
                # 路线名。演示路线自带名字（比如 “Boots”）；AiZynthFinder 的路线用最后一步的反应名
                "name": raw.get("name") or final_step["name"],
                # 路线树，结构见 _Builder.convert；每个反应节点上另有 step_index
                "tree": tree,
                # 按实验顺序排好的步骤编号：step_keys[0] 是第 1 步，最后一个是得到目标分子的那一步
                "step_keys": step_keys,
                # 这条路线的起始原料（路线树的叶子）的 key14，不重复
                "leaf_keys": leaf_keys(tree),
                # 引擎自己给这条路线打的分：AiZynthFinder 路线树 scores 里的第一项（测试样例里是
                # "state score"）；演示路线是 None。只是存下来，后端的评分（M10）不用它
                "engine_score": raw.get("score"),
                # 引擎认为这条路线拆到底了没有（叶子是不是都在它的在售清单里）：True / False；演示路线是 None。
                # False 的路线照样往下评估，只在第 2 阶段的进度说明里点出来
                "engine_solved": raw.get("solved"),
            }
        )

    return {
        # 结果结构的版本号。以后改了结构可以靠它区分新旧结果；现在只有 1，也还没有代码读它
        "schema": 1,
        # 结果做到了哪一步："preliminary" 只有路线树；第 7 阶段评完分改成 "scored"，
        # 第 8 阶段写完说明（或者一条路线都没找到）改成 "final"。接口靠它判断结果页能不能打开
        "stage": "preliminary",
        # 这次用的逆合成引擎：name 是 "aizynthfinder" 或 "demo"，version 是引擎的版本（演示引擎是 None），
        # stats 是引擎的搜索统计（比如 number_of_nodes：探索了多少个中间体）
        "engine": {"name": search.engine, "version": search.version, "stats": search.stats},
        # 目标分子，就是分子表里它的那一条（存进数据库再读出来以后，是内容相同的两份）
        "target": target,
        # 分子表 {key14: 分子}，每个键见 _Builder.molecule
        "molecules": builder.molecules,
        # 反应表 {step_key: 一步反应}，每个键见 _Builder._reaction
        "reactions": builder.reactions,
        # 路线列表，按引擎返回的先后（不是名次；名次在第 7 阶段写进每条路线的 rank）
        "routes": routes,
        # 给用户看的提示，结果页会显示：先是引擎的提示，再是上面跳过路线的说明；后面的阶段还会往里加
        "warnings": warnings,
    }


def build_graph(result: dict[str, Any]) -> dict[str, Any]:
    """R5 路线树画布用的合并视图：每个分子有哪几种做法（分叉），每种做法属于哪些路线。

    前端切换分叉标签（B8）时，直接换成该分叉里分数最高的那条路线，不用再问后端。

    参数：
        result  存在 Analysis.result 里的分析结果。评分前后都能用：还没评分时路线上没有 score 和
                excluded，这时每个分叉的 score 是 None、excluded 是 False。
    返回：
        {
          "target":       目标分子的 key14,
          "alternatives": {产物的 key14: [分叉, ...]}。路线里每个“由某一步做出来的分子”（目标和中间体）
                          各一项，只有一种做法的也在里面（列表里就一个分叉）,
          "top_branches": 目标分子的那几个分叉的拷贝，每个多一个 "label"（"A"、"B"……，画布上的分叉标签）,
        }
        一个分叉是得到同一个产物的一种做法（一步反应），七个键见内部函数 branch 里的注释。
        每个列表里没被排除的分叉在前，然后按分数从高到低。
    副作用：
        没有，不改 result。每次调用都重新算，结果不存进数据库。
    谁在用：
        api/views.py 的 result_view（结果页返回值里的 graph 字段）；tests/test_integrations.py。
    """
    routes = result["routes"]
    by_id = {route["id"]: route for route in routes}
    # makers：{产物的 key14: {得到它的步骤编号: [经过这一步的路线编号, ...]}}
    # setdefault(键, 默认值)：键不在字典里就先放进默认值，再把值取出来，省掉一次“在不在”的判断
    makers: dict[str, dict[str, list[str]]] = {}
    for route in routes:
        for step_key in route["step_keys"]:
            product = result["reactions"][step_key]["product"]
            makers.setdefault(product, {}).setdefault(step_key, []).append(route["id"])

    def branch(step_key: str, route_ids: list[str]) -> dict[str, Any]:
        """把一种做法整理成画布要的一个分叉。step_key 是这一步的编号，route_ids 是经过它的全部路线。"""
        # 被排除的路线不参加比较；一条都不剩时这个分叉整个算被排除
        eligible = [by_id[r] for r in route_ids if not by_id[r].get("excluded")]
        # 取路线分最高的那条，还没评分的按 0 比。default=None：列表是空的时不报错，返回 None
        best = max(eligible, key=lambda r: r.get("score") or 0, default=None)
        reaction = result["reactions"][step_key]
        return {
            # 这种做法是哪一步反应（反应表的键）
            "step_key": step_key,
            # 反应名
            "name": reaction["name"],
            # 这一步各个反应物的 key14
            "reactants": reaction["reactants"],
            # 经过这一步的全部路线的编号，被排除的也在内
            "route_ids": route_ids,
            # 切到这个分叉时该显示哪条路线：没被排除的里面路线分最高的那条；全被排除时是 None
            "best_route_id": best["id"] if best else None,
            # 上面那条路线的路线分（是整条路线的分数，不是这一步的步骤分）；没有或还没评分时是 None
            "score": best.get("score") if best else None,
            # True：经过这一步的路线全都被排除了
            "excluded": best is None,
        }

    alternatives = {}
    for product, options in makers.items():
        branches = [branch(step_key, route_ids) for step_key, route_ids in options.items()]
        # 排序的键是一个元组：先比 excluded（False 排在 True 前面），再比分数（取负数就成了从高到低）
        branches.sort(key=lambda b: (b["excluded"], -(b["score"] or 0)))
        alternatives[product] = branches

    target_key = result["target"]["key14"]
    # 目标分子的分叉拷一份再加标签，免得 alternatives 里的那几个字典也跟着多出 label
    top = [dict(b) for b in alternatives.get(target_key, [])]
    # 每条路线在 routes 列表里排第几（也就是引擎返回的先后）
    order = {route["id"]: position for position, route in enumerate(routes)}
    # 标签按“这个分叉最早出现在第几条路线里”来发，不按分数：重新评分、换权重以后列表里的先后会变，
    # 但同一个分叉的字母不变。chr(ord("A") + n)：n 是 0 得到 A，1 得到 B……；超过 26 个以后改用数字 27、28……
    for position, item in enumerate(sorted(top, key=lambda b: order[b["route_ids"][0]])):
        item["label"] = chr(ord("A") + position) if position < 26 else str(position + 1)
    return {"target": target_key, "alternatives": alternatives, "top_branches": top}
