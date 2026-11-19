"""审计的流程：排任务（schedule）、跑任务（run_audit）、给接口整理结果（audit_view / step_audit_view）。

audit_runs 一行的 status
    queued        排上了，还没跑
    running       正在送去审计
    done          审计完成，summary 里是统一格式（见 providers.py）
    not_eligible  SAL 按数据政策拒收（v0.1 只收 synthetic 数据），reason 是 SAL 给的代码
    failed        出错了，reason 是代码：STRUCTURE_CONFLICT（结果对不上）、JSON_LIMIT_EXCEEDED（太大）、
                  PROVIDER_UNAVAILABLE（SAL 没装好或连不上）、TIMEOUT、INTERNAL_ERROR……
    superseded    还没轮到它，这次分析就又重跑了（执行编号变了），这条作废
接口另外还会返回（不存在表里）：disabled（AUDIT_MODE=off）、not_eligible + OUT_OF_SCOPE（不在审计范围）、
waiting（分析还在跑）、none（这次分析没审计过）、outdated（最近一次审计的是上一轮的结果）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from syntherisk import models
from syntherisk.audit import export
from syntherisk.audit.providers import POLICY_CODES, AuditError, build_provider

logger = logging.getLogger("syntherisk.audit")


def _latest_run(session: Session, analysis_id: str, *, execution_id: str | None = None,
                status: str | None = None) -> models.AuditRun | None:
    query = select(models.AuditRun).where(models.AuditRun.analysis_id == analysis_id)
    if execution_id is not None:
        query = query.where(models.AuditRun.execution_id == execution_id)
    if status is not None:
        query = query.where(models.AuditRun.status == status)
    query = query.order_by(models.AuditRun.created_at.desc(), models.AuditRun.id.desc()).limit(1)
    return session.execute(query).scalar_one_or_none()


def schedule(ctx, session: Session, analysis: models.Analysis, *, trigger: str = "analysis") -> models.AuditRun | None:
    """分析成功（trigger="analysis"）或重新排名（trigger="rescore"）以后调用：建一条 queued 记录、排一个 "audit.run" 任务。

    不排的情况（返回 None）：AUDIT_MODE=off；分析不是 succeeded 或者还没有结果；不在审计范围（AUDIT_SCOPE）。
    重新排名时，新的一次审计用 parent_id 接上同一轮执行里上一次做完的审计。
    这一步出了任何错都只记日志、返回 None：审计不能影响分析本身。
    调用前，分析本身的改动要已经提交。这里会再提交一次（新建的审计记录）。
    """
    settings = ctx.settings
    if settings.audit_mode == "off" or analysis.status != "succeeded" or not analysis.result:
        return None
    try:
        if not export.in_scope(settings, analysis):
            return None
        if not analysis.execution_id:
            # 这个功能上线之前跑完的分析没有执行编号，现在补一个
            analysis.execution_id = uuid.uuid4().hex
        parent = None
        if trigger != "analysis":
            parent = _latest_run(session, analysis.id, execution_id=analysis.execution_id, status="done")
            if parent is not None and parent.mode != settings.audit_mode:
                parent = None
        run = models.AuditRun(
            analysis_id=analysis.id, org_id=analysis.org_id, execution_id=analysis.execution_id,
            observation_id=uuid.uuid4().hex, parent_id=parent.document_id if parent else None,
            trigger=trigger, mode=settings.audit_mode, status="queued",
        )
        session.add(run)
        session.commit()
        audit_id = run.id
    except Exception:  # noqa: BLE001 - 审计排不上不影响分析
        logger.exception("给分析 %s 排审计任务失败", analysis.id)
        session.rollback()
        return None
    try:
        ctx.queue.enqueue("audit.run", {"audit_id": audit_id})
    except Exception:  # noqa: BLE001 - 比如 Redis 连不上：记下来，不让调用方（接口、流水线）出错
        logger.exception("审计任务 %s 入队失败", audit_id)
        run.status, run.reason, run.finished_at = "failed", "QUEUE_UNAVAILABLE", models.utcnow()
        session.commit()
    return run


def recover_interrupted(ctx) -> list[str]:
    """服务重启时（进程内队列模式）收拾没跑完的审计：排着队的重新入队，跑到一半的标成 failed / INTERRUPTED。

    返回要重新入队的审计编号，由调用方（syntherisk/main.py）逐个 enqueue("audit.run", …)。
    """
    requeue = []
    with ctx.session_factory() as session:
        rows = session.execute(
            select(models.AuditRun).where(models.AuditRun.status.in_(("queued", "running")))
        ).scalars().all()
        for run in rows:
            if run.status == "queued":
                requeue.append(run.id)
            else:
                run.status, run.reason, run.finished_at = "failed", "INTERRUPTED", models.utcnow()
        session.commit()
    return requeue


def run_audit(ctx, audit_id: str) -> None:
    """"audit.run" 任务的处理函数：导出快照和上下文，交给 AUDIT_MODE 选的实现，结果写回 audit_runs。

    不抛异常：结果和出错原因都写在这一行的 status / reason 里。
    """
    with ctx.session_factory() as session:
        run = session.get(models.AuditRun, audit_id)
        if run is None or run.status != "queued":
            return
        run.status = "running"
        run.started_at = models.utcnow()
        session.commit()

        analysis = session.get(models.Analysis, run.analysis_id)
        outcome, reason = None, None
        try:
            if (analysis is None or analysis.status != "succeeded" or not analysis.result
                    or analysis.execution_id != run.execution_id):
                raise AuditError("SUPERSEDED")
            provider = build_provider(ctx.settings)
            run.provider = provider.label
            outcome = provider.submit(
                org_id=analysis.org_id, project_id=analysis.project_id, analysis_id=analysis.id,
                snapshot=export.build_snapshot(analysis),
                context=export.build_context(session, ctx.settings, analysis, run),
                parent_id=run.parent_id,
            )
            status = "done"
        except AuditError as exc:
            reason = exc.code
            status = "superseded" if exc.code == "SUPERSEDED" else "not_eligible" if exc.code in POLICY_CODES else "failed"
        except Exception:  # noqa: BLE001 - 任何失败都只记在审计记录里
            logger.exception("审计 %s 失败", audit_id)
            reason, status = "INTERNAL_ERROR", "failed"
        if outcome is not None:
            run.document_id = outcome["document_id"]
            run.summary = outcome["summary"]
        run.status = status
        run.reason = reason
        run.finished_at = models.utcnow()
        session.commit()


def audit_view(ctx, session: Session, analysis: models.Analysis) -> dict[str, Any]:
    """GET /api/analyses/{id}/audit 的返回值：这次分析最近一次审计的统一格式。各个 status 的含义见本文件开头。"""
    settings = ctx.settings
    view: dict[str, Any] = {"mode": settings.audit_mode, "approval_allowed": False, "review_required": True,
                            "reason": None, "routes": []}
    if settings.audit_mode == "off":
        return {**view, "status": "disabled"}
    if not export.in_scope(settings, analysis):
        return {**view, "status": "not_eligible", "reason": "OUT_OF_SCOPE"}
    if analysis.status in ("queued", "running"):
        return {**view, "status": "waiting"}
    run = _latest_run(session, analysis.id)
    if run is None:
        return {**view, "status": "none"}
    summary = run.summary or {}
    status = run.status
    if analysis.execution_id and run.execution_id != analysis.execution_id:
        status = "outdated"
    return {
        **view,
        "status": status,
        "reason": run.reason,
        "audit_id": run.id,
        "trigger": run.trigger,
        "provider": run.provider,
        "document_id": run.document_id,
        "mapping_status": summary.get("mapping_status"),
        "flags": summary.get("flags") or [],
        "routes": summary.get("routes") or [],
        "created_at": models.iso(run.created_at),
        "finished_at": models.iso(run.finished_at),
    }


def step_audit_view(ctx, session: Session, analysis: models.Analysis, route_id: str, step_index: int) -> dict[str, Any]:
    """GET …/routes/{route_id}/steps/{n}/audit 的返回值：证据卡“审计”页签用的那一步。

    审计还没做完、或者这一步不在审计结果里时，step 是 null，其余字段和 audit_view 一样（不含 routes）。
    """
    view = audit_view(ctx, session, analysis)
    routes = view.pop("routes", [])
    route = next((item for item in routes if item.get("route_id") == route_id), None)
    step = None
    if route is not None:
        step = next((item for item in route.get("steps") or [] if item.get("step_index") == step_index), None)
    return {**view, "route_id": route_id, "step_index": step_index,
            "route_snapshot_id": route.get("route_snapshot_id") if route else None,
            "route_flags": route.get("flags") if route else [], "step": step}
