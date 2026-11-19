"""审计适配层：把分析结果送去 SAL（团队做的 Audit 模型）留档、核对来源。

SAL 是什么
    Syntherisk Assurance Layer，团队另一位成员做的“证据和审计”软件。它不打分、不判断化学上能不能做，
    只做三件事：给每次分析的每条路线、每一步发一个永久编号并锁定内容（存档）；把每一步和它引用的
    文献、专利一条条挂上（对账）；来源被更正或撤稿时，标出受影响的步骤（跟踪）。
    它的所有输出都写明 approval_allowed=false：审计记录不等于路线被批准。

三种模式（配置项 AUDIT_MODE）
    off        默认。不审计，接口返回 {"status": "disabled"}
    embedded   在后端进程里直接调用 SAL 的 Mapping 库（源码放在 AUDIT_SAL_PATH），
               结果存在 <var_dir>/audit/mapping/<分析编号>.sqlite3
    http       调 AUDIT_BASE_URL 上的审计接口（本机的 SAL 服务，或者以后的远程 Audit API）
    前端和我们自己的接口只认统一格式（providers.summarize 的返回值），换模式时它们都不用改。

什么时候审计
    分析成功跑完（pipeline.run_analysis）、或者 B11 排除 / B12 标记已有之后重新排了名，
    service.schedule() 建一条 audit_runs 记录、排一个 "audit.run" 后台任务。审计在八个阶段之外跑，
    失败、超时都只记在 audit_runs 里，不影响分析本身。
    只审计范围之内的分析（AUDIT_SCOPE）：demo_only 时只审计 AUDIT_DEMO_ORGS 里那几个课题组的分析。
    SAL v0.1 只接受 data_classification=synthetic 的数据；别的分类会被它拒收，记为 not_eligible。

本包里有什么
    export.py     把一次分析导出成 SAL 要的快照（原始 result）和上下文（执行编号、指纹、数据分类）
    providers.py  两种实现（EmbeddedProvider、HttpProvider）和统一格式 summarize()
    service.py    schedule()、run_audit()（"audit.run" 任务）、接口用的 audit_view() / step_audit_view()

谁调用它
    pipeline.py 的 run_analysis（成功后 schedule）、taskqueue.py（登记 "audit.run"）、
    api/analyses.py（两个只读接口；排除、标记已有之后 schedule）。
"""

from syntherisk.audit.service import audit_view, run_audit, schedule, step_audit_view

__all__ = ["audit_view", "run_audit", "schedule", "step_audit_view"]
