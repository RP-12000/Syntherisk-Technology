"""把存好的分析结果整理成前端各个位置（R2–R9）要的样子。

这里的函数都是“视图”：输入是数据库里的一次分析（models.Analysis）或者它的 result 字典，输出是接口要返回的
普通字典。它们不查数据库、不联网，也不重新算分；只有最后一个函数 drop_stale_explanations 会改 result。

对应模块：M0 应用服务里“整理结果给前端”的那一层。分数、名次、置信度、汇总条都是 M10 算好存在 result 里的，
这里只是挑选和重新摆放。对应界面上的位置：
    R2 约束标签、R3 进度面板       analysis_view
    R4 路线列表、R8 被排除的路线   route_card、result_view
    R5 路线树                     tree_view、route_view、preliminary_view（分叉信息由 routes.build_graph 给出）
    R7 汇总条、B9 最薄弱环节       M10 已经算好放在每条路线上，route_card 原样带出
证据卡（R6）不在这里，在 syntherisk/cards.py。

result 字典是什么样的（由 pipeline.py 一个阶段一个阶段填出来；结构说明见 syntherisk/routes.py 的文件头）：
    result["stage"]      preliminary（只有路线树）/ scored（评过分了）/ final（说明也写完了）
    result["target"]     目标分子
    result["molecules"]  key14 -> 分子：smiles、name、formula、mol_weight、availability（第 3 阶段填）等
    result["reactions"]  步骤编号 -> 一步反应：name、product、reactants、reagents，以及各阶段填进来的
                         evidence、yield、hazards、conditions、green、dimensions、experiment 等
    result["routes"]     路线的列表。每条路线：
                           搜索完就有的   id、name、tree（路线树）、step_keys（按实验顺序排的步骤编号）、leaf_keys
                           评分以后才有   steps、score、rank、confidence、summary、weakest、dims_avg、
                                         excluded、exclusion_reasons、relaxed、availability_details
                           第 8 阶段再加  explanation（整条路线的推荐理由）、explanations（每一步的说明）
    result["ranking"]    可选路线的编号，按名次排好；result["excluded"] 是被排除路线的编号
    另有 result["engine"]、result["constraint_tags"]、result["weights"]、result["warnings"]

谁调用它：api/analyses.py（几乎每个接口）、api/core.py（analysis_list_item、iso）、api/reports.py（iso）。
它依赖谁：
    constraints.py            Constraints、merge_constraints：把提交时的约束合并，得到 R2 约束标签
    modules/m01_structure.py  mol_to_svg：画结构图
    routes.py                 build_graph：R5 分叉信息

本文件里有什么（按阅读顺序）：
    SORT_OPTIONS             结果页的六种排序方式
    iso                      时间 → ISO 8601 字符串
    _target_view             目标分子：按语言处理存着的立体化学提示
    analysis_view            一次分析的状态和进度
    analysis_list_item       列表里的一条分析
    _evidence_text           路线卡片上概括文献依据的那句话
    route_card               一张路线卡片
    _sort_key                按某种排序方式比较路线卡片用的键
    tree_view                路线树（递归）
    result_view              结果页一屏的数据
    route_view               一条路线的完整内容
    preliminary_view         初步结果
    step_scores              记下每一步现在的分数
    drop_stale_explanations  重新评分以后删掉过时的说明
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from syntherisk import i18n, models
from syntherisk.constraints import Constraints, merge_constraints
from syntherisk.context import AppContext
from syntherisk.modules import m01_structure as chem
from syntherisk.routes import build_graph

# 结果页的排序方式（B1）：编号 -> 界面上显示的文字。
# 编号就是 GET /api/analyses/{id}/result 的 sort 参数能取的值（analyses.get_result 用这六个键拼出
# 校验用的正则表达式）；整个字典还会放在结果页数据的 sort_options 里返回给前端。
# 每种排序具体怎么比，见下面的 _sort_key：
#   score         按 M10 排好的名次
#   steps         步数少的在前
#   safety、availability、literature、green   这个维度各步的平均分高的在前
SORT_OPTIONS = {
    "score": i18n.tr("综合", "Overall"),
    "steps": i18n.tr("最短", "Shortest"),
    "safety": i18n.tr("最安全", "Safest"),
    "availability": i18n.tr("原料最好拿", "Easiest to source"),
    "literature": i18n.tr("文献最充分", "Best documented"),
    "green": i18n.tr("最绿色", "Greenest"),
}


# 时间 → 带时区的 ISO 8601 字符串，返回给前端的时间都经过它。实现在 models.py（和 utcnow 放在一起），
# m04、m14 也直接用那一个；这里留一个同名的入口，api/core.py、api/reports.py 照旧从本文件导入。
iso = models.iso


def _target_view(target: dict[str, Any]) -> dict[str, Any]:
    """analyses.target 里存的目标分子，整理成返回给前端的样子。

    target 里除了 smiles、key14、name、formula、mol_weight、messages，还存着 messages_en：
    提交时记下的那几句立体化学提示的英文（见 api/analyses.py 的 create_analysis）。
    参数：
        target  analysis.target（解密后的字典）。
    返回：
        中文请求：去掉以 _en 结尾的键，和以前的格式一样；
        英文、法文请求：原样返回，之后 i18n.localize() 会用 messages_en（法文请求再翻成法文）顶替 messages，
        并把 messages_en 去掉（字典里 "x_en" 顶替 "x" 是 localize 的约定）。
    谁在用：
        本文件的 analysis_view、analysis_list_item。
    """
    if i18n.current_lang() != "zh":
        return target
    return {key: value for key, value in target.items() if not key.endswith("_en")}


def analysis_view(analysis: models.Analysis, ctx: AppContext) -> dict[str, Any]:
    """R2 约束标签和 R3 进度面板。

    把 analyses 表里的一行整理成“这次分析现在是什么状态”。提交、查状态、取消、重跑的接口都返回它。

    参数：
        analysis  一次分析（models.Analysis）。
        ctx       运行时上下文，用到 ctx.reference：合并约束时要查复杂度档位和约束词表。
    返回：
        字典：
            id、project_id
            status             queued 排队中 / running 运行中 / succeeded 成功 / failed 失败 / cancelled 已取消
            current_stage      进行到第几阶段（1–8）；还没开始是 0
            stage_count        一共几个阶段（8）
            stages             八个阶段的列表。每项有 index、key、name、module、hint、
                               status（pending / running / done / skipped / failed / cancelled）、
                               message（这个阶段的一句话结果）、started_at、finished_at。
                               由 pipeline.fresh_stages 建出来，AnalysisRunner._set_stage 逐步更新
            target             目标分子 {smiles, key14, name, formula, mol_weight, messages}
            constraints        提交时的约束，补全了默认值
            constraint_tags    R2 约束标签的列表，每项 {key, label, kind, hardness, source}
            engine             这次用的逆合成引擎的名字（demo / aizynthfinder）；第一次搜索完成之前是 None
            error              失败原因；没失败是 None
            cancel_requested   用户是否已经请求取消（A19）
            user_exclusions    B11 用户排除的分支和试剂的列表
            result_stage       结果到了哪一步：None（还没有结果）/ preliminary / scored / final
            preliminary_ready  结果里是否已经有路线，也就是 A18 可以先看初步结果了
            route_count        找到几条路线
            created_at、started_at、finished_at   带时区的 ISO 8601 字符串；还没有就是 None
            elapsed_seconds    从开始到结束（还没结束就算到现在）用了多少秒，保留一位小数；还没开始是 None
    副作用：
        没有，只读。
    谁在用：
        api/analyses.py 的 create_analysis、get_analysis、cancel_analysis、retry_analysis，
        以及 add_exclusion 触发重新搜索的时候。
    """
    # 数据库里存的是提交时的字典。过一遍 Constraints 模型，缺的字段会补上默认值
    constraints = Constraints.model_validate(analysis.constraints)
    # 还没跑到第 2 阶段的分析 result 是 None，当成空字典处理，下面的 .get 才不会出错
    result = analysis.result or {}
    # 算已经用了多少秒：从 started_at 到 finished_at；还在跑就算到现在
    started = analysis.started_at
    finished = analysis.finished_at
    elapsed = None
    if started is not None:
        end = finished or datetime.now(timezone.utc)
        # SQLite 读出来的时间不带时区（实际是 UTC）。带时区和不带时区的两个时间不能直接相减，
        # 所以先把两头都补成 UTC
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        elapsed = round((end - started).total_seconds(), 1)
    # stages 是 None 时当成空列表，下面的 len(stages) 才不会出错
    stages = analysis.stages or []
    return {
        # 这几项直接取自 analyses 表的列
        "id": analysis.id,
        "project_id": analysis.project_id,
        "status": analysis.status,
        "current_stage": analysis.current_stage,
        "stage_count": len(stages),
        "stages": stages,
        # target、constraints 在数据库里是加密的，这里已经是解密后的字典
        "target": _target_view(analysis.target),
        "constraints": constraints.model_dump(),
        # 每次现算：把提交时的约束合并成“实际执行的那一份”（流水线第 1 阶段做的是同一件事），
        # 取出其中给界面显示的标签
        "constraint_tags": merge_constraints(constraints, ctx.reference).tags,
        "engine": analysis.engine,
        "error": analysis.error,
        "cancel_requested": analysis.cancel_requested,
        "user_exclusions": analysis.user_exclusions or [],
        "result_stage": result.get("stage"),
        "preliminary_ready": bool(result.get("routes")),  # A18：可以先看初步结果了
        "route_count": len(result.get("routes") or []),
        "created_at": iso(analysis.created_at),
        "started_at": iso(analysis.started_at),
        "finished_at": iso(analysis.finished_at),
        "elapsed_seconds": elapsed,
    }


def analysis_list_item(analysis: models.Analysis) -> dict[str, Any]:
    """列表里的一条分析：比 analysis_view 简短，只有列表上要显示的几项。

    参数：
        analysis  一次分析（models.Analysis）。
    返回：
        字典：
            id、project_id、status
            target       目标分子 {smiles, key14, name, formula, mol_weight, messages}
            route_count  找到几条路线
            best_route   排名第一的路线 {"name": 路线名, "score": 路线分}；
                         还没评分、或者一条可选的路线都没有时是 None
            created_at、finished_at   带时区的 ISO 8601 字符串；还没有就是 None
    谁在用：
        api/analyses.py 的 list_analyses；api/core.py 的 project_history。
    """
    result = analysis.result or {}
    best = None
    if result.get("ranking"):
        # ranking 是可选路线的编号，按名次排好，第一个就是排名第一的；拿这个编号到 routes 里找出那条路线。
        # next(生成器, None)：取第一个符合条件的，一个都没有就给 None
        best = next((r for r in result["routes"] if r["id"] == result["ranking"][0]), None)
    # 只给列表上用得到的几项：路线条数和排第一的路线是从 result 里现取的，其余直接取自 analyses 表的列
    return {
        "id": analysis.id,
        "project_id": analysis.project_id,
        "status": analysis.status,
        "target": _target_view(analysis.target),
        "route_count": len(result.get("routes") or []),
        "best_route": {"name": best["name"], "score": best["score"]} if best else None,
        "created_at": iso(analysis.created_at),
        "finished_at": iso(analysis.finished_at),
    }


def _evidence_text(route: dict[str, Any]) -> str:
    """路线卡片上概括文献依据的一句话。

    参数：
        route  评过分的一条路线（要有 steps，每一步带 evidence_level）。
    返回：
        三种之一：
            "证据等级未评估"  有步骤的证据等级是 None（先例库还没有导入数据时就是这样）
            "每步都有文献"    没有一步是 E4
            "N 步没有文献先例"  有 N 步是 E4（证据等级只算期刊；这几步专利里可能有先例，路线的置信度会算上专利）
    谁在用：
        本文件的 route_card。
    """
    levels = [step["evidence_level"] for step in route["steps"]]
    if any(level is None for level in levels):
        return i18n.tr("证据等级未评估", "Evidence level not assessed")
    # 数一数有几步是 E4
    without = levels.count("E4")
    if not without:
        return i18n.tr("每步都有文献", "Every step has a literature precedent")
    # 英文要分单复数（1 step / 2 steps），所以按步数挑英文模板；中文还是原来那一句
    return i18n.tr("{n} 步没有文献先例", "{n} step without literature precedent" if without == 1
                   else "{n} steps without literature precedent", n=without)


def route_card(route: dict[str, Any]) -> dict[str, Any]:
    """R4 路线列表里的一张卡片；被排除的路线（R8）也用它，多带排除原因。

    参数：
        route  评过分的一条路线，也就是 result["routes"] 里的一项（M10 已经把分数、置信度、汇总条等写在上面）。
               只有路线树、还没评分的路线不能用这个函数，会缺字段。
    返回：
        字典：
            id                  路线编号，如 "r1"
            name                路线名
            title               卡片标题：有名次时是“路线 1 · 路线名”，没有名次（被排除）时就是路线名
            rank                名次，从 1 起；被排除的路线是 None
            score               路线分（0–100）；没有一步有分数时是 None
            confidence          置信度：high / medium / low
            confidence_label    置信度的中文：高 / 中 / 低
            confidence_reason   为什么是这个置信度，一句话
            n_steps             步数
            min_stars           各步证据星数里最低的（0–3）；有步骤没评证据等级时是 None
            evidence_text       见 _evidence_text
            inventory_count     “库存里已有的物料数 / 要准备的物料总数”，如 "1/9"；没有要准备的物料时是 None
            inventory_coverage  上面那个比例的百分数（0–100）；没有要准备的物料时是 None
            special_operations  需要的特殊操作的列表，每项 {key, label, steps, text}，text 形如“无水无氧操作 1 步”
            dims_avg            六个维度各步的平均分 {维度编号: 分数}；一步都没有数据的维度是 None
            summary             R7 汇总条的全部数据（由 m10_scoring._summary 算出）
            weakest             B9 最薄弱环节
                                {step_index, step_key, name, score, main_dimension, text, suggestion}
            excluded            是否被排除
            exclusion_reasons   排除原因的列表，每条 {constraint, label, user, …}。没有命中任何约束的路线是空列表；
                                靠放宽条件回来的路线，原因还留在这里，只是不再生效
            relaxed             是否靠放宽条件才进入可选列表（B6，界面上标“放宽后”）
            step_keys           按实验顺序排的步骤编号
            steps               每一步的简要信息。每项有 index、key、name、score、coverage、thin、dimensions、
                                soft_penalty、confidence、evidence_level、precedent_level（把专利也算上的等级，
                                置信度看它）、stars、operation_label、experiment
    谁在用：
        本文件的 result_view、route_view。
    """
    summary = route["summary"]
    # 被排除的路线没有名次（m10_scoring.rank_routes 把它的 rank 设成 None）
    rank = route.get("rank")
    return {
        "id": route["id"],
        "name": route["name"],
        # 有名次才加“路线 N · ”的前缀
        "title": i18n.tr("路线 {rank} · {name}", "Route {rank} · {name}", rank=rank, name=route["name"]) if rank else route["name"],
        "rank": rank,
        # 分数、置信度、最低星数都是 M10 的 evaluate_route 写在路线上的，这里原样带出
        "score": route["score"],
        "confidence": route["confidence"],
        "confidence_label": route["confidence_label"],
        "confidence_reason": route["confidence_reason"],
        "n_steps": summary["n_steps"],
        "min_stars": route["min_stars"],
        "evidence_text": _evidence_text(route),
        # 下面三项和 summary 里的同名字段是同一份数据，在卡片最外层再放一份
        "inventory_count": summary["inventory_count"],
        "inventory_coverage": summary["inventory_coverage"],
        "special_operations": summary["special_operations"],
        "dims_avg": route["dims_avg"],
        "summary": summary,
        "weakest": route.get("weakest"),
        "excluded": route["excluded"],
        "exclusion_reasons": route["exclusion_reasons"],
        "relaxed": route["relaxed"],
        "step_keys": route["step_keys"],
        "steps": [
            # 每一步只挑出列表页用得到的字段（materials、soft_hits 等留给证据卡）；
            # step.get(k) 在字段不存在时给 None，不报错
            {k: step.get(k) for k in ("index", "key", "name", "score", "coverage", "thin", "dimensions", "soft_penalty",
                                      "confidence", "evidence_level", "precedent_level", "stars", "operation_label",
                                      "experiment")}
            for step in route["steps"]
        ],
    }


def _sort_key(card: dict[str, Any], sort: str):
    """给一张路线卡片算出排序用的键。sorted() 按这个键从小到大排，所以要让“越好的越小”。

    参数：
        card  route_card 的结果。
        sort  排序方式，SORT_OPTIONS 的键之一。
    返回：
        一个元组。Python 比较元组时先比第一项，第一项相同再比第二项，依次类推：
            steps     (步数, -路线分)：步数少的在前；步数相同时分高的在前
            safety / availability / literature / green
                      (这个维度有没有数据, -这个维度的平均分, -路线分)：
                      没有数据的排在最后（False 排在 True 前面）；平均分高的在前；再相同时路线分高的在前
            其他（也就是 score）
                      (名次,)：按 M10 排好的名次；没有名次的排在最后
    谁在用：
        本文件的 result_view。
    """
    dims = card["dims_avg"]
    if sort == "steps":
        # 分数取负：sorted 从小到大排，取负以后分高的就排到前面。分数是 None 时当 0
        return (card["n_steps"], -(card["score"] or 0))
    if sort in ("safety", "availability", "literature", "green"):
        value = dims.get(sort)
        return (value is None, -(value or 0), -(card["score"] or 0))
    # 10**6（一百万）只是一个足够大的数，让没有名次的排在所有有名次的后面
    return (card["rank"] or 10**6,)


def tree_view(result: dict[str, Any], route: dict[str, Any], node: dict[str, Any], *, with_svg: bool = False) -> dict[str, Any]:
    """R5 路线树：把编号换成画布要显示的内容（分子名、叶子颜色、每一步的分数和“已做”标记）。

    存着的路线树里只有编号（分子的 key14、反应的 step_key），分子和反应的内容分别在 result["molecules"]、
    result["reactions"] 里。这个函数从 node 开始往下递归，把每个节点要显示的内容填进去。
    树的方向：根是目标分子，下面是得到它的那一步反应，再下面是这一步的反应物，一直到起始原料（叶子）。

    参数：
        result    分析结果。
        route     这棵树所属的路线。评过分的话，每一步的分数从 route["steps"] 里取。
        node      存着的分子节点 {"type": "mol", "key14": …, "children": [反应节点]}。
                  最外层调用时传 route["tree"]。
        with_svg  是否给每个分子附上结构图（180 × 120 像素的 SVG）。
    返回：
        分子节点的字典：
            type          "mol"
            key14         分子编号
            name          名字，可能是 None
            smiles、formula
            is_leaf       是不是叶子，也就是这条路线的起始原料
            status        只有叶子才有：inventory 库存 / purchasable 可购买 / unavailable 买不到。
                          中间体和目标分子是 None；可得性还没评估（第 3 阶段之前）时也是 None
            status_label  status 的中文
            svg           结构图；with_svg 为 True 时才有这个键
            children      得到这个分子的反应，列表里最多一项，叶子是空列表。每项是一个反应节点：
                type            "reaction"
                step_key        步骤编号
                step_index      这条路线的第几步（从 1 起，第 1 步最先做）
                name            反应名
                label           “第 N 步 · 反应名”
                score、stars、evidence_level   这一步的分数、证据星数、证据等级；还没评分时都是 None
                experiment      R12 回填的实验结果 {record_id, outcome, actual_yield, label}；没做过是 None
                children        这一步的反应物，每个又是一个分子节点（递归）
    谁在用：
        本文件的 route_view、preliminary_view，以及它自己（递归）。
    """
    molecule = result["molecules"][node["key14"]]
    availability = molecule.get("availability") or {}
    # 没有子节点的分子就是叶子：这条路线从它出发，不再往下拆
    is_leaf = not node["children"]
    view: dict[str, Any] = {
        "type": "mol",
        "key14": node["key14"],
        "name": molecule.get("name"),
        "smiles": molecule["smiles"],
        "formula": molecule["formula"],
        "is_leaf": is_leaf,
        # 只有叶子显示“从哪来”（画布上据此给叶子上色）；中间体是这条路线自己做出来的，不显示
        "status": availability.get("status") if is_leaf else None,
        "status_label": availability.get("label") if is_leaf else None,
        "children": [],
    }
    if with_svg:
        view["svg"] = chem.mol_to_svg(molecule["smiles"], 180, 120)
    # 这条路线每一步的评分结果，做成按步骤编号查的字典。初步结果里路线还没有 steps，这里就是空字典
    steps = {step["key"]: step for step in route.get("steps") or []}
    for reaction_node in node["children"]:
        reaction = result["reactions"][reaction_node["step_key"]]
        # 还没评分时查不到这一步，用空字典顶替，下面的 step.get(...) 就都是 None
        step = steps.get(reaction_node["step_key"], {})
        view["children"].append(
            {
                "type": "reaction",
                "step_key": reaction_node["step_key"],
                "step_index": reaction_node["step_index"],
                "name": reaction["name"],
                "label": i18n.tr("第 {n} 步 · {name}", "Step {n} · {name}", n=reaction_node["step_index"], name=reaction["name"]),
                # 分数、星数、证据等级取自这条路线的评分结果（同一步反应在不同路线里分数可以不同）
                "score": step.get("score"),
                "stars": step.get("stars"),
                "evidence_level": step.get("evidence_level"),
                # “已做”标记记在反应上，不是记在某条路线的某一步上：同一步反应出现在几条路线里，都会显示
                "experiment": reaction.get("experiment"),
                # 递归：这一步的每个反应物再展开成一棵子树
                "children": [tree_view(result, route, child, with_svg=with_svg) for child in reaction_node["children"]],
            }
        )
    return view


def result_view(result: dict[str, Any], *, sort: str = "score") -> dict[str, Any]:
    """结果页一屏要的数据：R4 路线列表、R5 分叉信息、R7 汇总条（在每张卡片里）、R8 被排除的路线。

    参数：
        result  评过分的分析结果（stage 是 scored 或 final）。
        sort    可选路线的排序方式，SORT_OPTIONS 的键之一；默认 score（综合）。
    返回：
        字典：
            stage            结果到了哪一步：scored / final
            engine           {name, version, stats}：逆合成引擎的名字、版本、搜索统计
            target           目标分子
            constraint_tags  R2 约束标签
            sort             这次用的排序方式
            sort_options     全部排序方式 {编号: 显示文字}
            routes           可选的路线卡片（见 route_card），按 sort 排好
            excluded_routes  被排除的路线卡片，保持搜索结果里原来的顺序；原因在各自的 exclusion_reasons 里
            graph            R5 分叉信息 {target, alternatives, top_branches}，见 routes.build_graph
            weights          这次评分用的权重 {维度编号: 权重}
            warnings         数据方面的提示，中文句子的列表
    副作用：
        没有。不改 result。
    谁在用：
        api/analyses.py 的 get_result、relax_constraints、add_exclusion、remove_exclusion、mark_owned。
    """
    # 每条路线先做成卡片，再分成“可选”和“被排除”两堆；只有可选的参与排序
    cards = [route_card(route) for route in result["routes"]]
    eligible = sorted((c for c in cards if not c["excluded"]), key=lambda c: _sort_key(c, sort))
    excluded = [c for c in cards if c["excluded"]]
    return {
        # 这四项原样取自 result，是流水线写进去的
        "stage": result.get("stage"),
        "engine": result.get("engine"),
        "target": result.get("target"),
        "constraint_tags": result.get("constraint_tags") or [],
        "sort": sort,
        "sort_options": SORT_OPTIONS,
        # routes 是排好序的可选路线；excluded_routes 是被排除的路线，不排序
        "routes": eligible,
        "excluded_routes": excluded,
        # 分叉信息每次现算：每个分子有哪几种做法、各属于哪些路线、每种做法里分数最高的是哪条路线
        "graph": build_graph(result),
        "weights": result.get("weights"),
        "warnings": result.get("warnings") or [],
    }


def route_view(result: dict[str, Any], route: dict[str, Any], *, with_svg: bool = False) -> dict[str, Any]:
    """一条路线的完整内容：路线卡片，加上路线树、推荐理由和每一步的可得性明细（B4 点击路线卡片）。

    参数：
        result    评过分的分析结果。
        route     其中的一条路线（result["routes"] 里的一项）。
        with_svg  路线树上的分子要不要带结构图。
    返回：
        route_card(route) 的全部字段，另加三项：
            tree                  路线树，见 tree_view
            explanation           整条路线的推荐理由，M11 写的 {sentences, text, generated_by, …}；没写过是 None
            availability_details  {步骤编号: {"score", "basis", "sources"}}：每一步“可得性”这一项的分数和依据
    谁在用：
        api/analyses.py 的 get_route。
    """
    card = route_card(route)
    card["tree"] = tree_view(result, route, route["tree"], with_svg=with_svg)
    # 推荐理由只有排名靠前的路线才有（第 8 阶段写的）；重新评分后分数变了也会被删掉，所以用 get
    card["explanation"] = route.get("explanation")
    card["availability_details"] = route.get("availability_details")
    return card


def preliminary_view(result: dict[str, Any], *, with_svg: bool = False) -> dict[str, Any]:
    """A18 先看初步结果：搜索一完成就有的路线树，这时还没有分数。

    参数：
        result    分析结果，至少要到 preliminary 这一步（第 2 阶段搜索完成之后）。
        with_svg  路线树上的分子要不要带结构图。
    返回：
        字典：
            stage     结果到了哪一步：preliminary / scored / final
            engine    {name, version, stats}：逆合成引擎的名字、版本、搜索统计
            target    目标分子
            routes    每条路线 {id, name, n_steps, score, tree}。score 在评分完成之前是 None；tree 见 tree_view
            warnings  数据方面的提示
    谁在用：
        api/analyses.py 的 get_preliminary。
    """
    return {
        "stage": result.get("stage"),
        "engine": result.get("engine"),
        "target": result.get("target"),
        "routes": [
            {
                "id": route["id"],
                "name": route["name"],
                # 初步结果里路线上还没有 summary，步数直接数步骤编号的个数
                "n_steps": len(route["step_keys"]),
                "score": route.get("score"),
                "tree": tree_view(result, route, route["tree"], with_svg=with_svg),
            }
            for route in result.get("routes") or []
        ],
        "warnings": result.get("warnings") or [],
    }


def step_scores(result: dict[str, Any]) -> dict[tuple[str, str], float | None]:
    """记下每条路线每一步现在的分数，留给 drop_stale_explanations 当“重新评分之前”的对照。

    参数：
        result  分析结果。
    返回：
        {(路线编号, 步骤编号): 步骤分}。键里要带路线编号：同一步反应在不同路线里分数可以不一样
        （“可得性”这一项取决于它在哪条路线里）。
    谁在用：
        api/analyses.py 的 add_exclusion、remove_exclusion、mark_owned：在调 pipeline.rescore 之前先调它。
    """
    return {(route["id"], step["key"]): step["score"] for route in result["routes"] for step in route.get("steps") or []}


def drop_stale_explanations(result: dict[str, Any], before: dict[tuple[str, str], float | None]) -> None:
    """重新评分后分数变了的步骤，原来写的说明里的数字就过时了：删掉，下次打开证据卡时由 M11 补写。

    参数：
        result  刚重新评过分的分析结果。
        before  重新评分之前调 step_scores(result) 记下的分数。
    返回：
        None。
    副作用：
        直接改 result：分数变了的步骤，从它所在路线的 explanations 里删掉它的说明；
        一条路线只要有一步的说明被删，这条路线的推荐理由 explanation 也一起删掉
        （推荐理由里写着路线分和最薄弱一步的分数，见 m11_explain.Explainer.explain_route）。
        不写数据库：调用方之后要自己 flag_modified 再 commit。
    谁在用：
        api/analyses.py 的 add_exclusion、remove_exclusion、mark_owned。
    """
    for route in result["routes"]:
        # 没写过说明的路线（排名靠后的）这里是空字典，下面的循环什么都不会删
        explanations = route.get("explanations") or {}
        changed = False
        for step in route.get("steps") or []:
            # 只看写过说明的步骤：现在的分数和之前记下的不一样，就把说明删掉
            if step["key"] in explanations and before.get((route["id"], step["key"])) != step["score"]:
                del explanations[step["key"]]
                changed = True
        if changed:
            # pop(键, None)：有这个键就删，没有也不报错
            route.pop("explanation", None)
