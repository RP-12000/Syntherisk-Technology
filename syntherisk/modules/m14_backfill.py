"""M14 回填与校准：记录每一步实际做出来的结果（G1–G4），授权的数据用来校准评分。

- 记录加密保存，只在本组织可见。
- “授权用于改进模型”默认不勾，可以随时撤回；只有授权了的记录才会进入校准和训练数据。
- 导出给校准和训练用的数据是匿名的：只有反应本身和结构化的结果，不带组织、人员和自由填写的备注。
- 校准只出统计报告，不自动改权重：0.6 / 0.4 和各项权重要由化学顾问看过报告后再定（PPT 附录 A）。

在整个流程里的位置
    分析跑完以后的事，不在流水线的八个阶段里：用户照着某条路线做了某一步实验，回来把结果记到这一步上。
    按钮和接口（接口都在 api/reports.py）：
        G1 结果、G2 产率和备注、G3 授权、G4 保存
            POST  /api/analyses/{id}/routes/{route_id}/steps/{n}/experiments   → save_record、record_view
        看这次分析的全部回填记录
            GET   /api/analyses/{id}/experiments                                → record_view
        G3 撤回或补上授权、修改记录
            PATCH /api/experiments/{id}                                         → apply_to_result、record_view
    保存以后，路线树上这一步显示“已做 · 实际 78%”（R12）。
    命令行（cli.py）：
        python -m syntherisk.cli calibrate          → run_calibration：出一份校准统计
        python -m syntherisk.cli export-consented   → consented_dataset：导出已授权的匿名数据

数据放在哪
    experiment_records 表  每次回填一行。结果（outcome）和授权标记是明文列；实际产率、条件改动、备注，
                           以及保存当时系统对这一步的判断（snapshot），放在加密的 payload 里。
    Analysis.result        每一步最近一次回填的摘要，挂在 reactions[步骤编号]["experiment"]
                           和各条路线 steps 里同一步的 "experiment" 上；界面显示“已做”靠它。
    calibration_runs 表    每次校准的统计结果。

依赖
    models.py（ExperimentRecord、CalibrationRun、Analysis）。不联网，不调大模型。

本文件里有什么
    OUTCOME_LABELS     结果的三个取值和中文
    experiment_label   路线树上显示的那一小段文字
    record_view        一条回填记录给接口返回的样子
    save_record        保存一条回填记录
    apply_to_result    把回填结果挂到分析结果上
    consented_dataset  已授权记录的匿名数据集
    _mean              算平均值
    run_calibration    做一次校准统计
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from syntherisk import i18n, models

# 实验结果的三个取值（接口的 outcome 字段只接受这三个）和显示用的中文。
# i18n.tr 返回的就是中文，同时登记它的英文（要英文的请求，返回前由中间件换过去）
OUTCOME_LABELS = {
    "success": i18n.tr("成功", "Succeeded"),
    "partial": i18n.tr("部分成功", "Partly succeeded"),
    "failure": i18n.tr("失败", "Failed"),
}
# 路线树上“已做 · 结果”的三种固定写法（experiment_label 用）。整句登记，英文才对得上
_DONE_LABELS = {
    "success": i18n.tr("已做 · 成功", "Done · succeeded"),
    "partial": i18n.tr("已做 · 部分成功", "Done · partly succeeded"),
    "failure": i18n.tr("已做 · 失败", "Done · failed"),
}


# ---------------------------------------------------------------- 回填：保存、显示


def experiment_label(outcome: str, actual_yield: float | None) -> str:
    """R12：路线树上这一步显示的文字，比如“已做 · 实际 78%”。

    参数：
        outcome       "success" / "partial" / "failure"。
        actual_yield  实际产率（0–100 的百分数）；没填是 None。
    返回：
        结果是失败    “已做 · 失败”（这时不管有没有填产率）
        填了产率      “已做 · 实际 78%”
        没填产率      “已做 · 成功” 或 “已做 · 部分成功”
    谁在用：
        本文件的 record_view、apply_to_result。
    """
    if outcome == "failure":
        return _DONE_LABELS["failure"]
    if actual_yield is not None:
        # 格式里的 “:g” 去掉多余的小数：78.0 显示成 78，78.5 还是 78.5
        return i18n.tr("已做 · 实际 {actual:g}%", "Done · actual {actual:g}%", actual=actual_yield)
    return _DONE_LABELS[outcome]


def record_view(record: models.ExperimentRecord) -> dict[str, Any]:
    """一条回填记录给接口返回的样子：把明文列和加密的 payload 里的内容合在一起。

    返回：
        {
          "id":                   记录编号,
          "analysis_id":          哪次分析,
          "route_id":             哪条路线（"r1"……）,
          "step_index":           第几步（从 1 开始）,
          "step_key":             步骤编号（反应表的键）,
          "step_name":            保存时这一步的反应名,
          "outcome":              "success" / "partial" / "failure",
          "outcome_label":        成功 / 部分成功 / 失败,
          "actual_yield":         实际产率（%），没填是 None,
          "condition_changes":    条件上做了什么改动（自由填写）,
          "notes":                备注（自由填写）,
          "consent_for_training": 是否授权用于改进模型,
          "label":                路线树上显示的文字，见 experiment_label,
          "created_at":           建立时间（ISO 8601 格式的字符串）,
          "updated_at":           最近一次修改的时间（同上）,
        }
        保存当时的 snapshot 不返回。
    谁在用：
        api/reports.py 的 record_experiment、list_experiments、update_experiment。
    """
    payload = record.payload
    return {
        "id": record.id,
        "analysis_id": record.analysis_id,
        "route_id": record.route_id,
        "step_index": record.step_index,
        "step_key": record.step_key,
        "step_name": payload.get("step_name"),
        "outcome": record.outcome,
        "outcome_label": OUTCOME_LABELS[record.outcome],
        "actual_yield": payload.get("actual_yield"),
        "condition_changes": payload.get("condition_changes", ""),
        "notes": payload.get("notes", ""),
        "consent_for_training": record.consent_for_training,
        "label": experiment_label(record.outcome, payload.get("actual_yield")),
        # models.iso() 把时间变成 “2026-10-11T08:30:00+00:00” 这样带时区的文字
        "created_at": models.iso(record.created_at),
        "updated_at": models.iso(record.updated_at),
    }


def save_record(
    session: Session,
    analysis: models.Analysis,
    route: dict[str, Any],
    step_index: int,
    user_id: str,
    *,
    outcome: str,
    actual_yield: float | None,
    condition_changes: str,
    notes: str,
    consent_for_training: bool,
) -> models.ExperimentRecord:
    """G4 保存：给某条路线的某一步记一条实验结果，并把它挂到分析结果上。

    参数：
        session               数据库会话。
        analysis              这次分析（接口保证它已经成功跑完）。
        route                 analysis.result["routes"] 里的那条路线。
        step_index            第几步，从 1 开始（接口已经检查过范围）。
        user_id               谁填的。
        outcome               "success" / "partial" / "failure"（G1）。
        actual_yield          实际产率，0–100 的百分数，可以是 None（G2）。
        condition_changes     条件上的改动，自由填写（G2）。
        notes                 备注，自由填写（G2）。
        consent_for_training  是否授权用于改进模型（G3）；接口上默认是 False。
    返回：
        新建的 ExperimentRecord（已经 flush，有 id 和时间）。
    副作用：
        往 experiment_records 表加一行（只 flush 不提交，提交由调用方做）；
        通过 apply_to_result 改 analysis.result，在这一步上挂“已做”的摘要。
    记下来的内容（payload，加密存）：
        step_name、actual_yield、condition_changes、notes，以及 snapshot：保存这一刻系统对这一步的判断
        （反应 SMILES、步骤分、六个维度的分数、证据等级、文献产率的中位数、命中的操作规则编号）。
        以后重新评分、重新搜索，结果变了，校准时拿来和实际结果对比的仍然是当时的判断。
    谁在用：
        api/reports.py 的 record_experiment。
    """
    # 路线上的这一步（步骤分和各维度的分数），和这一步反应本身（反应 SMILES、证据、产率、条件）
    step = route["steps"][step_index - 1]
    reaction = analysis.result["reactions"][step["key"]]
    evidence = reaction.get("evidence") or {}
    record = models.ExperimentRecord(
        org_id=analysis.org_id,
        analysis_id=analysis.id,
        route_id=route["id"],
        step_key=step["key"],
        step_index=step_index,
        user_id=user_id,
        outcome=outcome,
        consent_for_training=consent_for_training,
        # 授权状态最后一次变动的时间；新建时就是现在
        consent_updated_at=models.utcnow(),
        payload={
            "step_name": reaction["name"],
            "actual_yield": actual_yield,
            "condition_changes": condition_changes,
            "notes": notes,
            # 当时系统对这一步的判断，校准时拿来和实际结果对比
            "snapshot": {
                # 反应 SMILES（反应物>试剂>产物）
                "reaction_smiles": reaction["reaction_smiles"],
                # 步骤分（0–100），和六个维度各自的分数（没有数据的维度是 None）
                "step_score": step["score"],
                "dimensions": step["dimensions"],
                # 证据等级 E1–E4；没评估是 None
                "evidence_level": evidence.get("level"),
                # 文献报道产率的中位数（%）；没有报道值是 None
                "reported_yield_median": (reaction.get("yield") or {}).get("median"),
                # 命中的操作规则的编号列表
                "operation_tags": [t["rule_id"] for t in (reaction.get("conditions") or {}).get("tags") or []],
            },
        },
    )
    session.add(record)
    # flush 以后 record.id 才有值，下面挂到结果上要用
    session.flush()
    apply_to_result(analysis, record)
    return record


def apply_to_result(analysis: models.Analysis, record: models.ExperimentRecord) -> None:
    """把最新一次实验结果挂到分析结果里的那一步上，路线树才能显示“已做”。

    参数：
        analysis  这条记录所属的分析。
        record    刚保存或刚修改过的回填记录。
    做什么：
        按 record.step_key 找到这一步，把摘要 {record_id, outcome, actual_yield, label} 写到两个地方：
        反应表里这一步的 "experiment"，以及每条经过这一步的路线的 steps 里它的 "experiment"。
        同一步有几条记录时，后调用的盖掉先调用的。
        结果里已经没有这一步（步骤编号对不上）时什么都不做。
    副作用：
        改 analysis.result 并标记为“已修改”。不提交，提交由调用方做。
    谁在用：
        本文件的 save_record；api/reports.py 的 update_experiment（改了记录以后重新挂一遍）。
    """
    result = analysis.result
    # 用 .get：反应表里找不到这个步骤编号时得到 None，直接返回，不报错
    reaction = result["reactions"].get(record.step_key)
    if reaction is None:
        return
    # 挂到结果上的摘要：哪条记录、结果、实际产率、界面上显示的那句话
    summary = {
        "record_id": record.id,
        "outcome": record.outcome,
        "actual_yield": record.payload.get("actual_yield"),
        "label": experiment_label(record.outcome, record.payload.get("actual_yield")),
    }
    # 反应上挂一份：证据卡（cards.py）和路线树（api/views.py 的 tree_view）读这里
    reaction["experiment"] = summary
    # 每条经过这一步的路线的步骤上也挂一份：结果页的路线卡片（api/views.py 的 route_card）读这里
    for route in result["routes"]:
        for step in route.get("steps") or []:
            if step["key"] == record.step_key:
                step["experiment"] = summary
    # result 是加密的 JSON 列，上面是在原来的字典里直接改的，SQLAlchemy 察觉不到。
    # flag_modified 明确告诉它“这一列变了”，提交时才会重新加密、写回数据库
    analysis.result = result
    flag_modified(analysis, "result")


# ---------------------------------------------------------------- 校准：只用已授权的记录


def consented_dataset(session: Session) -> list[dict[str, Any]]:
    """已授权记录的匿名数据集：校准用，以后也是 M8 产率模型的训练数据。

    范围是数据库里所有课题组的记录，只要“授权用于改进模型”现在是勾着的；撤回授权的记录，下次就不在里面。

    返回：
        每条已授权的记录一个字典，只有这八项（没有组织、人员、分析编号，也没有备注和条件改动
        这些自由填写的内容）：
            reaction_smiles        这一步的反应（反应物>试剂>产物）
            outcome                "success" / "partial" / "failure"
            actual_yield           实际产率（%），没填是 None
            step_score             保存时系统给这一步的步骤分
            dimensions             保存时六个维度的分数 {维度: 分数或 None}
            evidence_level         保存时的证据等级 E1–E4，没评估是 None
            reported_yield_median  保存时文献报道产率的中位数，没有是 None
            operation_tags         保存时命中的操作规则编号
    副作用：
        只读数据库（要解密每条记录的 payload）。
    谁在用：
        本文件的 run_calibration；cli.py 的 cmd_export_consented（导出成 JSON 文件）；
        tests/test_reports_and_backfill.py。
    """
    # consent_for_training.is_(True) 即 SQL 的 “IS true”；session.scalars 直接给出 ExperimentRecord 对象
    records = session.scalars(
        select(models.ExperimentRecord).where(models.ExperimentRecord.consent_for_training.is_(True))
    )
    dataset = []
    for record in records:
        # 只从 snapshot 和结构化的字段里取，备注（notes）和条件改动（condition_changes）不取
        snapshot = record.payload.get("snapshot") or {}
        dataset.append(
            {
                "reaction_smiles": snapshot.get("reaction_smiles"),
                "outcome": record.outcome,
                "actual_yield": record.payload.get("actual_yield"),
                "step_score": snapshot.get("step_score"),
                "dimensions": snapshot.get("dimensions") or {},
                "evidence_level": snapshot.get("evidence_level"),
                "reported_yield_median": snapshot.get("reported_yield_median"),
                "operation_tags": snapshot.get("operation_tags") or [],
            }
        )
    return dataset


def _mean(values: list[float]) -> float | None:
    """平均值，保留两位小数；列表是空的返回 None。

    谁在用：
        本文件的 run_calibration。
    """
    return round(sum(values) / len(values), 2) if values else None


def run_calibration(session: Session) -> models.CalibrationRun:
    """定期校准：统计系统的判断和实际结果差多少。结果存进 calibration_runs，供顾问调权重时参考。

    用的数据是 consented_dataset()：所有已授权的回填记录。只做统计，不改任何权重和配置。

    返回：
        新建的 CalibrationRun（已经 flush）。它的 stats 字段是一个字典：
            n_records                           用了多少条记录
            outcomes                            {结果: 条数}
            mean_step_score_by_outcome          {结果: 步骤分的平均值}（用来看分数高的步骤是不是更容易成功）
            outcomes_by_evidence_level          {证据等级: {结果: 条数}}，没评等级的记在 “未评估” 下面
            outcomes_by_operation_tag           {操作规则编号: {结果: 条数}}
            mean_dimension_score_by_outcome     {维度: {结果: 这一维分数的平均值}}
            yield_mean_abs_error_vs_literature  实际产率和文献产率中位数之差的绝对值的平均（百分点）；
                                                没有可比的记录时是 None
            yield_pairs                         上一项用了多少条记录（实际产率、文献产率都有的）
            note                                一句提醒：样本少时这些数字波动很大
    副作用：
        往 calibration_runs 表加一行（只 flush 不提交，提交由调用方做）。
    谁在用：
        cli.py 的 cmd_calibrate；tests/test_reports_and_backfill.py。
    """
    dataset = consented_dataset(session)
    # defaultdict：取一个还不存在的键时自动放进默认值（int 是 0，list 是空列表），不用先判断键在不在。
    # defaultdict(lambda: defaultdict(int)) 是“字典套字典”，里外两层都会自动建
    # {结果: 条数}
    by_outcome: dict[str, int] = defaultdict(int)
    # {结果: [步骤分, ...]}
    scores: dict[str, list[float]] = defaultdict(list)
    # {证据等级: {结果: 条数}}
    by_level: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # {操作规则编号: {结果: 条数}}
    by_tag: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # 每条“实际产率和文献产率都有”的记录贡献一个差值（取绝对值，单位是百分点）
    yield_errors: list[float] = []
    # {维度: {结果: [这一维的分数, ...]}}
    dimension_scores: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    for row in dataset:
        outcome = row["outcome"]
        by_outcome[outcome] += 1
        if row["step_score"] is not None:
            scores[outcome].append(row["step_score"])
        by_level[row["evidence_level"] or "未评估"][outcome] += 1
        for tag in row["operation_tags"]:
            by_tag[tag][outcome] += 1
        if row["actual_yield"] is not None and row["reported_yield_median"] is not None:
            yield_errors.append(abs(row["actual_yield"] - row["reported_yield_median"]))
        # 没有数据的维度（None）不进平均值
        for dimension, value in row["dimensions"].items():
            if value is not None:
                dimension_scores[dimension][outcome].append(value)

    # 把 defaultdict 都转成普通字典再存（stats 是 JSON 列），分数的列表换成平均值
    stats = {
        "n_records": len(dataset),
        "outcomes": dict(by_outcome),
        "mean_step_score_by_outcome": {outcome: _mean(values) for outcome, values in scores.items()},
        "outcomes_by_evidence_level": {level: dict(counts) for level, counts in by_level.items()},
        "outcomes_by_operation_tag": {tag: dict(counts) for tag, counts in by_tag.items()},
        "mean_dimension_score_by_outcome": {
            dimension: {outcome: _mean(values) for outcome, values in groups.items()}
            for dimension, groups in dimension_scores.items()
        },
        "yield_mean_abs_error_vs_literature": _mean(yield_errors),
        "yield_pairs": len(yield_errors),
        "note": "样本少的时候这些数字波动很大，只作参考；调整权重前请化学顾问复核。",
    }
    run = models.CalibrationRun(n_records=len(dataset), stats=stats)
    session.add(run)
    session.flush()
    return run
