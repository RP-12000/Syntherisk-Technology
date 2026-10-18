"""分析任务、结果页和证据卡的接口。

对应按钮：
    A16  开始分析                         POST   /api/analyses
    A17  进度面板（R3，SSE 推送）          GET    /api/analyses/{id}/events    轮询可用 GET /api/analyses/{id}
    A18  先看初步结果                     GET    /api/analyses/{id}/preliminary
    A19  取消                             POST   /api/analyses/{id}/cancel
    B1 B4 B5 B7 B8 B9  排序、切换路线和分支、筛选、高亮最薄弱环节：
         前端用结果里已经算好的分数自己算       GET    /api/analyses/{id}/result
    B6   被排除的路线 · 放宽条件            POST   /api/analyses/{id}/relax
    B10  点击某一步，打开证据卡（R6）        GET    /api/analyses/{id}/routes/{route_id}/steps/{n}
    B11  右键 · 排除分支或试剂             POST   /api/analyses/{id}/exclusions
    B12  右键 · 标记为我有                 POST   /api/analyses/{id}/mark-owned
    C1 C2  文献列表、相似先例              GET    /api/analyses/{id}/routes/{route_id}/steps/{n}/precedents
    C5   问一问                           POST   /api/analyses/{id}/routes/{route_id}/steps/{n}/ask
    A6 A9 B2 B3 C3 C6 只是页面跳转，不调后端。

补充（相关的接口在别的文件里）：A6 的自动保存草稿是 api/core.py 的 /api/drafts；
A9 页面上的“已导入库存 N 种”是 api/inventory.py 的 /api/inventory/summary。见 docs/API.md。

本文件的全部接口（main.py 挂载时统一加了前缀 /api；都要登录）：
    提交和进度
        POST   /api/analyses                           A16 开始分析（返回 202）
        GET    /api/analyses?project_id=               分析列表，最多 200 条
        GET    /api/analyses/{id}                      当前状态和进度（A17 轮询用）
        POST   /api/analyses/{id}/cancel               A19 取消
        POST   /api/analyses/{id}/retry                失败或取消以后重跑（返回 202）
        GET    /api/analyses/{id}/events               A17 进度推送（SSE）
    结果页
        GET    /api/analyses/{id}/preliminary          A18 初步路线树
        GET    /api/analyses/{id}/result?sort=         结果页
        POST   /api/analyses/{id}/relax                B6 临时放宽条件
        GET    /api/analyses/{id}/routes/{route_id}    B4 一条路线的完整内容
    证据卡
        GET    /api/analyses/{id}/routes/{route_id}/steps/{n}              B10 证据卡
        GET    /api/analyses/{id}/routes/{route_id}/steps/{n}/precedents   C1、C2 先例列表
        POST   /api/analyses/{id}/routes/{route_id}/steps/{n}/ask          C5 问一问
    路线树上的右键操作
        POST   /api/analyses/{id}/exclusions                  B11 排除分支或试剂
        DELETE /api/analyses/{id}/exclusions/{exclusion_id}   B11 撤销排除
        POST   /api/analyses/{id}/mark-owned                  B12 标记为我有
    其他
        GET    /api/analyses/{id}/llm-usage            这次分析的大模型用量

对应模块：M0（应用服务）把任务交给 MQ（任务队列），后台按 pipeline.py 的八个阶段依次调 M3–M11。
结果页上的操作大多只读已经存好的结果，或者让 M10 重新过滤、排序，都是秒级的。
会当场调别的模块的只有：证据卡缺说明时调 M11，“问一问”调 M12，“标记为我有”调 M4，
先例列表调 M1 比较结构（开了配置时还会联网查文献题目）。

数据从哪来到哪去：
    提交  请求体 → analyses 表里新建一行（status = queued）→ ctx.queue.enqueue("analysis.run") →
          后台的 pipeline.run_analysis 一个阶段一个阶段地跑：进度写进 analyses.stages 和 analysis_events 表，
          结果写进 analyses.result（加密的 JSON，里面是 molecules、reactions、routes 三张互相引用的表，
          见 syntherisk/routes.py 的文件头）。
    读取  接口从 analyses.result 取出结果，用 api/views.py（结果页）或 syntherisk/cards.py（证据卡）
          整理成前端要的样子。
    改写  排除、撤销排除、标记为我有：先改 analyses.user_exclusions 或者库存，再调 pipeline.rescore
          在 result 上原地重算分数和名次（不重新搜索），然后存回数据库。

它依赖谁：
    api/deps.py     登录、按组织取分析（load_analysis / load_scored_analysis / load_finished_analysis）
    api/schemas.py  请求体的格式
    api/views.py    结果的各种视图
    cards.py        build_card 拼证据卡、find_route 按编号找路线
    constraints.py  Constraints、merge_constraints：把提交时的约束合并成实际执行的那一份
    pipeline.py     fresh_stages（八个阶段的初始状态）、add_event（写一条进度事件）、rescore（重新评分）
    modules         m01_structure（结构，简写成 chem）、m04_availability（库存和可得性）、
                    m10_scoring（score_result）、m11_explain（Explainer 写说明）、m12_qa（QuestionAnswerer 问答）
    external.py     ExternalServiceError：联网查文献题目失败时抛的错误
读写的表：
    analyses（读写）、analysis_events（读；取消排队中的分析时写一条）、drafts（开始分析时删掉用过的草稿）、
    inventory_items（B12 写）、catalog_compounds（B12 读）、llm_calls（读；调大模型时由大模型调用层写）。

本文件里有什么（按阅读顺序）：
    TERMINAL          分析的三种结束状态
    _route_or_404     在结果里找一条路线，找不到就 404
    _card             拼一张证据卡，路线或步骤不存在就 404
    create_analysis、list_analyses、get_analysis、cancel_analysis、retry_analysis   提交、列表、状态、取消、重跑
    _events_after     读某个编号之后的进度事件
    stream_events     SSE 进度推送
    get_preliminary、get_result、relax_constraints、get_route                    结果页
    get_step_card、get_step_precedents、ask_about_step                            证据卡
    add_exclusion、remove_exclusion、mark_owned                                    路线树上的右键操作
    llm_usage         大模型用量
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from syntherisk import i18n, models
from syntherisk.api import schemas
from syntherisk.api.deps import (
    Principal, authenticate, get_ctx, get_db, get_principal, load_analysis, load_finished_analysis,
    load_project, load_scored_analysis,
)
from syntherisk.api.views import (
    SORT_OPTIONS, analysis_list_item, analysis_view, drop_stale_explanations, preliminary_view, result_view,
    route_view, step_scores,
)
from syntherisk.cards import build_card, find_route
from syntherisk.constraints import Constraints, merge_constraints
from syntherisk.context import AppContext
from syntherisk.external import ExternalServiceError
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m04_availability import AvailabilityChecker, InventoryService
from syntherisk.modules.m10_scoring import score_result
from syntherisk.modules.m11_explain import Explainer
from syntherisk.modules.m12_qa import QuestionAnswerer
from syntherisk.pipeline import add_event, fresh_stages, rescore

# 本文件的所有接口都登记在这个 router 上，在接口文档页面（/docs）上归到“分析”一组
router = APIRouter(tags=["分析"])
# 分析的三种结束状态（analyses.status）：成功、失败、已取消；另外两种是 queued（排队中）和 running（运行中）。
# 进度推送发现分析到了这三种状态之一、事件也都发完了，就结束推送
TERMINAL = ("succeeded", "failed", "cancelled")


def _route_or_404(result: dict[str, Any], route_id: str) -> dict[str, Any]:
    """在分析结果里按编号找一条路线，找不到就返回 404。

    参数：
        result    analysis.result（解密后的字典）。
        route_id  路线编号，形如 "r1"、"r2"：搜索结果里的顺序号（由 routes.build_skeleton 编的）。
    返回：
        result["routes"] 里的那条路线。注意是字典本身，不是副本：改它就是改 result。
    抛出：
        HTTPException 404 “没有这条路线”。
    谁在用：
        本文件的 _card、relax_constraints、get_route、add_exclusion。
    """
    route = find_route(result, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这条路线", "Route not found"))
    return route


def _card(ctx: AppContext, result: dict[str, Any], route_id: str, step_index: int, *, svg: bool = False) -> tuple[dict, dict]:
    """拼出某条路线某一步的证据卡（R6）。

    参数：
        ctx         运行时上下文，用到 ctx.reference（评分配置里的维度名称和默认权重）。
        result      analysis.result，要已经评过分。
        route_id    路线编号。
        step_index  这条路线的第几步，从 1 数起；第 1 步是实验上最先做的那一步。
        svg         是否给卡里的每个分子附上结构图。
    返回：
        (route, card)：
            route  这条路线（result 里的字典本身）。
            card   证据卡，cards.build_card 的结果；主要字段见 get_step_card 的说明。
    抛出：
        HTTPException 404 “没有这条路线”或“这条路线没有这一步”。
    谁在用：
        本文件的 get_step_card、get_step_precedents、ask_about_step。
    """
    route = _route_or_404(result, route_id)
    # Python 的连续比较：等价于 step_index >= 1 and step_index <= 这条路线的步数
    if not 1 <= step_index <= len(route["steps"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("这条路线没有这一步", "This route has no such step"))
    return route, build_card(result, route, step_index, ctx.reference, with_svg=svg)


# ---------------------------------------------------------------- 提交、进度、取消


@router.post("/analyses", status_code=status.HTTP_202_ACCEPTED)
def create_analysis(body: schemas.AnalysisCreate, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """A16 开始分析：建任务、放进队列，立刻返回。之后用 SSE 或轮询看进度。

    需要登录。请求体见 `AnalysisCreate`。

    成功返回 202（已经接受，还没做完）。内容是这次分析的当前状态，字段同 `GET /api/analyses/{id}`；
    刚提交时 `status` 一般是 `queued`。前端要记住返回的 `id`，后面的接口都用它。
    请求里带了 `draft_id` 时，那份草稿会被删掉。

    出错：404 没有这个项目；422 目标分子的结构有问题（这时 `detail` 不是一句话，而是一个对象
    `{message, errors}`，`errors` 是具体原因的数组），或者起始骨架的结构读不出来。
    """
    # 实现的顺序：查项目 → 校验目标分子和起始骨架 → 在 analyses 表里建一行 → 删掉用过的草稿 → 提交 →
    # 把任务交给队列（后台由 pipeline.run_analysis 去跑八个阶段）→ 返回当前状态
    project = load_project(db, principal, body.project_id)
    # 目标分子：给了 smiles 就用 smiles，否则用 molblock（两个至少有一个，TargetInput 已经保证）。
    # 这里只要校验结果，不需要结构图，所以 with_svg=False
    if body.target.smiles:
        info = chem.validate_structure(body.target.smiles, "smiles", with_svg=False)
    else:
        info = chem.validate_structure(body.target.molblock, "mol", with_svg=False)
    if not info.valid:
        # 这里的 detail 是一个字典：errors 是 m01_structure 给出的具体原因（中文句子的列表）
        raise HTTPException(422, {"message": i18n.tr("目标分子的结构有问题", "The target structure is invalid"), "errors": info.errors})
    # 起始骨架（A5）随约束一起提交；这里只确认每一个都读得出来
    for material in body.constraints.starting_materials:
        if chem.mol_record(material.smiles) is None:
            raise HTTPException(422, i18n.tr("起始骨架的结构读不出来", "A starting scaffold structure could not be read"))

    analysis = models.Analysis(
        org_id=principal.org_id,
        project_id=project.id,
        created_by=principal.user_id,
        status="queued",
        # 八个阶段的初始状态，全部是 pending（pipeline.fresh_stages）
        stages=fresh_stages(),
        # 提交时界面用的语言：大模型写说明时用它
        language=i18n.current_lang(),
        # 目标分子：存校验以后的规范 SMILES 和算出来的基本信息，后台任务直接用
        target={
            "smiles": info.smiles,
            "key14": info.key14,
            # 名字：用户起的优先，其次是内置试剂表里的，都没有就是 None
            "name": body.target.name or ctx.reference.compounds.name_for(info.smiles),
            "formula": info.formula,
            "mol_weight": info.mol_weight,
            "messages": info.messages,
            # 立体化学提示的英文。这几句带数字（“1 个立体中心未指定……”），对照只记在这一次请求里，
            # 而 target 不在 result 里、用不上 result["_en"]，所以把英文直接存在旁边：
            # 返回时 views._target_view 按语言处理（中文去掉这一项，英文用它顶替 messages）
            "messages_en": [i18n.en_of(message) for message in info.messages],
        },
        # 约束原样存成字典；后台任务的第 1 阶段再把它合并成实际执行的那一份
        constraints=body.constraints.model_dump(),
        user_exclusions=[],
    )
    db.add(analysis)
    if body.draft_id:
        # 这次分析是从草稿来的：草稿用完了，删掉。只删本人的草稿；编号不对或者不是本人的就不管它，也不报错
        draft = db.get(models.Draft, body.draft_id)
        if draft is not None and draft.user_id == principal.user_id and draft.org_id == principal.org_id:
            db.delete(draft)
    db.commit()  # 先提交再入队：后台任务要能读到这一行
    # 任务类型 "analysis.run" 对应 pipeline.run_analysis（对照表在 taskqueue.job_handlers）。
    # 进程内队列和 Redis 队列下，这一行马上返回，分析在后台跑；测试用的同步队列会在这一行里把分析整个跑完
    ctx.queue.enqueue("analysis.run", {"analysis_id": analysis.id})
    # 从数据库重新读一遍这一行：后台任务可能已经改了它的状态
    db.refresh(analysis)
    return analysis_view(analysis, ctx)


@router.get("/analyses")
def list_analyses(project_id: str | None = None, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """本课题组的分析列表，最新的在前，最多 200 条。

    需要登录。

    - 查询参数 `project_id`：只看某个项目下的分析；不带就是全组的

    返回一个数组，每项：`id`、`project_id`、`status`、`target`（目标分子）、`route_count`（路线条数）、
    `best_route`（排第一的路线 `{name, score}`；还没有排名时是 `null`）、`created_at`、`finished_at`。
    """
    # 先按课题组过滤，所以带了别的课题组的 project_id 也只会得到空列表。limit(200) 是写死的上限，没有分页
    query = select(models.Analysis).where(models.Analysis.org_id == principal.org_id)
    if project_id:
        query = query.where(models.Analysis.project_id == project_id)
    return [analysis_list_item(a) for a in db.scalars(query.order_by(models.Analysis.created_at.desc()).limit(200))]


@router.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                 ctx: AppContext = Depends(get_ctx)):
    """R2 约束标签 + R3 进度面板的当前状态。

    需要登录。A17 不用 SSE 时，可以轮询这个接口。返回：

    - `id`、`project_id`
    - `status`：`queued` 排队中 / `running` 运行中 / `succeeded` 成功 / `failed` 失败 / `cancelled` 已取消
    - `current_stage`：进行到第几阶段（1–8，还没开始是 0）；`stage_count`：一共几个阶段
    - `stages`：八个阶段的数组。每项：`index`、`key`、`name`、`module`、`hint`、
      `status`（`pending` / `running` / `done` / `skipped` / `failed` / `cancelled`）、
      `message`（这个阶段的一句话结果）、`started_at`、`finished_at`
    - `target`：目标分子 `{smiles, key14, name, formula, mol_weight, messages}`
    - `constraints`：提交时的约束（补全了默认值）
    - `constraint_tags`：R2 约束标签，每项 `{key, label, kind, hardness, source}`
    - `engine`：这次用的逆合成引擎（`demo` 或 `aizynthfinder`）；第一次搜索完成之前是 `null`
    - `error`：失败原因；没失败是 `null`
    - `cancel_requested`：用户是否已经点了取消
    - `user_exclusions`：B11 用户排除的分支和试剂
    - `result_stage`：结果到了哪一步：`null` 还没有 / `preliminary` 初步路线树 / `scored` 已评分 / `final` 全部完成
    - `preliminary_ready`：是否可以先看初步结果了（A18）
    - `route_count`：找到几条路线
    - `created_at`、`started_at`、`finished_at`：带时区的 ISO 8601 时间
    - `elapsed_seconds`：已经用了多少秒

    出错：404 没有这个分析（别的课题组的分析也是 404）。
    """
    # 只读 analyses 表里的这一行，整理成字典（views.analysis_view）
    return analysis_view(load_analysis(db, principal, analysis_id), ctx)


@router.post("/analyses/{analysis_id}/cancel")
def cancel_analysis(analysis_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                    ctx: AppContext = Depends(get_ctx)):
    """A19 取消。正在跑的分析会在当前这一小步做完后停下。

    需要登录。还在排队的分析立刻变成 `cancelled`。正在跑的分析先记下“请求取消”
    （`cancel_requested` 变成 `true`），后台任务查到以后才停，所以 `status` 要稍后才变成 `cancelled`。
    已经结束的分析不受影响。取消之前已经算出来的部分（比如初步路线树）会保留。

    返回这次分析的当前状态，字段同 `GET /api/analyses/{id}`。出错：404 没有这个分析。
    """
    analysis = load_analysis(db, principal, analysis_id)
    if analysis.status == "queued":
        # 还没开始跑：直接标成已取消。队列里的那个任务以后轮到时，pipeline.run_analysis 看到状态
        # 已经不是 queued / running，会直接返回，不会再跑
        analysis.status = "cancelled"
        analysis.finished_at = models.utcnow()
        # 补一条 done 事件：正在听进度推送的前端靠它知道分析结束了
        add_event(db, analysis.id, "done", {"status": "cancelled", "error": None})
    elif analysis.status == "running":
        # 正在跑：只立一个标记。后台任务在每个阶段开始之前、以及几处耗时的循环里会查这个标记
        # （pipeline.AnalysisRunner._check_cancel），查到就停下，由它自己把状态改成 cancelled 并发 done 事件
        analysis.cancel_requested = True
    # 其余状态（已经成功、失败或取消）：什么都不改，照样返回当前状态
    db.commit()
    return analysis_view(analysis, ctx)


@router.post("/analyses/{analysis_id}/retry", status_code=status.HTTP_202_ACCEPTED)
def retry_analysis(analysis_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                   ctx: AppContext = Depends(get_ctx)):
    """失败或取消之后，用同样的目标分子和约束重新跑一遍。

    需要登录。成功返回 202 和这次分析的当前状态（字段同 `GET /api/analyses/{id}`），
    之后像新提交的分析一样看进度。分析的 `id` 不变。

    出错：404 没有这个分析；409 只有失败或已取消的分析可以重跑。
    """
    analysis = load_analysis(db, principal, analysis_id)
    if analysis.status not in ("failed", "cancelled"):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("只有失败或已取消的分析可以重跑", "Only failed or cancelled analyses can be rerun"))
    # 把状态改回“排队中”，清掉上一次留下的取消标记、尝试次数、错误信息和各阶段的进度。
    # 上一次留在 result 里的内容这里不清；后台任务跑完第 2 阶段（搜索）时会把它整个换成新的
    analysis.status = "queued"
    analysis.cancel_requested = False
    analysis.attempts = 0
    analysis.error = None
    analysis.stages = fresh_stages()
    # 新的一轮从这条事件开始：进度推送对新连上来的前端只发最近一轮的事件（见 _round_start）
    add_event(db, analysis.id, "status", {"status": "queued"})
    # 和开始分析一样：先提交，再入队，再重新读一遍这一行
    db.commit()
    ctx.queue.enqueue("analysis.run", {"analysis_id": analysis.id})
    db.refresh(analysis)
    return analysis_view(analysis, ctx)


def _events_after(ctx: AppContext, analysis_id: str, last_id: int) -> tuple[list[tuple[int, str, dict]], str | None]:
    """从数据库读某次分析在编号 last_id 之后新写入的进度事件，以及这次分析现在的状态。

    参数：
        ctx          运行时上下文，用它的 session_factory 临时开一个数据库会话。
        analysis_id  分析编号。
        last_id      已经发给前端的最后一条事件的编号（analysis_events.id，自增的整数）；从头读就传 0。
    返回：
        (events, current)：
            events   新事件的列表，按编号从小到大排。每条是 (事件编号, 事件类型, 事件内容的字典)。
            current  这次分析现在的 status；分析不存在时是 None。
    副作用：
        只读数据库（analysis_events、analyses 两张表）。每次调用开一个会话，用完就关。
    谁在用：
        本文件的 stream_events，每半秒调一次。
    """
    # with 块结束时会话自动关闭。进度推送的连接可能开很久；这里不一直占着一个会话，每查一次现开现关
    with ctx.session_factory() as session:
        events = session.scalars(
            select(models.AnalysisEvent)
            .where(models.AnalysisEvent.analysis_id == analysis_id, models.AnalysisEvent.id > last_id)
            .order_by(models.AnalysisEvent.id)
        ).all()
        # 只取 status 这一列：session.scalar 返回第一行第一列的值
        current = session.scalar(select(models.Analysis.status).where(models.Analysis.id == analysis_id))
        # 在会话关闭之前把需要的值取出来，做成普通的元组再返回
        return [(event.id, event.type, event.payload) for event in events], current


def _round_start(ctx: AppContext, analysis_id: str) -> int:
    """最近一轮的进度事件从哪里开始：返回这一轮第一条事件的前一个编号，第一轮返回 0。

    一次分析可以跑好几轮（失败或取消以后重跑、路线被排光以后重新搜索、服务重启以后接着跑），每一轮的事件
    都留在 analysis_events 表里。除了第一轮，每一轮开始时都会先记一条 status 事件 {"status": "queued"}；
    这里找的就是最后一条这样的事件。

    参数：
        ctx          运行时上下文，用它的 session_factory 临时开一个数据库会话。
        analysis_id  分析编号。
    返回：
        一个事件编号，交给 _events_after 当 last_id 用：编号比它大的事件就是最近一轮的全部事件
        （包括开头那条 status: queued）。没有重跑过的分析返回 0，也就是从头发。
    副作用：
        只读数据库（analysis_events 表）。
    谁在用：
        本文件的 stream_events：前端没有说从哪条事件接着发时，用它决定起点。
    """
    with ctx.session_factory() as session:
        # 只看 status 类型的事件（每一轮只有一两条），从新到旧排
        statuses = session.execute(
            select(models.AnalysisEvent.id, models.AnalysisEvent.payload)
            .where(models.AnalysisEvent.analysis_id == analysis_id, models.AnalysisEvent.type == "status")
            .order_by(models.AnalysisEvent.id.desc())
        ).all()
    for event_id, payload in statuses:
        if payload.get("status") == "queued":
            return event_id - 1
    return 0


@router.get("/analyses/{analysis_id}/events")
async def stream_events(analysis_id: str, request: Request, access_token: str | None = None,
                        last_event_id: int = 0, ctx: AppContext = Depends(get_ctx)):
    """R3 进度推送（Server-Sent Events）。

    事件类型：status、stage（某个阶段开始或完成）、preliminary_ready（可以先看初步结果）、
    scored（结果页可以打开了）、done（结束），最后发一个 end。
    浏览器自带的 EventSource 不能带请求头，所以这个接口也接受 ?access_token=；
    断线重连时带上 Last-Event-ID，会从断开的地方接着发。

    需要登录：令牌放在请求头 `Authorization: Bearer <令牌>` 里，或者放在查询参数 `access_token` 里。

    - 查询参数 `access_token`：登录令牌，请求头里没带时用它
    - 查询参数 `last_event_id`：从哪条事件之后开始发；请求头 `Last-Event-ID` 优先。
      都不带就从最近一轮的开头发

    返回 `text/event-stream`。每条事件三行：`id: 事件编号`、`event: 事件类型`、`data: 一段 JSON`。
    各类事件的 `data`：

    - `status`：`{"status": "running"}`；重跑、重新搜索、服务重启后接着跑时，新的一轮先发一条
      `{"status": "queued"}`
    - `stage`：`{"index": 第几阶段, "status": "running" / "done" 等, "message": 这个阶段的一句话结果}`
    - `preliminary_ready`、`scored`：`{}`
    - `done`：`{"status": "succeeded" / "failed" / "cancelled", "error": 失败原因或 null}`
    - `end`：`{"status": 最终状态}`，没有 `id` 那一行。收到它就可以关闭连接

    分析早就结束了再来连，也会把存着的事件发一遍，然后发 `end`。关掉页面不影响分析，它在后台继续跑。
    重跑或者重新搜索过的分析，前几轮的事件还留在库里，但新连上来时只发最近一轮的，
    所以收到的 `done` 一定是这一轮的。

    出错（在开始推送之前返回，是普通的 JSON 错误）：401 没登录；404 没有这个分析。
    """
    # 这个接口没有用 Depends(get_principal)：get_principal 只认请求头里的令牌，而这里的令牌也可能在网址里。
    # 所以自己取令牌、自己调 authenticate。
    # 请求头形如 “Bearer xxxxx”：前 7 个字符是 “Bearer ”（含一个空格），[7:] 取出后面的令牌
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else access_token

    def check_access() -> None:
        """检查令牌有效，并且这次分析属于他所在的课题组。

        不合格时抛 HTTPException：401（authenticate）或 404（load_analysis）。只读数据库。
        """
        with ctx.session_factory() as session:
            load_analysis(session, authenticate(session, ctx, token), analysis_id)

    # 这是 async 接口：直接在这里查数据库，查的时候整个事件循环都得等着，别的请求也被卡住。
    # run_in_threadpool 把这个同步函数放到线程池里跑，等它跑完再往下走
    await run_in_threadpool(check_access)
    # 从哪条事件之后开始发。浏览器的 EventSource 断线重连时，会自动带上请求头 Last-Event-ID
    # （值是它收到的最后一条事件的 id）
    cursor = int(request.headers.get("last-event-id") or last_event_id or 0)
    if not cursor:
        # 前端没有说从哪里接着发（新打开页面）：只发最近一轮的事件。不然重跑过的分析会先把上一轮的事件
        # 重放一遍，前端一收到上一轮的 done 就以为这次分析结束了
        cursor = await run_in_threadpool(_round_start, ctx, analysis_id)

    # 进度说明要哪种语言：?lang=en 或者请求头 X-Language（EventSource 带不了请求头，所以一般用查询参数）
    lang = i18n.normalize_lang(request.query_params.get("lang") or request.headers.get("x-language"))

    async def generate():
        """异步生成器：一段一段地产出要发给前端的文本，StreamingResponse 边产出边发送。

        每半秒查一次数据库：有新事件就发；没有新事件、分析又已经结束，就发 end 并停下。
        前端断开连接以后，循环自己退出。
        """
        # nonlocal：下面要修改外层函数里的 cursor 变量
        nonlocal cursor
        # quiet：累计有多少次没查到新事件（查到新事件时不清零）
        quiet = 0
        while not await request.is_disconnected():
            events, current = await run_in_threadpool(_events_after, ctx, analysis_id, cursor)
            for event_id, event_type, payload in events:
                cursor = event_id
                # 英文：换成英文的进度说明；中文：去掉事件里附带的英文（message_en），和以前的格式一样
                shown = (i18n.localize(payload, lang) if lang == "en"
                         else {k: v for k, v in payload.items() if not k.endswith("_en")})
                # SSE 的格式：每条事件是几行 “字段: 值”，以一个空行结束。
                # ensure_ascii=False 让中文原样输出，不变成 \uXXXX
                yield f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(shown, ensure_ascii=False)}\n\n"
            if not events:
                # 没有新事件了，分析也到了结束状态：发一个 end，推送结束。
                # 先把事件发完、再看状态，所以最后那条 done 事件一定在 end 之前发出去
                if current in TERMINAL:
                    yield f"event: end\ndata: {json.dumps({'status': current})}\n\n"
                    return
                quiet += 1
                # 每满 30 次没动静（一次间隔 0.5 秒，约 15 秒），发一行以冒号开头的内容。
                # SSE 里冒号开头的行是注释，浏览器会忽略；它的用处是让连接上一直有数据在走
                if quiet % 30 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    # media_type 是 SSE 规定的 text/event-stream。Cache-Control: no-cache 不让浏览器和代理缓存；
    # X-Accel-Buffering: no 是给 nginx 这类反向代理看的，让它不要把数据攒着不发
    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- 结果页


@router.get("/analyses/{analysis_id}/preliminary")
def get_preliminary(analysis_id: str, svg: bool = False, principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)):
    """A18：搜索一完成就能看的路线树（还在评估，没有分数）。

    需要登录。进度推送里收到 `preliminary_ready`（或者状态里 `preliminary_ready` 是 `true`）以后就可以调。

    - 查询参数 `svg`：是否给每个分子附上结构图（SVG），默认不附

    返回：

    - `stage`：结果到了哪一步（`preliminary` / `scored` / `final`）
    - `engine`：`{name, version, stats}`，这次用的逆合成引擎和它的搜索统计
    - `target`：目标分子
    - `routes`：每条路线 `{id, name, n_steps, score, tree}`。`score` 在评分完成之前是 `null`；
      `tree` 是路线树，格式同 `GET /api/analyses/{id}/routes/{route_id}` 返回的 `tree`
    - `warnings`：数据方面的提示

    出错：404 没有这个分析；409 逆合成搜索还没完成，暂时没有初步结果（搜索完了但一条路线都没找到时，
    也是这个 409）。
    """
    analysis = load_analysis(db, principal, analysis_id)
    # result 是 None（还没搜完）或者 routes 是空列表（没搜到路线），都走这个 409
    if not (analysis.result or {}).get("routes"):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("逆合成搜索还没完成，暂时没有初步结果", "The retrosynthesis search has not finished; no preliminary results yet"))
    return preliminary_view(analysis.result, with_svg=svg)


@router.get("/analyses/{analysis_id}/result")
def get_result(analysis_id: str, sort: str = Query("score", pattern=f"^({'|'.join(SORT_OPTIONS)})$"),
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """结果页：R4 路线列表、R5 分叉、R7 汇总条、R8 被排除的路线（写明原因）。

    需要登录。进度推送里收到 `scored` 以后就可以调，不用等第 8 阶段把说明写完。

    - 查询参数 `sort`：B1 排序方式。`score` 综合（默认）、`steps` 最短、`safety` 最安全、
      `availability` 原料最好拿、`literature` 文献最充分、`green` 最绿色

    返回：

    - `stage`、`engine`、`target`、`warnings`：同初步结果
    - `constraint_tags`：R2 约束标签
    - `sort`、`sort_options`：这次用的排序方式，和全部可选的排序方式 `{编号: 显示文字}`
    - `routes`：可选的路线，已经按 `sort` 排好。每条是一张路线卡片（字段见下）
    - `excluded_routes`：被排除的路线，卡片的格式相同，原因在 `exclusion_reasons` 里
    - `graph`：R5 分叉信息 `{target, alternatives, top_branches}`：每个分子有哪几种做法、
      各属于哪些路线、每种做法里分数最高的是哪条路线（B8 切换分支时前端直接用）
    - `weights`：这次评分用的权重

    路线卡片的字段：`id`、`name`、`title`、`rank`（名次；被排除的是 `null`）、`score`（路线分）、
    `confidence` / `confidence_label` / `confidence_reason`（置信度和原因）、`n_steps`、`min_stars`、
    `evidence_text`、`inventory_count`（如 `1/9`）、`inventory_coverage`（百分数）、`special_operations`、
    `dims_avg`（六个维度各步的平均分）、`summary`（R7 汇总条）、`weakest`（B9 最薄弱环节）、`excluded`、
    `exclusion_reasons`、`relaxed`、`step_keys`、`steps`（每一步的分数和六个维度的分数）。

    出错：404 没有这个分析；409 这次分析还没有评分结果；422 `sort` 不是上面六种之一。
    """
    # sort 的取值由参数上的正则表达式限制：把 SORT_OPTIONS 的六个键用 | 连起来，
    # 得到 ^(score|steps|safety|availability|literature|green)$，不符合时 FastAPI 直接返回 422。
    # 只读：从存好的 result 整理出结果页的数据（views.result_view），不重新算分
    return result_view(load_scored_analysis(db, principal, analysis_id).result, sort=sort)


@router.post("/analyses/{analysis_id}/relax")
def relax_constraints(analysis_id: str, body: schemas.RelaxRequest, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """B6：临时放宽条件，看被排除的路线。只重新过滤和排序，不重新搜索，也不改动保存的结果。

    需要登录。请求体见 `RelaxRequest`：给约束编号，或者给一条被排除的路线，两种可以同时给。

    返回的格式和结果页（`GET /api/analyses/{id}/result`）相同，按综合排序，另外多一个字段：

    - `relaxed_constraints`：这次实际放宽了哪些约束编号（排过序）

    靠放宽才回到可选列表里的路线，卡片上的 `relaxed` 是 `true`（界面上标“放宽后”）。

    出错：404 没有这个分析，或者 `route_id` 不存在；409 这次分析还没有评分结果。
    """
    analysis = load_scored_analysis(db, principal, analysis_id)
    # 深拷贝一份结果，下面的重新评分都在副本上做。数据库里存的那份不动，所以“放宽”只对这一次返回有效
    result = copy.deepcopy(analysis.result)
    relaxed = set(body.constraints)
    if body.route_id:
        route = _route_or_404(result, body.route_id)
        # 这条路线的每条排除原因都带着挡住它的约束编号（constraint），全部加进要放宽的集合。
        # user 为 True 的是用户自己右键排除的（B11），那种要用撤销排除来恢复，这里不放宽。
        # |= 是集合的“并进来”
        relaxed |= {reason["constraint"] for reason in route["exclusion_reasons"] if not reason["user"]}
    # 把提交时存的约束重新合并成实际执行的那一份，再让 M10 在副本上重新过滤、排序。
    # relaxed 里的约束编号在判断“这条路线是否被排除”时不算数（m10_scoring.score_result）
    effective = merge_constraints(Constraints.model_validate(analysis.constraints), ctx.reference)
    score_result(result, effective, ctx.reference, user_exclusions=analysis.user_exclusions or [], relaxed=relaxed)
    view = result_view(result)
    view["relaxed_constraints"] = sorted(relaxed)
    return view


@router.get("/analyses/{analysis_id}/routes/{route_id}")
def get_route(analysis_id: str, route_id: str, svg: bool = False, principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)):
    """一条路线的完整内容：路线树（R5）、每一步的分数、汇总条（R7）、最薄弱环节（B9）。

    B4 点击路线卡片。需要登录。

    - 查询参数 `svg`：是否给路线树上的每个分子附上结构图，默认不附

    返回这条路线的卡片（字段同结果页 `routes` 里的一项），另外多三个字段：

    - `tree`：路线树，从目标分子开始，一层层往原料方向展开。
      分子节点：`type` 是 `mol`，另有 `key14`、`name`、`smiles`、`formula`、`is_leaf`（是不是起始原料）、
      `status` / `status_label`（起始原料从哪来：`inventory` 库存 / `purchasable` 可购买 / `unavailable`
      买不到；中间体是 `null`）、`svg`（要了才有）、`children`（得到它的那一步反应）。
      反应节点：`type` 是 `reaction`，另有 `step_key`、`step_index`（第几步）、`name`、`label`、`score`、
      `stars`、`evidence_level`、`experiment`（R12“已做”标记；没做过是 `null`）、`children`（这一步的反应物）
    - `explanation`：这条路线的推荐理由（M11 写的）；没写过是 `null`
    - `availability_details`：每一步“可得性”一项的分数和依据，键是步骤编号

    出错：404 没有这个分析，或者没有这条路线；409 这次分析还没有评分结果。
    """
    # 只读：从存好的 result 里找到这条路线，整理成字典（views.route_view）
    result = load_scored_analysis(db, principal, analysis_id).result
    return route_view(result, _route_or_404(result, route_id), with_svg=svg)


# ---------------------------------------------------------------- 证据卡


@router.get("/analyses/{analysis_id}/routes/{route_id}/steps/{step_index}")
def get_step_card(analysis_id: str, route_id: str, step_index: int, svg: bool = False,
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                  ctx: AppContext = Depends(get_ctx)):
    """B10 → R6 证据卡。读的是已经存好的结果；只有这一步的说明还没写过时，才调 M11 补写。

    需要登录。路径里的 `step_index` 是这条路线的第几步，从 1 数起（第 1 步是最先做的那一步）。

    - 查询参数 `svg`：是否给产物、反应物、试剂附上结构图，默认不附

    返回证据卡，主要字段：

    - `route_id`、`route_name`、`step_index`、`step_count`、`step_key`、`name`、`title`
    - `score`（步骤分）、`coverage`（这个分数用到了多少权重，0–1）、`thin`（依据太少）、
      `confidence` / `confidence_label`（置信度）
    - `evidence`：证据等级 `{level, label, stars, basis, n_exact, best_similarity, db_empty, compared_by}`
    - `center`：这一步改了哪些键（反应中心）；没有原子映射时是 `null`
    - `precedents`：文献先例的数组（字段见 `…/precedents` 接口）
    - `reaction_smiles`、`product`、`reactants`、`reagents`：反应式和各个分子
    - `conditions`：反应条件和操作标签；`yield`：产率；`hazards`：每个物质的危害；
      `materials`：这一步要准备的物料；`green`：绿色化学
    - `dimensions`：六个维度的数组，每项 `{key, label, short, weight, score, basis, sources}`；
      `score` 是 `null` 表示这一项没有评估
    - `soft_hits`、`soft_penalty`：命中的软偏好，和因此扣掉的分
    - `model`：逆合成模型自己给的依据（不是文献证据）
    - `sources`：卡里用到的全部来源，每项 `{id, type, label}`，文献先例类的来源另有 `url`；
      说明和问答里的出处编号都指向这里
    - `explanation`：这一步的说明 `{sentences, text, generated_by, provider, model, dropped_sentences, note}`
    - `experiment`：回填的实验结果（R12）；没做过是 `null`
    - `alternatives`：得到同一个产物的其他做法（C3 替代分支）

    出错：404 没有这个分析、没有这条路线，或者这条路线没有这一步；409 这次分析还没有评分结果。
    """
    analysis = load_scored_analysis(db, principal, analysis_id)
    result = analysis.result
    # 证据卡是用存好的结果当场拼出来的（cards.build_card），不查库、不联网
    route, card = _card(ctx, result, route_id, step_index, svg=svg)
    # explanation 是 None 的两种情况：第 8 阶段只给排名靠前的几条路线写说明（配置项 explain_top_routes，
    # 默认 3）；重新评分以后分数变了的步骤，旧说明会被删掉（views.drop_stale_explanations）。
    # 这时现写一段并存回去。条件里还要求分析已经跑完（status 是 succeeded）：
    # 没跑完时后台任务还会改写 result（参见 deps.load_finished_analysis 的说明），这里就不写、不存
    if card["explanation"] is None and analysis.status == "succeeded":
        # M11：配了大模型就让它根据这张卡写（会联网，并在 llm_calls 表里记一次用量），没配就按模板生成
        explanation = Explainer(ctx.llm).explain_step(card, org_id=principal.org_id, analysis_id=analysis.id)
        # 存进这条路线的 explanations 里，键是步骤编号；下次再打开这张卡就不用重写了
        route.setdefault("explanations", {})[card["step_key"]] = explanation
        # result 是加密的 JSON 列，SQLAlchemy 看不出字典里面被改过。
        # 要用 flag_modified 明确告诉它“这一列变了”，commit 时才会重新加密、写回数据库
        analysis.result = result
        flag_modified(analysis, "result")
        db.commit()
        card["explanation"] = explanation
    return card


@router.get("/analyses/{analysis_id}/routes/{route_id}/steps/{step_index}/precedents")
def get_step_precedents(analysis_id: str, route_id: str, step_index: int, principal: Principal = Depends(get_principal),
                        db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """C1 文献列表、C2 相似先例：每条先例的相似度、产率、原文链接，并标出它和这一步产物的结构差异。

    需要登录。返回：

    - `step`：这一步的标题，如“第 4 步 · 格氏羧基化”
    - `evidence`：证据等级，同证据卡里的 `evidence`
    - `precedents`：先例的数组，和证据卡里的 `precedents` 是同一批，这里另外加了结构差异。每条：
      `ref`（出处编号 `P1`、`P2`…）、`exact`（是不是同一个反应）、`same_class`（是不是同类反应；
      `false` 是做法不同的相关反应）、`compared_by`（怎么认定的：`exact` / `center` / `fingerprint`）、
      `similarity`（底物相似度）、`class_similarity`（反应一致的程度）、`doc_type` / `doc_type_label`、
      `document_id`（专利号等）、`year`、`doi`、`title`、`url`（原文链接）、`source` / `source_name`
      （数据来源）、`yield_pct`（报道的产率）、`reaction_smiles`、`agents`（试剂）、`conditions_text`、
      `temperature_c`

    不是同一反应的先例多一个 `structure_diff`：这一步的产物和先例的产物差在哪，格式同
    `POST /api/structures/diff` 的返回（`query` 一侧是这一步的产物，`reference` 一侧是先例的产物）。

    后端开了“补文献题目”的配置时，有 DOI 而没有题目的先例还会多一个 `literature`：
    `{title, authors, year, venue, url}`，查不到是 `null`。

    出错：404、409 同证据卡。
    """
    result = load_scored_analysis(db, principal, analysis_id).result
    # 这里只用得到卡，用不到路线本身，所以用 _route 接住不用
    _route, card = _card(ctx, result, route_id, step_index)
    product = card["product"]["smiles"]
    items = []
    for precedent in card["precedents"]:
        # 复制一份再往里加字段，不去改存着的结果
        item = dict(precedent)
        if not precedent["exact"]:
            # 先例的 reaction_smiles 是 “反应物>试剂>产物”；rsplit(">", 1) 从右边切一刀，[-1] 就是产物。
            # 拿这一步的产物和先例的产物比，标出各自多出来的原子（C2）
            item["structure_diff"] = chem.structure_diff(product, precedent["reaction_smiles"].rsplit(">", 1)[-1])
        # 只有 DOI、没有题目的文献：配置里开了 ENABLE_LITERATURE_METADATA 时（ctx.literature 不是 None），
        # 去 Crossref / OpenAlex 查题目、作者、年份。这一步要联网；查不通就填 None，不让整个请求失败
        if ctx.literature is not None and precedent["doi"] and not precedent["title"]:
            try:
                item["literature"] = ctx.literature.by_doi(precedent["doi"])
            except ExternalServiceError:
                item["literature"] = None
        items.append(item)
    return {"step": card["title"], "evidence": card["evidence"], "precedents": items}


@router.post("/analyses/{analysis_id}/routes/{route_id}/steps/{step_index}/ask")
def ask_about_step(analysis_id: str, route_id: str, step_index: int, body: schemas.AskRequest,
                   principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                   ctx: AppContext = Depends(get_ctx)):
    """C5 问一问：只根据这张证据卡回答，每句附出处；卡里没有的信息会直接说没有依据。

    需要登录。请求体见 `AskRequest`。问答不保存，每次都是独立的一问一答。返回：

    - `question`：问题的原文（去掉了首尾的空格）
    - `answerable`：证据卡里有没有回答这个问题的依据
    - `answer`：回答的文字，句末带出处标记，如“…[评分]。”；没有依据时固定是“证据卡里没有这项依据。”
    - `sentences`：回答拆成的句子，每句 `{text, sources}`，`sources` 是出处编号的数组
    - `generated_by`：`llm` 是大模型回答的；`template` 是按模板回答的（没配大模型时只能答几类常见问题）
    - `provider`、`model`：用的哪家的哪个模型；按模板回答时是 `null`
    - `dropped_sentences`：大模型写的句子里，有几句因为出处或数字核对不上被丢掉了
    - `note`：补充说明
    - `sources`：这张证据卡的全部来源，用来把出处编号对上名称和链接

    出错：404、409 同证据卡；422 问题是空的，或者超过 500 个字符。
    """
    result = load_scored_analysis(db, principal, analysis_id).result
    _route, card = _card(ctx, result, route_id, step_index)
    # M12：配了大模型就把（精简后的）证据卡和问题一起发给它，回来的每句话都要核对出处和数字；
    # 没配就用关键词判断问的是哪一类问题，按模板回答。调大模型时会在 llm_calls 表里记一次用量
    answer = QuestionAnswerer(ctx.llm).answer(card, body.question, org_id=principal.org_id, analysis_id=analysis_id)
    # 回答里的每句话只带出处编号；编号对应的名称和链接在卡的来源表里，所以把来源表一起返回
    answer["sources"] = card["sources"]
    return answer


# ---------------------------------------------------------------- 路线树上的右键操作


@router.post("/analyses/{analysis_id}/exclusions")
def add_exclusion(analysis_id: str, body: schemas.ExclusionCreate, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """B11：排除一个分支或一种试剂，用到它的路线被拿掉，剩下的重新排序。

    如果这样一来一条可选路线都不剩，就重新搜索（返回 research = true，之后像新分析一样看进度）。

    需要登录，并且分析已经跑完（`status` 是 `succeeded`）。请求体见 `ExclusionCreate`。
    排除会保存下来（记在这次分析的 `user_exclusions` 里），可以用
    `DELETE /api/analyses/{id}/exclusions/{exclusion_id}` 撤销。

    返回：

    - `research`：是否触发了重新搜索
    - `exclusion`：这条排除记录。分支：`{id, type, step_key, name, created_at}`；
      试剂：`{id, type, key14, smiles, name, created_at}`。撤销时要用里面的 `id`
    - `result`：重新排序以后的结果页数据，格式同 `GET /api/analyses/{id}/result`
      （`research` 是 `false` 时才有）
    - `analysis`：这次分析的当前状态，格式同 `GET /api/analyses/{id}`（`research` 是 `true` 时才有）

    出错：404 没有这个分析、没有这条路线，或者这条路线没有这一步；409 分析还没评分或者还在进行中，
    或者这个分支 / 这种试剂已经排除过了；422 结构读不出来。
    """
    analysis = load_finished_analysis(db, principal, analysis_id)
    result = analysis.result
    if body.type == "branch":
        # 排除一个分支：记下的是这一步反应的编号（step_key）。同一步反应可能出现在好几条路线里，它们都会被排除
        route = _route_or_404(result, body.route_id)
        # step_index 不小于 1 已经由 ExclusionCreate 保证，这里只查上限
        if body.step_index > len(route["step_keys"]):
            raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("这条路线没有这一步", "This route has no such step"))
        # step_keys 按实验顺序排，第 1 步在下标 0
        step_key = route["step_keys"][body.step_index - 1]
        exclusion = {"type": "branch", "step_key": step_key, "name": result["reactions"][step_key]["name"]}
        # identity 用来判断“是不是已经排除过了”：(类型, 步骤编号) 或者 (类型, 分子的 key14)
        identity = ("branch", step_key)
    else:
        # 排除一种试剂：按结构认（key14 是 InChIKey 的前 14 位），同一个物质不管 SMILES 怎么写都算同一个
        record = chem.mol_record(body.smiles)
        if record is None:
            raise HTTPException(422, i18n.tr("结构读不出来", "The structure could not be read"))
        # 显示用的名字：分析结果里有就用结果里的，其次是内置试剂表里的，都没有就用分子式
        known = result["molecules"].get(record.key14) or {}
        name = known.get("name") or ctx.reference.compounds.name_for(record.smiles) or record.formula
        exclusion = {"type": "substance", "key14": record.key14, "smiles": record.smiles, "name": name}
        identity = ("substance", record.key14)

    existing = list(analysis.user_exclusions or [])
    # 已有的排除记录里，分支带 step_key、试剂带 key14；和这次的 identity 相同就是重复排除
    if any((e["type"], e.get("step_key") or e.get("key14")) == identity for e in existing):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("已经排除过了", "Already excluded"))
    # 给这条记录一个编号（随机的 32 位十六进制串，取前 10 位）和时间；撤销时按这个编号找
    exclusion.update({"id": models.new_id()[:10], "created_at": models.utcnow().isoformat()})
    # 排除之前有没有可选的路线：下面用它判断“是不是这一次排除把路线排光了”
    had_choices = bool(result.get("ranking"))
    # 整个列表重新赋值，而不是 append：user_exclusions 是加密的 JSON 列，原地修改不会被保存
    analysis.user_exclusions = [*existing, exclusion]

    # 三步：记下重新评分之前每一步的分数 → 重新评分（pipeline.rescore 直接改写 result，不重新搜索）→
    # 分数变了的步骤，旧说明作废。最后用 flag_modified 标记 result 这一列变了
    before = step_scores(result)
    rescore(ctx, analysis)
    drop_stale_explanations(result, before)
    flag_modified(analysis, "result")

    if had_choices and not result["ranking"]:
        # 原来有可选路线，现在一条都不剩：把这次分析重新放进队列，让后台从头再跑一遍。
        # 被排除的试剂会交给逆合成引擎避开；这里不清零尝试次数（attempts），后台据此把搜索轮数成倍增加，
        # 第一次重新搜索是 2 倍（见 pipeline.AnalysisRunner._search）。
        # 被排除的分支不会传给引擎：新搜到的路线里如果还有这一步，评分时照样被排除
        analysis.status = "queued"
        analysis.cancel_requested = False
        # 和重跑一样，记一条“新的一轮开始了”的事件
        add_event(db, analysis.id, "status", {"status": "queued"})
        db.commit()
        ctx.queue.enqueue("analysis.run", {"analysis_id": analysis.id})
        db.refresh(analysis)
        return {"research": True, "exclusion": exclusion, "analysis": analysis_view(analysis, ctx)}
    db.commit()
    return {"research": False, "exclusion": exclusion, "result": result_view(result)}


@router.delete("/analyses/{analysis_id}/exclusions/{exclusion_id}")
def remove_exclusion(analysis_id: str, exclusion_id: str, principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """撤销一次排除。

    B11 的撤销。需要登录，并且分析已经跑完。路径里的 `exclusion_id` 是排除时返回的 `exclusion.id`。
    撤销以后用已有的评估结果重新排序（不重新搜索），返回 `{"result": 结果页数据}`，
    格式同 `GET /api/analyses/{id}/result`。

    出错：404 没有这个分析，或者没有这条排除记录；409 分析还没评分或者还在进行中。
    """
    analysis = load_finished_analysis(db, principal, analysis_id)
    # 留下编号不是 exclusion_id 的那些；长度没变，说明根本没有这条记录
    remaining = [e for e in analysis.user_exclusions or [] if e["id"] != exclusion_id]
    if len(remaining) == len(analysis.user_exclusions or []):
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这条排除记录", "Exclusion not found"))
    analysis.user_exclusions = remaining
    result = analysis.result
    # 和排除时一样的三步：记下旧分数 → 重新评分 → 删掉过时的说明；然后标记 result 变了，提交
    before = step_scores(result)
    rescore(ctx, analysis)
    drop_stale_explanations(result, before)
    flag_modified(analysis, "result")
    db.commit()
    return {"result": result_view(result)}


@router.post("/analyses/{analysis_id}/mark-owned")
def mark_owned(analysis_id: str, body: schemas.MarkOwnedRequest, principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """B12 标记为我有：原料加入库存，节点变绿，库存覆盖率和分数随之更新。

    需要登录，并且分析已经跑完。请求体见 `MarkOwnedRequest`。库存里已经有同一个结构时不会重复添加，
    只把这次给的数量和位置更新到那一条上。跟着更新的只有当前这次分析的结果；已经跑完的其他分析不会变。

    返回：

    - `item`：库存里的这一条
      `{id, name, cas, smiles, quantity, location, note, source, visibility, updated_at}`
    - `created`：`true` 是新加的，`false` 是库存里原来就有
    - `result`：更新以后的结果页数据，格式同 `GET /api/analyses/{id}/result`

    出错：404 没有这个分析；409 分析还没评分或者还在进行中；422 结构读不出来。
    """
    analysis = load_finished_analysis(db, principal, analysis_id)
    record = chem.mol_record(body.smiles)
    if record is None:
        raise HTTPException(422, i18n.tr("结构读不出来", "The structure could not be read"))
    result = analysis.result
    # 这个物质在不在这次分析的结果里（按 key14 找）。不在也照样记进库存，只是不用重新评分
    molecule = result["molecules"].get(record.key14)
    # 内置试剂表里有没有它：有的话借用表里的名字和 CAS 号
    compound = ctx.reference.compounds.find_by_smiles(record.smiles)
    # 名字的先后：用户填的 → 分析结果里的 → 内置试剂表里的 → 分子式
    name = body.name or (molecule or {}).get("name") or (compound.display_name if compound else record.formula)

    inventory = InventoryService(db, principal.org_id, principal.user_id)
    # upsert：库存里已经有同一个结构就更新那一条，没有才新建；返回 (条目, 是不是新建的)。
    # source="marked_owned" 记下这一条是从结果页“标记为我有”来的
    item, created = inventory.upsert(
        name=name, cas=compound.cas if compound else None, smiles=record.smiles,
        quantity=body.quantity, location=body.location, source="marked_owned",
    )
    if molecule is not None:
        # 只给这一个分子重新判断可得性：库存索引里只放刚才那一条（{key14: 库存条目}）。
        # engine_in_stock 是逆合成引擎自己对“这个分子买不买得到”的判断，原样传回去
        checker = AvailabilityChecker(db, ctx.reference, {record.key14: InventoryService.to_dict(item)})
        molecule["availability"] = checker.check(record.smiles, engine_in_stock=molecule.get("engine_in_stock"))
        # 可得性变了，分数和名次跟着重算；分数变了的步骤，旧说明作废
        before = step_scores(result)
        rescore(ctx, analysis)
        drop_stale_explanations(result, before)
        flag_modified(analysis, "result")
    # 一次提交，把库存的改动和 result 的改动一起写进数据库
    db.commit()
    return {"item": InventoryService.to_dict(item), "created": created, "result": result_view(result)}


@router.get("/analyses/{analysis_id}/llm-usage")
def llm_usage(analysis_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """这次分析调了几次大模型、用了多少 token，用来核对成本。

    需要登录。返回：

    - `calls`：一共调了几次（失败的调用也算）
    - `by_module`：按模块和模型分组，每项 `{module, model, calls, input_tokens, output_tokens}`。
      `module` 是 `M11`（写说明）或 `M12`（问答）

    没配大模型时 `calls` 是 0，`by_module` 是空数组。一句话要求的解析（M2）发生在提交分析之前，
    不记在哪一次分析名下，所以不在这里。出错：404 没有这个分析。
    """
    analysis = load_analysis(db, principal, analysis_id)
    # 一条 SQL 做分组统计：llm_calls 表里属于这次分析的行，按（模块, 模型）分组，
    # 每组数一数有几行、输入和输出的 token 各加起来是多少。
    # func.count()、func.sum() 就是 SQL 的 COUNT(*)、SUM()；.all() 返回的每一行是一个五元组
    rows = db.execute(
        select(models.LLMCall.module, models.LLMCall.model, func.count(), func.sum(models.LLMCall.input_tokens),
               func.sum(models.LLMCall.output_tokens))
        .where(models.LLMCall.analysis_id == analysis.id)
        .group_by(models.LLMCall.module, models.LLMCall.model)
    ).all()
    return {
        # row[2] 是每一组的调用次数
        "calls": sum(row[2] for row in rows),
        "by_module": [
            # tokens_in or 0：SUM 的结果有可能是 None，这时当 0
            {"module": module, "model": model, "calls": calls, "input_tokens": int(tokens_in or 0), "output_tokens": int(tokens_out or 0)}
            for module, model, calls, tokens_in, tokens_out in rows
        ],
    }
