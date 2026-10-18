"""M7 条件与复杂度：按规则给每一步打操作标签（无水无氧、加压、低温……）并算操作分。

规则在 data/condition_rules.toml，应当由化学顾问制定；这里只负责执行。
逆合成引擎不返回反应条件，所以判断依据是：这一步用到的物质、反应类型，
以及先例里记录的试剂、温度和条件文字（如果有）。

对应关系
    PPT 模块：M7 条件与复杂度。
    流水线：第 6 阶段“条件、产率、绿色化学”。syntherisk/pipeline.py 的 AnalysisRunner._conditions()
    对每一步调用一次 ConditionEvaluator.evaluate()，把结果并进 reaction["conditions"]。
    另外，第 2 阶段整理路线树时（syntherisk/routes.py 的 _Builder._reaction）用本文件的
    name_reaction() 给引擎没有起名字的步骤起名。
    界面：
        A10 “做不了的条件”   勾选项的编号就是规则里的 constraint。M10 看到标签带着被勾掉的 constraint，
                             就把整条路线排除并写明是哪一步（见 m10_scoring.exclusion_reasons）。
        A13 / A14 纯化偏好   对应规则里的 purification。软偏好由 M10 扣分；A14 一句话要求里的硬性要求
                             （“不用柱色谱”）由 M10 排除路线。
        R6 证据卡            显示操作标签（“操作 中等 · 需无水”）和每一条命中的规则及其理由。
        R7 汇总条            “特殊操作”一项数的是带 constraint 的标签。

数据从哪来、到哪去
    输入：这一步的反应名（reaction["name"]）、反应物和试剂的 SMILES、最好那条先例的条件文字和温度；
          data/condition_rules.toml（启动时读成 reference.condition_rules，一条规则是一个 ConditionRule，
          定义在 syntherisk/reference.py）；data/scoring.toml 的 [operation]（基础分和三档的分界线）。
    输出：{"tags", "operation_score", "operation_level", "operation_label"}。流水线把它和
          text（条件文字）、source（条件文字的出处）、temperature_c 一起存成 reaction["conditions"]。
    谁读 reaction["conditions"]：
          m10_scoring   操作这一维的分数和依据、软偏好扣分、硬约束排除、汇总条里的特殊操作
          cards         证据卡的 conditions 字段和来源编号 "RULE:<规则编号>"
          m11 / m12     写说明、回答问题
          m13           报告里的“操作”和“特殊操作”
          m14           实验回填快照里的 operation_tags
    本文件不读写数据库，不联网，不改传进来的对象。

本文件里有什么
    _GROUPS、_CO2、_CO   认官能团用的 SMARTS 表；二氧化碳、一氧化碳的结构编号
    _count()             数一组分子里某个官能团一共出现几次
    name_reaction()      按官能团的增减猜一个反应名
    ConditionEvaluator   规则执行器
        evaluate()       对一步反应执行全部规则，返回标签、操作分、三档复杂度和显示文字
        _matches()       判断一条规则有没有命中
        _tag()           把命中的规则转成存进结果里的标签字典
"""

from __future__ import annotations

from typing import Any

from syntherisk import i18n
from syntherisk.modules import m01_structure as chem
from syntherisk.reference import ConditionRule, ReferenceData

# name_reaction() 起的全部反应名的英文。函数里返回的仍然是中文名（规则的 class_keywords 要在中文名里找关键词，
# 结果里存的也是中文）；这里把“中文名 → 英文名”登记进 i18n，请求带 ?lang=en 时换成英文（见 syntherisk/i18n.py）。
# name_reaction() 里加了新的名字，这里也要补一行，不然那个名字在英文界面上还是中文
_REACTION_NAMES_EN = {
    "Suzuki 偶联": "Suzuki coupling",
    "Kolbe–Schmitt 羧基化": "Kolbe–Schmitt carboxylation",
    "格氏羧基化": "Grignard carboxylation",
    "羰基化": "Carbonylation",
    "傅-克酰基化": "Friedel–Crafts acylation",
    "Darzens 缩合": "Darzens condensation",
    "羰基环氧化": "Carbonyl epoxidation",
    "酰胺化": "Amidation",
    "酯化（O-酰化）": "Esterification (O-acylation)",
    "腈水解": "Nitrile hydrolysis",
    "肟脱水成腈": "Oxime dehydration to nitrile",
    "成肟": "Oxime formation",
    "水解脱羧": "Hydrolysis and decarboxylation",
    "环氧重排成醛": "Epoxide rearrangement to aldehyde",
    "酯水解": "Ester hydrolysis",
    "羰基还原": "Carbonyl reduction",
    "氯代": "Chlorination",
    "醇氧化": "Alcohol oxidation",
    "硝基还原": "Nitro reduction",
    "酰氯化": "Acyl chloride formation",
    "酰氯还原成醛": "Acyl chloride reduction to aldehyde",
    "氧化成羧酸": "Oxidation to carboxylic acid",
    "酰胺水解": "Amide hydrolysis",
    "酯或酰胺转化为醇": "Ester or amide reduction to alcohol",
    "酯化（卤代烃取代）": "Esterification (alkyl halide substitution)",
    "脱保护成酚": "Deprotection to phenol",
    "Dakin 氧化": "Dakin oxidation",
    "C–N 偶联": "C–N coupling",
    "还原胺化": "Reductive amination",
    "羰基烯化": "Carbonyl olefination",
    "烯烃加氢": "Alkene hydrogenation",
    "羰基还原成亚甲基": "Carbonyl reduction to methylene",
    "烷基化": "Alkylation",
}
for _name_zh, _name_en in _REACTION_NAMES_EN.items():
    i18n.register(_name_zh, _name_en)

# 操作难度的三档（operation_level）。i18n.tr 返回的就是中文，同时登记了英文
_LEVEL_SIMPLE = i18n.tr("简单", "Simple")
_LEVEL_MEDIUM = i18n.tr("中等", "Moderate")
_LEVEL_COMPLEX = i18n.tr("复杂", "Complex")

# ---------------------------------------------------------------- 反应命名（启发式）

# 官能团名字 -> SMARTS。SMARTS 是描述“子结构”的写法（可以看成 SMILES 的查询版），这里用到的记号：
#   C / c       脂肪碳 / 芳香碳；[#6] 是任意碳（不分是否芳香）
#   [CX3]       连着 3 个原子的碳（X 后面是连接的原子数）；[CX4] 是饱和碳，[CX2] 连着 2 个原子
#   [OX2H1]     连着 2 个原子、带 1 个氢的氧，也就是羟基氧；H2,H1 表示带 2 个或 1 个氢
#   =  #        双键、三键；!@ 表示“不在环里的键”
#   [Cl,Br,I]   逗号是“或”；分号是“并且”；感叹号是“不是”
#   $(...)      “这个原子处在括号里描述的环境中”，括号里的其他原子不算进匹配
# 每一项上面的注释写的是它认的官能团。只在本文件的 _count() 和 name_reaction() 里用。
_GROUPS = {
    # 羧酸：羰基碳上连着一个羟基
    "acid": "[CX3](=O)[OX2H1]",
    # 酯：羰基碳–氧–碳，氧另一边的碳自己不能也是羰基碳
    "ester": "[CX3](=O)[OX2][#6;!$(C=O)]",  # 排除酸酐
    # 酰胺：羰基碳上连着一个三配位的氮
    "amide": "[CX3](=O)[NX3]",
    # 酮：羰基碳两边都是碳
    "ketone": "[#6][CX3](=O)[#6]",
    # 芳基酮：羰基碳一边是芳香碳，另一边是任意碳
    "aryl_ketone": "c[CX3](=O)[#6]",
    # 醛：羰基碳上带一个氢，另一边是碳（所以甲醛不算）
    "aldehyde": "[CX3H1](=O)[#6]",
    # 醇：饱和碳上的羟基
    "alcohol": "[CX4][OX2H]",
    # 酚：芳香碳上的羟基
    "phenol": "c[OX2H]",
    # 卤代烷：饱和碳上的氯、溴或碘
    "alkyl_halide": "[CX4][Cl,Br,I]",
    # 氯代烷：饱和碳上的氯
    "alkyl_chloride": "[CX4]Cl",
    # 芳基卤：芳香碳上的氯、溴或碘
    "aryl_halide": "c[Cl,Br,I]",
    # 腈：碳氮三键
    "nitrile": "[CX2]#N",
    # 肟：C=N–OH
    "oxime": "[CX3]=N[OX2H]",
    # 硝基
    "nitro": "[N+](=O)[O-]",
    # 伯胺或仲胺：三配位、带 1 到 2 个氢的氮，并且不连在羰基上（否则是酰胺）
    "amine": "[NX3;H2,H1;!$(NC=O)]",
    # 环氧：碳–氧–碳的三元环
    "epoxide": "C1OC1",
    # 硼酸或硼酸酯：三配位的硼上连着两个氧
    "boronic": "[BX3](O)O",
    # 联芳基：两个芳香碳之间一根不在环里的键，也就是把两个芳环连起来的那根键
    "biaryl": "c!@c",
    # 酰化试剂：酰氯，或者酸酐（羰基碳连着的氧另一边还是羰基）
    "acylating": "[CX3](=O)[Cl,$(O[CX3]=O)]",
    # 酰氯
    "acyl_chloride": "[CX3](=O)Cl",
    # 烯烃：碳碳双键
    "alkene": "[CX3]=[CX3]",
    # 芳基烷基醚：芳香碳–氧–饱和碳
    "aryl_ether": "c[OX2][CX4]",
    # 芳胺：芳香碳上连着三配位的氮
    "aryl_amine": "c[NX3]",
}
# 二氧化碳和一氧化碳的结构编号（key14：InChIKey 的前 14 位）。
# name_reaction() 用它们判断“二氧化碳 / 一氧化碳是不是反应物之一”。
_CO2 = chem.key14_of("O=C=O")
_CO = chem.key14_of("[C-]#[O+]")


def _count(smiles_list: list[str], group: str) -> int:
    """数一组分子里某个官能团一共出现了几次。

    参数：
        smiles_list  一组分子的 SMILES。
        group        _GROUPS 里的一个键，比如 "acid"。
    返回：
        出现次数之和。一个分子里有两个羧基算 2；读不出来的 SMILES 跳过，不报错。
    谁在用：
        本文件的 name_reaction()。
    """
    # smarts_pattern() 把 SMARTS 编译成 RDKit 的查询对象（带缓存，同一个 SMARTS 只编译一次）
    pattern = chem.smarts_pattern(_GROUPS[group])
    total = 0
    for smiles in smiles_list:
        mol = chem.to_mol(smiles)
        if mol is not None:
            # GetSubstructMatches() 返回全部匹配，每个匹配是一组原子序号；这里只要个数
            total += len(mol.GetSubstructMatches(pattern))
    return total


def name_reaction(reactants: list[str], product: str) -> str | None:
    """按官能团的增减猜一个反应名，给步骤起标题用。认不出来返回 None。

    注意这个名字不只是显示：它存成这一步的 name，第 6 阶段 ConditionEvaluator.evaluate() 会在里面找规则的
    class_keywords（比如名字里有“加氢”就算加压气体反应、有“格氏”就算无水无氧操作），所以猜出来的名字会影响
    操作分；用户勾了对应的“做不了的条件”时，还会让这条路线被排除。

    做法：数一数 _GROUPS 里每个官能团在全部反应物里有几个、在产物里有几个，
    再按下面一串“少了什么、多了什么”的规则从上到下试，第一条成立的就是结果。
    这是启发式的判断：不看原子的对应关系，也不看试剂。

    参数：
        reactants  反应物的 SMILES 列表。
        product    产物的 SMILES。
    返回：
        中文反应名（比如“傅-克酰基化”“羰基还原”）；一条规则都不成立时返回 None。
    副作用：没有。
    谁在用：
        syntherisk/routes.py 的 _Builder._reaction()（流水线第 2 阶段整理路线树时）：引擎的元数据里
        既没有名字、也没有可用的反应类别时才调它；它也返回 None 的话，那一步叫“片段连接”
        （有多个反应物）或“官能团转化”（只有一个反应物）。
        tests/test_structure.py 的 test_reaction_naming_and_atom_economy 列了一批例子。
    """
    # 每个官能团在全部反应物里出现几次（before）、在产物里出现几次（after）
    before = {g: _count(reactants, g) for g in _GROUPS}
    after = {g: _count([product], g) for g in _GROUPS}
    # gained(g)：产物里的 g 比反应物里多；lost(g)：比反应物里少。
    # 行尾的 noqa: E731 是写给代码检查工具看的（允许把 lambda 赋给变量），不影响运行
    gained = lambda g: after[g] > before[g]  # noqa: E731
    lost = lambda g: after[g] < before[g]  # noqa: E731
    # 各个反应物的结构编号，用来看二氧化碳、一氧化碳在不在反应物里
    keys = {chem.key14_of(s) for s in reactants}

    # 以下规则从上到下试，第一条成立的就返回，所以调换顺序会改变结果
    # （比如“酰胺化”排在“酯化”前面：产物同时多出酰胺和酯时，叫酰胺化）。

    # 反应物里有硼酸（酯）和芳基卤，产物多出一根连接两个芳环的键
    if before["boronic"] and before["aryl_halide"] and gained("biaryl"):
        return "Suzuki 偶联"
    # 二氧化碳是反应物、产物多出羧基：反应物里有酚的叫 Kolbe–Schmitt，否则叫格氏羧基化
    if _CO2 in keys and gained("acid"):
        return "Kolbe–Schmitt 羧基化" if before["phenol"] else "格氏羧基化"
    # 一氧化碳是反应物，产物多出羧酸、酯或酰胺
    if _CO in keys and (gained("acid") or gained("ester") or gained("amide")):
        return "羰基化"
    # 产物多出芳基酮，并且反应物里有酰氯或酸酐
    if gained("aryl_ketone") and before["acylating"]:
        return "傅-克酰基化"
    # 酮或醛少了，环氧多了
    if gained("epoxide") and (lost("ketone") or lost("aldehyde")):
        # Darzens 用 α-卤代酯，产物是环氧酯；没有酯的是硫叶立德那一类（Corey–Chaykovsky）
        return "Darzens 缩合" if after["ester"] else "羰基环氧化"
    # 产物多出酰胺
    if gained("amide"):
        return "酰胺化"
    # 产物多出酯，同时醇或酚少了
    if gained("ester") and (lost("alcohol") or lost("phenol")):
        return "酯化（O-酰化）"
    # 腈变成羧酸
    if lost("nitrile") and gained("acid"):
        return "腈水解"
    # 肟变成腈
    if lost("oxime") and gained("nitrile"):
        return "肟脱水成腈"
    # 产物多出肟
    if gained("oxime"):
        return "成肟"
    # 环氧变成醛：同时少了酯的叫水解脱羧（环氧酯水解、脱羧得到醛），否则叫环氧重排
    if lost("epoxide") and gained("aldehyde"):
        return "水解脱羧" if lost("ester") else "环氧重排成醛"
    # 酯变成羧酸
    if lost("ester") and gained("acid"):
        return "酯水解"
    # 酮或醛变成醇
    if (lost("ketone") or lost("aldehyde")) and gained("alcohol"):
        return "羰基还原"
    # 醇变成氯代烷
    if lost("alcohol") and gained("alkyl_chloride"):
        return "氯代"
    # 醇变成酮或醛
    if lost("alcohol") and (gained("ketone") or gained("aldehyde")):
        return "醇氧化"
    # 硝基变成胺
    if lost("nitro") and gained("amine"):
        return "硝基还原"
    # 下面这些是接上 AiZynthFinder 以后补的：公开模型不给反应类别，常见的官能团转化要自己认
    # 羧酸少了，产物里有酰氯
    if lost("acid") and after["acyl_chloride"]:  # 不用 gained：草酰氯自己就带两个酰氯
        return "酰氯化"
    # 酰氯变成醛
    if lost("acyl_chloride") and gained("aldehyde"):
        return "酰氯还原成醛"
    # 醇或醛变成羧酸
    if (lost("alcohol") or lost("aldehyde")) and gained("acid"):
        return "氧化成羧酸"
    # 酰胺变成羧酸
    if lost("amide") and gained("acid"):
        return "酰胺水解"
    # 酯或酰胺少了，醇多了
    if (lost("ester") or lost("amide")) and gained("alcohol"):
        return "酯或酰胺转化为醇"
    # 卤代烷少了，酯多了（羧酸根取代卤素成酯）
    if lost("alkyl_halide") and gained("ester"):
        return "酯化（卤代烃取代）"
    # 芳基醚或酯少了，酚多了
    if (lost("aryl_ether") or lost("ester")) and gained("phenol"):
        return "脱保护成酚"
    # 醛变成酚
    if lost("aldehyde") and gained("phenol"):
        return "Dakin 氧化"
    # 芳基卤少了，芳胺多了
    if lost("aryl_halide") and gained("aryl_amine"):
        return "C–N 偶联"
    # 酮或醛变成胺
    if (lost("ketone") or lost("aldehyde")) and gained("amine"):
        return "还原胺化"
    # 酮或醛变成烯烃
    if (lost("ketone") or lost("aldehyde")) and gained("alkene"):
        return "羰基烯化"
    # unchanged：酮、醛、醇、羧酸、酯、酰胺、胺这七种官能团的个数都没有变。
    # 只少了碳碳双键、别的都没动，才叫烯烃加氢
    unchanged = not any(gained(g) or lost(g) for g in ("ketone", "aldehyde", "alcohol", "acid", "ester", "amide", "amine"))
    if lost("alkene") and unchanged:
        return "烯烃加氢"
    # 芳基酮少了，又没有变成醇、烯烃、胺、肟或环氧：当作羰基被还原成了 CH2
    if lost("aryl_ketone") and not any(gained(g) for g in ("alcohol", "alkene", "amine", "oxime", "epoxide")):
        return "羰基还原成亚甲基"
    # 前面都不是、只是卤代烷少了：笼统地叫烷基化
    if lost("alkyl_halide"):
        return "烷基化"
    return None


# ---------------------------------------------------------------- 规则执行


class ConditionEvaluator:
    """规则执行器：拿 data/condition_rules.toml 里的规则逐条去套一步反应。

    属性：
        reference  参考数据（ReferenceData）。这里用到 condition_rules（规则列表）
                   和 compounds（把规则里写的物质 key 换成结构编号）。
        config     data/scoring.toml 的 [operation] 一节：base（基础分，现在是 100）、
                   simple_from（现在是 80）、medium_from（现在是 50）。
    流水线每次跑到第 6 阶段时新建一个（AnalysisRunner._conditions），它不保存任何一步的状态。
    """

    def __init__(self, reference: ReferenceData):
        """记下参考数据，并取出 [operation] 这一节配置。

        参数：
            reference  服务启动时读好的参考数据（syntherisk/reference.py 的 ReferenceData）。
        """
        self.reference = reference
        self.config = reference.scoring["operation"]

    def evaluate(
        self,
        *,
        name: str | None,
        substances: list[str],
        conditions_text: str | None = None,
        temperature_c: float | None = None,
        conditions_known: bool = True,
    ) -> dict[str, Any]:
        """substances：这一步的反应物和已知试剂（SMILES）。

        conditions_known=False 表示既没有试剂也没有条件说明（逆合成引擎只给反应物和产物）。
        这时如果一条规则都没命中，不能说“操作简单”，只能说没评估。

        做什么：
            把全部规则对这一步试一遍，命中的规则各变成一个标签；
            从基础分里减去这些标签的扣分，得到操作分，再分成简单 / 中等 / 复杂三档。
        参数（签名里的 * 表示后面的参数都必须写成 名字=值 的形式）：
            name              这一步的反应名（reaction["name"]），可以是 None。它可能是演示数据里写的名字、
                              逆合成引擎给的反应类别、本文件 name_reaction() 起的名字，
                              或者兜底的“片段连接”“官能团转化”（见 syntherisk/routes.py 的 _Builder._reaction）。
                              规则的 class_keywords 就在它里面找关键词。
            substances        见上。规则的 substances 和 smarts 在这些分子里找。
            conditions_text   条件文字，没有就是 None。规则的 text_keywords 在它里面找关键词。
                              流水线传的是最好那条先例（证据等级 E1 / E2 时）的条件文字；
                              演示数据自带的条件概述不传进来。
            temperature_c     反应温度，单位摄氏度，没有就是 None。流水线传的是最好那条先例记录的温度。
            conditions_known  见上。流水线传的是 bool(条件文字 or 试剂)。
        返回（字典，流水线把它并进 reaction["conditions"]）：
            tags             命中的规则，每项的字段见 _tag()；顺序和规则文件里的顺序一致
            operation_score  操作分，0–100，保留一位小数：基础分减去各标签的 penalty 之和，最低 0
            operation_level  "简单"、"中等" 或 "复杂"。按现在的配置：≥ 80 简单，50–79 中等，< 50 复杂
            operation_label  界面上显示的文字，例如“操作 中等 · 需无水”。后半截是扣分最多的那个标签的 short；
                             没有标签时只有“操作 简单”
            没评估时（一个标签都没有，并且 conditions_known 是 False）：
            tags 是空列表，operation_score 和 operation_level 是 None，operation_label 是“操作 未评估”。
        例子：
            演示路线里的格氏羧基化命中“无水无氧操作”（扣 22 分）：100 − 22 = 78，中等，“操作 中等 · 需无水”。
        副作用：没有。
        谁在用：
            syntherisk/pipeline.py 的 AnalysisRunner._conditions()（流水线第 6 阶段，每一步调一次）。
        """
        # 按规则文件里的顺序逐条试。name、conditions_text 是 None 时换成空字符串，后面好做字符串比较
        tags = [
            self._tag(rule)
            for rule in self.reference.condition_rules
            if self._matches(rule, name or "", substances, conditions_text or "", temperature_c)
        ]
        # 什么都不知道、也什么都没命中：返回“未评估”，分数留空，M10 就不计操作这一维
        if not tags and not conditions_known:
            return {"tags": [], "operation_score": None, "operation_level": None,
                    "operation_label": i18n.tr("操作 未评估", "Operation: not assessed")}
        # 操作分 = 基础分（[operation] 的 base）减去每个标签的扣分，不低于 0
        score = max(0.0, float(self.config["base"]) - sum(tag["penalty"] for tag in tags))
        # 三档的分界线是 [operation] 的 simple_from 和 medium_from
        if score >= float(self.config["simple_from"]):
            level = _LEVEL_SIMPLE
        elif score >= float(self.config["medium_from"]):
            level = _LEVEL_MEDIUM
        else:
            level = _LEVEL_COMPLEX
        # 扣分最多的那个标签写进显示文字里；一个标签都没有时 max() 走 default，得到 None
        main = max(tags, key=lambda tag: tag["penalty"], default=None)
        return {
            "tags": tags,
            "operation_score": round(score, 1),
            "operation_level": level,
            # 整句经过 i18n.tr 才有英文；level 和标签的 short 各有自己的对照（short 的英文由 reference.py 登记）
            "operation_label": (
                i18n.tr("操作 {level} · {short}", "Operation: {level} · {short}", level=level, short=main["short"])
                if main
                else i18n.tr("操作 {level}", "Operation: {level}", level=level)
            ),
        }

    def _matches(self, rule: ConditionRule, name: str, substances: list[str], text: str, temperature) -> bool:
        """判断一条规则对这一步有没有命中。下面五种判据命中任意一种就返回 True。

        参数：
            rule         一条规则（ConditionRule，对应 condition_rules.toml 里的一个 [[rule]]）。
            name         反应名，已经保证是字符串（可能是空的）。
            substances   反应物和试剂的 SMILES。
            text         条件文字，已经保证是字符串（可能是空的）。
            temperature  温度（摄氏度）或 None。
        判据（按代码里的先后）：
            1. rule.substances        这一步用到了规则点名的物质（按结构比，不看名字）
            2. rule.smarts            这一步的某个物质含有规则给的子结构
            3. rule.class_keywords    反应名里有规则给的关键词（不分大小写）
            4. rule.text_keywords     条件文字里有规则给的关键词（不分大小写）
            5. rule.max_temperature   温度不高于这个界限；rule.min_temperature：温度不低于这个界限
        规则里没有填的判据是空列表或 None，不会命中。
        谁在用：
            只有本类的 evaluate()。
        """
        compounds = self.reference.compounds
        if rule.substances:
            # 规则里写的是 data/compounds.csv 的 key（比如 "hydrogen"），先换成结构编号 key14，
            # 再看这一步有没有哪个物质的 key14 在里面。这些 key 存不存在，启动时已经检查过（ReferenceData.load）
            wanted = {compounds.by_key[key].key14 for key in rule.substances}
            if any(chem.key14_of(s) in wanted for s in substances):
                return True
        # matches_smarts()：SMILES 读得出来、并且含有这个 SMARTS 子结构时为 True
        if any(chem.matches_smarts(s, pattern) for pattern in rule.smarts for s in substances):
            return True
        # 关键词比较之前，两边都转成小写
        lowered_name, lowered_text = name.lower(), text.lower()
        if any(keyword.lower() in lowered_name for keyword in rule.class_keywords):
            return True
        if lowered_text and any(keyword.lower() in lowered_text for keyword in rule.text_keywords):
            return True
        # 温度判据：有温度记录时才比
        if temperature is not None:
            # 低温规则（规则文件里是 max_temperature = -40）：温度不高于界限就命中
            if rule.max_temperature is not None and temperature <= rule.max_temperature:
                return True
            # 高温规则（规则文件里是 min_temperature = 150）：温度不低于界限就命中
            if rule.min_temperature is not None and temperature >= rule.min_temperature:
                return True
        return False

    @staticmethod
    def _tag(rule: ConditionRule) -> dict[str, Any]:
        """把一条命中的规则转成存进分析结果里的标签（普通字典，可以直接存成 JSON）。

        返回的字段（都照抄规则文件里同名的项）：
            rule_id       规则编号（规则文件里的 id）。证据卡里的来源编号 "RULE:<rule_id>" 就是用它拼的
            label         完整的名字，例如“无水无氧操作”
            short         短名字，用在操作标签里，例如“需无水”
            category      类别。规则文件里现在用到的有：设备、气氛、温度、安全设施、纯化
            penalty       操作分里扣多少分
            reason        为什么需要这种操作（给人看的一句话）
            constraint    对应 A10 “做不了的条件”的哪一项：pressurized_gas / anhydrous_anaerobic / glovebox /
                          cryogenic / photo_electro；不对应任何一项时是 None
            purification  对应哪种纯化方式：column_chromatography / distillation / prep_hplc；不对应时是 None
        @staticmethod 表示这个方法不用 self，只是放在类里归类。
        谁在用：
            只有本类的 evaluate()。
        """
        return {
            "rule_id": rule.id,
            "label": rule.label,
            "short": rule.short,
            "category": rule.category,
            "penalty": float(rule.penalty),
            "reason": rule.reason,
            "constraint": rule.constraint,
            "purification": rule.purification,
        }
