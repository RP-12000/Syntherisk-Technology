"""点下“开始分析”（A16）之后的八个阶段。

    ① 读入约束            M0 + M2 已解析的要求
    ② 逆合成搜索          M3（最慢的一步）
    ③ 原料可得性          M4
    ④ 文献先例            M5
    ⑤ 毒性与法规          M6
    ⑥ 条件、产率、绿色化学  M7 + M9（M8 产率模型暂未接入，产率只用文献报道值）
    ⑦ 评分与排序          M10
    ⑧ 写说明              M11（唯一调用大模型的阶段，只给前几条路线写）

每完成一个阶段就把进度写进数据库，前端的进度面板（R3）通过 SSE 收到后打一个勾。
第 ② 阶段结束就保存初步路线树，用户可以先看（A18）；第 ⑦ 阶段结束结果页就能打开。

这个文件负责什么
    它是“流水线”：自己不做化学上的判断，只负责按顺序调用 M3–M11 各个模块，把它们的产出汇总进一个结果字典，
    并且一路记录进度、响应取消、把失败写进数据库。化学和评分的规则都在 syntherisk/modules/ 下各个模块里。

对应界面上的哪些按钮
    A16 开始分析、A17 进度面板、A18 先看初步结果、A19 取消：接口在 api/analyses.py，真正干活的是这里。
    B11 右键排除、B12 标记为我有之后的重新排序，用的是这里的 rescore()。

谁调用它、它依赖谁
    run_analysis()          taskqueue.py 把它登记为 "analysis.run" 任务的处理函数，
                            由进程内队列的线程或者 Worker 进程（worker.py）调用
    fresh_stages()、add_event()、rescore()   api/analyses.py
    recover_interrupted()   syntherisk/main.py 启动时
    它依赖 routes.py（整理路线树）、constraints.py（合并约束）、cards.py（证据卡），
    以及 modules/ 下的 m01、m03、m04、m05、m06、m07、m09、m10、m11。

数据从哪来到哪去
    读  analyses 表里这次分析的 target（目标分子）、constraints（约束）、user_exclusions（用户排除的分支和试剂）；
        inventory_items（库存）、catalog_compounds（在售目录）、reaction_precedents（先例库）、
        substance_hazards（PubChem 危害缓存）；data/ 下的参考数据。第 5 阶段可能联网查 PubChem，
        第 8 阶段在配了大模型时会调大模型
    写  analyses 表：status、current_stage、stages、attempts、error、engine、result、started_at、finished_at
        analysis_events 表：每次进度变化一行，SSE 接口从这里读
        substance_hazards 表（第 5 阶段新查到的）、llm_calls 表（第 8 阶段的调用记录）

结果字典 result 长什么样（存在 analyses.result 里，加密）
    三张互相引用的表（详见 routes.py 的文件头）：
        molecules  {key14: 分子}         目标、中间体、原料、试剂；key14 是 InChIKey 的前 14 位
        reactions  {步骤编号: 一步反应}   各阶段的评估结果都挂在这里；同一步被几条路线共用时只存一份
        routes     [路线]                路线树、按实验顺序排好的步骤编号、叶子原料；第 7 阶段以后还有分数和名次
    其余的键：schema、stage（preliminary / scored / final）、engine、target、warnings、constraint_tags，
    以及第 7 阶段加上的 ranking、excluded、weights、scoring_version。

八个阶段各自调谁、往 result 里写什么（读什么等细节见 AnalysisRunner 各方法的文档）
    ① run() 里          调 constraints.merge_constraints
                        不写 result；得到 self.effective（实际执行的约束）
    ② _search()         调 M3 的 retro_engine.search、routes.build_skeleton
                        写骨架：target、molecules、reactions、routes、engine、warnings、constraint_tags；
                        stage = "preliminary"
    ③ _availability()   调 M4 的 AvailabilityChecker
                        写 molecules[*]["availability"]
    ④ _precedents()     调 M5 的 PrecedentSearch
                        写 reactions[*]["evidence"]、["yield"]；从先例借来的 ["reagents"]
    ⑤ _safety()         调 M6 的 SafetyService
                        写 reactions[*]["hazards"]；补 molecules[*]["name"]
    ⑥ _conditions()     调 M7 的 ConditionEvaluator、M9 的 evaluate_green、M10 的 base_dimensions
                        写 reactions[*]["conditions"]、["green"]、["dimensions"]
    ⑦ _score()          调 M10 的 score_result
                        写 routes[*] 的分数、置信度、排除原因、名次；ranking、excluded、weights；
                        stage = "scored"
    ⑧ _explain()        调 M11 的 Explainer、cards.build_card
                        写 routes[前几条] 的 explanation、explanations；stage = "final"

进度、取消、失败、重跑
    进度  每个阶段开始和结束各改一次 analyses.stages，并记一条 stage 事件，立刻提交。
    取消  取消接口把 cancel_requested 置为 True；流水线在各个检查点查到后抛 AnalysisCancelled，
          状态记为 cancelled。已经存下的阶段性结果保留。
    失败  任何异常都被 run_analysis() 接住：状态记为 failed，原因写进 analyses.error。
    重跑  同一行分析可以被再跑一次（失败或取消后重试、B11 把路线全排除后重新搜索、服务重启后恢复）。
          每次都从第 1 阶段重新开始；到第 2 阶段结束时，result 被新的骨架整个替换。

本文件里有什么
    logger                   名字是 "syntherisk.pipeline" 的日志器
    STAGES                   八个阶段的名称和说明（进度面板显示用）
    AnalysisCancelled        “用户取消了”这个信号
    fresh_stages()           一份全新的进度表
    add_event()              记一条进度事件
    ensure_molecule()        把一个物质登记进 result["molecules"]
    AnalysisRunner           跑一次分析的对象：进度方法，加上八个阶段的方法
    _close_running_stages()  出错或取消时给进度表收尾
    run_analysis()           任务队列的入口
    rescore()                不重新搜索，只重新过滤和排序
    recover_interrupted()    服务重启后处理没跑完的分析
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from syntherisk import i18n, models
from syntherisk.cards import build_card
from syntherisk.constraints import Constraints, EffectiveConstraints, merge_constraints
from syntherisk.context import AppContext
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m03_retrosynthesis import SearchCancelled, SearchRequest
from syntherisk.modules.m04_availability import AvailabilityChecker, InventoryService
from syntherisk.modules.m05_precedents import PrecedentSearch
from syntherisk.modules.m06_safety import SafetyService
from syntherisk.modules.m07_conditions import ConditionEvaluator
from syntherisk.modules.m09_green import evaluate_green
from syntherisk.modules.m10_scoring import base_dimensions, score_result
from syntherisk.modules.m11_explain import Explainer
from syntherisk.routes import build_skeleton

logger = logging.getLogger("syntherisk.pipeline")

# 八个阶段的固定信息，进度面板（R3）照着它显示。每一项的字段：
#   index   第几阶段（1–8）。代码里 _set_stage(index, …) 用的就是它
#   key     英文代号。后端自己没有用到，随进度表一起发给前端
#   name    显示的阶段名
#   module  显示在阶段名旁边的“由谁来做”。这是固定的文字，不随配置变：
#           比如用演示引擎时，第 2 阶段这里写的仍然是 AiZynthFinder
#   hint    一句话说明这个阶段在做什么
# fresh_stages() 给每一项再加上 status、message、started_at、finished_at，存进 analyses.stages
# name / module / hint 存下来的是中文；i18n.tr() 在这里登记了它们的英文，返回给前端时按语言对照着换
STAGES = [
    {"index": 1, "key": "constraints", "name": i18n.tr("读入约束", "Read constraints"),
     "module": i18n.tr("应用服务", "App service"),
     "hint": i18n.tr("合并档位、勾选和一句话要求", "Merge presets, checkboxes and free-text requirements")},
    {"index": 2, "key": "search", "name": i18n.tr("逆合成搜索", "Retrosynthesis search"),
     "module": "AiZynthFinder",
     "hint": i18n.tr("单步模型提出前体，树搜索一层层回推",
                     "A single-step model proposes precursors; tree search works backwards level by level")},
    {"index": 3, "key": "availability", "name": i18n.tr("原料可得性", "Starting-material availability"),
     "module": i18n.tr("库存 + 在售目录", "Inventory + vendor catalogs"),
     "hint": i18n.tr("每个原料：库存、可购买，还是买不到", "Each starting material: in stock, purchasable or unavailable")},
    {"index": 4, "key": "precedents", "name": i18n.tr("文献先例", "Literature precedents"),
     "module": "USPTO · ORD",
     "hint": i18n.tr("每一步找相同或最相似的已报道反应", "Find the same or the most similar reported reaction for each step")},
    {"index": 5, "key": "safety", "name": i18n.tr("毒性与法规", "Toxicity and regulation"),
     "module": i18n.tr("PubChem · 管制清单", "PubChem · controlled-substance lists"),
     "hint": i18n.tr("GHS 危害、致癌致突变、管制品", "GHS hazards, CMR, controlled substances")},
    {"index": 6, "key": "conditions", "name": i18n.tr("条件、产率、绿色化学", "Conditions, yield, green chemistry"),
     "module": i18n.tr("规则", "Rules"),
     "hint": i18n.tr("复杂度标签、产率、原子经济性", "Complexity tags, yield, atom economy")},
    {"index": 7, "key": "scoring", "name": i18n.tr("评分与排序", "Scoring and ranking"),
     "module": i18n.tr("评分", "Scoring"),
     "hint": i18n.tr("步骤分 → 路线分 → 置信度", "Step score → route score → confidence")},
    {"index": 8, "key": "explain", "name": i18n.tr("写说明", "Write explanations"),
     "module": i18n.tr("大模型", "LLM"),
     "hint": i18n.tr("给前几条路线的每一步写说明，每句附出处",
                     "Explain every step of the top routes, with a source for each sentence")},
]


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """英文进度说明里的单复数：count 是 1 用 singular，否则用 plural（不给就在 singular 后面加 s）。

    只当作 i18n.tr() 的参数用：中文模板里不写这个参数，所以中文句子不受影响。
    """
    return singular if count == 1 else (plural or singular + "s")


# 下面这些固定的句子会存进分析结果（或者 analyses 表）。在这里、也就是导入本文件时就登记它们的英文，
# 而不是等执行到用它的那一行才登记：结果可能是另一个进程（Worker）算的，或者是服务重启之前算的，
# 读它的进程同样要查得到这些句子的英文。
NO_PRECEDENT_DATA_WARNING = i18n.tr(
    "先例库还没有导入 USPTO / ORD 数据：证据等级没有评估，文献依据这一项不计分",
    "No USPTO / ORD data has been imported into the precedent library: evidence levels were not assessed "
    "and the literature dimension is not scored",
)
OLD_PRECEDENT_DATA_WARNING = i18n.tr(
    "先例库是在加入“反应中心”之前导入的：这次只能按指纹判断同类反应，容易把不同的反应当成先例。重新导入一遍先例库即可",
    "The precedent library was imported before reaction centers were added: same-class reactions can only be "
    "judged by fingerprint this time, so different reactions may be mistaken for precedents. "
    "Re-import the precedent library to fix this",
)
PUBCHEM_FAILED_WARNING = i18n.tr(
    "PubChem 这次连不上：内置试剂表之外的物质没有危害数据",
    "PubChem could not be reached this time: substances outside the built-in reagent table have no hazard data",
)
INTERRUPTED_ERROR = i18n.tr(
    "服务重启时任务被中断，请重新开始分析",
    "The task was interrupted by a service restart. Please start the analysis again",
)


class AnalysisCancelled(RuntimeError):
    """用户取消了这次分析（A19）。

    在 AnalysisRunner 的取消检查点抛出，一路穿过各个阶段，由 run_analysis() 接住，把状态写成 cancelled。
    它不算出错：不写进 analyses.error，也不打错误日志。
    """

    pass


def fresh_stages() -> list[dict[str, Any]]:
    """八个阶段的初始进度表：全部是“还没开始”。

    返回：
        长度为 8 的列表。每一项是 STAGES 里对应的那一项，再加四个字段：
          status       "pending"；之后会变成 running / done / skipped / failed / cancelled
          message      ""；阶段完成时写上一句结果，比如 “找到 12 条完整路线”
          started_at   None；阶段开始时写上 ISO 格式的 UTC 时间字符串
          finished_at  None；阶段结束时写上时间
        每次调用返回的都是全新的字典，可以放心修改。
    谁在用：
        api/analyses.py 的 create_analysis()、retry_analysis()（新建或重跑时重置进度）；
        本文件的 run_analysis()（每次真正开始跑之前再重置一遍）。
    """
    # {**stage, …}：把 stage 的键值复制进一个新字典，再加上后面几个键；不会改到 STAGES 本身
    return [{**stage, "status": "pending", "message": "", "started_at": None, "finished_at": None} for stage in STAGES]


def add_event(session: Session, analysis_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """记一条进度事件（往 analysis_events 表加一行）。前端的进度面板就是靠这些事件更新的。

    参数：
        session      数据库会话。
        analysis_id  哪一次分析。
        event_type   事件类型，也就是 SSE 里 event: 后面的那个词。现在有五种：
                       status             分析开始跑了。payload 是 {"status": "running"}
                       stage              某个阶段的状态变了。payload 是 {"index": 1–8, "status": …, "message": …}
                       preliminary_ready  第 2 阶段完成，可以先看初步路线树（A18）。payload 是 {}
                       scored             第 7 阶段完成，结果页可以打开了。payload 是 {}
                       done               整次分析结束。payload 是 {"status": succeeded / failed / cancelled,
                                          "error": 失败原因或 None}
        payload      事件的内容，前端原样收到。这一列不加密，所以不要往里放结构这类敏感内容。
    副作用：
        只是 session.add()，没有提交。调用方随后要 commit()，事件才会真正入库、被 SSE 接口读到。
    谁在用：
        本文件的 _set_stage()、_save_result()、run_analysis()、recover_interrupted()；
        api/analyses.py 的 cancel_analysis()（取消一个还在排队的分析时，直接记一条 done）。
        读事件的是 api/analyses.py 的 stream_events()（GET /api/analyses/{id}/events）。
    """
    session.add(models.AnalysisEvent(analysis_id=analysis_id, type=event_type, payload=payload))


def best_precedent(evidence: dict[str, Any]) -> dict[str, Any] | None:
    """一步反应的“最好的先例”：借用试剂、条件文字都用它。

    M5 在 evidence["best_ref"] 里记了它的编号（期刊论文优先，见 m05_precedents.PrecedentSearch._best_ref）。
    旧的分析结果里没有这一项，那时先例列表的第一条就是最好的那条。没有先例时返回 None。
    谁在用：本文件的 AnalysisRunner._adopt_precedent_reagents() 和 _conditions()。
    """
    precedents = evidence.get("precedents") or []
    wanted = evidence.get("best_ref")
    return next((p for p in precedents if p.get("ref") == wanted), precedents[0] if precedents else None)


def ensure_molecule(result: dict[str, Any], smiles: str, ctx: AppContext) -> str | None:
    """先例里的试剂可能是路线里没出现过的物质，用到时登记进 molecules。

    参数：
        result  分析结果字典。会被修改：可能往 result["molecules"] 里加一项。
        smiles  这个物质的 SMILES。
        ctx     运行时上下文，用它的内置试剂表查中文名。
    返回：
        这个物质的 key14（molecules 表的键）；SMILES 读不出来时返回 None。
    登记的内容：
        和 routes.py 里 _Builder.molecule() 建的分子条目字段相同：key14、smiles（规范写法）、inchikey、
        name（内置试剂表里有才有，否则是 None）、formula、mol_weight；
        engine_in_stock 固定为 None（逆合成引擎没有评价过这个物质）。已经登记过的物质不动。
    谁在用：
        本文件的 AnalysisRunner._adopt_precedent_reagents()。
    """
    # mol_record：用 RDKit 解析 SMILES，得到规范 SMILES、InChIKey、分子式、分子量等；解析不了返回 None
    record = chem.mol_record(smiles)
    if record is None:
        return None
    if record.key14 not in result["molecules"]:
        result["molecules"][record.key14] = {
            "key14": record.key14,
            "smiles": record.smiles,
            "inchikey": record.inchikey,
            "name": ctx.reference.compounds.name_for(record.smiles),
            "formula": record.formula,
            "mol_weight": record.mol_weight,
            "engine_in_stock": None,
        }
    return record.key14


class AnalysisRunner:
    """把一次分析的八个阶段依次跑完的对象。一次分析建一个，跑完就不用了。

    它手里有三样东西：运行时上下文（各个引擎和参考数据）、一个数据库会话、这次分析在 analyses 表里的那一行。
    跑的过程中不断往 self.result 这个大字典里添内容，每个阶段结束时整个存回 analysis.result（加密列）。
    self.result 的结构见本文件开头的说明。

    方法分两组：
        进度  _cancelled、_check_cancel、_set_stage、_save_result、_stage
        阶段  run（总流程）、_search、_availability、_precedents、_adopt_precedent_reagents、
              _safety、_conditions、_score、_explain

    出错和取消：这里的方法都不捕获异常。任何一步抛了异常（包括用户取消时抛的 AnalysisCancelled），
    都会一路抛到 run_analysis()，由它把状态写成 failed 或 cancelled。

    谁在用：本文件的 run_analysis()。
    """

    def __init__(self, ctx: AppContext, session: Session, analysis: models.Analysis):
        """建一个 Runner。这里只是把东西记下来，不读写数据库。

        参数：
            ctx       运行时上下文。
            session   数据库会话。整次分析共用这一个；每个阶段开始和结束时都会 commit。
            analysis  这次分析的那一行（run_analysis 已经把它的状态改成 running）。
        """
        self.ctx = ctx
        self.session = session
        self.analysis = analysis
        # 分析结果。第 2 阶段由 build_skeleton() 建出骨架，之后各阶段往里添内容
        self.result: dict[str, Any] = {}
        # 这次分析的“中文 → 英文”对照表：各阶段用 i18n.tr() 拼文字时往里记，_save_result() 把它存进 result["_en"]
        self.en: dict[str, str] = {}
        # 第 1 阶段合并出来的“实际执行的约束”；第 2、7 阶段要用
        self.effective: EffectiveConstraints | None = None
        # 第 3 阶段建的可得性检查器；第 4 阶段借用先例的试剂时，还要用它查这些试剂能不能买到
        self.checker: AvailabilityChecker | None = None

    # ------------------------------------------------------------ 进度

    def _cancelled(self) -> bool:
        """用户是不是点了取消（A19）。每次调用都重新查一下数据库里的 cancel_requested。

        取消是处理 POST …/cancel 的那个请求（另一个会话，可能在另一个进程里）写进数据库的，
        所以不能看 self.analysis 上的旧值，要单独发一条 SELECT 只取这一列。
        返回：
            True 表示已经请求取消。
        谁在用：
            _check_cancel()；另外作为 should_cancel 回调交给逆合成引擎
            （AiZynthFinder 另起进程搜索时，每半秒问一次）。
        """
        # select(某一列).where(…) 只查一个值；session.scalar() 直接返回这个值，而不是一整行
        return bool(
            self.session.scalar(
                select(models.Analysis.cancel_requested).where(models.Analysis.id == self.analysis.id)
            )
        )

    def _check_cancel(self) -> None:
        """取消检查点：用户已经请求取消的话，抛 AnalysisCancelled 中止整次分析；否则什么都不做。

        取消不是立刻生效的，只在这些检查点生效：每个阶段开始之前；第 4 阶段每检索完一批步骤；
        第 5 阶段查完全部物质的危害之后；第 8 阶段写每一段说明之前。
        第 2 阶段的搜索由引擎自己调 _cancelled() 来问。
        """
        if self._cancelled():
            raise AnalysisCancelled()

    def _set_stage(self, index: int, status: str, message: str = "") -> None:
        """更新某个阶段的进度：改 analysis.stages、记一条 stage 事件，并且立刻提交。

        参数：
            index    第几阶段，1–8。
            status   新状态：running（开始，或者进行中更新进度文字）、done（完成）、skipped（跳过）。
                     代码也认 failed，但现在没有地方这样调用：出错时由 _close_running_stages() 统一处理。
            message  给用户看的一句话，显示在进度面板这一阶段的下面。空字符串表示不改原来的话。
        副作用：
            - analysis.stages 的第 index 项：改 status；message 非空时改 message；
              第一次变成 running 时记 started_at；变成 done / skipped / failed 时记 finished_at；
            - analysis.current_stage 改成 index；
            - 加一条事件，内容是 {"index", "status", "message"}，其中 message 是这一阶段当前的话
              （这次没传 message 时，就是之前留下的那句）；
            - session.commit()：进度要马上写进数据库，SSE 接口（另一个会话，甚至另一个进程）才读得到。
              这次提交会把会话里其他还没提交的改动一起提交。
        谁在用：
            本类各个阶段的方法。同一阶段可以多次以 running 调用，用来更新进度文字（第 4 阶段这样做）。
        """
        now = models.utcnow().isoformat()
        # 复制一份再改，最后整体赋值回去：stages 是 JSON 列，原地修改里面的字典 SQLAlchemy 察觉不到，不会保存
        stages = [dict(stage) for stage in self.analysis.stages]
        # 阶段编号从 1 开始，列表下标从 0 开始
        stage = stages[index - 1]
        stage["status"] = status
        if message:
            stage["message"] = message
            # 进度说明的英文直接存在旁边（message_en），返回时由 i18n.localize() 按语言挑
            stage["message_en"] = i18n.en_of(message)
        if status == "running" and not stage["started_at"]:
            stage["started_at"] = now
        if status in ("done", "skipped", "failed"):
            stage["finished_at"] = now
        self.analysis.stages = stages
        self.analysis.current_stage = index
        add_event(self.session, self.analysis.id, "stage", {"index": index, "status": status, "message": stage["message"],
                                                           "message_en": stage.get("message_en") or ""})
        self.session.commit()

    def _save_result(self, event: str | None = None) -> None:
        """把 self.result 整个存回数据库的 analysis.result（加密列），可以顺带发一条事件。会提交。

        参数：
            event  要一起记的事件类型：第 2 阶段结束时是 "preliminary_ready"，第 7 阶段结束时是 "scored"；
                   其他阶段不传。事件的内容是空字典。
        说明：
            flag_modified(对象, "属性名") 是明确告诉 SQLAlchemy“这一列变了，提交时要写”。
            self.result 从第 2 阶段起一直是同一个字典对象，各阶段只是往里添内容；
            再次赋同一个对象时 SQLAlchemy 看不出它变了，所以必须加这一句。
            每次保存都是把整个结果重新转成 JSON、重新加密、整列重写。
        """
        # 这次分析攒下的“中文 → 英文”对照跟着结果一起存（见 i18n.py）
        self.result["_en"] = self.en
        self.analysis.result = self.result
        flag_modified(self.analysis, "result")
        if event:
            add_event(self.session, self.analysis.id, event, {})
        self.session.commit()

    def _stage(self, index: int):
        """开始第 index 阶段：先过一次取消检查点，再把这个阶段标成 running（会记事件并提交）。"""
        self._check_cancel()
        self._set_stage(index, "running")

    # ------------------------------------------------------------ 八个阶段

    def run(self) -> None:
        """总流程：按顺序跑八个阶段。

        每个阶段都是同一个套路：self._stage(n) 标记开始 -> 干活 -> self._set_stage(n, "done", 一句结果)
        -> self._save_result() 存盘。第 1 阶段很短，直接写在这里；其余各有一个方法。
        第 2 阶段一条路线都没找到时，后面六个阶段全部标成 skipped，这次分析照常算“成功”结束
        （结果里 routes 是空的，进度面板第 2 阶段的那句话会说明原因）。

        抛出：
            AnalysisCancelled  用户取消。
            其他任何异常        哪个阶段出了错，就从哪里抛出来；存着的约束不合法时 pydantic 的
                               ValidationError 也在内。都由 run_analysis() 接住。
        """
        # ---- 第 1 阶段：读入约束 ----
        # 读：analysis.constraints（提交分析时前端发来的约束原样，解密后是一个字典）
        # 做：model_validate 把字典还原成 Constraints 对象（顺便校验一遍）；
        #     merge_constraints（constraints.py）把复杂度档位、勾选项、高级参数和确认过的一句话要求
        #     合并成“实际执行的约束” EffectiveConstraints
        # 写：只记在 self.effective 上，不往 result 里写（result 要到第 2 阶段才建出来）
        # 进度里的话是 effective.tag_summary，比如 “合并 9 条约束，其中 2 条来自一句话要求”
        self._stage(1)
        constraints = Constraints.model_validate(self.analysis.constraints)
        self.effective = merge_constraints(constraints, self.ctx.reference)
        self._set_stage(1, "done", self.effective.tag_summary)

        # ---- 第 2 阶段：逆合成搜索（M3）。_search() 返回 False 表示一条路线都没有 ----
        self._stage(2)
        if not self._search():
            # 没有路线：第 3–8 阶段无事可做，逐个标成“跳过”（range(3, 9) 是 3 到 8）
            for index in range(3, 9):
                self._set_stage(index, "skipped", i18n.tr("没有路线，跳过", "No routes; skipped"))
            return

        # ---- 第 3–8 阶段：每个阶段一个方法，说明见各方法的文档 ----
        self._stage(3)
        self._availability()
        self._stage(4)
        self._precedents()
        self._stage(5)
        self._safety()
        self._stage(6)
        self._conditions()
        self._stage(7)
        self._score()
        self._stage(8)
        self._explain()

    def _search(self) -> bool:
        """第 2 阶段：逆合成搜索（M3）。找候选路线，并把引擎返回的路线树整理成 result 的骨架。

        读：
            analysis.target["smiles"]   目标分子
            analysis.user_exclusions    用户在结果页排除过的试剂（B11）；重新搜索时不让引擎把它们当原料
            self.effective              最多几步（max_steps）、原料来源档位（stock_source）
            课题组的库存（inventory_items 表，要解密）：库存里有的分子，搜索碰到就不再往下拆
            内置试剂表里标了可购买的试剂：补引擎自带的在售清单里没有的小分子试剂
            配置 AIZYNTH_ITERATION_LIMIT、AIZYNTH_TIME_LIMIT
        调用：
            ctx.retro_engine.search()（m03_retrosynthesis.py 的 AiZynthEngine 或 DemoEngine）
            routes.build_skeleton()：把引擎格式的路线树换成 molecules / reactions / routes 三张表
        写进 result（到这里 result 才第一次有内容）：
            schema、stage = "preliminary"、engine（引擎名、版本、搜索统计）、target
            molecules   每个分子：key14、smiles、inchikey、name、formula、mol_weight、
                        engine_in_stock（引擎自己的在售清单里有没有它）
            reactions   每一步：key、rxn_key、name、classification、product、reactants、reagents、reaction_smiles、
                        center（反应中心，引擎给了原子映射才有）、model（模型依据）、
                        given_conditions（演示数据自带的条件说明）
            routes      每条：id（r1、r2……）、name、tree（路线树）、step_keys（按实验顺序排的步骤编号）、
                        leaf_keys（叶子原料）、engine_score、engine_solved
            warnings    引擎的提示，以及被跳过的路线
            constraint_tags   第 1 阶段合并出来的约束标签
            没有路线时另外写 stage = "final"、ranking = []、excluded = []。
        另外写：analysis.engine（引擎名）。
        事件：有路线时发 preliminary_ready，前端可以调 GET …/preliminary 先看路线树（A18）。
        返回：
            True 找到了至少一条路线；False 一条都没有（调用方据此跳过后面的阶段）。
        抛出：
            AnalysisCancelled  搜索过程中用户取消了。
            EngineError 等      引擎出错（配置文件找不到、子进程失败、超时），整次分析失败。
        """
        ctx, analysis, effective = self.ctx, self.analysis, self.effective
        # 库存按“提交这次分析的人”能看到的范围取：全组共享的，加上他自己私有的
        inventory = InventoryService(self.session, analysis.org_id, analysis.created_by)
        # 把库存里每个有结构的条目换成完整的 InChIKey，交给引擎当“到这里就不用再拆”的终点。
        # item.payload 是解密后的字典；没有结构的条目（混合物）smiles 为空，mol_record 返回 None，跳过
        inchikeys = []
        for item in inventory.list_items():
            record = chem.mol_record(item.payload.get("smiles") or "")
            if record:
                inchikeys.append(record.inchikey)
        # 用户排除的试剂（B11，type 是 substance 的那些）。排除“分支”的记录不交给引擎，第 7 阶段评分时才用
        excluded = [e["smiles"] for e in analysis.user_exclusions or [] if e["type"] == "substance" and e.get("smiles")]
        # attempts 在 run_analysis() 里每跑一次加 1，所以第一次跑时是 1，retry 是 0；
        # 之后同一行每多跑一次（B11 触发的重新搜索、服务重启后的恢复），retry 就加 1
        retry = max(0, analysis.attempts - 1)
        # 交给引擎的是标准互变异构体写法：反应模板是从专利反应里抽的，那里几乎都写成酰胺这类常见写法，
        # 用户画成亚胺醇时模板对不上（chem.standard_tautomer）。只换 key14 不变的写法，结果里的分子照样对得上
        search_smiles = chem.standard_tautomer(analysis.target["smiles"])
        # 搜索请求。演示引擎只用其中的 target_smiles；其余各项是给 AiZynthFinder 的
        request = SearchRequest(
            target_smiles=search_smiles,
            # 步数上限：默认由复杂度档位决定（data/scoring.toml 的 [complexity]，现在是简洁 3、平衡 6、探索 10），
            # 高级参数和一句话要求可以改
            max_steps=effective.max_steps,
            # 重新搜索（B11 把路线全排除了之后）时多搜几轮，才找得到别的路线
            iteration_limit=ctx.settings.aizynth_iteration_limit * (1 + retry),
            # 秒。引擎自己搜到这个时间就停；另起进程搜索时，后端在这之外再等 300 秒，还没结束才判为超时
            time_limit=ctx.settings.aizynth_time_limit,
            # inventory_only / inventory_first / any。只有 inventory_only 会改变搜索：这时只把库存当终点
            # （库存是空的话仍按在售清单搜，并给一条提示）
            stock_mode=effective.stock_source,
            inventory_inchikeys=inchikeys,
            # 内置常用试剂表里标了可购买的，也算终点
            catalog_inchikeys=[c.inchikey for c in ctx.reference.compounds.compounds if c.purchasable],
            excluded_smiles=excluded,
        )
        try:
            # should_cancel 传的是方法本身（没有括号）：引擎在搜索过程中反复调用它，返回 True 就中止搜索
            search = ctx.retro_engine.search(request, should_cancel=self._cancelled)
        except SearchCancelled as exc:
            # 引擎的“搜索被取消”换成流水线的“分析被取消”，由 run_analysis() 统一处理
            raise AnalysisCancelled() from exc

        # search 是 SearchResult：engine（引擎名）、version、routes（引擎格式的路线树）、stats、warnings。
        # build_skeleton 把它整理成 result 的骨架；读不出来的、终点不是目标分子的、一步都没拆的、
        # 重复的路线在这一步被丢掉
        self.result = build_skeleton(search, analysis.target["smiles"], ctx.reference)
        if search_smiles != analysis.target["smiles"]:
            # 换了写法要告诉用户：路线树上的目标分子是按标准写法拆的
            self.result["warnings"].append(i18n.tr(
                "目标分子按标准互变异构体写法搜索：{smiles}（和输入的是同一个分子）",
                "The target was searched in its standard tautomeric form: {smiles} "
                "(the same molecule as the input)",
                smiles=search_smiles))
        # 第 1 阶段的约束标签（R2）抄一份进结果
        self.result["constraint_tags"] = effective.tags
        analysis.engine = search.engine
        count = len(self.result["routes"])
        if not count:
            # 一条路线都没有：结果直接算“最终版”，名次和被排除的列表都是空的
            self.result["stage"] = "final"
            self.result.update({"ranking": [], "excluded": []})
            # 引擎给了提示，就把第一条接在后面（比如演示引擎会说它只有哪几个分子的路线）
            if search.warnings:
                message = i18n.tr("没有找到路线：{warning}", "No routes found: {warning}", warning=search.warnings[0])
            else:
                message = i18n.tr("没有找到路线", "No routes found")
            self._set_stage(2, "done", message)
            self._save_result()
            return False
        # 搜索树的节点数（AiZynthFinder 的统计项），进度里说成“已探索多少个中间体”；演示引擎没有这一项
        nodes = search.stats.get("number_of_nodes")
        # AiZynthFinder 至少返回 5 条路线，凑不够完整的就拿没拆到底的补：那些路线的某个原料它认为买不到。
        # 照样往下评估（我们自己的库存和在售表里可能有），但进度里要说清楚
        # engine_solved 是引擎对每条路线的判断：False 表示没拆到它认为在售的原料。
        # 演示路线的这一项是 None，`is False` 不成立，所以不计入
        incomplete = sum(route["engine_solved"] is False for route in self.result["routes"])
        # 进度说明都用 i18n.tr() 拼成整句：_set_stage() 靠它查出英文存进 message_en。
        # routes、verb 这类参数只有英文模板用（单复数）
        if incomplete:
            found = i18n.tr("找到 {count} 条路线，其中 {incomplete} 条没有拆到在售原料",
                            "Found {count} {routes}, {incomplete} of which {verb} not reach purchasable starting materials",
                            count=count, incomplete=incomplete, routes=_plural(count, "route"),
                            verb=_plural(incomplete, "does", "do"))
        else:
            found = i18n.tr("找到 {count} 条完整路线", "Found {count} complete {routes}",
                            count=count, routes=_plural(count, "route"))
        # 进度里的话分三种：演示引擎；有节点数（{…:,} 给数字加千位分隔符）；没有节点数
        if search.engine == "demo":
            message = i18n.tr("演示引擎：给出 {count} 条内置路线（还没有接入 AiZynthFinder）",
                              "Demo engine: {count} built-in {routes} (AiZynthFinder is not connected yet)",
                              count=count, routes=_plural(count, "route"))
        elif nodes:
            message = i18n.tr("已探索 {nodes:,} 个中间体，{found}", "Explored {nodes:,} intermediates. {found}",
                              nodes=int(nodes), found=found)
        else:
            message = found
        self._set_stage(2, "done", message)
        self._save_result("preliminary_ready")
        return True

    def _availability(self) -> None:
        """第 3 阶段：原料可得性（M4）。给 result 里的每个分子判定：库存里有、能买到，还是买不到。

        读：
            result["molecules"]（每个分子的 smiles 和 engine_in_stock）、result["routes"] 的 leaf_keys
            课题组的库存（inventory_items，解密后按结构建索引）
            在售目录（catalog_compounds 表）和内置试剂表（哪些是随手可得的物质）
        调用：
            m04_availability.py 的 InventoryService.index_by_key14()、AvailabilityChecker.preload() / check()
        写进 result：
            molecules[*]["availability"] = {
                status          "inventory" 库存 / "purchasable" 可购买 / "unavailable" 买不到
                label           对应的中文
                inventory       库存里的那一条（item_id、name、quantity、location）；库存里没有则为 None
                purchasable     能不能买到
                catalog_source  在哪个目录里查到的（seed、zinc、engine_stock……）；查不到则为 None
                ubiquitous      是不是内置试剂表里标了 ubiquitous 的随手可得的物质（现在只有水）
                suppliers / price / lead_time  现在都是 None；price_label 是 “待询价”
            }
            所有分子都判（包括中间体和目标分子），不只是叶子原料；评分时只看需要准备的那些。
        不改数据库。检查器留在 self.checker 上，第 4 阶段还要用。
        进度里的话只统计各条路线的叶子原料（去重、不算随手可得的），
        比如 “3 个原料：库存 1 · 可购买 2 · 买不到 0”。
        """
        inventory = InventoryService(self.session, self.analysis.org_id, self.analysis.created_by)
        # index_by_key14() 把这个人看得到的全部库存解密，整理成 {key14: 库存条目}；整次分析只做这一次
        self.checker = AvailabilityChecker(self.session, self.ctx.reference, inventory.index_by_key14())
        molecules = self.result["molecules"]
        # molecules 的键就是 key14：先一次把它们在在售目录里的情况全查出来（每 500 个一条 SQL），免得逐个查库
        self.checker.preload(molecules)
        for molecule in molecules.values():
            # engine_in_stock 是逆合成引擎自己的在售清单给出的判断（AiZynthFinder 的 ZINC 清单）：
            # 我们的目录里查不到、而引擎说有货的，也算可购买
            molecule["availability"] = self.checker.check(molecule["smiles"], engine_in_stock=molecule["engine_in_stock"])

        # 下面只是为了拼进度里的那句话。
        # {… for route in … for key in …} 是集合推导式：所有路线的叶子原料放在一起，自动去重
        leaves = {key for route in self.result["routes"] for key in route["leaf_keys"]}
        statuses = [molecules[key]["availability"] for key in leaves]
        # 随手可得的物质不算“需要准备的原料”
        needed = [s for s in statuses if not s["ubiquitous"]]
        # 三种状态各有几个。sum(条件 for …) 是在数满足条件的有几个（True 当 1，False 当 0）
        counts = {name: sum(s["status"] == name for s in needed) for name in ("inventory", "purchasable", "unavailable")}
        self._set_stage(
            3, "done",
            i18n.tr("{total} 个原料：库存 {inventory} · 可购买 {purchasable} · 买不到 {unavailable}",
                    "{total} starting {materials}: {inventory} in stock · {purchasable} purchasable · {unavailable} unavailable",
                    total=len(needed), materials=_plural(len(needed), "material"), **counts),
        )
        self._save_result()

    def _precedents(self) -> None:
        """第 4 阶段：文献先例（M5）。给每一步找相同或相似的已报道反应，定证据等级 E1–E4。

        读：
            result["reactions"] 的每一步：反应物和产物的 SMILES（从 molecules 里取）、
            model.policy_probability（逆合成模型给这一步的概率）、center（反应中心）
            先例库（reaction_precedents 表）和它的内存索引 ctx.precedent_index
            评分配置 data/scoring.toml 的 [evidence]（阈值、每个等级的分数段）
        调用：
            m05_precedents.py 的 PrecedentSearch.find()；借用试剂时还会用到 reference.compounds.combine_ions()、
            本文件的 ensure_molecule() 和第 3 阶段留下的 self.checker
        写进 result：
            reactions[*]["evidence"] = {
                level / label / stars  证据等级 E1–E4、中文名、星数；先例库是空的时都是 None（未评估）
                literature_score       “文献依据”这一维的分数（0–100）；未评估时是 None
                precedents             先例列表，每条带 ref（P1、P2……）、文献号、年份、相似度、产率、试剂、条件等
                n_exact                有几个先例报道了同一个反应
                best_similarity        同类反应的先例里，底物相似度最高是多少（有同一反应的报道时是 1.0）
                compared_by            "center" 按反应中心 / "fingerprint" 按指纹判断的同类反应
                basis                  一句话依据
                db_empty               先例库是不是空的
            }
            reactions[*]["yield"] = {source, label, refs}，有报道值时还有 low、high、median、n。
                source 是 "literature"（同一反应的报道）、"similar_literature"（高相似先例的报道）或 "none"
            借用了先例试剂的步骤：reactions[*]["reagents"]、["reagents_source"]、["reaction_smiles"]，
                以及 molecules 里新登记的试剂（带 availability）
            warnings：先例库是空的，或者先例库是加入“反应中心”之前导入的，各加一条提醒
        第一次走到这里时要把先例库的索引读进内存，所以这个阶段可能明显比别的阶段慢。
        """
        # 建检索器时会检查内存里的索引是不是最新的：库的行数或版本号变了就重新加载
        search = PrecedentSearch(self.session, self.ctx.reference, self.ctx.precedent_index)
        reactions, molecules = self.result["reactions"], self.result["molecules"]
        total = len(reactions)
        # reactions 里同一步只存一份，被几条路线共用的步骤也只检索一次。
        # enumerate(…, 1)：边遍历边数数，从 1 开始，done 就是“处理到第几步了”
        for done, reaction in enumerate(reactions.values(), 1):
            # 演示路线没有模型依据，reaction["model"] 是 None：换成空字典，下面的 .get 才不会出错
            model = reaction.get("model") or {}
            evidence = search.find(
                [molecules[key]["smiles"] for key in reaction["reactants"]],
                molecules[reaction["product"]]["smiles"],
                model_probability=model.get("policy_probability"),
                center=reaction.get("center"),
            )
            # find() 返回的字典里，产率信息在 "yield" 键下：单独拿出来放到 reaction["yield"]，其余的作为 evidence
            reaction["yield"] = evidence.pop("yield")
            reaction["evidence"] = evidence
            self._adopt_precedent_reagents(reaction)
            # 步骤多的时候中途报几次进度：total // 5 是总数的五分之一（至少算 1），每处理这么多步
            # 更新一次进度文字，同时过一次取消检查点。最后一步不在这里报（循环结束后会报 done）
            if done % max(1, total // 5) == 0 and done < total:
                self._check_cancel()
                self._set_stage(4, "running", i18n.tr("已检索 {done} / {total} 个步骤", "Searched {done} / {total} steps",
                                                      done=done, total=total))

        if search.db_empty:
            # 先例库一条数据都没有：不是“每一步都没有先例”，而是“没法评估”。等级留空，文献依据这一维不计分
            self.result["warnings"].append(NO_PRECEDENT_DATA_WARNING)
            message = i18n.tr("先例库还没有导入数据，这次没有评估证据等级",
                              "The precedent library has no data yet; evidence levels were not assessed this time")
        else:
            # 有的步骤带反应中心，库里却一条带反应中心的先例都没有：说明先例库是旧版本导入的
            if self.ctx.precedent_index.with_center == 0 and any(r.get("center") for r in reactions.values()):
                self.result["warnings"].append(OLD_PRECEDENT_DATA_WARNING)
            # 数一数四个等级各有几步，拼成进度里的那句话
            levels = [r["evidence"]["level"] for r in reactions.values()]
            message = i18n.tr(
                "{total} 个步骤：直接报道 {e1} · 高相似 {e2} · 低相似 {e3} · 未找到先例 {e4}",
                "{total} {steps}: {e1} directly reported · {e2} high similarity · {e3} low similarity · {e4} without precedent",
                total=total, steps=_plural(total, "step"), e1=levels.count("E1"), e2=levels.count("E2"),
                e3=levels.count("E3"), e4=levels.count("E4"),
            )
        self._set_stage(4, "done", message)
        self._save_result()

    def _adopt_precedent_reagents(self, reaction: dict[str, Any]) -> None:
        """逆合成引擎不给试剂。有同一反应或高相似先例时，借用最好的那条先例的试剂，并注明出处。

        参数：
            reaction  result["reactions"] 里的一步，已经有 evidence。会被直接修改。
        什么时候借：
            这一步自己没有试剂（演示路线自带试剂，不借），证据等级是 E1 或 E2，并且列出了先例。
        借哪一条：
            先例列表的第一条（M5 已经排好序：同一反应的在最前，然后是同类反应里底物最像的）。
        写进 result：
            reaction["reagents"]         试剂的 key14 列表（去掉重复的，也去掉本来就是反应物的）
            reaction["reagents_source"]  出处：那条先例的编号，比如 "P1"
            reaction["reaction_smiles"]  重新拼成 “反应物>试剂>产物”
            molecules                    先例里的试剂如果是路线里没出现过的物质，登记进去，并查好 availability
        后面的阶段用得上这些试剂：第 5 阶段查它们的危害，第 6 阶段按它们判断操作条件和溶剂评级。
        谁在用：
            本类的 _precedents()。
        """
        evidence = reaction["evidence"]
        # 三种情况不借：已经有试剂了；等级不够（E3、E4，或者没评估）；没有先例
        if reaction["reagents"] or evidence.get("level") not in ("E1", "E2") or not evidence["precedents"]:
            return
        # “最好的先例”由 M5 挑好（期刊论文优先，见 m05_precedents.PrecedentSearch._best_ref）
        best = best_precedent(evidence)
        agents = self.ctx.reference.compounds.combine_ions(best["agents"])  # 专利数据把盐拆成了离子，先拼回去
        # 逐个登记进 molecules，拿到 key14；读不出来的（ensure_molecule 返回 None）丢掉
        keys = [key for key in (ensure_molecule(self.result, smiles, self.ctx) for smiles in agents) if key]
        # dict.fromkeys(keys) 利用“字典的键不重复、并且保持先后顺序”来去重；再去掉已经是反应物的
        keys = [k for k in dict.fromkeys(keys) if k not in reaction["reactants"]]
        if not keys:
            return
        molecules = self.result["molecules"]
        for key in keys:
            # 新登记的试剂还没有可得性：现查一下。引擎没有评价过它，所以不传 engine_in_stock
            if "availability" not in molecules[key]:
                molecules[key]["availability"] = self.checker.check(molecules[key]["smiles"])
        reaction["reagents"] = keys
        reaction["reagents_source"] = best["ref"]
        reaction["reaction_smiles"] = chem.build_reaction_smiles(
            [molecules[k]["smiles"] for k in reaction["reactants"]],
            molecules[reaction["product"]]["smiles"],
            [molecules[k]["smiles"] for k in keys],
        )

    def _safety(self) -> None:
        """第 5 阶段：毒性与法规（M6）。查每一步用到的每个物质的 GHS 危害分类和管制情况。

        读：
            result["reactions"] 每一步的反应物、试剂、产物（从 molecules 里取 smiles 和 name）
            内置试剂表里的危害分类（data/compounds.csv）、管制清单（data/controlled_substances.csv）
            PubChem 的缓存（substance_hazards 表）；缓存里没有、并且启用了 PubChem 时，联网去查
            配置 CONTROLLED_JURISDICTIONS（只报这些地区的管制清单）
        调用：
            m06_safety.py 的 SafetyService.assess()
        写进 result：
            reactions[*]["hazards"] = 一个列表，这一步的每个物质一项（反应物、试剂、产物都有）：{
                key14、smiles、name
                h_codes / statements / pictograms  H 编码、对应的中文危险性说明、象形图编号
                flags                              归并后的危害类别，比如 acute_toxic_12（剧毒）、cmr_1、
                                                   controlled_strict（管得严的管制品）
                top_flag / top_label               其中最重的一类
                controlled                         命中的管制清单条目
                data_source                        "local" 内置试剂表 / "pubchem" / "none" 没有危害数据
                reviewed                           内置试剂表里这一行有没有人复核过
                role                               "reactant" / "reagent" / "product"
            }
            molecules[*]["name"]：原来没有名字的分子（引擎给的中间体），PubChem 上有名字的话借来用（英文）
            warnings：这次 PubChem 连不上时加一条提醒
        另外写数据库：
            新查到的 PubChem 结果存进 substance_hazards 表（SafetyService 把新行加进会话，
            本阶段末尾的 commit 一起提交）。
        联网：查 PubChem 时每秒最多 3 次请求；有一次请求失败，这次分析后面就不再查，
            查不到的物质如实标“没有危害数据”。
        """
        service = SafetyService(self.session, self.ctx.reference, self.ctx.pubchem, self.ctx.settings.jurisdictions)
        molecules = self.result["molecules"]
        # assessed：这次分析里已经查过的物质 {key14: 评估结果}。同一个物质会在很多步里出现，只查一次
        assessed: dict[str, dict[str, Any]] = {}
        for reaction in self.result["reactions"].values():
            hazards = []
            # 按角色依次处理三组物质。产物只有一个，包成列表是为了和前两组走同一段代码
            for role, keys in (("reactant", reaction["reactants"]), ("reagent", reaction["reagents"]), ("product", [reaction["product"]])):
                for key in keys:
                    if key not in assessed:
                        assessed[key] = service.assess(molecules[key]["smiles"], molecules[key].get("name"))
                        # 这个分子还没有名字、而 PubChem 给了名字：写回 molecules，之后各处显示的就是它
                        if not molecules[key].get("name") and assessed[key].get("pubchem_name"):
                            molecules[key]["name"] = assessed[key]["pubchem_name"]
                    # {**a, "role": role}：把评估结果复制一份再加上角色。同一个物质在不同的步骤里角色可以不同
                    hazards.append({**assessed[key], "role": role})
            reaction["hazards"] = hazards
        self._check_cancel()

        def count(flag: str) -> int:
            """这次查过的物质里，带某个危害类别（flag）的有几种。"""
            return sum(flag in hazard["flags"] for hazard in assessed.values())

        # 下面拼进度里的那句话：一共查了几种物质，其中剧毒（急性毒性 1–2 类）、
        # 致癌致突变生殖毒性（1 类）、列入管制清单的各有几种，还有几种没有危害数据
        controlled = sum(bool(hazard["controlled"]) for hazard in assessed.values())
        unknown = sum(hazard["data_source"] == "none" for hazard in assessed.values())
        message = i18n.tr(
            "检查了 {total} 种物质：剧毒 {toxic} · 致癌致突变生殖毒性 {cmr} · 管制 {controlled}",
            "Checked {total} {substances}: {toxic} highly toxic · {cmr} CMR · {controlled} controlled",
            total=len(assessed), substances=_plural(len(assessed), "substance"),
            toxic=count("acute_toxic_12"), cmr=count("cmr_1"), controlled=controlled,
        )
        if unknown:
            # 接在后面的半句也经过 i18n.tr，合成一整句，英文才对得上
            message = i18n.tr("{message} · {unknown} 种没有危害数据", "{message} · {unknown} without hazard data",
                              message=message, unknown=unknown)
        if service.pubchem_failed:
            self.result["warnings"].append(PUBCHEM_FAILED_WARNING)
        self._set_stage(5, "done", message)
        self._save_result()

    def _conditions(self) -> None:
        """第 6 阶段：条件与复杂度（M7）、产率、绿色化学（M9），并算出每一步“和路线无关”的五个维度分。

        读：
            result["reactions"] 每一步的反应物、试剂、产物、反应名
            第 4 阶段的 evidence（最好的那条先例的条件文字和温度）和 yield
            第 5 阶段的 hazards（base_dimensions 算安全分时用）
            演示路线自带的条件说明 given_conditions
            规则和配置：data/condition_rules.toml、data/scoring.toml、内置试剂表里的溶剂评级
        调用：
            m07_conditions.py 的 ConditionEvaluator.evaluate()  按规则打操作标签、算操作分
            m09_green.py 的 evaluate_green()                   原子经济性和溶剂评级
            m10_scoring.py 的 base_dimensions()                把前面各阶段的结果换算成维度分
            M8 产率模型没有接入：产率只用第 4 阶段从先例里取到的报道值，这里不做预测
        写进 result：
            reactions[*]["conditions"] = {
                text             条件说明文字（演示数据自带的，或者最好的先例里记的）；没有则为 None
                source           文字的出处："DEMO"、先例编号（比如 "P1"），或者借用试剂的出处；都没有则为 None
                temperature_c    先例记录的温度（摄氏度）；没有则为 None
                tags             命中的条件规则，每条有 rule_id、label、short、category、penalty（扣分）、
                                 reason、constraint、purification
                operation_score  操作分（0–100）；既不知道试剂也不知道条件、又没有命中规则时是 None（未评估）
                operation_level / operation_label  “简单 / 中等 / 复杂”，以及显示用的标签
            }
            reactions[*]["green"] = {atom_economy, solvents, solvent_class, score, basis}
            reactions[*]["dimensions"] = literature、operation、safety、yield、green 五项，
                每项是 {score（0–100 或 None）, basis（一句话依据）, sources（出处编号）}
            第六个维度“可得性”取决于这一步在哪条路线里（同一个分子在这条路线里是原料，在那条里可能是中间体），
            留到第 7 阶段按路线算。
        """
        evaluator = ConditionEvaluator(self.ctx.reference)
        molecules = self.result["molecules"]
        target_key = self.result["target"]["key14"]
        # 两个计数器，只为了拼进度里的那句话：需要特殊操作的有几步、有文献报道产率的有几步
        special = with_yield = 0
        for reaction in self.result["reactions"].values():
            # 把这一步的反应物、试剂、产物从 key14 换成 SMILES
            reactants = [molecules[k]["smiles"] for k in reaction["reactants"]]
            reagents = [molecules[k]["smiles"] for k in reaction["reagents"]]
            product = molecules[reaction["product"]]["smiles"]
            precedents = reaction["evidence"]["precedents"]
            # “最好的先例”只在证据等级是 E1 / E2 时才采信（和借用试剂是同一个标准）；否则当作没有
            best = (best_precedent(reaction["evidence"])
                    if precedents and reaction["evidence"].get("level") in ("E1", "E2") else None)

            # 条件说明文字从哪来，按先后顺序：
            #   1. 演示路线自带的（教科书层面的概述），出处记 "DEMO"
            #   2. 最好的先例里记的条件文字，出处记这条先例的编号
            #   3. 都没有：文字是 None；试剂是从先例借来的话，出处记那条先例，否则也是 None
            if reaction.get("given_conditions"):
                text, source = reaction["given_conditions"], "DEMO"
            elif best and best.get("conditions_text"):
                text, source = best["conditions_text"], best["ref"]
            else:
                text, source = None, reaction.get("reagents_source")
            evaluated = evaluator.evaluate(
                name=reaction["name"],
                substances=reactants + reagents,
                # 演示数据里的条件是给人看的概述，不拿去做关键词匹配，免得一句“无水乙醚”触发两条规则
                conditions_text=text if source != "DEMO" else None,
                temperature_c=best.get("temperature_c") if best else None,
                # 有条件文字或者有试剂，才算“知道这一步怎么做”。都没有时，即使一条规则都没命中，
                # 也不能说操作简单，只能标“未评估”
                conditions_known=bool(text or reagents),
            )
            # **evaluated 把 tags、operation_score、operation_level、operation_label 四项并进来
            reaction["conditions"] = {
                "text": text,
                "source": source,
                "temperature_c": best.get("temperature_c") if best else None,
                **evaluated,
            }
            reaction["green"] = evaluate_green(self.ctx.reference, reactants, product, reagents)
            # M8 接入后，在这里对没有文献产率的步骤调用 self.ctx.yield_predictor
            reaction["dimensions"] = base_dimensions(reaction, target_key, self.ctx.reference.scoring)
            # 命中的规则里只要有一条带 constraint（对应 A10 那五种“做不了的条件”：加压气体、无水无氧、
            # 手套箱、低温、光电），这一步就算需要特殊操作
            special += any(tag.get("constraint") for tag in evaluated["tags"])
            with_yield += reaction["yield"]["source"] != "none"
        total = len(self.result["reactions"])
        self._set_stage(6, "done", i18n.tr(
            "{total} 个步骤：{special} 步需要特殊操作 · {with_yield} 步有文献报道产率",
            "{total} {steps}: {special} {need} special handling · {with_yield} with literature-reported yield",
            total=total, steps=_plural(total, "step"), special=special, need=_plural(special, "needs", "need"),
            with_yield=with_yield,
        ))
        self._save_result()

    def _score(self) -> None:
        """第 7 阶段：评分与排序（M10）。算步骤分、路线分、置信度，按硬约束排除路线，给剩下的排名。

        读：
            result 里前面各阶段写好的全部内容（这个阶段不查数据库、不联网，只做计算）
            self.effective（第 1 阶段合并的约束）、analysis.user_exclusions（用户排除的分支和试剂）
            data/scoring.toml（权重、0.6 / 0.4 系数、置信度规则）
        调用：
            m10_scoring.py 的 score_result()
        写进 result：
            routes[*] 每条路线新增：
                steps                 每一步的六个维度分 dimensions、步骤分 score、soft_hits、soft_penalty、
                                      coverage、thin、confidence、evidence_level、stars、operation_label、
                                      materials（这一步要准备的物料）、experiment
                availability_details  每一步“可得性”这一维的分数和依据
                score                 路线分 = 0.6 × 各步平均分 + 0.4 × 最弱一步
                dims_avg              六个维度各步的平均分（雷达图用）
                confidence / confidence_label / confidence_reason  置信度 high / medium / low、中文、原因
                min_stars、weakest（最薄弱环节）、summary（R7 汇总条）
                exclusion_reasons     违反了哪些硬约束，每条带 constraint 编号和中文说明
                excluded              是否被排除
                relaxed               这里恒为 False（只有 B6 临时放宽条件时才会是 True）
                rank                  名次；被排除的路线是 None
            ranking   可选路线的 id，按名次排好
            excluded  被排除的路线的 id
            weights   这次用的权重
            stage = "scored"、scoring_version（scoring.toml 里的 version）
        事件：发 scored。结果页（GET …/result）从这时起可以打开；这时每一步的说明还没写。
        """
        score_result(self.result, self.effective, self.ctx.reference, user_exclusions=self.analysis.user_exclusions or [])
        self.result["stage"] = "scored"
        # 记下评分配置的版本号，事后能知道这个结果是按哪一版参数算的
        self.result["scoring_version"] = self.ctx.reference.scoring.get("version")
        routes = self.result["routes"]
        ranking = self.result["ranking"]
        message = i18n.tr("{total} 条路线已评分：可选 {eligible} 条，被排除 {excluded} 条",
                          "{total} {routes} scored: {eligible} eligible, {excluded} excluded",
                          total=len(routes), routes=_plural(len(routes), "route"),
                          eligible=len(ranking), excluded=len(routes) - len(ranking))
        if ranking:
            # ranking[0] 是第一名的路线 id；next(…) 在 routes 里把这条路线找出来。
            # :g 去掉分数末尾多余的 0（81.0 显示成 81）
            best = next(route for route in routes if route["id"] == ranking[0])
            message = i18n.tr("{message}；最高 {score:g} 分", "{message}; top score {score:g}",
                              message=message, score=best["score"])
        self._set_stage(7, "done", message)
        self._save_result("scored")

    def _explain(self) -> None:
        """第 8 阶段：写说明（M11）。给排名最前的几条路线各写一段推荐理由，并给它们的每一步写一段说明。

        读：
            result["ranking"]（只取前 EXPLAIN_TOP_ROUTES 条，默认 3）和这些路线的 steps
            每一步的证据卡：cards.build_card() 把这一步在 result 里的全部依据拼成一张卡
        调用：
            m11_explain.py 的 Explainer.explain_route()、explain_step()。
            配了大模型就调大模型（每句话都要带出处，通不过核对的句子丢掉）；
            没配、大模型没调通（LLMError）或者句子全被丢掉时，按模板生成，不会因此让分析失败。
        写进 result：
            routes[前几条]["explanation"]   整条路线的推荐理由（报告封面用）
            routes[前几条]["explanations"]  {步骤编号: 这一步的说明}（证据卡用）
                两者的结构相同：{sentences（每句带 sources）, text（拼好的一段话，句末带出处标记）,
                generated_by（"llm" 或 "template"）, provider, model, dropped_sentences, note}
            stage = "final"
        另外写数据库：
            每次大模型调用记一行 llm_calls（LLMService 用它自己的会话写，不经过这里的会话）。
        没排进前几名的路线和被排除的路线，这里不写；用户以后点开它们的证据卡时由接口现写
        （api/analyses.py 的 get_step_card）。
        """
        explainer = Explainer(self.ctx.llm)
        # 传给大模型调用层记账用：这次调用算在哪个课题组、哪次分析头上
        ids = {"org_id": self.analysis.org_id, "analysis_id": self.analysis.id}
        by_id = {route["id"]: route for route in self.result["routes"]}
        # ranking 是按名次排好的路线 id；切片取前 N 个。可选路线不足 N 条时，有几条算几条
        top = [by_id[route_id] for route_id in self.result["ranking"][: self.ctx.settings.explain_top_routes]]
        written = 0
        # 同一步反应可能出现在好几条路线里（比如几条路线共用第 1 步）。
        # 这里按 (步骤编号, 在路线里是第几步, 步骤分) 认定“是同一段说明”：三项都相同就直接复用，
        # 不再生成一遍（也就少调一次大模型）
        cache: dict[tuple[str, int, float | None], dict[str, Any]] = {}
        for route in top:
            self._check_cancel()
            route["explanation"] = explainer.explain_route(route, **ids)
            route["explanations"] = {}
            for step in route["steps"]:
                identity = (step["key"], step["index"], step["score"])
                if identity not in cache:
                    self._check_cancel()
                    # 证据卡是 M11 唯一的输入：大模型只看这张卡，卡里每一项都带来源编号
                    card = build_card(self.result, route, step["index"], self.ctx.reference)
                    cache[identity] = explainer.explain_step(card, **ids)
                route["explanations"][step["key"]] = cache[identity]
            # 这条路线算写了“1 段推荐理由 + 每步 1 段说明”。复用缓存的也算在内，
            # 所以进度里报的段数可能比实际调用大模型的次数多
            written += 1 + len(route["steps"])
        self.result["stage"] = "final"
        if self.ctx.llm.enabled:
            message = i18n.tr("给前 {routes} 条路线写了 {written} 段说明",
                              "Wrote {written} {explanations} for the top {routes} {noun}",
                              routes=len(top), written=written, explanations=_plural(written, "explanation"),
                              noun=_plural(len(top), "route"))
        else:
            message = i18n.tr("给前 {routes} 条路线生成了 {written} 段说明（未配置大模型，按模板生成）",
                              "Generated {written} {explanations} for the top {routes} {noun} (no LLM configured; built from templates)",
                              routes=len(top), written=written, explanations=_plural(written, "explanation"),
                              noun=_plural(len(top), "route"))
        self._set_stage(8, "done", message)
        self._save_result()


def _close_running_stages(analysis: models.Analysis, status: str, message: str) -> None:
    """分析中途结束（出错或取消）时收尾：把还停在 running 的阶段改成给定的状态和说明。

    参数：
        analysis  这次分析的那一行。直接改它的 stages，不提交，由调用方提交。
        status    "failed" 或 "cancelled"。
        message   写进这一阶段的话：“这一阶段出错了”或“已取消”。
    说明：
        还没开始的阶段保持 pending，已经完成的保持 done，所以进度面板上能看出停在了哪一步。
        这里不记 stage 事件，也不写 finished_at。
    谁在用：
        本文件的 run_analysis()。
    """
    # 和 _set_stage 一样：复制一份、改完再整体赋值，JSON 列才会被保存
    stages = [dict(stage) for stage in analysis.stages]
    for stage in stages:
        if stage["status"] == "running":
            stage["status"] = status
            stage["message"] = message
            stage["message_en"] = i18n.en_of(message)
    analysis.stages = stages


def run_analysis(ctx: AppContext, analysis_id: str) -> None:
    """任务队列的入口。成功、失败、取消都会落到数据库里，前端通过进度事件知道结果。

    参数：
        ctx          运行时上下文。
        analysis_id  要跑的分析的编号（任务参数里的 analysis_id）。
    返回：
        None。八个阶段里出的任何错都不会抛出来，而是记在这次分析的 status 和 error 里。
        （只有本函数自己读写数据库失败时，异常才会抛给 taskqueue.run_job()，在那里记进日志。）
    流程：
        1. 开一个会话，读出这一行。不存在，或者状态已经不是 queued / running（比如排队期间被取消了），
           就直接返回，什么都不做；
        2. 状态改成 running，attempts 加 1，记开始时间，清掉上一轮的错误和结束时间，进度表重置，
           发一条 status 事件，提交；
        3. 建 AnalysisRunner，跑八个阶段；
        4. 按结局写状态：
             正常跑完           succeeded
             AnalysisCancelled  cancelled，正在跑的阶段标 “已取消”
             其他异常           failed，error 记 “异常类型: 说明”（最多 2000 个字符），
                                正在跑的阶段标 “这一阶段出错了”，并打一条带堆栈的错误日志
           后两种情况先 rollback：丢掉出事那一刻会话里还没提交的改动。之前各阶段已经提交的进度和结果都还在，
           所以取消以后仍然可以看第 2 阶段存下的初步路线树；
        5. 记结束时间，发一条 done 事件，提交。
    读写：
        analyses 表（这一行的大部分列）、analysis_events 表；各阶段还会读库存、在售目录、先例库，
        写 substance_hazards、llm_calls。
    谁在用：
        taskqueue.py 的 job_handlers()（任务类型 "analysis.run"）；哪些地方会把它入队，见 taskqueue.py 的文件头。
        tests/test_app_service.py 里也直接调它。
    """
    # with ctx.session_factory() as session：开一个会话，退出 with 时自动关闭。
    # 它不会自动提交，下面都是自己调 commit
    with ctx.session_factory() as session:
        # session.get(模型, 主键)：按主键取一行，没有就返回 None
        analysis = session.get(models.Analysis, analysis_id)
        if analysis is None or analysis.status not in ("queued", "running"):
            return
        analysis.status = "running"
        analysis.attempts += 1
        analysis.started_at = models.utcnow()
        analysis.finished_at = None
        analysis.error = None
        analysis.stages = fresh_stages()
        analysis.current_stage = 0
        add_event(session, analysis.id, "status", {"status": "running"})
        session.commit()

        runner = AnalysisRunner(ctx, session, analysis)
        try:
            # 八个阶段都在这个 with 里跑：期间 tr() 拼出来的每句话的英文都记进 runner.en
            with i18n.collecting(runner.en, analysis.language or "zh"):
                runner.run()
            analysis.status = "succeeded"
        except AnalysisCancelled:
            # rollback 以后，analysis 上的属性会在下一次读取时从数据库重新加载，
            # 拿到的是最后一次提交时的样子（比如 stages 里正在跑的那个阶段还是 running）
            session.rollback()
            analysis.status = "cancelled"
            _close_running_stages(analysis, "cancelled", i18n.tr("已取消", "Cancelled"))
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，让用户看到原因
            logger.exception("分析 %s 失败", analysis_id)
            session.rollback()
            analysis.status = "failed"
            # [:2000] 截断：有的异常说明很长（比如 AiZynthFinder 子进程失败时带着一大段日志）
            analysis.error = f"{type(exc).__name__}: {exc}"[:2000]
            _close_running_stages(analysis, "failed", i18n.tr("这一阶段出错了", "This stage failed"))
        analysis.finished_at = models.utcnow()
        add_event(session, analysis.id, "done", {"status": analysis.status, "error": analysis.error})
        session.commit()


def rescore(ctx: AppContext, analysis: models.Analysis, *, relaxed=(), weights=None) -> dict[str, Any]:
    """B6 / B11 / B12 之后：用已经存好的评估结果重新过滤和排序，不重新搜索。直接改写 analysis.result。

    参数：
        ctx       运行时上下文（用它的参考数据）。
        analysis  已经评过分的分析。用它的 constraints、user_exclusions 和 result。
        relaxed   要临时放宽的约束编号（只能用关键字传），比如 ["safety:acute_toxic"]；
                  被放宽的约束不再排除路线。默认不放宽。
        weights   六个维度的权重（只能用关键字传）；None 表示用 data/scoring.toml 里的默认权重。
    返回：
        analysis.result 这个字典本身，已经被原地改过：每条路线的 steps、score、exclusion_reasons、excluded、
        rank 等，以及 ranking、excluded、weights 都重新算过。molecules 和 reactions 里的评估结果不变。
    副作用：
        只改内存里的字典，不提交。调用方之后要 flag_modified(analysis, "result") 再 commit 才会保存。
    说明：
        相当于把流水线的第 1 阶段（合并约束）和第 7 阶段（评分排序）重做一遍，其余阶段的结果原样复用。
    谁在用：
        api/analyses.py 的 add_exclusion()、remove_exclusion()（B11）和 mark_owned()（B12），
        三处都没有传 relaxed 和 weights。
        B6 放宽条件用的是同一套算法（m10_scoring.score_result），那个接口在结果的副本上直接调它，
        不改保存的结果。
    """
    effective = merge_constraints(Constraints.model_validate(analysis.constraints), ctx.reference)
    result = analysis.result
    score_result(result, effective, ctx.reference, user_exclusions=analysis.user_exclusions or [], relaxed=relaxed, weights=weights)
    return result


def recover_interrupted(ctx: AppContext) -> list[str]:
    """进程内队列没有持久化：服务重启后，把没跑完的分析重新排队；已经重试过的标记为失败。

    “没跑完”指状态还是 queued 或 running 的分析：进程内队列的任务只在内存里，进程一退出就没人管它们了。
    按 attempts（这一行已经被跑过几次）分两种处理：
        attempts < 2   状态改回 queued，记一条 status 事件 {"status": "queued"}（表示新的一轮开始了），
                       编号放进返回值，由调用方重新入队；
        attempts >= 2  不再重试：标成 failed，error 写 “服务重启时任务被中断，请重新开始分析”，
                       记结束时间，发一条 done 事件。
                       已经被打断过一次又重跑的分析是这种情况；B11 触发的重新搜索跑到一半被打断的也是
                       （那一行此前已经跑过一次）。
    参数：
        ctx  运行时上下文。
    返回：
        需要重新入队的分析编号列表。本函数自己不入队。
    副作用：
        改 analyses 表里这些行的 status / error / finished_at，写 analysis_events，提交。
    谁在用：
        syntherisk/main.py 的 lifespan，只在 QUEUE_BACKEND=inprocess 时调用；
        拿到编号以后逐个 enqueue("analysis.run", …)。
    """
    requeue: list[str] = []
    with ctx.session_factory() as session:
        # session.scalars(select(模型)…) 一行给出一个 Analysis 对象（而不是一行一个元组）；
        # status.in_((…)) 相当于 SQL 里的 status IN ('queued', 'running')
        unfinished = session.scalars(select(models.Analysis).where(models.Analysis.status.in_(("queued", "running"))))
        for analysis in unfinished:
            if analysis.attempts >= 2:
                analysis.status = "failed"
                # 固定的一句，登记了英文（INTERRUPTED_ERROR）：接口返回 error 时按语言换
                analysis.error = INTERRUPTED_ERROR
                analysis.finished_at = models.utcnow()
                add_event(session, analysis.id, "done", {"status": "failed", "error": analysis.error})
            else:
                analysis.status = "queued"
                # 新的一轮从这条事件开始：进度推送对新连上来的前端只发最近一轮的事件
                # （api/analyses.py 的 _round_start），被打断的那一轮留下的半截进度就不会再发出去
                add_event(session, analysis.id, "status", {"status": "queued"})
                requeue.append(analysis.id)
        session.commit()
    return requeue
