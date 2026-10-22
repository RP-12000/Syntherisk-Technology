"""M10 评分与排序、M5 证据定级的算法。

这个文件测的是什么
    不走接口，直接调算法函数，核对算出来的数字和等级。按文件里的先后分三块：
    1. M10 评分与排序（syntherisk/modules/m10_scoring.py）
           route_score         路线分 = 0.6 × 各步平均分 + 0.4 × 最弱一步
           weighted_score      六个维度按权重合成步骤分；没有数据的维度不计入
           rank_routes         排名；低置信度的路线不排在高置信度的前面（“探索”档除外）
           _route_confidence   整条路线的置信度和理由
    2. M6 毒性与安全里和评分有关的换算（syntherisk/modules/m06_safety.py）
           safety_score            一步的安全分：最重的一类危害全额扣分，其余依次减半
           flags_for、top_hazard   H 编码 → 危害类别；一组类别里最重的一个
           strongest_per_hazard    同一种危害报了轻重两档时只留重的
    3. M5 文献先例（syntherisk/modules/m05_precedents.py）
           parse_yield、document_url   导入先例时读产率、拼原文链接
           PrecedentSearch.find        给一步反应找先例、定证据等级、汇总文献产率
       判断“先例和这一步是不是同类反应”有两种办法，各有一组测试：
           按指纹      这一步没有原子映射时用（“证据等级”一段）
           按反应中心  这一步和先例都带原子映射时用（“按反应中心判断同类反应”一段）
    这些结果在界面上的去处：步骤分、路线分、置信度在结果页的路线卡片（R4）上；证据等级、先例列表、
    文献产率在证据卡（R6、C1、C2）上。公式的说明见 docs/API.md “评分公式”，参数都在 data/scoring.toml。

读这个文件要先知道的几个词（完整的说明在 m05_precedents.py 和 m01_structure.py 的文件头）
    SMILES      用一行文字写一个分子。反应写成 “反应物>试剂>产物”，同一栏里的几个分子用 “.” 隔开；
                中间一栏空着就是没写试剂
    证据等级    E1 文献直接报道：库里有反应物、产物都相同的记录
                E2 高相似先例：有同类反应的先例，底物相似度 ≥ 0.7
                E3 低相似先例：同类反应但底物差得较远；或者只找到做法不同的相关反应
                E4 仅模型预测：找不到可参考的先例
    底物相似度  两步反应的产物结构有多像，0 到 1（结构指纹的 Tanimoto 相似度）
    原子映射    反应 SMILES 里原子后面的编号，比如 [CH3:1]。反应物和产物里编号相同的是同一个原子，
                由此看得出每个原子从哪来、到哪去
    反应中心    根据原子映射列出来的“这一步到底改了哪些键”，写成一串文字，比如
                F:C.acyl~N.ar:1|L:C.acyl~Cl:1：酰基碳和芳环上的氨基氮之间成了一根单键，酰基碳上掉了一个氯。
                写法见 m01_structure.py 的 reaction_center

夹具和辅助函数
    ctx        来自 tests/conftest.py：每个测试一个新的临时数据库，所以先例库一开始是空的。
               这里用它的三样东西：session_factory（开数据库会话）、reference（data/ 下的参考数据）、
               precedent_index（先例库的内存索引）
    _route()   造一条只带排名要用的字段的假路线
    _seed()    往这个测试的先例库里放几条现编的先例
    _search()  建一个先例检索器
    _find()    用一步带原子映射的反应去检索
    本文件没有替身类。先例都是现编的，文献号以 TEST- 开头。

本文件里有什么
    test_route_score_matches_the_worked_example_in_the_deck
        路线分的公式，用 PPT 附录 A 的例子核对
    test_step_score_skips_dimensions_without_data
        步骤分是加权平均；没有数据的维度不计入，不是按 0 分算
    test_low_confidence_routes_never_rank_above_high_confidence_ones
        排名时低置信度的路线不排在高置信度的前面；“探索”档只按分数排
    test_route_confidence_looks_at_how_many_weak_steps_and_what_share_they_are
        路线的置信度看弱的步骤有几个、占多大比例
    test_safety_score_takes_the_worst_hazard_in_full_and_halves_the_rest
        安全分：最重的一类危害全额扣，其余依次减半
    test_ghs_codes_map_to_categories_including_suffixed_ones
        H 编码换成危害类别，带字母后缀的编码也认
    test_the_same_hazard_reported_at_two_levels_counts_once_at_the_stronger_one
        同一种危害报了两档时只算重的一档
    test_yield_parsing
        从文字里读产率
    test_precedent_links_use_formats_that_open_and_are_left_out_otherwise
        专利号换成 Google Patents 能打开的写法；认不出来的文献号不给链接，不给打不开的链接
    test_empty_precedent_library_is_reported_as_not_assessed_rather_than_no_precedent
        先例库是空的时候说“没有评估”，不说“没有先例”
    test_same_reaction_in_the_library_is_e1_with_reported_yields
        库里有同一个反应的记录时是 E1，文献产率取这些记录的报道值
    test_reported_yield_of_the_same_reaction_is_summarised_over_all_records_not_just_the_listed_ones
        同一反应的记录很多时，产率按全部记录统计，不只是卡上列出来的那几条
    test_journal_and_patent_precedents_are_searched_separately_and_journals_come_first
        期刊论文和专利各自检索、各有名额，卡上期刊论文那一组排在前面；分组不改变证据等级
    test_the_same_reaction_reported_in_a_journal_is_preferred_over_a_patent
        同一反应期刊和专利都报道过时，期刊那条排第一，借试剂和条件也用它
    test_a_step_using_two_molecules_of_one_reactant_matches_a_patent_that_writes_it_once
        2X + Y → Z：这一步把 X 写了两次、专利只写一次，也认作同一反应
    test_a_journal_record_that_writes_the_second_reactant_with_the_reagents_is_the_same_reaction
        期刊记录把第二种原料写在试剂栏里，也认作同一反应，只列一次
    test_a_precedent_written_with_dative_bonds_does_not_break_the_search
        库里有含配位键（“->”）的先例时，检索照常完成，不出错
    test_similar_substrate_same_reaction_type_is_graded_by_similarity
        没有同一反应时按同类反应的底物相似度定级；完全没有先例是 E4
    test_a_reaction_with_most_of_the_product_unaccounted_for_gets_no_similar_precedents
        反应物明显没给全时不找相似先例，但同一反应的记录照样算
    test_precedents_count_as_the_same_reaction_only_when_the_same_bonds_change
        按反应中心：改的键一样才是同类反应，只是成的键一样的算相关反应，成的键不同的不算先例
    test_a_different_reaction_giving_a_similar_product_is_not_a_precedent
        产物相近、反应不同的先例不能把这一步的等级顶上去
    test_a_library_imported_before_reaction_centers_existed_falls_back_to_fingerprints
        先例库里没有反应中心时退回按指纹判断
"""

import pytest

from syntherisk.modules.m05_precedents import PrecedentSearch, document_url, parse_yield, precedent_from_reaction
from syntherisk.modules.m06_safety import flags_for, safety_score, strongest_per_hazard, top_hazard
from syntherisk.modules.m10_scoring import _route_confidence, rank_routes, route_score, weighted_score

# 六个维度的权重（文献依据、操作复杂度、毒性与安全、可得性与成本、产率、绿色化学），和路线分的两个系数。
# 数值和 data/scoring.toml 的 [weights]、[route] 现在的值相同，但这里是另写的一份，不是从文件里读的：
# 前两个测试核对的是公式本身，配置文件里的数以后改了，这两个字典不会跟着变。
WEIGHTS = {"literature": 0.30, "operation": 0.20, "safety": 0.20, "availability": 0.15, "yield": 0.10, "green": 0.05}
ROUTE = {"mean_coefficient": 0.6, "weakest_coefficient": 0.4}


def test_route_score_matches_the_worked_example_in_the_deck():
    """路线分的公式：0.6 × 各步平均分 + 0.4 × 最弱一步。用 PPT 附录 A 的例子核对。

    保证的行为：syntherisk/modules/m10_scoring.py 的 route_score。分数最低的一步单独占 0.4 的分量，
    所以有一步很差的路线，其余各步再好也拉不上去多少。一步都没有分数（空列表）时返回 None，不是 0 分。
    怎么读：四步的分数是 90、88、80、48。平均分是 (90 + 88 + 80 + 48) ÷ 4 = 76.5，最低的是 48，
    路线分 = 0.6 × 76.5 + 0.4 × 48 = 45.9 + 19.2 = 65.1。
    """
    # PPT 附录 A：四步 90、88、80、48，平均 76.5，路线分 0.6 × 76.5 + 0.4 × 48 ≈ 65
    # pytest.approx(65.1)：小数运算有极小的误差，所以不直接比相等，相差在很小的范围之内就算对
    assert route_score([90, 88, 80, 48], ROUTE) == pytest.approx(65.1)
    assert route_score([], ROUTE) is None


def test_step_score_skips_dimensions_without_data():
    """步骤分是六个维度的加权平均；没有数据的维度不计入，其余的权重按比例放大，而不是把它当 0 分。

    保证的行为：m10_scoring.py 的 weighted_score：Σ(维度分 × 权重) ÷ Σ(有数据的维度的权重)。
    维度分是 None 表示“这一项没有评估”。六个维度都没有数据时返回 None（没法算分），不是 0。
    怎么读：
      六项都有数据：100×0.30 + 78×0.20 + 85×0.20 + 70×0.15 + 68×0.10 + 60×0.05
                    = 30 + 15.6 + 17 + 10.5 + 6.8 + 3 = 82.9（权重加起来是 1，不用再除）；
      产率没有数据：去掉产率那一项（6.8），剩下五项的权重加起来是 0.9，所以是 76.1 ÷ 0.9 ≈ 84.6。
                    要是把缺的产率当 0 分，会得到 76.1，比 82.9 低了一截。
    为什么重要：README 把“没有数据的维度不计分，而不是按 0 分算，也不按满分算”列为评分的规则：
    先例库没导入、没有文献产率时，这些维度不该拖低分数，也不该抬高分数。
    """
    full = {"literature": 100, "operation": 78, "safety": 85, "availability": 70, "yield": 68, "green": 60}
    assert weighted_score(full, WEIGHTS) == pytest.approx(30 + 15.6 + 17 + 10.5 + 6.8 + 3)
    # 没有产率数据：不是按 0 分算，而是把剩下的权重按比例放大
    # {**full, "yield": None}：复制 full，再把 yield 这一项换成 None
    without_yield = {**full, "yield": None}
    assert weighted_score(without_yield, WEIGHTS) == pytest.approx((30 + 15.6 + 17 + 10.5 + 3) / 0.9)
    # 六个维度全是 None
    assert weighted_score({d: None for d in WEIGHTS}, WEIGHTS) is None


def _route(route_id, score, confidence, excluded=False):
    """造一条只带四个字段的假路线：编号、路线分、置信度、是否被排除。

    rank_routes 排名时只读这四个字段（再把名次写回每条路线的 rank），所以不用真的跑一次分析。
    谁在用：下面测排名的那个测试。
    """
    return {"id": route_id, "score": score, "confidence": confidence, "excluded": excluded}


def test_low_confidence_routes_never_rank_above_high_confidence_ones():
    """排名：按路线分从高到低，但低置信度的路线不排在高置信度的前面；“探索”档只按分数排。

    保证的行为：m10_scoring.py 的 rank_routes。被排除的路线不参加排名，它的 rank 是 None。
    怎么读：四条假路线，编号就是它的置信度：low 90 分、medium 85 分、high 80 分，还有一条 99 分但已经被排除。
      “平衡”档（balanced）：先按分数排成 low、medium、high；高置信度的那条排在最后，它前面的低置信度路线
          挪到它后面，得到 medium、high、low。中置信度的路线不动，所以 85 分的 medium 仍然在 80 分的 high 前面；
      “探索”档（explore）：只按分数，low、medium、high。
    为什么重要：docs/API.md 把它写成排序规则——除“探索”档外，低置信度路线不排在高置信度路线前面。
    低置信度的路线（有几步没有文献先例，或者依据太少）分数再高，也排不到高置信度路线的前面。
    """
    routes = [_route("low", 90, "low"), _route("medium", 85, "medium"), _route("high", 80, "high"),
              _route("gone", 99, "high", excluded=True)]
    assert [r["id"] for r in rank_routes(routes, "balanced")] == ["medium", "high", "low"]
    # routes[3] 是被排除的那条：分数最高也没有名次
    assert routes[3]["rank"] is None
    # “探索”档只按分数排
    assert [r["id"] for r in rank_routes(routes, "explore")] == ["low", "medium", "high"]


def test_route_confidence_looks_at_how_many_weak_steps_and_what_share_they_are(ctx):
    """整条路线的置信度：看低置信、中置信的步骤有几个，以及它们在路线里占多大比例。

    保证的行为：m10_scoring.py 的 _route_confidence，返回 (置信度, 一句理由)。阈值在 data/scoring.toml 的
    [confidence]（这里通过 ctx.reference.scoring 读的就是那个文件）：
      - 有低置信度的步骤：达到 2 个，或者占比超过 0.34 → 低；否则 → 中；
      - 没有低置信度的步骤：中置信度的达到 2 个，或者占比超过 0.34 → 中；否则 → 高。
    占比这一条是给短路线用的：三步里有一步是 1/3 ≈ 0.333，没超过 0.34；两步里有一步是 0.5，超过了。
    理由里写明是哪几步：“无文献先例”指这一步的证据等级是 E4；“多数评分项没有数据”指这一步依据太少
    （thin：有数据的维度加起来不到 30% 的权重）。
    怎么读：里面的小函数 steps(…) 用几个三元组造出步骤列表。high 代表“有文献直接报道的一步”，
    similar 代表“只有相似先例的一步”。下面六个断言各是一种组合，从全是 high 开始，一步步变差。
    """
    scoring = ctx.reference.scoring

    def steps(*specs):
        """造一个步骤列表。每个参数是一个三元组 (这一步的置信度, 证据等级, 是否依据太少)，步骤的序号从 1 开始编。"""
        return [{"index": i, "confidence": c, "evidence_level": level, "thin": thin} for i, (c, level, thin) in enumerate(specs, 1)]

    high, similar = ("high", "E1", False), ("medium", "E3", False)
    # 三步都有文献直接报道
    assert _route_confidence(steps(high, high, high), scoring) == ("high", "每一步都有文献依据")
    # 三步里有一步只有相似先例：只有 1 个，占比 1/3，两个门槛都没到，仍然是高
    assert _route_confidence(steps(high, high, similar), scoring) == ("high", "每一步都有文献依据")
    # 短路线里一步只有相似先例，占比就够大了；一步的路线更不能算“高”
    # 两步里一步：占比 0.5；只有一步的路线：占比 1
    assert _route_confidence(steps(high, similar), scoring) == ("medium", "第 2 步只有相似先例")
    assert _route_confidence(steps(similar), scoring) == ("medium", "第 1 步只有相似先例")
    # 三步里有一步没有文献先例（E4，低置信度）：只有 1 个，占比 1/3，没到“低”的门槛，整条路线是中
    assert _route_confidence(steps(high, ("low", "E4", False), high), scoring) == ("medium", "第 2 步无文献先例")
    # 依据太少的步骤也算低置信度，原因分开写
    # 第 1 步是 E4，第 2 步是依据太少（证据等级没评估，thin 为 True）：低置信度的有 2 个，整条路线是低
    mixed = steps(("low", "E4", False), ("low", None, True), high)
    assert _route_confidence(mixed, scoring) == ("low", "第 1 步无文献先例；第 2 步多数评分项没有数据")


def test_safety_score_takes_the_worst_hazard_in_full_and_halves_the_rest():
    """一步的安全分：从 100 分往下扣，最重的一类危害全额扣，第二类扣一半，第三类扣四分之一。

    保证的行为：syntherisk/modules/m06_safety.py 的 safety_score，返回 (分数, 扣分明细)。
    按危害类别扣，不按物质扣；出现了的类别按各自的扣分从大到小排，依次减半。
    一个物质都没有时是满分 100，明细是空的。
    怎么读：扣分表是测试里自己给的三项，数值和 data/scoring.toml 的 [safety.penalties] 里这三项相同：
    剧毒（acute_toxic_12）40、极易燃（extremely_flammable）15、腐蚀（corrosive）10。
    两个物质：氟化氢带剧毒和腐蚀，乙醚带极易燃。所以 100 − 40 − 15÷2 − 10÷4 = 100 − 40 − 7.5 − 2.5 = 50。
    这个函数是 M10 算“毒性与安全”这一维时调的（m10_scoring.py 的 base_dimensions）。
    """
    penalties = {"acute_toxic_12": 40, "extremely_flammable": 15, "corrosive": 10}
    # 每个物质只需要名字和危害类别（flags）两项
    hazards = [
        {"name": "氟化氢", "flags": ["acute_toxic_12", "corrosive"]},
        {"name": "乙醚", "flags": ["extremely_flammable"]},
    ]
    score, deductions = safety_score(hazards, penalties)
    assert score == pytest.approx(100 - 40 - 15 / 2 - 10 / 4)
    # 明细按扣分从多到少排：剧毒、极易燃、腐蚀
    assert [d["flag"] for d in deductions] == ["acute_toxic_12", "extremely_flammable", "corrosive"]
    assert safety_score([], penalties) == (100.0, [])


def test_ghs_codes_map_to_categories_including_suffixed_ones():
    """GHS 的 H 编码换成评分用的危害类别；带字母后缀的编码（H360FD、H361d）也认得出来。

    保证的行为（m06_safety.py）：
      - flags_for：一组 H 编码 → 类别列表，不重复，从重到轻排。对照表是 H_STATEMENTS；
        带后缀的编码按前四位查；
      - top_hazard：一组类别里最重的一个和它显示的文字（顺序在 SEVERITY_ORDER）；空列表返回 None。
    怎么读：第一句用的是氟化氢的四个编码：H300 吞咽致命、H310 皮肤接触致命、H330 吸入致命，
    三条都属于剧毒（acute_toxic_12），只算一次；H314 造成严重皮肤灼伤，属于腐蚀（corrosive）。
    评分（安全分）和约束（“排除剧毒”等）都按类别来，不直接看编码，所以编码换错了类别，两处都会错。
    """
    assert flags_for(["H300", "H310", "H314", "H330"]) == ["acute_toxic_12", "corrosive"]
    # H360 是“可能对生育能力或胎儿造成伤害”（cmr_1），H361 是“怀疑……”（cmr_2）；后面的字母是更细的说明
    assert "cmr_1" in flags_for(["H360FD"]) and "cmr_2" in flags_for(["H361d"])
    # 高度易燃、致癌致突变生殖毒性、腐蚀三类里，最重的是致癌致突变生殖毒性
    assert top_hazard(["highly_flammable", "cmr_1", "corrosive"]) == ("cmr_1", "致癌、致突变或生殖毒性")
    assert top_hazard([]) is None


def test_the_same_hazard_reported_at_two_levels_counts_once_at_the_stronger_one():
    """同一种危害被报成轻重两档时，只算重的一档。

    保证的行为（m06_safety.py）：
      - strongest_per_hazard：一组 H 编码里，同一种危害出现了几档的只留最重的，其余的先后顺序不变。
        哪些编码是“同一种危害的几档”见 _TIERS，比如皮肤接触：H310 致命 > H311 会中毒 > H312 有害。
        它用在从 PubChem 取来的编码上（SafetyService._from_pubchem）；
      - flags_for：已经算了“剧毒”就不再另算“有毒”，已经算了 1 类致癌致突变生殖毒性就不再另算 2 类
        （_COVERED_BY）。
    为什么需要：PubChem 的分类是两个来源合起来的（欧盟统一分类，加上多数企业申报的分类），
    同一种危害可能一个来源报轻的、一个来源报重的。两档都算的话，同一件事会扣两次分。
    怎么读：第一个列表里故意放了五对“一轻一重”的编码，看留下的是哪些。
    """
    # 两个来源合起来：统一分类“皮肤接触会中毒”（H311）+ 多数企业申报“皮肤接触致命”（H310）
    # 这个列表里成对的有：H310 / H311（皮肤接触）、H314 / H318（H314 的皮肤灼伤和眼损伤已经包含了 H318 的眼损伤）、
    # H350 / H351（致癌）、H360FD / H361d（生殖毒性）、H410 / H411（水生环境，长期影响）。每一对去掉后一个。
    # H226、H301、H400 各自只有一档，原样留着
    merged = ["H226", "H301", "H310", "H311", "H314", "H318", "H350", "H351", "H360FD", "H361d", "H400", "H410", "H411"]
    assert strongest_per_hazard(merged) == ["H226", "H301", "H310", "H314", "H350", "H360FD", "H400", "H410"]
    # H302（吞咽有害）和 H319（造成严重眼刺激）不是同一种危害，互不影响
    assert strongest_per_hazard(["H302", "H319"]) == ["H302", "H319"]
    # 评分时已经算“剧毒”的不再另算一次“有毒”，致癌致突变生殖毒性同理
    # H300 是剧毒，H311 是有毒（被剧毒盖过）；H350 是 1 类致癌，H361 是 2 类生殖毒性（被 1 类盖过）
    assert flags_for(["H300", "H311", "H350", "H361"]) == ["acute_toxic_12", "cmr_1"]
    # 没有更重的一档时，轻的一档照常列出
    assert flags_for(["H301", "H351"]) == ["acute_toxic_3", "cmr_2"]


def test_yield_parsing():
    """导入先例时从文字里读出产率。

    保证的行为（syntherisk/modules/m05_precedents.py 的 parse_yield）：
      认 “85%” “~78.5 %” 这样的写法；“57 to 60%” 这样的范围取中点；
      没有数字、或者不在 0–100 之间的，返回 None（不拿一个不可信的数当产率）。
    谁在用：导入先例库时（syntherisk/loaders.py）用 parse_yield 读产率那一列。
    """
    assert parse_yield("85%") == 85
    assert parse_yield("~78.5 %") == 78.5
    # 范围取两头的平均：(57 + 60) ÷ 2 = 58.5
    assert parse_yield("57 to 60%") == 58.5
    # 空的、没有数字的（quantitative 是“定量”，没有给数）、超过 100 的，都不要
    assert parse_yield("") is None and parse_yield("quantitative") is None and parse_yield("150%") is None


def test_precedent_links_use_formats_that_open_and_are_left_out_otherwise():
    """先例的原文链接：专利号换成 Google Patents 能打开的写法；拼不出能打开的链接时不给链接。

    保证的行为（syntherisk/modules/m05_precedents.py 的 document_url、google_patents_number）：
      - USPTO 数据里的专利号去掉补位的 0 和末尾的类型代码（A、B1、B2、E1、H1……），再接到 Google Patents
        的地址后面。下面每个号码代表先例库里的一种写法，换出来的链接都在 Google Patents 上实际打开过；
      - 申请公开号（US 加 11 位数字）里的 0 不是补位，不能去掉；
      - 有 DOI 的用 doi.org 的地址；DOI 前面带着的 https://doi.org/ 先去掉，地址里有特殊含义的字符转成 %XX；
      - 认不出来的文献号（导入时编出来的 “来源-行号”、现编的 TEST-0001、美国以外的专利号）、
        不像 DOI 的 DOI、没有 DOI 的期刊论文和数据集，都返回 None，界面上就不显示链接。
    为什么重要：以前认不出来的专利号原样接在地址后面。2001 年以后授权的专利带着 B1、B2 这样的后缀，
    再版专利和法定发明登记带着 RE、H，这些链接在 Google Patents 上全是 404，占 USPTO 先例的七成多。
    """
    # 键是先例库里的写法，值是 Google Patents 地址里的写法
    patents = {
        "US03930836": "US3930836",  # 2000 年以前的授权专利：只要去掉补位的 0（以前唯一能打开的一种）
        "US06171520B2": "US6171520",  # 2001 年以后的授权专利。Google Patents 上这篇其实是 B1，带着 B2 就打不开
        "US06168655A": "US6168655",  # 2001 年初的一批，数据里写的是 A，Google Patents 上也是 B1
        "USRE037029E1": "USRE37029",  # 再版专利（2001 年以后）
        "USRE028728": "USRE28728",  # 再版专利（2001 年以前）
        "USH0000041": "USH41",  # 法定发明登记
        "USH0001943H1": "USH1943",
        "US20010000038A1": "US20010000038",  # 申请公开号：11 位数字里的 0 不是补位，留着
        "US 6,171,520 B2": "US6171520",  # 带空格、逗号的写法也认
    }
    # 三个参数依次是文献类型、文献号、DOI
    for document_id, number in patents.items():
        assert document_url("patent", document_id, None) == f"https://patents.google.com/patent/{number}"
    # 认不出来的文献号不给链接：导入时编的 “来源-行号”、现编的测试文献号、欧洲专利号、空的
    for document_id in ("uspto-17", "TEST-0001", "EP1234567A1", ""):
        assert document_url("patent", document_id, None) is None
    # DOI：直接用 doi.org 的地址；带着的前缀去掉；< 和 > 转成 %3C、%3E
    assert document_url("journal", "x", "10.1000/demo") == "https://doi.org/10.1000/demo"
    assert document_url("journal", "x", "https://doi.org/10.1000/demo") == "https://doi.org/10.1000/demo"
    assert document_url("journal", "x", "10.1002/(SICI)1:2<3::AID>4.0.CO;2-H") == (
        "https://doi.org/10.1002/(SICI)1:2%3C3::AID%3E4.0.CO;2-H")
    # 不像 DOI 的当作没有 DOI：期刊论文就没有链接了；专利还能按专利号拼
    assert document_url("journal", "x", "not-a-doi") is None
    assert document_url("patent", "US03930836", "not-a-doi") == "https://patents.google.com/patent/US3930836"
    # 没有 DOI 的期刊论文、数据集不给链接（不管文献号长什么样）
    assert document_url("journal", "x", None) is None and document_url("dataset", "US03930836", None) is None


# ---------------------------------------------------------------- 证据等级
# 下面的“先例”是测试里现编的，文献号都以 TEST- 开头，只用来验证检索和定级的逻辑。

# 这一段的测试查的大多是同一步反应：4-异丁基苯乙酮（KETONE）还原成 1-(4-异丁基苯基)乙醇（ALCOHOL），
# 也就是把酮的 C=O 变成 CH–OH。它是布洛芬演示路线（data/demo_routes/ibuprofen.json）里的“羰基还原”那一步。
# 这两个 SMILES 都不带原子映射，所以这一段走的是“按指纹判断同类反应”。
KETONE, ALCOHOL = "CC(C)Cc1ccc(cc1)C(C)=O", "CC(C)Cc1ccc(cc1)C(C)O"


def _seed(ctx, rows):
    """往这个测试的先例库（reaction_precedents 表）里放几条现编的先例。

    参数：
        ctx   conftest.py 的夹具（用它开数据库会话）。
        rows  [(反应 SMILES, 产率), …]。反应写成 “反应物>试剂>产物”；产率是百分数，没有就给 None。
    每条先例的来源都记成 uspto（没指定文献类型，按专利算），文献号按顺序编成 TEST-0001、TEST-0002……，
    年份是 2001、2002……。precedent_from_reaction（m05_precedents.py）把一条反应 SMILES 算成表里的一行：
    反应编号、两种指纹；反应带原子映射时还算出反应中心。
    注意编号每次调用都从 1 开始：同一个测试里调两次，第二次放进去的又是 TEST-0001 起。
    """
    with ctx.session_factory() as session:
        # enumerate(rows, 1) 边遍历边编号，从 1 开始。{index:04d} 把编号写成四位、不够的前面补 0
        for index, (reaction, yield_pct) in enumerate(rows, 1):
            session.add(precedent_from_reaction(reaction, source="uspto", document_id=f"TEST-{index:04d}", year=2000 + index,
                                                yield_pct=yield_pct))
        # add 只是登记，commit 才真正写进数据库
        session.commit()


def _search(ctx, session):
    """建一个先例检索器（m05_precedents.py 的 PrecedentSearch），和流水线第 4 阶段建的一样。

    建的时候它会检查内存里的索引（ctx.precedent_index）是不是最新的：表里的行数或者先例库的版本号
    和上次加载时不一样，就重新加载（PrecedentIndex.refresh；_seed 直接往表里加行，变的是行数）。
    所以要先 _seed、再建检索器，才查得到刚放进去的先例。
    """
    return PrecedentSearch(session, ctx.reference, ctx.precedent_index)


def test_empty_precedent_library_is_reported_as_not_assessed_rather_than_no_precedent(ctx):
    """先例库是空的时候如实说“没有评估”：等级和文献分都是 None，而不是把这一步判成“没有先例”。

    保证的行为：m05_precedents.py 的 PrecedentSearch.find 在库里一条先例都没有时，返回 db_empty = True，
    证据等级和文献分是 None，产率是“无记录”。M10 看到文献分是 None，就不把“文献依据”这一维算进步骤分。
    怎么读：这个测试什么先例都不放，直接检索 KETONE → ALCOHOL。find 的两个参数是反应物的列表和产物。
    为什么重要：README 写明，先例库里只要有数据，查不到先例的步骤就会被判成“仅模型预测”并且扣分。
    库是空的时候要是也这样判，每一步都会被判成没有先例；所以空库单独处理，界面上显示“未评估”。
    """
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([KETONE], ALCOHOL)
    assert found["db_empty"] and found["level"] is None and found["literature_score"] is None
    assert found["yield"]["source"] == "none"


def test_same_reaction_in_the_library_is_e1_with_reported_yields(ctx):
    """库里有同一个反应（反应物、产物都相同）的记录：证据等级 E1，文献产率取这些记录的报道值。

    保证的行为（m05_precedents.py 的 PrecedentSearch.find、_exact_rows、_grade、_yield_info）：
      - “同一反应”只看反应物和产物，不看试剂；
      - E1 的文献分在 90–100 之间（data/scoring.toml 的 [evidence.levels.E1]），同一反应的记录越多越高，
        3 条封顶；
      - 同一反应的先例，有产率的排前面、产率高的在前，按这个顺序编号 P1、P2……；
      - 产率汇总只用有产率的记录，给出最低、最高、中位数、条数和一句显示用的文字。
    怎么读：放四条先例。前三条都是 KETONE → ALCOHOL，只是试剂不同：硼氢化钠和甲醇（产率 92%）、
    乙醇（85%）、没写试剂也没有产率；第四条是不相干的酯化（乙酸 + 甲醇 → 乙酸甲酯）。然后检索 KETONE → ALCOHOL。
    """
    _seed(ctx, [
        (f"{KETONE}>[Na+].[BH4-].CO>{ALCOHOL}", 92.0),
        (f"{KETONE}>CCO>{ALCOHOL}", 85.0),
        (f"{KETONE}>>{ALCOHOL}", None),
        ("CC(=O)O.CO>>COC(C)=O", 70.0),
    ])
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([KETONE], ALCOHOL)
    # 前三条都算同一反应；E1 对应三颗星
    assert found["level"] == "E1" and found["stars"] == 3 and found["n_exact"] == 3
    assert found["literature_score"] == 100  # 三个直接先例：文献分满分
    exact = [p for p in found["precedents"] if p["exact"]]
    # 编号的顺序：92% 的是 P1，85% 的是 P2，没有产率的排最后是 P3
    assert [p["ref"] for p in exact] == ["P1", "P2", "P3"]
    # agents 是先例里写的试剂，按 “.” 拆开原样给出。专利数据把盐拆成离子来写：
    # 这里是钠离子、硼氢根（合起来是硼氢化钠）和甲醇
    assert exact[0]["yield_pct"] == 92.0 and exact[0]["agents"] == ["[Na+]", "[BH4-]", "CO"]
    # 有产率的是 92 和 85 两条：最低 85，最高 92，中位数 (85 + 92) ÷ 2 = 88.5，n 是 2；refs 是这两条的编号
    assert found["yield"] == {"source": "literature", "low": 85.0, "high": 92.0, "median": 88.5, "n": 2,
                              "label": "文献报道 85–92%（2 个先例）", "refs": ["P1", "P2"]}


def test_reported_yield_of_the_same_reaction_is_summarised_over_all_records_not_just_the_listed_ones(ctx):
    """同一反应的记录多于证据卡上能列的条数时，产率区间和中位数仍然按全部记录算。

    卡上只列产率最高的几条；只统计它们会把这一步的产率报高。

    保证的行为：证据卡最多列 max_precedents 条先例（data/scoring.toml 的 [evidence]，现在是 8）。
    同一反应的记录比这多时，m05_precedents.py 的 find 把库里查到的全部产率交给 _yield_info 统计，
    所以 n 可以比列出来的条数多；refs 仍然只有卡上那几条的编号。
    怎么读：放 10 条同一反应的记录，产率从 20% 到 99%。卡上列的是产率最高的 8 条
    （99、95、90、80、70、60、50、40）。10 个数的中位数是中间两个的平均 (60 + 70) ÷ 2 = 65；
    只算卡上那 8 条的话是 (70 + 80) ÷ 2 = 75，高了 10 个点。
    产率的中位数就是“产率”这一维的分数，所以报高了会直接抬高步骤分。
    """
    yields = [20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 95.0, 99.0]
    # 10 条记录的反应一样，只是产率不同；文献号是 TEST-0001 到 TEST-0010
    _seed(ctx, [(f"{KETONE}>CCO>{ALCOHOL}", value) for value in yields])
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([KETONE], ALCOHOL)
    assert found["level"] == "E1" and found["n_exact"] == 10
    assert len(found["precedents"]) == 8  # 卡上最多列 max_precedents 条，产率高的在前
    assert [p["yield_pct"] for p in found["precedents"]][:2] == [99.0, 95.0]
    # 统计用的是全部 10 条：n 是 10，区间是 20–99
    assert found["yield"]["n"] == 10 and (found["yield"]["low"], found["yield"]["high"]) == (20.0, 99.0)
    assert found["yield"]["median"] == 65.0  # 只算卡上那 8 条的话会是 75
    assert found["yield"]["label"] == "文献报道 20–99%（10 个先例）" and len(found["yield"]["refs"]) == 8


def _add_journal(ctx, reaction, *, document_id, yield_pct=None):
    """往先例库里放一条期刊论文的先例（带 DOI，文献类型 journal）。_seed 放的都是专利，这个补上期刊。"""
    with ctx.session_factory() as session:
        session.add(precedent_from_reaction(reaction, source="ord", doc_type="journal", document_id=document_id,
                                            doi=f"10.1000/{document_id.lower()}", year=2015, yield_pct=yield_pct))
        session.commit()


def test_journal_and_patent_precedents_are_searched_separately_and_journals_come_first(ctx):
    """期刊论文和专利分开检索、各有各的名额，证据卡上期刊论文那一组排在前面。

    保证的行为（m05_precedents.py 的 PrecedentSearch.find、_per_type、_best_ref；cards.py 的 precedent_groups）：
      - 每种文献类型各自挑候选、各自最多列 max_precedents 条（现在是 8）：专利再多、再像，也挤不掉期刊；
      - 列表里期刊论文在前，专利在后；
      - 证据等级照旧按全部先例来评，分组不改变等级；
      - 借用试剂和条件文字的“最好的先例”（best_ref）：没有同一反应时，够得上 E2 的同类先例里期刊优先；
        期刊那条够不上（底物相似度低于 0.7）时，用最像的那条专利。
    怎么读：放九条专利，都是对位取代的芳基甲基酮还原成醇，底物和这一步（4-异丁基苯乙酮还原）很像
    （产物相似度 0.72–0.93）；再放一条期刊论文，是 4-甲氧基苯乙酮的还原，没那么像（0.45）。
    分组以前卡上一共只列 8 条，这条期刊会被九条更像的专利挤掉。
    """
    patents = ["CCC(=O)c1ccc(CC(C)C)cc1", "CCCc1ccc(C(C)=O)cc1", "CCCCc1ccc(C(C)=O)cc1", "CC(C)CCc1ccc(C(C)=O)cc1",
               "CCCCCc1ccc(C(C)=O)cc1", "CCc1ccc(C(C)=O)cc1", "CC(=O)c1ccc(C)cc1", "CCC(C)c1ccc(C(C)=O)cc1",
               "CC(=O)c1ccc(C(C)C)cc1"]
    # 每个酮把 C=O 换成 C–OH 就是对应的醇
    _seed(ctx, [(f"{ketone}>>{ketone.replace('C(C)=O', 'C(C)O').replace('CC(=O)', 'CC(O)').replace('CCC(=O)', 'CCC(O)')}", None)
                for ketone in patents])
    _add_journal(ctx, "CC(=O)c1ccc(OC)cc1>>CC(O)c1ccc(OC)cc1", document_id="JOURNAL-1")
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([KETONE], ALCOHOL)

    # 最像的专利相似度 0.93，等级仍是 E2（高相似先例）：分组不影响定级
    assert found["level"] == "E2"
    types = [p["doc_type"] for p in found["precedents"]]
    # 期刊那条排第一；专利最多 8 条（九条里最不像的那条没列出来）
    assert types == ["journal"] + ["patent"] * 8
    assert found["precedents"][0]["document_id"] == "JOURNAL-1" and found["precedents"][0]["same_class"]
    # 专利组内仍按底物相似度从高到低
    patent_similarities = [p["similarity"] for p in found["precedents"][1:]]
    assert patent_similarities == sorted(patent_similarities, reverse=True)
    # 期刊那条底物相似度 0.45，够不上 E2，借用试剂时用最像的专利（P2）
    assert found["precedents"][0]["similarity"] < 0.7 and found["best_ref"] == "P2"
    from syntherisk.cards import precedent_groups

    groups = precedent_groups(found["precedents"], found["n_exact_by_type"])
    assert [(g["doc_type"], g["count"], g["n_exact"]) for g in groups] == [("journal", 1, 0), ("patent", 8, 0)]


def test_the_same_reaction_reported_in_a_journal_is_preferred_over_a_patent(ctx):
    """同一个反应，期刊论文和专利都报道过：期刊那条排前面，借用试剂和条件时用它；产率两条一起统计。

    保证的行为（m05_precedents.py 的 _exact_rows、find、_best_ref、_yield_info）：
    同一反应的记录先按文献类型排（期刊在前），同一类型里再按产率排；best_ref 指向期刊那条；
    n_exact_by_type 分开数；E1 的产率统计照旧用全部同一反应的记录。
    怎么读：专利报道 KETONE → ALCOHOL 产率 95%，期刊论文报道同一个反应产率 60%。
    """
    _seed(ctx, [(f"{KETONE}>[Na+].[BH4-]>{ALCOHOL}", 95.0)])
    _add_journal(ctx, f"{KETONE}>[Li+].[AlH4-]>{ALCOHOL}", document_id="JOURNAL-2", yield_pct=60.0)
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([KETONE], ALCOHOL)
    assert found["level"] == "E1" and found["n_exact"] == 2 and found["n_exact_by_type"] == {"journal": 1, "patent": 1}
    # 期刊那条产率低，但仍排在前面，并且是“最好的先例”
    assert [(p["ref"], p["doc_type"], p["yield_pct"]) for p in found["precedents"]] == [("P1", "journal", 60.0), ("P2", "patent", 95.0)]
    assert found["best_ref"] == "P1"
    assert found["yield"]["label"] == "文献报道 60–95%（2 个先例）"


def test_a_step_using_two_molecules_of_one_reactant_matches_a_patent_that_writes_it_once(ctx):
    """2X + Y → Z：逆合成引擎把两份 X 都列出来，专利数据里 X 只写一次，也认作同一反应（E1）。

    保证的行为：m01_structure.py 的 reaction_key(…, distinct=True) 同一种反应物只算一次；
    m05_precedents.py 的 find 用照写的和“每种原料只算一次”的两个编号一起查同一反应。
    为什么重要：专利数据不写化学计量（实测先例库里只有 0.28% 的记录把同一个反应物写了两次），
    不这样查的话，用两份原料的反应永远找不到“文献直接报道”。
    怎么读：专利写的是“苯胺 + 光气 → 1,3-二苯基脲”（苯胺只写一次，实际要两份）；
    这一步写成 [苯胺, 苯胺, 光气]。
    """
    from syntherisk.modules import m01_structure as chem

    aniline, phosgene, urea = "Nc1ccccc1", "O=C(Cl)Cl", "O=C(Nc1ccccc1)Nc1ccccc1"
    assert chem.reaction_key([aniline, aniline, phosgene], urea) != chem.reaction_key([aniline, phosgene], urea)
    assert chem.reaction_key([aniline, aniline, phosgene], urea, distinct=True) == chem.reaction_key([aniline, phosgene], urea)
    _seed(ctx, [(f"{aniline}.{phosgene}>>{urea}", 80.0)])
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([aniline, aniline, phosgene], urea)
    assert found["level"] == "E1" and found["n_exact"] == 1 and found["precedents"][0]["exact"]


def test_a_journal_record_that_writes_the_second_reactant_with_the_reagents_is_the_same_reaction(ctx):
    """期刊记录把第二种原料写在试剂栏里，逆合成引擎给的这一步把它列在反应物里：也认作同一反应（E1），只列一次。

    保证的行为：m05_precedents.py 的 _exact_rows、_reagent_side_rows。没有反应中心的先例，产物一样、
    先例反应物栏里的每一种这一步都有、这一步多出来的反应物都在先例的试剂栏里，就算同一反应。
    试剂栏里没有这种反应物的记录不算。
    为什么重要：期刊数据（academic13k）没有原子映射，常常只把主要原料写在反应物栏里。不这样认的话，
    期刊里明明报道过的同一个反应只能算相似先例，证据等级偏低，“期刊优先”也就落了空。
    怎么读：期刊记录写的是“5-碘水杨酸 > 硫酸.甲醇 > 5-碘水杨酸甲酯”（Fischer 酯化），这一步是
    “5-碘水杨酸 + 甲醇 → 5-碘水杨酸甲酯”。另一条记录用三甲基硅基重氮甲烷做甲酯，试剂栏里没有甲醇，不算同一反应。
    """
    acid, methanol, ester = "O=C(O)c1cc(I)ccc1O", "CO", "COC(=O)c1cc(I)ccc1O"
    _add_journal(ctx, f"{acid}>OS(=O)(=O)O.{methanol}>{ester}", document_id="J-FISCHER", yield_pct=90.0)
    _add_journal(ctx, f"{acid}>C[Si](C)(C)C=[N+]=[N-]>{ester}", document_id="J-TMSCHN2")
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([acid, methanol], ester)
    assert found["level"] == "E1" and found["n_exact"] == 1
    # 第一条就是那篇 Fischer 酯化，标成同一反应；它没有在相似先例里再出现一次
    first = found["precedents"][0]
    assert first["document_id"] == "J-FISCHER" and first["exact"]
    assert [p["document_id"] for p in found["precedents"]].count("J-FISCHER") == 1
    assert not any(p["exact"] for p in found["precedents"] if p["document_id"] == "J-TMSCHN2")


def test_a_precedent_written_with_dative_bonds_does_not_break_the_search(ctx):
    """库里有含配位键的先例（铂配合物的规范写法带 “->”）时，按指纹找相似先例照常完成。

    保证的行为：m05_precedents.py 的 _similar_rows 用 chem.split_reaction_smiles 拆库里存的反应 SMILES。
    怎么读：放一条“顺铂 + 1,1-环丁烷二羧酸 → 卡铂”的先例；这一步换成二羟基二氨合铂做原料，产物还是卡铂，
    所以不是同一反应，要按指纹找相似先例，那条先例会被拿出来细比。
    为什么重要：以前这里直接 split(">")，碰到 “->” 切出四段，整次分析在第 4 阶段出错（实测 2026-10-22）。
    """
    carboplatin = "N->[Pt]1(<-N)OC(=O)C2(CCC2)C(=O)O1"
    _seed(ctx, [(f"N->[Pt](<-N)(Cl)Cl.O=C(O)C1(C(=O)O)CCC1>>{carboplatin}", 80.0)])
    with ctx.session_factory() as session:
        found = _search(ctx, session).find(["N->[Pt](<-N)(O)O", "O=C(O)C1(C(=O)O)CCC1"], carboplatin)
    assert found["level"] is not None and found["n_exact"] == 0

def test_similar_substrate_same_reaction_type_is_graded_by_similarity(ctx):
    """库里没有同一反应时，看同类反应的先例：底物够像是 E2，产率只借够像的那条；完全没有先例是 E4。

    保证的行为（这一步没有原子映射，按指纹判断同类反应；m05_precedents.py 的 find、_similar_rows、_grade、
    _yield_info；阈值在 data/scoring.toml 的 [evidence]）：
      - 只有同类反应的先例才列出来，按底物相似度从高到低排；
      - 最高的底物相似度达到 0.7（high_similarity_threshold）是 E2，文献分在 70–90 之间；
      - 产率只取同类、并且相似度达到 0.7 的先例报道的值，来源标 similar_literature；
      - 一条先例都找不到是 E4。这时文献分 = 15 +（40 − 15）× 逆合成模型给这一步的概率：
        15 是 no_precedent_score，40 是 E4 分数段的上限。
    怎么读：放三条先例（各是什么见每行后面的注释），先检索 KETONE → ALCOHOL；
    再检索一个库里根本没有同类的反应：溴苯 + 4-吡啶硼酸 → 4-苯基吡啶（一个 Suzuki 偶联），模型概率给 0.5。
    """
    _seed(ctx, [
        ("CC(C)Cc1ccc(cc1)C(CC)=O>>CC(C)Cc1ccc(cc1)C(CC)O", 88.0),  # 多一个碳的同系物
        ("CC(=O)c1ccccc1>>CC(O)c1ccccc1", 95.0),  # 苯乙酮：同类反应，但底物差得远
        ("CC(=O)O.CO>>COC(C)=O", 70.0),  # 酯化：不是同类反应
    ])
    with ctx.session_factory() as session:
        found = _search(ctx, session).find([KETONE], ALCOHOL)
        # model_probability 只在找不到任何先例（E4）时起作用
        unrelated = _search(ctx, session).find(["c1ccccc1Br", "OB(O)c1ccncc1"], "c1ccc(cc1)-c1ccncc1", model_probability=0.5)
    assert found["level"] == "E2" and found["n_exact"] == 0
    # 先例按底物相似度排：同系物在前（高相似），苯乙酮在后（同类反应，但底物差得远）；酯化不是同类反应，不列
    assert [p["document_id"] for p in found["precedents"]] == ["TEST-0001", "TEST-0002"]
    # 相似度比的是产物：同系物的产物和 ALCOHOL 只差一个碳，在 0.7 以上；
    # 1-苯乙醇少了整个异丁基，落在 0.2（low_similarity_threshold，再低就不列了）和 0.7 之间
    assert found["precedents"][0]["similarity"] > 0.7 > found["precedents"][1]["similarity"] > 0.2
    assert found["best_similarity"] == found["precedents"][0]["similarity"]
    # E2 的分数段是 70–90，相似度从 0.7 到 1.0 按比例对应
    assert 70 <= found["literature_score"] <= 90
    # 产率只取高相似先例报道的，不拿相差很远的底物的产率来凑
    # 苯乙酮那条报道的 95% 没有被用上
    assert found["yield"]["source"] == "similar_literature" and found["yield"]["refs"] == ["P1"]
    assert found["yield"]["label"] == "相似先例报道 88%（1 个先例）"

    assert unrelated["level"] == "E4" and unrelated["precedents"] == []
    # 没有先例时文献分落在 0–40：基础 15 分，加上单步模型概率带来的部分
    # 15 + (40 − 15) × 0.5 = 27.5
    assert unrelated["literature_score"] == pytest.approx(15 + 25 * 0.5)
    assert unrelated["yield"]["label"] == "无产率记录"


def test_a_reaction_with_most_of_the_product_unaccounted_for_gets_no_similar_precedents(ctx):
    """反应物没给全（比如只写了一个苯）时，反应类型比不准：不硬凑相似先例，但同一反应的记录照样算。

    保证的行为：m05_precedents.py 的 find 先数“产物比全部反应物多出几个重原子”（_unaccounted_atoms；
    重原子指氢以外的原子）。多出的超过 max_unaccounted_atoms（data/scoring.toml 的 [evidence]，现在是 4）时，
    按指纹已经没法判断这是哪一类反应，所以不找相似先例，等级是 E4，并在 basis 里写明原因。
    按反应物、产物完全相同来认的“同一反应”不受影响。
    怎么读：库里放两条先例：一条是油酸（十八碳-9-烯酸）和甲苯的傅-克烷基化，产物和布洛芬一样是芳基烷酸，
    按指纹看容易被当成相似先例；另一条是“苯 → 布洛芬”，反应物明显没写全。
    然后查三步，产物都是布洛芬（15 个重原子）：
      从甲苯（7 个重原子）出发：多出 8 个，超过 4 → 不找相似先例，E4；
      从苯出发：库里正好有反应物、产物都一样的那条记录 → 同一反应，E1；
      从对应的醛（14 个重原子）出发：氧化成酸只多 1 个氧 → 不算没给全。
    为什么重要：m05_precedents.py 的 find 里写明了这样做的原因：反应物没给全时，按指纹比不准反应类型，
    不找相似先例，免得硬凑出一个证据等级。
    """
    _seed(ctx, [
        ("CCCCCCCCC=CCCCCCCCC(=O)O.Cc1ccccc1>>CCCCCCCCCCCCCCCCC(C(=O)O)c1ccccc1C", 82.0),  # 傅-克烷基化，产物也是芳基烷酸
        ("c1ccccc1>>CC(C)Cc1ccc(cc1)C(C)C(=O)O", None),  # 库里真有人这么写
    ])
    ibuprofen = "CC(C)Cc1ccc(cc1)C(C)C(=O)O"
    with ctx.session_factory() as session:
        search = _search(ctx, session)
        from_toluene = search.find(["Cc1ccccc1"], ibuprofen)
        from_benzene = search.find(["c1ccccc1"], ibuprofen)
        oxidation = search.find(["CC(C)Cc1ccc(C(C)C=O)cc1"], ibuprofen)
    # 甲苯 → 布洛芬：产物多出 8 个原子
    assert from_toluene["level"] == "E4" and from_toluene["precedents"] == []
    assert from_toluene["basis"] == "反应物没有给全（产物多出 8 个原子），没法按反应类型找相似先例"
    assert from_benzene["level"] == "E1" and from_benzene["n_exact"] == 1
    # 醛氧化成酸只多出一个氧，不算“没给全”
    assert "没有给全" not in oxidation["basis"]


# ---------------------------------------------------------------- 按反应中心判断同类反应
# 这一步和先例都带原子映射时（AiZynthFinder 的输出、USPTO 数据都带），比的是到底改了哪些键。

# 这一段用到的分子和反应。带方括号和冒号的是原子映射，比如 [NH2:4] 是“4 号原子：一个带两个氢的氮”。
# 两边编号相同的原子是同一个原子，所以能看出哪根键是新成的、哪个基团掉了。
#
# 目标产物都是对乙酰氨基酚（扑热息痛）：
#   PARACETAMOL          不带映射的写法
#   MAPPED_PARACETAMOL   同一个分子，每个原子带映射号：1–3 号是乙酰基（CH3–C=O），4 号是氮，5–11 号是苯环和酚羟基
#   AMINOPHENOL          对氨基苯酚，编号和上面对得上
#
# “这一步”有两种做法，产物相同，成的键不同（逆合成引擎的写法是 产物>>反应物，每个原子都带映射）：
#   ACYLATION     乙酰氯 + 对氨基苯酚：酰基碳（2 号）和胺的氮（4 号）之间成键，掉下去的是氯。
#                 算出来的反应中心是 F:C.acyl~N.ar:1|L:C.acyl~Cl:1
#   N_ARYLATION   乙酰胺 + 4-溴苯酚：酰胺的氮（4 号）和苯环的碳（5 号）之间成键，掉下去的是溴。
#                 算出来的反应中心是 F:N.am~c:1|L:c~Br:1
#
# 库里的先例（专利数据的写法是 反应物>试剂>产物，掉下去的基团不带映射号）：
#   ANISIDINE、METHOXY_ACETANILIDE   对甲氧基苯胺，和它酰化以后的产物 N-(4-甲氧基苯基)乙酰胺
#   FROM_ACID_CHLORIDE   对氨基苯酚 + 丙酰氯 → N-(4-羟基苯基)丙酰胺。酰氯酰化，反应中心和 ACYLATION 一样
#   FROM_ACID            对甲氧基苯胺 + 乙酸 → N-(4-甲氧基苯基)乙酰胺。成的键和 ACYLATION 一样，
#                        但掉下去的是羟基，不是氯（反应中心 F:C.acyl~N.ar:1|L:C.acyl~OH:1）
#   BY_N_ARYLATION       乙酰胺 + 4-溴苯甲醚 → N-(4-甲氧基苯基)乙酰胺。产物和上一条相同，
#                        成的却是氮–芳环碳键，反应中心和 N_ARYLATION 一样
#   UNMAPPED_ACYLATION   乙酰氯 + 4-氨基-2-甲基苯酚 → 苯环上多一个甲基的对乙酰氨基酚。没有原子映射，算不出反应中心
PARACETAMOL = "CC(=O)Nc1ccc(O)cc1"
MAPPED_PARACETAMOL = "[CH3:1][C:2](=[O:3])[NH:4][c:5]1[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1"
AMINOPHENOL = "[NH2:4][c:5]1[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1"
# 逆合成引擎给的写法：产物>>反应物
ACYLATION = f"{MAPPED_PARACETAMOL}>>[CH3:1][C:2](=[O:3])[Cl:12].{AMINOPHENOL}"
N_ARYLATION = f"{MAPPED_PARACETAMOL}>>[CH3:1][C:2](=[O:3])[NH2:4].[c:5]1([Br:12])[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1"
ANISIDINE = "[NH2:1][c:2]1[cH:3][cH:4][c:5]([O:6][CH3:13])[cH:7][cH:8]1"
METHOXY_ACETANILIDE = "[CH3:11][C:9](=[O:10])[NH:1][c:2]1[cH:3][cH:4][c:5]([O:6][CH3:13])[cH:7][cH:8]1"
# 专利数据的写法：反应物>试剂>产物，离去基团不带映射
FROM_ACID_CHLORIDE = ("[NH2:1][c:2]1[cH:3][cH:4][c:5]([OH:6])[cH:7][cH:8]1.Cl[C:9](=[O:10])[CH2:11][CH3:12]"
                      ">>[CH3:12][CH2:11][C:9](=[O:10])[NH:1][c:2]1[cH:3][cH:4][c:5]([OH:6])[cH:7][cH:8]1")
FROM_ACID = f"{ANISIDINE}.O[C:9](=[O:10])[CH3:11]>>{METHOXY_ACETANILIDE}"
BY_N_ARYLATION = f"[CH3:11][C:9](=[O:10])[NH2:1].Br[c:2]1[cH:3][cH:4][c:5]([O:6][CH3:13])[cH:7][cH:8]1>>{METHOXY_ACETANILIDE}"
UNMAPPED_ACYLATION = "CC(=O)Cl.Cc1cc(N)ccc1O>>CC(=O)Nc1ccc(O)c(C)c1"


def _find(ctx, mapped_step):
    """用一步带原子映射的反应去检索先例，返回 PrecedentSearch.find 的结果。

    参数：
        mapped_step  逆合成引擎的写法 “产物>>反应物”，每个原子带映射号（上面的 ACYLATION 或 N_ARYLATION）。
    做的事和流水线第 4 阶段对每一步做的一样，只是这里手工准备 find 的三个输入：
      - 反应物：去掉映射号、写成规范 SMILES 的列表；
      - 产物：固定用不带映射的 PARACETAMOL；
      - center：用 m01_structure.py 的 retro_reaction_center 按原子映射算出来的反应中心。
        给了它，库里又有带反应中心的先例，find 就按反应中心判断同类反应。
    谁在用：这一段的三个测试。
    """
    # partition(">>") 在第一个 “>>” 处把字符串切成三份：前面（产物）、“>>” 本身、后面（反应物）
    product, _, reactants = mapped_step.partition(">>")
    from syntherisk.modules import m01_structure as chem

    with ctx.session_factory() as session:
        return _search(ctx, session).find(
            # parse_reaction_smiles 要的是 “反应物>>产物” 的顺序，所以先把两边调过来再交给它。
            # 它返回 (反应物, 试剂, 产物) 三个列表，都已经去掉映射号；[0] 取反应物
            [chem.canonical_smiles(smiles) for smiles in chem.parse_reaction_smiles(f"{reactants}>>{product}")[0]],
            PARACETAMOL,
            center=chem.retro_reaction_center(mapped_step),
        )


def test_precedents_count_as_the_same_reaction_only_when_the_same_bonds_change(ctx):
    """按反应中心判断同类反应：改的键完全一样才算；只是成的键一样的算相关反应；成的键不同的不算先例。

    保证的行为（m05_precedents.py 的 PrecedentSearch.find、_class_rows；比较之前的归并规则在
    m01_structure.py 的 center_class）：
      - 这一步带反应中心、库里也有带反应中心的先例时，结果里的 compared_by 是 center；
      - 反应中心和这一步一样的先例是同类反应：same_class = true，class_similarity = 1.0；
      - 成的键一样、掉下去的基团不一样的（先例用羧酸，这一步用酰氯）是相关反应：列出来，
        但 same_class = false，class_similarity 在 0 和 1 之间；
      - 产物相近、成的键不同的（N-芳基化对酰化）不列；
      - 库里没带原子映射的先例只能按指纹比：列出来当旁证，compared_by 是 fingerprint，same_class = false；
      - 证据等级和产率只看同类反应的先例。
    怎么读：库里放四条先例，文献号依次是 TEST-0001 酰氯酰化、TEST-0002 羧酸酰化、TEST-0003 N-芳基化、
    TEST-0004 没带映射的酰化（各是什么反应见上面常量的注释）。先用 ACYLATION 这一步去查，逐条看四条先例
    各被怎么对待；再反过来用 N_ARYLATION 去查。
    为什么重要：README 说明了只按指纹比会出什么错：指纹不看哪个原子接到了哪里，会把
    “乙酰胺 + 对溴苯酚 → 对乙酰氨基酚”（酰胺的 N-芳基化）当成苯胺酰化的同类反应。
    """
    _seed(ctx, [(FROM_ACID_CHLORIDE, 91.0), (FROM_ACID, 80.0), (BY_N_ARYLATION, 60.0), (UNMAPPED_ACYLATION, 75.0)])
    found = _find(ctx, ACYLATION)
    assert found["compared_by"] == "center"
    # 做成 {文献号: 这条先例}，下面按文献号取
    listed = {p["document_id"]: p for p in found["precedents"]}
    # 酰氯酰化的先例是同类反应；用羧酸的那条成的键一样、离去基团不一样，算相关反应；
    # N-芳基化的那条产物几乎一样，但成的不是同一根键，不列
    assert listed["TEST-0001"]["same_class"] and listed["TEST-0001"]["class_similarity"] == 1.0
    assert listed["TEST-0001"]["compared_by"] == "center"
    # class_similarity 是两边相同的变化占全部变化的比例：这一步和 TEST-0002 都有“酰基碳–氮成键”，
    # 另外各有一项不同的（掉氯、掉羟基），所以在 0 和 1 之间
    assert not listed["TEST-0002"]["same_class"] and 0 < listed["TEST-0002"]["class_similarity"] < 1
    assert "TEST-0003" not in listed
    # 没带映射的先例只能按指纹比：列出来，但只当旁证，不算同类反应
    assert listed["TEST-0004"]["compared_by"] == "fingerprint" and not listed["TEST-0004"]["same_class"]
    # 同类先例（TEST-0001）的产物只比对乙酰氨基酚多一个碳，底物相似度在 0.7 以上，所以是 E2
    assert found["level"] == "E2" and found["basis"].startswith("找到同类反应的先例")
    # 产率只借同类、高相似的那一条（91%）；相关反应的 80% 和旁证的 75% 都不用。
    # refs 里是 TEST-0001 在这次结果里的编号
    assert found["yield"] == {"source": "similar_literature", "low": 91.0, "high": 91.0, "median": 91.0, "n": 1,
                              "label": "相似先例报道 91%（1 个先例）", "refs": [listed["TEST-0001"]["ref"]]}

    # 反过来：这一步是 N-芳基化。库里同类的只有一条（底物差一个甲基）；酰化的那几条带映射的都不列
    # 同类的那条是 TEST-0003：它的产物是甲氧基，对乙酰氨基酚是羟基，差一个甲基。
    # TEST-0004 没有映射，照旧按指纹列出来当旁证
    arylation = _find(ctx, N_ARYLATION)
    by_document = {p["document_id"]: p for p in arylation["precedents"]}
    assert set(by_document) == {"TEST-0003", "TEST-0004"}
    assert by_document["TEST-0003"]["same_class"] and not by_document["TEST-0004"]["same_class"]
    # best_similarity 只看同类反应的先例
    assert arylation["best_similarity"] == by_document["TEST-0003"]["similarity"]


def test_a_different_reaction_giving_a_similar_product_is_not_a_precedent(ctx):
    """按指纹比的时候，“乙酰胺 + 4-溴苯酚 → 对乙酰氨基酚”会被苯胺酰化的先例顶成高相似；按反应中心比不会。

    保证的行为（这一步带反应中心时；m05_precedents.py 的 find、_grade）：
      - 库里只有成键不同的先例时，哪怕产物很像也不列：等级 E4，basis 是“未找到先例”，没有产率；
      - 库里另有一条没带原子映射的相近先例时：按指纹找得到它，但没法确认是不是同一种转化，只当旁证。
        等级是 E3，文献分只给 E3 分数段最下面的一段：40 +（70 − 40）× 0.3 × 相似度，也就是 40 到 49 之间
        （0.3 是 data/scoring.toml 的 related_score_share）；它的产率不借用。
    怎么读：这一步是 N_ARYLATION。第一次检索时库里只有两条带映射的酰化先例（酰氯的、羧酸的），产物都和
    对乙酰氨基酚很像；第二次检索前再放进一条没带映射的酰化先例。
    """
    _seed(ctx, [(FROM_ACID_CHLORIDE, 91.0), (FROM_ACID, 80.0)])
    found = _find(ctx, N_ARYLATION)
    assert found["compared_by"] == "center"
    assert found["level"] == "E4" and found["precedents"] == [] and found["basis"] == "未找到先例"
    assert found["yield"]["source"] == "none"

    # 库里再加一条没带映射的酰化先例：按指纹看它很像，但没法确认是同一种转化，只落在 E3 的最低一段
    # （_seed 每次都从 TEST-0001 编号，所以这一条的文献号和前面第一条重了；下面不按文献号断言，不受影响）
    _seed(ctx, [(UNMAPPED_ACYLATION, 75.0)])
    found = _find(ctx, N_ARYLATION)
    assert found["level"] == "E3" and 40 <= found["literature_score"] <= 49
    assert found["basis"] == "只找到没带原子映射的相近先例，没法确认是不是同一种转化，只当旁证"
    # 列出来的只有那一条按指纹找到的；它报道的 75% 没有被当成这一步的产率
    assert [p["compared_by"] for p in found["precedents"]] == ["fingerprint"] and found["yield"]["source"] == "none"


def test_a_library_imported_before_reaction_centers_existed_falls_back_to_fingerprints(ctx):
    """先例库是加入“反应中心”之前导入的（center 一列全是空的）：退回按指纹判断，先例照样找得到。

    保证的行为：m05_precedents.py 的 find 只有在“这一步带反应中心，并且库里至少有一条先例带反应中心”时
    才按反应中心比（看 PrecedentIndex.with_center）；否则整步按指纹比，compared_by 是 fingerprint。
    怎么读：放一条带映射的先例，再用一条 UPDATE 把表里所有先例的 center 清成空，模拟旧版本导入的库；
    然后用带反应中心的 ACYLATION 去查。
    为什么重要：README 说明了这种库的样子：status 里 precedents_with_center 是 0，要重新导入一遍。
    在重新导入之前，后端退回按指纹判断，流水线还会在结果里加一条提醒（syntherisk/pipeline.py 的 _precedents）。
    """
    from sqlalchemy import update

    from syntherisk import models

    _seed(ctx, [(FROM_ACID_CHLORIDE, 91.0)])
    with ctx.session_factory() as session:
        # update(表).values(列=值) 是一条 UPDATE 语句；没有加 where，所以改的是表里的每一行。
        # 这一句要在第一次检索之前做：索引是建检索器时才加载的；加载以后再这样改（行数不变），它发现不了
        session.execute(update(models.ReactionPrecedent).values(center=None))
        session.commit()
    found = _find(ctx, ACYLATION)
    # 按指纹比的结果可能是 E2 或 E3，这里两种都接受；要紧的是那条先例被找到了
    assert found["compared_by"] == "fingerprint" and found["level"] in ("E2", "E3")
    assert [p["document_id"] for p in found["precedents"]] == ["TEST-0001"]
