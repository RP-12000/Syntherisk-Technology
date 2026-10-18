"""M6 毒性与法规：每个物质的 GHS 危害分类和管制情况。

数据来源的先后：
    1. data/compounds.csv 里的分类（团队自己维护；初稿还没复核，结果里会标出来）。
       h_codes 留空表示“不属于危险品”；不知道的写 “?”，会接着去查 PubChem
    2. PubChem 的 GHS 分类（查到后缓存在数据库里）：欧盟统一分类，加上多数企业申报的分类，
       具体取法见 syntherisk/external.py 的 collect_ghs
查不到的物质如实标“没有危害数据”，不当作安全。

管制清单见 data/controlled_substances.csv；按 CONTROLLED_JURISDICTIONS 配置的地区过滤。

---- 在整个系统里的位置 ----

对应 PPT 的模块 M6，是一次分析的第 5 阶段“毒性与法规”（pipeline.AnalysisRunner._safety）：
把每一步的反应物、试剂、产物逐个交给 SafetyService.assess()（同一个物质只评估一次），把结果
（再加上一个 role 键：reactant / reagent / product）存成 result["reactions"][步骤]["hazards"]。之后谁读它：
    M10 评分    safety_score() 算“毒性与安全”这一维的分数；
                用户在 A11 勾了“排除剧毒 / 致癌致突变生殖毒性 / 管制化学品”时按 flags 排除路线，
                排除原因里的危险性说明由 statements_behind() 给出；
                R7 汇总条上的“最高危害”用 top_hazard()
    M11 说明    most_severe() 挑出这一步最要注意的那个物质
    界面、报告  R6 证据卡的危害列表；F1 报告里的“安全”一节
命令行 python -m syntherisk.cli check-hazards 用 compare_with_pubchem() 把内置表和 PubChem 对一遍（要联网）。

依赖：external.py（PubChem 客户端、GHS 页面的解析）、m01_structure（key14、盐里的各个片段）、
reference（内置试剂表、管制清单、scoring.toml 的 [safety] 一节）、models.SubstanceHazard（PubChem 结果的缓存表）。
联网只发生在 SafetyService._from_pubchem() 和 compare_with_pubchem() 里；不调大模型。

---- 几个词 ----

    H 编码       GHS 危险性说明的编号，比如 H225 “高度易燃液体和蒸气”。带字母后缀的（H360FD）是更细的说明。
    类别（flag） 代码给一组 H 编码起的名字（acute_toxic_12、cmr_1、corrosive……）。评分和排除都按类别来，
                 不直接按 H 编码。另有两个不是从 H 编码来的类别：controlled_strict、controlled_basic（管制清单）。
    data_source  这个物质的危害数据是哪来的：local 内置表，pubchem，none 没有数据。none 不等于安全。
    评估结果     SafetyService.assess() 返回的那个字典。statements_behind()、most_severe()、safety_score()
                 都拿它当输入。

---- 本文件里有什么（按阅读顺序）----

    H_STATEMENTS、SEVERITY_ORDER、_TIERS、_COVERED_BY   四张对照表
    statement_for          H 编码 -> (中文说明, 类别, 象形图)
    strongest_per_hazard   同一种危害报了轻重几档时，只留最重的
    flags_for              一组 H 编码 -> 类别列表
    statements_behind      一个物质凭哪几条危险性说明归到某一类
    most_severe            一组物质里危害最重的那个
    top_hazard             一组类别里最重的那个
    SafetyService          评估一个物质：先看内置表，再查 PubChem（带缓存），再查管制清单
        assess             入口
        _from_pubchem      从缓存或 PubChem 取 H 编码
        _controlled        查管制清单
        _build             拼出评估结果
    compare_with_pubchem   内置表和 PubChem 差在哪（check-hazards 命令用）
    safety_score           一步的安全分
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from syntherisk import i18n, models
from syntherisk.external import ExternalServiceError, PubChemClient, collect_ghs, parse_ghs_codes
from syntherisk.modules import m01_structure as chem
from syntherisk.reference import Compound, ReferenceData

# ---------------------------------------------------------------- 对照表

# H 编码 -> (中文危险性说明, 评分和约束用的类别, 象形图)
# 元组里的三项：
#   中文危险性说明  证据卡、报告、排除原因里显示的文字
#   类别            评分（scoring.toml 的 [safety.penalties]）和 A11 安全要求用的名字。
#                   None 表示这条说明只显示，不扣分，也不会让路线被排除
#   象形图          GHS 象形图的编号：GHS01 爆炸物、GHS02 火焰、GHS03 氧化性、GHS04 气瓶、GHS05 腐蚀、
#                   GHS06 骷髅、GHS07 感叹号、GHS08 健康危害、GHS09 环境。空字符串表示没有象形图
# 表里只有四位的基本编码；H360FD、H361d 这类带后缀的，由 statement_for() 按前四位来查
H_STATEMENTS: dict[str, tuple[str, str | None, str]] = {
    "H200": ("不稳定爆炸物", "explosive", "GHS01"),
    "H201": ("爆炸物；整体爆炸危险", "explosive", "GHS01"),
    "H202": ("爆炸物；严重迸射危险", "explosive", "GHS01"),
    "H203": ("爆炸物；起火、爆轰或迸射危险", "explosive", "GHS01"),
    "H204": ("起火或迸射危险", "explosive", "GHS01"),
    "H205": ("遇火可能整体爆炸", "explosive", "GHS01"),
    "H220": ("极易燃气体", "extremely_flammable", "GHS02"),
    "H221": ("易燃气体", "highly_flammable", "GHS02"),
    "H222": ("极易燃气溶胶", "extremely_flammable", "GHS02"),
    "H224": ("极易燃液体和蒸气", "extremely_flammable", "GHS02"),
    "H225": ("高度易燃液体和蒸气", "highly_flammable", "GHS02"),
    "H226": ("易燃液体和蒸气", "flammable", "GHS02"),
    "H228": ("易燃固体", "flammable", "GHS02"),
    "H240": ("加热可能爆炸", "explosive", "GHS01"),
    "H241": ("加热可能起火或爆炸", "explosive", "GHS01"),
    "H242": ("加热可能起火", "flammable", "GHS02"),
    "H250": ("暴露在空气中会自燃", "pyrophoric", "GHS02"),
    "H251": ("自热；可能燃烧", "flammable", "GHS02"),
    "H252": ("数量大时自热；可能燃烧", "flammable", "GHS02"),
    "H260": ("遇水放出可自燃的易燃气体", "water_reactive", "GHS02"),
    "H261": ("遇水放出易燃气体", "water_reactive", "GHS02"),
    "H270": ("可引起或加剧燃烧；氧化剂", "oxidizer", "GHS03"),
    "H271": ("可引起燃烧或爆炸；强氧化剂", "oxidizer", "GHS03"),
    "H272": ("可加剧燃烧；氧化剂", "oxidizer", "GHS03"),
    "H280": ("内装加压气体；遇热可能爆炸", None, "GHS04"),
    "H281": ("内装冷冻气体；可能造成低温灼伤", None, "GHS04"),
    "H290": ("可能腐蚀金属", None, "GHS05"),
    "H300": ("吞咽致命", "acute_toxic_12", "GHS06"),
    "H301": ("吞咽会中毒", "acute_toxic_3", "GHS06"),
    "H302": ("吞咽有害", None, "GHS07"),
    "H304": ("吞咽并进入呼吸道可能致命", None, "GHS08"),
    "H310": ("皮肤接触致命", "acute_toxic_12", "GHS06"),
    "H311": ("皮肤接触会中毒", "acute_toxic_3", "GHS06"),
    "H312": ("皮肤接触有害", None, "GHS07"),
    "H314": ("造成严重皮肤灼伤和眼损伤", "corrosive", "GHS05"),
    "H315": ("造成皮肤刺激", None, "GHS07"),
    "H317": ("可能造成皮肤过敏反应", "sensitizer", "GHS07"),
    "H318": ("造成严重眼损伤", None, "GHS05"),
    "H319": ("造成严重眼刺激", None, "GHS07"),
    "H330": ("吸入致命", "acute_toxic_12", "GHS06"),
    "H331": ("吸入会中毒", "acute_toxic_3", "GHS06"),
    "H332": ("吸入有害", None, "GHS07"),
    "H334": ("吸入可能导致过敏、哮喘症状或呼吸困难", "sensitizer", "GHS08"),
    "H335": ("可能造成呼吸道刺激", None, "GHS07"),
    "H336": ("可能造成昏昏欲睡或眩晕", None, "GHS07"),
    "H340": ("可能导致遗传性缺陷", "cmr_1", "GHS08"),
    "H341": ("怀疑可导致遗传性缺陷", "cmr_2", "GHS08"),
    "H350": ("可能致癌", "cmr_1", "GHS08"),
    "H351": ("怀疑会致癌", "cmr_2", "GHS08"),
    "H360": ("可能对生育能力或胎儿造成伤害", "cmr_1", "GHS08"),
    "H361": ("怀疑对生育能力或胎儿造成伤害", "cmr_2", "GHS08"),
    "H362": ("可能对母乳喂养的儿童造成伤害", None, ""),
    "H370": ("会损害器官", "organ_toxicity", "GHS08"),
    "H371": ("可能损害器官", None, "GHS08"),
    "H372": ("长期或反复接触会损害器官", "organ_toxicity", "GHS08"),
    "H373": ("长期或反复接触可能损害器官", None, "GHS08"),
    "H400": ("对水生生物毒性极大", None, "GHS09"),
    "H410": ("对水生生物毒性极大并具有长期持续影响", None, "GHS09"),
    "H411": ("对水生生物有毒并具有长期持续影响", None, "GHS09"),
    "H412": ("对水生生物有害并具有长期持续影响", None, ""),
    "H413": ("可能对水生生物造成长期持续的有害影响", None, ""),
    "H420": ("破坏高层大气中的臭氧，危害公众健康和环境", None, "GHS07"),
    # 下面几条是 GHS 里最轻的几档，欧盟 CLP 没有采用，但日本等来源的分类里会出现
    "H227": ("可燃液体", None, ""),
    "H303": ("吞咽可能有害", None, ""),
    "H305": ("吞咽并进入呼吸道可能有害", None, ""),
    "H313": ("皮肤接触可能有害", None, ""),
    "H316": ("造成轻微皮肤刺激", None, ""),
    "H320": ("造成眼刺激", None, ""),
    "H333": ("吸入可能有害", None, ""),
    "H401": ("对水生生物有毒", None, ""),
    "H402": ("对水生生物有害", None, ""),
}

# H 编码 -> 英文危险性说明（GHS 的标准英文措辞）。和上面那张表一一对应；紧接着的循环把
# “中文说明 → 英文说明”登记进 i18n，请求带 ?lang=en 时证据卡、报告里的说明就换成英文（见 syntherisk/i18n.py）。
# 上面的表里加了新的编码，这里也要补上英文；tests 之外没有别的地方检查，漏了的那条会照旧显示中文
_H_STATEMENTS_EN: dict[str, str] = {
    "H200": "Unstable explosive",
    "H201": "Explosive; mass explosion hazard",
    "H202": "Explosive; severe projection hazard",
    "H203": "Explosive; fire, blast or projection hazard",
    "H204": "Fire or projection hazard",
    "H205": "May mass explode in fire",
    "H220": "Extremely flammable gas",
    "H221": "Flammable gas",
    "H222": "Extremely flammable aerosol",
    "H224": "Extremely flammable liquid and vapour",
    "H225": "Highly flammable liquid and vapour",
    "H226": "Flammable liquid and vapour",
    "H228": "Flammable solid",
    "H240": "Heating may cause an explosion",
    "H241": "Heating may cause a fire or explosion",
    "H242": "Heating may cause a fire",
    "H250": "Catches fire spontaneously if exposed to air",
    "H251": "Self-heating; may catch fire",
    "H252": "Self-heating in large quantities; may catch fire",
    "H260": "In contact with water releases flammable gases which may ignite spontaneously",
    "H261": "In contact with water releases flammable gas",
    "H270": "May cause or intensify fire; oxidizer",
    "H271": "May cause fire or explosion; strong oxidizer",
    "H272": "May intensify fire; oxidizer",
    "H280": "Contains gas under pressure; may explode if heated",
    "H281": "Contains refrigerated gas; may cause cryogenic burns or injury",
    "H290": "May be corrosive to metals",
    "H300": "Fatal if swallowed",
    "H301": "Toxic if swallowed",
    "H302": "Harmful if swallowed",
    "H304": "May be fatal if swallowed and enters airways",
    "H310": "Fatal in contact with skin",
    "H311": "Toxic in contact with skin",
    "H312": "Harmful in contact with skin",
    "H314": "Causes severe skin burns and eye damage",
    "H315": "Causes skin irritation",
    "H317": "May cause an allergic skin reaction",
    "H318": "Causes serious eye damage",
    "H319": "Causes serious eye irritation",
    "H330": "Fatal if inhaled",
    "H331": "Toxic if inhaled",
    "H332": "Harmful if inhaled",
    "H334": "May cause allergy or asthma symptoms or breathing difficulties if inhaled",
    "H335": "May cause respiratory irritation",
    "H336": "May cause drowsiness or dizziness",
    "H340": "May cause genetic defects",
    "H341": "Suspected of causing genetic defects",
    "H350": "May cause cancer",
    "H351": "Suspected of causing cancer",
    "H360": "May damage fertility or the unborn child",
    "H361": "Suspected of damaging fertility or the unborn child",
    "H362": "May cause harm to breast-fed children",
    "H370": "Causes damage to organs",
    "H371": "May cause damage to organs",
    "H372": "Causes damage to organs through prolonged or repeated exposure",
    "H373": "May cause damage to organs through prolonged or repeated exposure",
    "H400": "Very toxic to aquatic life",
    "H410": "Very toxic to aquatic life with long lasting effects",
    "H411": "Toxic to aquatic life with long lasting effects",
    "H412": "Harmful to aquatic life with long lasting effects",
    "H413": "May cause long lasting harmful effects to aquatic life",
    "H420": "Harms public health and the environment by destroying ozone in the upper atmosphere",
    "H227": "Combustible liquid",
    "H303": "May be harmful if swallowed",
    "H305": "May be harmful if swallowed and enters airways",
    "H313": "May be harmful in contact with skin",
    "H316": "Causes mild skin irritation",
    "H320": "Causes eye irritation",
    "H333": "May be harmful if inhaled",
    "H401": "Toxic to aquatic life",
    "H402": "Harmful to aquatic life",
}
for _code, _english in _H_STATEMENTS_EN.items():
    i18n.register(H_STATEMENTS[_code][0], _english)

# 从重到轻。汇总条上的“最高危害”取这里排在最前面的一类。
# 每一项是 (类别, 汇总条上显示的文字)。管制的两个类别（controlled_strict、controlled_basic）不在这张表里，
# 所以它们不会成为“最高危害”
# 显示文字写成 i18n.tr("中文", "English")：表里存的仍是中文，英文登记在 i18n 里。
# “遇水放出易燃气体”和 H261 的说明是同一句中文，所以英文也用同一句
SEVERITY_ORDER = [
    ("acute_toxic_12", i18n.tr("剧毒", "Highly toxic")),
    ("cmr_1", i18n.tr("致癌、致突变或生殖毒性", "Carcinogenic, mutagenic or toxic to reproduction")),
    ("explosive", i18n.tr("易爆", "Explosive")),
    ("pyrophoric", i18n.tr("自燃", "Pyrophoric")),
    ("acute_toxic_3", i18n.tr("有毒", "Toxic")),
    ("extremely_flammable", i18n.tr("极易燃", "Extremely flammable")),
    ("water_reactive", i18n.tr("遇水放出易燃气体", "In contact with water releases flammable gas")),
    ("corrosive", i18n.tr("腐蚀", "Corrosive")),
    ("cmr_2", i18n.tr("疑似致癌或生殖毒性", "Suspected carcinogen or reproductive toxicant")),
    ("oxidizer", i18n.tr("氧化剂", "Oxidizer")),
    ("highly_flammable", i18n.tr("易燃", "Flammable")),
    ("organ_toxicity", i18n.tr("损害器官", "Organ toxicity")),
    ("flammable", i18n.tr("易燃", "Flammable")),
    ("sensitizer", i18n.tr("致敏", "Sensitizer")),
]
# 类别 -> 名次（0 最重）。enumerate 给出 (序号, 元素)，这里只要元素里的类别名
_SEVERITY_RANK = {flag: rank for rank, (flag, _) in enumerate(SEVERITY_ORDER)}
# 类别 -> 显示文字
_SEVERITY_LABEL = dict(SEVERITY_ORDER)

# 同一种危害从重到轻的几档。几个来源的分类合在一起时，同一种危害可能报了两档
# （比如统一分类写“皮肤接触会中毒”，多数企业报“皮肤接触致命”），只留最重的。
# H314（皮肤灼伤和眼损伤）已经包含了眼损伤，所以也排在 H318、H319 前面。
# 每个元组是一种危害：吞咽（H300 致命 > H301 会中毒 > H302 有害）、皮肤接触、吸入、皮肤、眼、
# 易燃气体、易燃液体、自热、遇水放出易燃气体、氧化性、致突变、致癌、生殖毒性、
# 损害器官（一次接触 / 反复接触）、水生环境
_TIERS = [
    ("H300", "H301", "H302"), ("H310", "H311", "H312"), ("H330", "H331", "H332"),
    ("H314", "H315"), ("H314", "H318", "H319"),
    ("H220", "H221"), ("H224", "H225", "H226"), ("H251", "H252"), ("H260", "H261"), ("H271", "H272"),
    ("H340", "H341"), ("H350", "H351"), ("H360", "H361"), ("H370", "H371"), ("H372", "H373"),
    ("H410", "H411", "H412", "H413"),
]
# 评分时同一类只按最重的一档扣一次：已经算“剧毒”的物质不再另算一次“有毒”
# 写法是 {轻的一档: 盖过它的重的一档}
_COVERED_BY = {"acute_toxic_3": "acute_toxic_12", "cmr_2": "cmr_1"}


# ---------------------------------------------------------------- H 编码和类别之间的换算


def statement_for(code: str) -> tuple[str, str | None, str]:
    """H360FD、H361d 这类带后缀的编码按前四位查。

    参数：
        code  H 编码，比如 "H225"、"H360FD"。
    返回：
        (中文危险性说明, 类别或 None, 象形图编号或空字符串)，也就是 H_STATEMENTS 里的那一项。
        表里没有的编码返回 (编码本身, None, "")：照样显示出来，但不扣分。
    谁在用：
        本文件的 flags_for()、statements_behind()、most_severe()、SafetyService._build()。
    """
    # 先按完整的编码查，查不到再按前四位查，还查不到就用一个“只有编码本身”的默认值
    return H_STATEMENTS.get(code) or H_STATEMENTS.get(code[:4]) or (code, None, "")


def strongest_per_hazard(codes: list[str]) -> list[str]:
    """同一种危害报了轻重几档时，只留最重的一档。

    参数：
        codes  H 编码列表（可以带后缀）。
    返回：
        去掉较轻的档以后的新列表，先后顺序不变。比如 ["H310", "H311"] 得到 ["H310"]。
        不是同一种危害的互不影响：["H302", "H319"] 原样返回。
    为什么需要：
        PubChem 的结果是两个来源合起来的（欧盟统一分类 + 多数企业申报），同一种危害可能一个报“会中毒”、
        一个报“致命”。
    算法：
        _TIERS 里每一组是同一种危害从重到轻的几档。对每一组，看列表里出现了其中哪几档：
        除了最重的那一档，其余的都记为“较轻”；最后把较轻的去掉。比较时只看编码的前四位。
    谁在用：
        SafetyService._from_pubchem()、compare_with_pubchem()；tests/test_scoring.py。
        只用在从 PubChem 取来的编码上；内置表（compounds.csv）里写的编码不经过它。
    """
    # 列表里出现了哪些编码（只看前四位：H360FD 算 H360）
    present = {code[:4] for code in codes}
    weaker: set[str] = set()
    for tiers in _TIERS:
        # 这一组里出现了的几档（保持从重到轻）；[1:] 去掉第一个，也就是最重的，剩下的都是要去掉的
        weaker.update([tier for tier in tiers if tier in present][1:])
    return [code for code in codes if code[:4] not in weaker]


def flags_for(codes: list[str]) -> list[str]:
    """一组 H 编码 -> 评分和约束用的类别列表，从重到轻排。

    参数：
        codes  H 编码列表。
    返回：
        类别名的列表，不重复，比如 ["acute_toxic_12", "corrosive"]；没有对应类别时是空列表。
        同一类里已经有重的一档时，轻的一档不再列（见 _COVERED_BY）：
        有 acute_toxic_12 就不列 acute_toxic_3，有 cmr_1 就不列 cmr_2。
    谁在用：
        SafetyService._build()；tests/test_scoring.py。
    """
    # 每个编码对应的类别（statement_for 返回值的第二项），放进集合里去重。里面可能有 None
    flags = {statement_for(code)[1] for code in codes}
    # 集合相减：去掉已经被更重的一档盖过的类别
    flags -= {flag for flag, stronger in _COVERED_BY.items() if stronger in flags}
    # 去掉 None，按名次排（名次小的重）；万一有不在 SEVERITY_ORDER 里的类别，排在最后
    return sorted((f for f in flags if f), key=lambda f: _SEVERITY_RANK.get(f, 99))


def statements_behind(hazard: dict[str, Any], flag: str) -> str:
    """这个物质凭哪几条危险性说明归到这一类，比如“H310 皮肤接触致命”。

    参数：
        hazard  一个物质的评估结果（SafetyService.assess 返回的字典）。
        flag    类别名，比如 "acute_toxic_12"。
    返回：
        “编码 说明”用顿号连起来的一串文字；没有对应的说明时是空字符串。
    谁在用：
        m10_scoring.exclusion_reasons()：写“第 2 步用到剧毒物质 X（H310 皮肤接触致命）”这样的排除原因。
    """
    # 每一段“编码 说明”各自经过 i18n.tr（说明有英文对照，英文那段会自动换上），再用 i18n.join 连起来：
    # 这样调用方把整串当参数拼进排除原因时，英文句子里用的就是英文说明
    return i18n.join([
        i18n.tr("{code} {text}", "{code} {text}", code=s["code"], text=s["text"])
        for s in hazard["statements"] if statement_for(s["code"])[1] == flag
    ])


def most_severe(hazards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """一组物质里危害最重的那一个，以及它最重的那条危险性说明。

    参数：
        hazards  一组物质的评估结果（assess 返回的字典）的列表。
    返回：
        {"name": 物质名, "statement": 它最重那一类对应的危险性说明, "flag": 类别}；
        这些物质都没有能排上名次的危害时返回 None。
    谁在用：
        m11_explain.py 写一步的说明时（“安全方面要注意 X：……”）。
    """
    # 只看有“最重类别”的物质（top_flag 是 None 的不在 _SEVERITY_RANK 里）
    ranked = [h for h in hazards if h.get("top_flag") in _SEVERITY_RANK]
    if not ranked:
        return None
    # 名次最小的就是最重的
    worst = min(ranked, key=lambda h: _SEVERITY_RANK[h["top_flag"]])
    # next(生成器, 默认值)：取第一条属于这个类别的危险性说明；一条都没有就用类别的显示文字
    statement = next(
        (s["text"] for s in worst["statements"] if statement_for(s["code"])[1] == worst["top_flag"]), worst["top_label"]
    )
    return {"name": worst["name"], "statement": statement, "flag": worst["top_flag"]}


def top_hazard(flags: list[str]) -> tuple[str, str] | None:
    """一组类别里最重的那个，和它的显示文字。

    参数：
        flags  类别名列表。里面不在 SEVERITY_ORDER 里的（比如 controlled_strict）会被忽略。
    返回：
        (类别, 显示文字)，比如 ("cmr_1", "致癌、致突变或生殖毒性")；没有能排上名次的类别时返回 None。
    谁在用：
        SafetyService._build()（一个物质的最高危害）；
        m10_scoring._summary()（R7 汇总条上整条路线的最高危害）；tests/test_scoring.py。
    """
    ranked = [f for f in flags if f in _SEVERITY_RANK]
    if not ranked:
        return None
    # _SEVERITY_RANK.__getitem__ 就是“按字典查名次”这个函数，min 用查到的名次比大小
    worst = min(ranked, key=_SEVERITY_RANK.__getitem__)
    return worst, _SEVERITY_LABEL[worst]


# ---------------------------------------------------------------- 评估一个物质


class SafetyService:
    """评估一个物质的危害和管制情况。一次分析建一个，用完就丢。

    属性：
        session         数据库会话：读写 substance_hazards 表（PubChem 结果的缓存）。
        reference       参考数据：内置试剂表（compounds）、管制清单（controlled）、scoring.toml。
        pubchem         PubChem 客户端；None 表示不查 PubChem（配置里关掉了，或者在测试里）。
        jurisdictions   要看哪些地区的管制清单，是一个集合，比如 {"CN", "US", "CA", "TW"}。
        min_share       企业申报的分类至少要占全部申报的百分之几才采用。来自 scoring.toml 的
                        [safety] pubchem_min_share，现在是 30。
        _cache          本对象自己的缓存 {key14: 评估结果}：同一个物质在一次分析里出现多次，只评估一次。
        pubchem_failed  这次 PubChem 是不是出过错（连不上、超时、被限流）。一旦是 True，这个对象就不再去问
                        PubChem，剩下的物质都按“没有数据”处理；流水线据此在结果里加一条提醒。
    """

    def __init__(
        self,
        session: Session,
        reference: ReferenceData,
        pubchem: PubChemClient | None,
        jurisdictions: list[str],
    ):
        """记下要用的东西，建一个空缓存。不查库，不联网。

        参数：
            session        数据库会话。
            reference      参考数据（AppContext.reference）。
            pubchem        PubChem 客户端（AppContext.pubchem），可以是 None。
            jurisdictions  地区代码的列表（大写），来自配置 CONTROLLED_JURISDICTIONS，默认是 CA、US、CN、TW。
        谁在用：
            pipeline.py 的 _safety；tests/test_integrations.py、tests/test_reference.py。
        """
        self.session = session
        self.reference = reference
        self.pubchem = pubchem
        self.jurisdictions = set(jurisdictions)
        # scoring.toml 里没有 [safety] 这一节、或者没有这一项时，用 30
        self.min_share = float(reference.scoring.get("safety", {}).get("pubchem_min_share", 30))
        self._cache: dict[str, dict[str, Any]] = {}
        self.pubchem_failed = False

    def assess(self, smiles: str, name: str | None = None) -> dict[str, Any]:
        """评估一个物质：有哪些危害、归哪几类、受不受管制。

        参数：
            smiles  物质的 SMILES。
            name    显示用的名字。不给就用内置表里的名字，再没有用 PubChem 上的名字，还没有就用分子式。
        返回（一个字典，本文件里叫“评估结果”）：
            key14         InChIKey 前 14 位；结构读不出来时是空字符串
            smiles        规范 SMILES（读不出来时是传进来的原样）
            name          显示名
            h_codes       H 编码列表。空列表可能是“不属于危险品”，也可能是“没有数据”，要看 data_source
            statements    [{"code": 编码, "text": 中文危险性说明}, …]
            pictograms    象形图编号的列表，去重并排好序，比如 ["GHS02", "GHS07"]
            flags         类别列表（从重到轻）；受管制时最后再加一个 controlled_strict 或 controlled_basic
            top_flag      最重的类别，没有是 None
            top_label     最重类别的显示文字，没有是 None
            controlled    管制清单里的条目，每项是 {name, jurisdiction, list, category, severity, reviewed}
            data_source   local 内置表 / pubchem / none 没有数据
            reviewed      内置表里这一行有没有人复核过；数据不是来自内置表的一律是 False
            pubchem_name  只有走了下面第 4 步的物质才有这个键：PubChem 上的名字（英文），没有是 None
        查找顺序：
            1. 结构读不出来：直接按“没有数据”返回。
            2. 这个对象已经评估过它：直接用上次的结果（这时传进来的 name 不起作用）。
            3. 内置表里有，而且 h_codes 写的不是 “?”：用内置表的分类。
            4. 其余的（内置表里没有，或者写的是 “?”）：查 PubChem，见 _from_pubchem()。
        副作用：
            第 4 步可能发网络请求，并往 substance_hazards 表里加一行缓存（flush，不提交）。
            返回的字典同时留在本对象的缓存里；调用方要往里加东西，应当先复制一份（流水线就是这样做的）。
        出错时：
            不抛异常。PubChem 出错时这个物质标成“没有数据”，并把 pubchem_failed 置为 True。
        谁在用：
            pipeline.py 的 _safety；tests/test_integrations.py、tests/test_reference.py。
        """
        record = chem.mol_record(smiles)
        if record is None:
            # 结构读不出来：什么表都没法查，如实标“没有数据”。这种结果不进缓存
            return self._build(key14="", smiles=smiles, name=name or smiles, codes=[], source="none", reviewed=False)
        if record.key14 in self._cache:
            return self._cache[record.key14]

        compound = self.reference.compounds.by_key14.get(record.key14)
        # 名字：调用方给的优先，其次是内置表里的
        known_name = name or (compound.display_name if compound else None)
        if compound is not None and compound.h_codes is not None:
            # 内置表里有明确的分类（空列表也算，表示“不属于危险品”）：直接用，不去问 PubChem
            result = self._build(
                key14=record.key14, smiles=record.smiles, name=known_name or record.formula, codes=compound.h_codes,
                source="local", reviewed=compound.reviewed,
            )
        else:
            # 内置表里没有这个物质，或者表里写的是 “?”（读进来是 None）：去查 PubChem
            codes, source, title = self._from_pubchem(record)
            result = self._build(
                key14=record.key14, smiles=record.smiles, name=known_name or title or record.formula,
                codes=codes, source=source, reviewed=False,
            )
            # 逆合成引擎给的中间体没有名字，只能显示分子式；PubChem 上有名字的话借来用（英文）
            result["pubchem_name"] = title
        self._cache[record.key14] = result
        return result

    def _from_pubchem(self, record: chem.MolRecord) -> tuple[list[str], str, str | None]:
        """返回 (H 编码, 数据来源, PubChem 上的名字)。

        参数：
            record  这个物质的 MolRecord（m01_structure.mol_record 的结果）。这里用它的 key14 和 inchikey。
        返回：
            (H 编码列表, "pubchem" 或 "none", 名字或 None)。
            "pubchem" 配空列表表示“查到了，结论是不属于危险品”；"none" 表示没有数据。
        步骤：
            1. 先看数据库里的缓存（substance_hazards 表，主键是 key14）。有就直接用，不联网。
            2. 没有 PubChem 客户端，或者这次已经出过错：返回“没有数据”，也不写缓存（以后的分析还会再试）。
            3. 按 InChIKey 查 PubChem 的记录编号（CID）；依次看前 3 条记录的 GHS 分类，用第一条有数据的；
               同一种危害报了几档的只留最重的（strongest_per_hazard）；再查这条记录的名字。
            4. 把结果存进缓存表。“PubChem 上没有这个物质”和“有这个物质但没有分类数据”也存
               （found 是 False），这样下次不用再问。缓存没有设过期时间。
        副作用：
            第 3 步发网络请求：一个新物质一般是 3 次（查编号、取 GHS 页面、取名字），最多 5 次；
            PubChem 上没有这个物质时只有 1 次。
            第 4 步往数据库加一行并 flush，不提交。
            PubChem 出错时把 self.pubchem_failed 置为 True，返回“没有数据”，不写缓存。
        谁在用：
            本类的 assess()。
        """
        # session.get(模型, 主键)：按 key14 取缓存的那一行，没有是 None
        cached = self.session.get(models.SubstanceHazard, record.key14)
        if cached is not None:
            # found 是 False 表示上次查过，PubChem 没有它的分类数据
            return list(cached.h_codes or []), ("pubchem" if cached.found else "none"), cached.name
        if self.pubchem is None or self.pubchem_failed:
            return [], "none", None
        try:
            cids = self.pubchem.cids_by_inchikey(record.inchikey)
            # 同一个结构偶尔对应几条记录，其中有的没有分类数据：往后多看两条
            # 下面一行同时给两个变量赋值：codes 先是 None；cid 先指向第一条记录（一条都没有就是 None）
            codes, cid = None, cids[0] if cids else None
            for candidate in cids[:3]:
                # ghs_codes 返回 None 表示这条记录没有分类数据；返回列表（可以是空的）表示有结论
                codes = self.pubchem.ghs_codes(candidate, self.min_share)
                if codes is not None:
                    codes, cid = strongest_per_hazard(codes), candidate
                    break
            # 取这条记录的标题当名字。properties 返回一个列表；next(…, None) 取第一项的 Title，列表是空的就得到 None
            title = next((p.get("Title") for p in self.pubchem.properties([cid])), None) if cid else None
        except ExternalServiceError:
            # 连不上就不再试了，免得每个物质都等一次超时；这次分析里查不到的物质标“没有数据”
            self.pubchem_failed = True
            return [], "none", None
        # 写缓存。codes 是 None（没有数据）时存空列表，另用 found = False 记下“没有数据”
        self.session.add(
            models.SubstanceHazard(key14=record.key14, cid=cid, name=title, h_codes=codes or [], found=codes is not None)
        )
        # flush 把这一行发给数据库但不提交；提交由调用方（流水线保存结果时）一起做
        self.session.flush()
        return codes or [], ("pubchem" if codes is not None else "none"), title

    def _controlled(self, smiles: str, key14: str) -> list[dict[str, Any]]:
        """查这个物质在管制清单里的条目，只看配置了的地区。

        参数：
            smiles  物质的 SMILES。
            key14   它的 key14；是空字符串（结构读不出来）时直接返回空列表。
        返回：
            条目的列表，每项是 reference.ControlledEntry.to_dict() 的结果：
            {name, jurisdiction（CN / US / CA / TW）, list（清单名）, category（清单里的类别）,
             severity（strict 管得严 / basic 一般管理）, reviewed（是否复核过）}。
            没有就是空列表。同一个地区、同一张清单、同一个类别只列一次。
        细节：
            除了物质本身，还会拿它的各个含碳片段去查。盐（比如麻黄素盐酸盐）写成 SMILES 是两个不相连的部分，
            这样能对上清单里的“麻黄素”。
        谁在用：
            本类的 _build()。
        """
        # 要查的 key：整个物质的，加上多组分物质里每个含碳片段的（单组分的物质没有后者）
        keys = [key14, *chem.fragment_key14s(smiles)] if key14 else []
        entries, seen = [], set()
        for key in keys:
            # reference.controlled 是 {key14: [ControlledEntry, …]}：一个物质在几个地区的清单上，就有几项
            for entry in self.reference.controlled.get(key, []):
                identity = (entry.jurisdiction, entry.list_name, entry.category)
                # 只要配置里启用的地区；同样的条目不重复列
                if entry.jurisdiction in self.jurisdictions and identity not in seen:
                    seen.add(identity)
                    entries.append(entry.to_dict())
        return entries

    def _build(self, *, key14: str, smiles: str, name: str, codes: list[str], source: str, reviewed: bool):
        """把 H 编码和管制情况拼成评估结果（各个键的含义见 assess）。

        参数（都必须用关键字传）：
            key14     InChIKey 前 14 位；结构读不出来时是空字符串。
            smiles    SMILES。
            name      显示名。
            codes     H 编码列表。
            source    数据来源：local / pubchem / none。
            reviewed  内置表里这一行有没有复核过。
        做的事：
            flags_for() 把编码归成类别；查管制清单，有管得严的条目就加上 controlled_strict，
            只有一般管理的条目就加上 controlled_basic（两个只加一个）；再取最重的类别。
        谁在用：
            本类的 assess()。
        """
        flags = flags_for(codes)
        controlled = self._controlled(smiles, key14)
        # 管制的类别加在列表最后。它们不参与“最高危害”的比较，但 M10 会按它们扣分，勾了“排除管制化学品”时
        # 也是看 controlled_strict
        if any(entry["severity"] == "strict" for entry in controlled):
            flags.append("controlled_strict")
        elif controlled:
            flags.append("controlled_basic")
        worst = top_hazard(flags)
        return {
            "key14": key14,
            "smiles": smiles,
            "name": name,
            "h_codes": codes,
            "statements": [{"code": c, "text": statement_for(c)[0]} for c in codes],
            # 各个编码的象形图放进集合去重，减去 {""}（没有象形图的），再排序成列表
            "pictograms": sorted({statement_for(c)[2] for c in codes} - {""}),
            "flags": flags,
            "top_flag": worst[0] if worst else None,
            "top_label": worst[1] if worst else None,
            "controlled": controlled,
            "data_source": source,  # local / pubchem / none
            "reviewed": reviewed,
        }


# ---------------------------------------------------------------- 复核内置表、算安全分


def compare_with_pubchem(compound: Compound, pubchem: PubChemClient, min_share: float) -> dict[str, Any] | None:
    """内置表里一个物质的危害分类和 PubChem 上的差在哪。PubChem 没有它的分类数据时返回 None。

    复核内置表用的（python -m syntherisk.cli check-hazards），分析流程不调它。

    参数：
        compound   内置表里的一行（reference.Compound）。
        pubchem    PubChem 客户端。check-hazards 命令用的是一个放得很慢的（3 秒一次请求）。
        min_share  企业申报的分类至少占全部申报的百分之几才采用（同 SafetyService.min_share）。
    返回：
        有数据时是一个字典：
            cid           用的是 PubChem 的哪条记录
            pubchem       按我们的规则从 PubChem 取到的 H 编码（同一种危害只留最重的）
            harmonized    其中欧盟统一分类给出的编码
            echa_reports  企业申报有多少份；页面上没有申报汇总时是 None
            missing       PubChem 有、表里没有的编码
            extra         表里有、PubChem 没有的编码
        算 missing 和 extra 时只比编码的前四位（H360FD 和 H360 算同一个）。
        表里写 “?” 的物质按“表里一个编码都没有”来比。
    副作用：
        发网络请求：按结构查记录编号 1 次，再逐条取 GHS 页面（最多看 3 条记录）；
        都没有数据、而表里有 CAS 号时，再按 CAS 号查 1 次，看最多 3 条没看过的记录。不碰数据库。
    抛出：
        ExternalServiceError  PubChem 连不上或者被限流。命令行那边接住以后停下来，并告诉用户从哪里接着查。
    谁在用：
        cli.py 的 cmd_check_hazards。
    """
    record = chem.mol_record(compound.smiles)
    # 已经看过的记录编号。按结构和按 CAS 号查到的常常是同一条记录，不重复请求
    seen: list[int] = []

    def first_with_data(cids: list[int]) -> dict[str, Any] | None:
        """在这些记录里找第一条有 GHS 分类数据的，返回它和表里的差异；都没有数据时返回 None。"""
        # 没看过的记录里取前 3 条
        for cid in [c for c in cids if c not in seen][:3]:
            seen.append(cid)
            page = pubchem.ghs_page(cid)
            # collect_ghs 把页面按来源拆开；found 是 False 表示这一页没有任何分类结论
            ghs = collect_ghs(page) if page else None
            if ghs and ghs["found"]:
                theirs = strongest_per_hazard(parse_ghs_codes(page, min_share) or [])
                ours = compound.h_codes or []
                return {
                    "cid": cid,
                    "pubchem": theirs,
                    "harmonized": ghs["harmonized"],
                    "echa_reports": ghs["echa_reports"],
                    # {c[:4] for c in ours} 是表里各个编码的前四位；PubChem 的编码不在其中的，就是表里缺的
                    "missing": [code for code in theirs if code[:4] not in {c[:4] for c in ours}],
                    "extra": [code for code in ours if code[:4] not in {c[:4] for c in theirs}],
                }
        return None

    found = first_with_data(pubchem.cids_by_inchikey(record.inchikey))
    # 按结构查到的记录没有分类数据时，才再按 CAS 号查一次（硼氢化钠就是这样）；能少问就少问，PubChem 会限流
    if found is None and compound.cas:
        found = first_with_data(pubchem.cids_by_name(compound.cas))
    return found


def safety_score(hazards: list[dict[str, Any]], penalties: dict[str, float]) -> tuple[float, list[dict[str, Any]]]:
    """一步的安全分：最重的一项全额扣分，后面的依次减半。返回 (分数, 扣分明细)。

    参数：
        hazards    这一步用到的各个物质的评估结果（assess 返回的字典）的列表。这里只用其中的 name 和 flags。
        penalties  每个类别扣多少分，来自 data/scoring.toml 的 [safety.penalties]，比如 acute_toxic_12 = 40。
                   表里没有的类别、扣分是 0 的类别不参与。
    返回：
        分数      0–100，一位小数；没有要扣分的类别时是 100.0。
        扣分明细  列表，按扣分从多到少排，每项是
                  {"flag": 类别, "substances": [带这个类别的物质名], "penalty": 实际扣的分（一位小数）}。
    算法：
        按类别算，不按物质算：两个物质都易燃，只扣一次。
        把出现了的类别按配置的扣分从大到小排，第 1 个扣全额，第 2 个扣一半，第 3 个扣四分之一，依次减半。
        比如剧毒 40、极易燃 15、腐蚀 10：扣 40 + 15 ÷ 2 + 10 ÷ 4 = 50，得 50 分。
    注意：
        这里不管“有没有数据”：物质都查不到危害时这里算出来是 100 分，
        由调用它的 m10_scoring.base_dimensions() 改成“没有评估”。
    谁在用：
        m10_scoring.base_dimensions()；tests/test_scoring.py。
    """
    # 类别 -> 带这个类别的物质名（不重复，保持出现的先后）
    substances_by_flag: dict[str, list[str]] = {}
    for hazard in hazards:
        for flag in hazard["flags"]:
            # 配置里没有这个类别、或者扣分是 0：不算
            if float(penalties.get(flag, 0)) > 0:
                # setdefault：第一次遇到这个类别时先放一个空列表，然后往里加名字
                names = substances_by_flag.setdefault(flag, [])
                if hazard["name"] not in names:
                    names.append(hazard["name"])
    # 按配置的扣分从大到小排（key 里取负数，大的就排到前面）
    ranked = sorted(substances_by_flag, key=lambda flag: -float(penalties[flag]))
    total, deductions = 0.0, []
    for position, flag in enumerate(ranked):
        # 2**position 是 2 的 position 次方：第 0 个除以 1，第 1 个除以 2，第 2 个除以 4……
        applied = float(penalties[flag]) / (2**position)
        total += applied
        deductions.append(
            {"flag": flag, "substances": substances_by_flag[flag], "penalty": round(applied, 1)}
        )
    # 扣得再多也不低于 0 分
    return max(0.0, round(100 - total, 1)), deductions
