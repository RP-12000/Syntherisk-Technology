"""证据卡（R6）：把一步反应的全部依据拼成一张卡。

证据卡是 M11 写说明、M12 问答、M13 报告的唯一输入：大模型只读这张卡，
卡里的每一项都带来源编号（sources），所以说明和回答里的每句话都能附出处。

在整个流程里的位置
    证据卡不存进数据库：每次要用的时候，从存好的分析结果（Analysis.result，结构见 routes.py 的文件头）
    里取出某条路线的某一步，现拼一张。拼卡只读内存里的字典，不查库、不联网；
    只有要带结构图（with_svg=True）时才用 RDKit 画图。
    前提是这条路线已经评过分：流水线第 7 阶段之后，路线上才有 steps。

对应的按钮和接口（接口都在 api/analyses.py）
    B10 点击某一步，打开证据卡      GET  /api/analyses/{id}/routes/{route_id}/steps/{n}
    C1 文献列表、C2 相似先例        GET  …/steps/{n}/precedents（取卡上的 precedents）
    C3 替代分支                    卡上的 alternatives 字段
    C5 问一问                      POST …/steps/{n}/ask（把卡交给 M12）
    另外两处也先拼卡：流水线第 8 阶段给前几条路线的每一步写说明（pipeline.py 的 _explain），
    生成报告（modules/m13_reports.py 的 build_blocks）。

来源编号（卡上 sources 里每一项的 id，也是说明和回答里每句话引用的出处）
    ROUTE           逆合成路线本身
    P1、P2……        第几条文献先例
    RULE:<规则编号>  命中的一条条件规则（data/condition_rules.toml）；一条都没命中时只有一个笼统的 RULE
    GHS             GHS 危害分类
    STOCK           库存与在售目录
    CALC            计算出来的量（原子经济性、溶剂评级）
    SCORE           评分规则
    MODEL           逆合成模型自己的依据（不是文献证据）；这一步是模型提出来的才有
    DEMO            演示数据里的条件说明；用的是内置演示路线才有

依赖
    modules/m01_structure.py（画结构图）、modules/m10_scoring.py（六个维度的顺序、置信度的中文）、
    modules/m05_precedents.py（按文献号重新拼先例的原文链接）、reference.py（data/scoring.toml 里的权重和维度名）。

本文件里有什么
    SOURCE_MARKERS    来源编号 → 句子后面方括号里显示的字
    find_route        按路线编号在结果里找路线
    _molecule_view    把分子表里的一个分子整理成卡上的样子
    build_sources     列出这张卡的全部来源
    marker_for        一个来源编号 → 句子后面的引用标记，比如 [1]、[规则 · 气氛]
    render_sentences  把带出处的句子拼成一段带引用标记的文字
    build_card        拼证据卡（本文件的主角）
    card_facts        把证据卡精简成给大模型看的版本
"""

from __future__ import annotations

from typing import Any

from syntherisk import i18n
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m05_precedents import precedent_url
from syntherisk.modules.m10_scoring import CONFIDENCE_LABELS, DIMENSIONS
from syntherisk.reference import ReferenceData

# 来源编号 → 句子后面方括号里显示的字（marker_for 用）。这里只有固定的几种；
# 先例（P1、P2……）显示成数字，具体某条规则（RULE:xxx）显示成那条规则的名字，都不在这张表里
SOURCE_MARKERS = {
    "ROUTE": "路线", "GHS": "GHS", "STOCK": "库存", "CALC": "计算", "SCORE": "评分",
    "MODEL": "模型", "DEMO": "演示数据", "RULE": "规则",
}
# 同一张表的英文。这几个词太短，别处也当标签用（“库存”是物料的状态，“规则”“评分”是进度面板上的模块名），
# 英文各不相同，所以不登记成固定对照；marker_for() 拼方括号时再把“[库存] → [Stock]”这样的整段记下来
SOURCE_MARKERS_EN = {
    "ROUTE": "Route", "GHS": "GHS", "STOCK": "Stock", "CALC": "Calc", "SCORE": "Scoring",
    "MODEL": "Model", "DEMO": "Demo data", "RULE": "Rule",
}


def _pair(zh: str, en: str) -> str:
    """把“这段中文 → 这段英文”记进当前的对照表，返回中文。两段都已经拼好、没法写成一个模板时用。

    做法是借 i18n.tr() 带参数的写法：中文模板只取 zh，英文模板只取 en。
    """
    return i18n.tr("{zh}", "{en}", zh=zh, en=en) if zh != en else zh


def find_route(result: dict[str, Any], route_id: str) -> dict[str, Any] | None:
    """按路线编号（"r1"、"r2"……）在分析结果里找路线。

    参数：
        result    Analysis.result。
        route_id  路线编号，接口路径里的 {route_id}。
    返回：
        result["routes"] 里的那个路线字典（原对象，不是拷贝）；没有这个编号时返回 None，
        调用它的接口据此回 404。
    谁在用：
        api/analyses.py 的 _route_or_404；api/reports.py 的 create_report、preview_materials、
        record_experiment；tests/test_llm.py。
    """
    # next(生成器, 默认值)：取第一个符合条件的；一个都没有就返回默认值 None，不会抛 StopIteration
    return next((route for route in result["routes"] if route["id"] == route_id), None)


def _molecule_view(result: dict[str, Any], key: str, *, with_svg: bool) -> dict[str, Any]:
    """把分子表里的一个分子整理成证据卡上的样子。卡上的产物、反应物、试剂都用它。

    参数：
        result    Analysis.result。
        key       分子的 key14。
        with_svg  要不要带结构图。
    返回：
        一个新字典，键的含义见函数体里的注释。
    抛出：
        KeyError  分子表里没有这个 key14（正常不会发生：反应里引用的分子都登记过）。
    谁在用：
        本文件的 build_card。
    """
    molecule = result["molecules"][key]
    view = {
        # 分子编号（InChIKey 的前 14 位）
        "key14": key,
        # 名字；没有名字的中间体是 None（card_facts 里这时改用分子式）
        "name": molecule.get("name"),
        # 规范 SMILES
        "smiles": molecule["smiles"],
        # 分子式
        "formula": molecule["formula"],
        # 分子量（g/mol）
        "mol_weight": molecule["mol_weight"],
        # M4 给的可得性（C4 原料标签用），还没判断过时是 None。里面的键：
        #   status（inventory / purchasable / unavailable）、label（库存 / 可购买 / 买不到）、
        #   inventory（库存里的那一条：item_id、name、quantity、location；库存里没有是 None）、
        #   purchasable、catalog_source（在哪个在售目录里查到的：seed、zinc 这样的来源编号，
        #   engine_stock 表示只有逆合成引擎自己的清单里有）、
        #   ubiquitous（内置试剂表里标了 ubiquitous 的物质，现在只有水；这类物质不算要准备的物料）、
        #   suppliers、price、lead_time（第一版没有数据，都是 None）、price_label（“待询价”）
        "availability": molecule.get("availability"),
    }
    if with_svg:
        # 结构图：一段 SVG 文本，宽 200、高 140 像素
        view["svg"] = chem.mol_to_svg(molecule["smiles"], 200, 140)
    return view


def build_sources(result: dict[str, Any], reaction: dict[str, Any]) -> list[dict[str, Any]]:
    """列出这一步的证据卡可以引用的全部来源。

    说明（M11）和回答（M12）里每句话的出处只能从这个列表里选，后端会逐句核对
    （modules/m11_explain.py 的 validate_sentences）。

    参数：
        result    Analysis.result，这里只用 engine.name。
        reaction  反应表里这一步的字典。
    返回：
        来源列表，每一项是 {"id": 来源编号, "type": 来源类型, "label": 显示的名字}，
        先例那几项另有 "url"（专利或文献的链接；拼不出能打开的链接时是 None）。顺序固定：
        路线 → 各条先例 → 条件规则 → GHS → 库存 → 计算 → 评分 →（有的话）模型 →（有的话）演示数据。
        type 的取值：route / precedent / rule / ghs / stock / calculation / score / model / demo。
    谁在用：
        本文件的 build_card（放进卡的 sources 字段）。
    """
    engine = result["engine"]["name"]
    # 第一项总是路线本身。引擎名不是 aizynthfinder 的（现在只有内置的演示引擎 demo）都标成“演示数据”
    sources = [{
        "id": "ROUTE", "type": "route",
        "label": i18n.tr("逆合成路线（AiZynthFinder）", "Retrosynthesis route (AiZynthFinder)") if engine == "aizynthfinder"
        else i18n.tr("逆合成路线（演示数据）", "Retrosynthesis route (demo data)"),
    }]
    # 每条先例一个来源。先例的 ref 是 M5 编的 "P1"、"P2"……，去掉开头的 P 就是显示用的序号
    for precedent in (reaction.get("evidence") or {}).get("precedents") or []:
        number = precedent["ref"][1:]
        sources.append(
            {
                "id": precedent["ref"],
                "type": "precedent",
                # 形如 “先例 1 · 专利 USTEST0001”
                "label": i18n.tr("先例 {number} · {doc_type} {document_id}", "Precedent {number} · {doc_type} {document_id}",
                                 number=number, doc_type=precedent["doc_type_label"], document_id=precedent["document_id"]),
                # 按文献号重新拼，不用结果里存着的 url：旧分析存的是旧写法拼的链接，很多打不开（m05 的 precedent_url）
                "url": precedent_url(precedent),
            }
        )
    # 命中的每条条件规则一个来源：编号是 “RULE:” 加规则编号，名字用规则的类别（比如 “规则 · 气氛”）
    tags = (reaction.get("conditions") or {}).get("tags") or []
    for tag in tags:
        sources.append({"id": f"RULE:{tag['rule_id']}", "type": "rule",
                        "label": i18n.tr("规则 · {category}", "Rule · {category}", category=tag["category"])})
    # 一条规则都没命中时放一个笼统的 RULE，“没有命中需要特殊操作的规则”这类话才有出处可引
    if not tags:
        sources.append({"id": "RULE", "type": "rule", "label": i18n.tr("条件规则", "Condition rules")})
    # 下面四个每张卡都有：危害分类、可得性、计算出来的量、评分规则
    sources.append({"id": "GHS", "type": "ghs", "label": i18n.tr("GHS 危害分类", "GHS hazard classification")})
    sources.append({"id": "STOCK", "type": "stock", "label": i18n.tr("库存与在售目录", "Inventory and vendor catalogs")})
    sources.append({"id": "CALC", "type": "calculation", "label": i18n.tr("计算（原子经济性、溶剂评级）", "Calculated (atom economy, solvent rating)")})
    sources.append({"id": "SCORE", "type": "score", "label": i18n.tr("评分规则", "Scoring rules")})
    # 这一步是逆合成模型提出来的（反应上有 model 信息）才有 MODEL
    if reaction.get("model"):
        sources.append({"id": "MODEL", "type": "model", "label": i18n.tr("逆合成模型依据（不是文献证据）", "Retrosynthesis model output (not literature evidence)")})
    # 条件说明来自内置演示路线时才有 DEMO
    if (reaction.get("conditions") or {}).get("source") == "DEMO":
        sources.append({"id": "DEMO", "type": "demo", "label": i18n.tr("演示数据（教科书层面的条件概述）", "Demo data (textbook-level outline of conditions)")})
    return sources


def marker_for(source_id: str, sources: list[dict[str, Any]]) -> str:
    """一个来源编号对应的引用标记，就是句子后面方括号里的那一小段。

    参数：
        source_id  来源编号，比如 "P2"、"RULE:anhydrous_organometallic"、"SCORE"。
        sources    这张卡的来源列表（build_sources 的返回值），用来查具体规则的名字。
    返回：
        "P2" → "[2]"；
        "RULE:xxx" → "[规则 · 气氛]"（用来源列表里那条规则的名字，列表里查不到就是 "[规则]"）；
        其余的按 SOURCE_MARKERS 换成中文，比如 "SCORE" → "[评分]"；表里没有的编号原样放进方括号。
    谁在用：
        本文件的 render_sentences。
    """
    # “P” 后面全是数字的是先例，标记里只留数字
    if source_id.startswith("P") and source_id[1:].isdigit():
        return f"[{source_id[1:]}]"
    if source_id.startswith("RULE:"):
        # 来源列表里那条规则的名字（build_sources 拼的，有英文对照）；查不到就用笼统的“规则”
        label = next((s["label"] for s in sources if s["id"] == source_id), None)
        if label is None:
            return _pair(f"[{SOURCE_MARKERS['RULE']}]", f"[{SOURCE_MARKERS_EN['RULE']}]")
        return i18n.tr("[{label}]", "[{label}]", label=label)
    # 固定的几种：中文、英文各查各的表，把整个方括号记成一条对照
    return _pair(f"[{SOURCE_MARKERS.get(source_id, source_id)}]", f"[{SOURCE_MARKERS_EN.get(source_id, source_id)}]")


def render_sentences(sentences: list[dict[str, Any]], sources: list[dict[str, Any]]) -> str:
    """把带出处的句子拼成一段文字：这一步……[1][2]。操作一项……[规则 · 气氛]。

    参数：
        sentences  句子列表，每句是 {"text": 句子, "sources": [来源编号, ...]}。
                   大模型写的（已经通过出处核对）和模板生成的都是这个格式。
        sources    这张卡的来源列表，交给 marker_for 查规则的名字。
    返回：
        一整段文字。每句去掉自带的句末标点，接上它的引用标记，再统一补一个句号；
        句子之间不加空格和换行。sentences 是空列表时返回空字符串。
    谁在用：
        modules/m11_explain.py 的 Explainer._package（说明的 text 字段）；
        modules/m12_qa.py 的 QuestionAnswerer._package（回答的 answer 字段）。
    """
    parts = []
    for sentence in sentences:
        # rstrip 的参数是“一组字符”而不是一个词：把句尾的中英文句号、分号和空格都去掉，下面统一补句号
        text = sentence["text"].rstrip("。.；; ")
        # 英文那一份同样处理：句子有英文对照（模板生成的句子）就取它的英文，去掉句尾标点，
        # 再把“去掉标点的中文 → 去掉标点的英文”记下来。大模型直接写成英文的句子没有对照，两份相同
        text = _pair(text, str(i18n.en_of(sentence["text"])).rstrip("。.；; "))
        # 引用标记一个挨一个，中间不加分隔符；i18n.join 记下连起来以后的英文
        markers = i18n.join([marker_for(source_id, sources) for source_id in sentence["sources"]], "", "")
        parts.append(i18n.tr("{text}{markers}。", "{text}{markers}.", text=text, markers=markers))
    # 中文句子之间不加空格，英文句子之间空一格
    return i18n.join(parts, "", " ")


def build_card(
    result: dict[str, Any],
    route: dict[str, Any],
    step_index: int,
    reference: ReferenceData,
    *,
    with_svg: bool = False,
) -> dict[str, Any]:
    """拼出一条路线里某一步的证据卡（R6）。

    参数：
        result      Analysis.result（结构见 routes.py 的文件头）。
        route       result["routes"] 里的一条路线。必须已经评过分：要用第 7 阶段写上去的
                    steps 和 availability_details。
        step_index  第几步，从 1 开始，按实验顺序（第 1 步最先做）。调用方要先确认它在
                    1 到 len(route["steps"]) 之间。
        reference   参考数据，用 data/scoring.toml 里的权重和维度名。
        with_svg    True 时给产物、反应物、试剂各带一张结构图（SVG）。接口上是 ?svg=true，默认不带。
    返回：
        证据卡，一个可以直接转成 JSON 的字典。每个字段的含义见函数末尾 return 处的注释。
        卡里的 conditions、hazards、green、model、soft_hits 是 result 里原对象的引用，
        不是拷贝，调用方不要原地改它们。precedents 里的每一项是复制出来的（换上了重新拼的 url），
        但它里面的列表（agents）仍然是原对象。
    副作用：
        没有。不改 result，不查数据库，不联网。
    抛出：
        KeyError    路线还没评分（没有 steps）。
        IndexError  step_index 比步数大。传 0 或负数不会报错，而是按 Python 的倒数下标取到别的步骤，
                    所以要靠调用方先检查。
    谁在用：
        api/analyses.py 的 _card（证据卡、先例列表、问一问三个接口）；pipeline.py 的 _explain（第 8 阶段）；
        modules/m13_reports.py 的 build_blocks（报告）；tests/test_llm.py。
    """
    scoring = reference.scoring
    # 路线上的这一步（M10 在第 7 阶段写的）：步骤分、六个维度的分数、置信度、要准备的物料……
    step = route["steps"][step_index - 1]
    # 这一步反应本身（反应表）：反应物、产物、先例、条件、危害……几条路线共用同一条
    reaction = result["reactions"][step["key"]]
    # 权重用这份结果上一次评分时用的那一组；结果里没有就用配置文件里的
    weights = result.get("weights") or scoring["weights"]
    labels = scoring["dimension_labels"]
    # 六个维度各自的 {score, basis, sources}。文献、操作、安全、产率、绿色化学五项跟着反应走，存在反应上；
    # 可得性要看这一步在哪条路线里（它的反应物是不是这条路线的起始原料），所以存在路线上，在这里并进来。
    # dict(...) 先拷一份，免得把可得性写进几条路线共用的 reaction["dimensions"]
    details = dict(reaction.get("dimensions") or {})
    details["availability"] = route["availability_details"][step["key"]]
    evidence = reaction.get("evidence") or {}

    # 这一步要准备的物料（作为起始原料的反应物，加上已知的试剂），每项补上 M4 的完整可得性信息
    materials = []
    for material in step["materials"]:
        availability = result["molecules"][material["key14"]].get("availability") or {}
        # {**a, "k": v}：把 a 的键值都拷进一个新字典，再加上（或盖掉）k 这个键
        materials.append({**material, "availability": availability})

    # C3 替代分支：整份结果里得到同一个产物的其他做法（另一步反应），不管它在哪条路线里
    alternatives = [
        {"step_key": key, "name": other["name"], "reactants": other["reactants"]}
        for key, other in result["reactions"].items()
        if other["product"] == reaction["product"] and key != step["key"]
    ]

    return {
        # ---- 这是哪一步
        # 路线编号（"r1"……）和路线名
        "route_id": route["id"],
        "route_name": route["name"],
        # 第几步（从 1 开始）、这条路线一共几步
        "step_index": step_index,
        "step_count": len(route["steps"]),
        # 步骤编号（反应表的键）
        "step_key": step["key"],
        # 反应名；title 是卡片的标题，形如 “第 4 步 · 格氏羧基化”
        "name": reaction["name"],
        "title": i18n.tr("第 {index} 步 · {name}", "Step {index} · {name}", index=step_index, name=reaction["name"]),
        # ---- 分数和置信度
        # 步骤分（0–100，一位小数）；六个维度都没有数据时是 None
        "score": step["score"],
        # coverage 是 0–1 的小数（0.2 表示只有占 20% 权重的维度有数据）；thin 为 True 表示它低于
        # data/scoring.toml 的 min_assessed_weight，这一步按低置信度处理
        "coverage": step.get("coverage"),  # 这个分数用到了百分之多少的权重；其余的维度没有数据
        "thin": step.get("thin", False),  # 有数据的维度太少，分数参考价值有限
        # 这一步的置信度 high / medium / low，和它的中文 高 / 中 / 低
        "confidence": step["confidence"],
        "confidence_label": CONFIDENCE_LABELS[step["confidence"]],
        # ---- 文献依据（M5 的结论）
        "evidence": {
            # 证据等级 E1–E4；先例库是空的时是 None（未评估）
            "level": evidence.get("level"),
            # 等级的中文名：文献直接报道 / 高相似先例 / 低相似先例 / 仅模型预测；未评估时是一句说明
            "label": evidence.get("label"),
            # 星数 3 / 2 / 1 / 0，对应 E1–E4；未评估时是 None
            "stars": evidence.get("stars"),
            # 一句话的定级理由，比如 “2 个先例报道了同一个反应”
            "basis": evidence.get("basis"),
            # 库里报道了同一个反应（反应物、产物都相同）的先例有几条
            "n_exact": evidence.get("n_exact", 0),
            # 同类反应的先例里最高的底物相似度（0–1）；有同一反应的先例时是 1.0，没有同类先例时是 None
            "best_similarity": evidence.get("best_similarity"),
            # True：先例库还没有导入数据，所以没有评等级
            "db_empty": evidence.get("db_empty", False),
            # 判断“同类反应”用的办法：center 按反应中心（这一步带原子映射），fingerprint 按指纹（粗）
            "compared_by": evidence.get("compared_by"),
        },
        "center": reaction.get("center"),  # 这一步改了哪些键；没有原子映射时是 null
        # 先例列表，最多 data/scoring.toml 的 max_precedents 条，同一反应的排在最前。每条的字段：
        #   ref 来源编号（P1、P2……）；exact 是不是同一反应；same_class 是不是同类反应（False 是“相关反应”）；
        #   compared_by 怎么认定的（exact / center / fingerprint）；similarity 底物相似度；
        #   class_similarity 反应一致的程度；doc_type、doc_type_label、document_id、year、doi、title、url
        #   文献信息；source、source_name 数据来源；yield_pct 报道的产率（%）；reaction_smiles 先例的反应；
        #   agents 先例用的试剂（SMILES 列表）；conditions_text、temperature_c 条件；id 先例库里的行号
        # 每条复制一份，url 按文献号重新拼（m05 的 precedent_url）：旧分析里存的链接很多打不开，
        # 拼不出能打开的链接时是 None。{**precedent, "url": …} 是“原字典的全部键，再把 url 换掉”的新字典
        "precedents": [{**precedent, "url": precedent_url(precedent)}
                       for precedent in evidence.get("precedents") or []],
        # ---- 反应本身
        # “反应物>试剂>产物”形式的反应 SMILES
        "reaction_smiles": reaction["reaction_smiles"],
        # 产物、各个反应物、各个试剂，每个都是 _molecule_view 的结果（with_svg 时带结构图）
        "product": _molecule_view(result, reaction["product"], with_svg=with_svg),
        "reactants": [_molecule_view(result, key, with_svg=with_svg) for key in reaction["reactants"]],
        "reagents": [_molecule_view(result, key, with_svg=with_svg) for key in reaction["reagents"]],
        # ---- 各模块的评估结果
        # 条件与操作（M7）：text 条件说明；source 说明的出处（DEMO / 某条先例的编号 / None）；
        # temperature_c 温度；tags 命中的规则（rule_id、label、short、category、penalty、reason、
        # constraint、purification）；operation_score 操作分；operation_level 简单 / 中等 / 复杂；
        # operation_label 形如 “操作 中等 · 需无水”
        "conditions": reaction.get("conditions") or {},
        # 产率（M5 从先例里取的文献值）：source 是 literature / similar_literature / none，label 是显示的文字；
        # 有报道值时还有 low、high、median、n（几个先例）、refs（出自哪几条先例）
        "yield": reaction.get("yield") or {"source": "none", "label": i18n.tr("无产率记录", "No yield on record"), "refs": []},
        # 危害（M6）：这一步的每个反应物、试剂和产物各一项。每项的字段：
        #   role 它在这一步里的角色（reactant / reagent / product）；key14、smiles、name 是哪个物质；
        #   h_codes 危险性说明的编码；statements 编码和中文（code、text）；pictograms 象形图编号；
        #   flags 评分和约束用的危害类别；top_flag、top_label 其中最重的一类和它的中文；
        #   controlled 管制清单（name、jurisdiction、list、category、severity、reviewed）；
        #   data_source 数据来源 local / pubchem / none（none 表示没有危害数据，不等于安全）；
        #   reviewed 内置表里这一行有没有人复核过；查过 PubChem 的物质另有 pubchem_name
        "hazards": reaction.get("hazards") or [],
        # 要准备的物料：key14、name、smiles、role（reactant / reagent）、status、label，外加完整的 availability
        "materials": materials,
        # 绿色化学（M9）：atom_economy 原子经济性（%）；solvents 用到的溶剂（name、class、class_label）；
        # solvent_class 其中最差的一档；score 这一项的分数；basis 一句话的依据
        "green": reaction.get("green") or {},
        # 六个维度的明细，顺序固定：文献、操作、安全、可得性、产率、绿色化学
        "dimensions": [
            {
                # 维度的英文键：literature / operation / safety / availability / yield / green
                "key": key,
                # 中文全称，和写进句子里用的短名（都在 data/scoring.toml）
                "label": labels[key],
                "short": scoring["dimension_short_labels"][key],
                # 权重（0–1）
                "weight": float(weights.get(key, 0)),
                # 这一维的分数（0–100）；None 表示没有数据、没参加计分，不是 0 分
                "score": step["dimensions"][key],
                # 一句话的打分依据，和支持它的来源编号
                "basis": details[key]["basis"],
                "sources": details[key]["sources"],
            }
            for key in DIMENSIONS
        ],
        # 命中的软偏好（一句句中文，比如 “少用试剂：钯催化剂”）和因此扣掉的分数；步骤分已经减过这个数
        "soft_hits": step["soft_hits"],
        "soft_penalty": step["soft_penalty"],
        # 逆合成模型的依据（template_hash、library_occurrence、policy_probability、policy_rank、
        # policy_name，见 routes.py）；演示路线是 None
        "model": reaction.get("model"),
        # ---- 出处、说明、回填
        # 这张卡可以引用的全部来源，见 build_sources
        "sources": build_sources(result, reaction),
        # M11 给这一步写的说明（sentences、text、generated_by、provider、model、dropped_sentences、note）；
        # 还没写过是 None，证据卡接口这时会补写
        "explanation": (route.get("explanations") or {}).get(step["key"]),
        # 回填的实验结果摘要（record_id、outcome、actual_yield、label，见 m14_backfill.py）；没做过是 None
        "experiment": reaction.get("experiment"),
        # C3 替代分支：得到同一个产物的其他做法，每项是 {step_key, name, reactants}
        "alternatives": alternatives,
    }


def card_facts(card: dict[str, Any]) -> dict[str, Any]:
    """给大模型看的精简版证据卡：只留事实和来源编号，去掉图片和界面用的字段。

    这份字典会转成 JSON，作为用户消息发给大模型（M11 写说明、M12 问答）。后端核对模型写的句子时，
    也拿它当“卡里有哪些数字”的依据，所以这里出现的数字，就是模型可以写进句子的全部数字。
    每一项里的 source 是它的来源编号，模型引用出处时只能从 allowed_sources 里选。
    键名是写给模型看的，和证据卡本身的键名不完全一样。
    分子的结构（SMILES）不在里面：发给模型的只有名字，没有名字的给分子式。

    参数：
        card  build_card 的返回值。
    返回：
        一个新字典，键的含义见函数体里的注释。
    谁在用：
        modules/m11_explain.py 的 Explainer.explain_step；modules/m12_qa.py 的 QuestionAnswerer.answer。
    """
    return {
        # 卡片标题（“第 4 步 · 格氏羧基化”）和路线名
        "step": card["title"],
        "route": card["route_name"],
        # 步骤分，和置信度的中文（高 / 中 / 低）
        "score": card["score"],
        "confidence": card["confidence_label"],
        # 产物、反应物、试剂：只给名字，没有名字的给分子式
        "product": card["product"]["name"] or card["product"]["formula"],
        "reactants": [m["name"] or m["formula"] for m in card["reactants"]],
        "reagents": [m["name"] or m["formula"] for m in card["reagents"]],
        # 文献依据的结论。证据等级没评估（先例库是空的）时，这一条的出处记成 SCORE；
        # 评估过的话这里的 source 是 None，出处用下面各条先例自己的编号
        "evidence": {
            "source": "SCORE" if card["evidence"]["level"] is None else None,
            "level": card["evidence"]["level"],
            "label": card["evidence"]["label"],
            "basis": card["evidence"]["basis"],
            "exact_precedents": card["evidence"]["n_exact"],
        },
        # 每条先例：编号（P1……）、文献类型（专利 / 期刊论文 / 数据集）、文献号、年份、是不是同一反应、
        # 底物相似度、报道的产率（%）、条件说明
        "precedents": [
            {
                "source": p["ref"],
                "type": p["doc_type_label"],
                "document": p["document_id"],
                "year": p["year"],
                "same_reaction": p["exact"],
                "similarity": p["similarity"],
                "yield_percent": p["yield_pct"],
                "conditions": p["conditions_text"],
            }
            for p in card["precedents"]
        ],
        # 条件：source 是条件说明的出处，operation 是操作标签（“操作 中等 · 需无水”）；
        # tags 里每条命中的规则各有自己的出处 “RULE:” 加规则编号
        "conditions": {
            "source": card["conditions"].get("source"),
            "text": card["conditions"].get("text"),
            "operation": card["conditions"].get("operation_label"),
            "tags": [
                {"source": f"RULE:{t['rule_id']}", "label": t["label"], "reason": t["reason"], "penalty": t["penalty"]}
                for t in card["conditions"].get("tags") or []
            ],
        },
        # 产率：显示的文字，和它出自哪几条先例
        "yield": {"label": card["yield"]["label"], "sources": card["yield"].get("refs") or []},
        # 危害：每个物质最多给 6 条危险性说明的中文；controlled 拼成 “地区 清单 类别”；
        # no_data 为 True 表示这个物质查不到危害数据（不能当成安全）
        "hazards": [
            {"source": "GHS", "substance": h["name"], "statements": [s["text"] for s in h["statements"]][:6],
             # 清单名和类别是中文：经过 i18n.tr，要英文时（m11 / m12 把这份事实换成英文）整句才有对照
             "controlled": [i18n.tr("{jurisdiction} {list} {category}", "{jurisdiction} {list} {category}",
                                    jurisdiction=c["jurisdiction"], list=c["list"], category=c["category"])
                            for c in h["controlled"]],
             "no_data": h["data_source"] == "none"}
            for h in card["hazards"]
        ],
        # 要准备的物料：名字，和可得性的中文（库存 / 可购买 / 买不到 / 未知）
        "materials": [{"source": "STOCK", "name": m["name"], "status": m["label"]} for m in card["materials"]],
        # 绿色化学：一句话的依据（原子经济性、溶剂评级）
        "green": {"source": "CALC", "summary": card["green"].get("basis")},
        # 六个维度：中文名、权重（换成百分数并取整）、分数、依据
        "dimensions": [
            {"source": "SCORE", "name": d["label"], "weight_percent": round(d["weight"] * 100),
             "score": d["score"], "basis": d["basis"]}
            for d in card["dimensions"]
        ],
        # 模型可以引用的全部来源编号
        "allowed_sources": [s["id"] for s in card["sources"]],
    }
