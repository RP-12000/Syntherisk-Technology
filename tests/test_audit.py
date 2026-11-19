"""审计适配层（syntherisk/audit/）的测试。

不需要 SAL 源码的测试总会跑：路线树和反应表的顺序一致、默认关闭、不在范围、SAL 没装好、导出的上下文、
http 模式（用假的传输层）。
需要 SAL 源码的端到端测试，只在环境变量 SYNTHERISK_TEST_SAL_PATH 指向 SAL 源码根目录
（里面有 sal_core/ 和 syntherisk_mapping/）时才跑，否则跳过。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest

from syntherisk import models
from syntherisk.audit import export, service
from syntherisk.audit.providers import AuditError, HttpProvider, summarize
from tests.conftest import ASPIRIN, IBUPROFEN, start_analysis

SAL_PATH = os.environ.get("SYNTHERISK_TEST_SAL_PATH", "")
needs_sal = pytest.mark.skipif(
    not (SAL_PATH and (Path(SAL_PATH) / "syntherisk_mapping").is_dir()),
    reason="没有 SAL 源码（设置 SYNTHERISK_TEST_SAL_PATH 才跑）",
)
HEX64 = re.compile(r"[0-9a-f]{64}")


def _enable(ctx, account, **overrides):
    """打开审计：embedded 模式，只审计这个账号的课题组。"""
    ctx.settings.audit_mode = overrides.pop("mode", "embedded")
    ctx.settings.audit_demo_orgs = account["org"]["id"]
    ctx.settings.audit_sal_path = overrides.pop("sal_path", SAL_PATH)
    ctx.settings.audit_demo_classification = overrides.pop("classification", "synthetic")


def _runs(ctx, analysis_id):
    with ctx.session_factory() as session:
        rows = session.query(models.AuditRun).filter_by(analysis_id=analysis_id).order_by(models.AuditRun.created_at).all()
        return [{"status": r.status, "reason": r.reason, "trigger": r.trigger, "parent_id": r.parent_id,
                 "document_id": r.document_id, "execution_id": r.execution_id} for r in rows]


def test_route_tree_follows_reaction_table_order(client, account):
    """同一步出现在几条路线里时，每棵树里反应物的顺序都和反应表一致（阿司匹林的 r1、r3 共用一步）。"""
    analysis = start_analysis(client, account, ASPIRIN)
    with client.app.state.ctx.session_factory() as session:
        result = session.get(models.Analysis, analysis["id"]).result
    shared = {}

    def walk(node):
        for reaction in node.get("children") or []:
            keys = [child["key14"] for child in reaction["children"]]
            assert keys == result["reactions"][reaction["step_key"]]["reactants"]
            shared[reaction["step_key"]] = shared.get(reaction["step_key"], 0) + 1
            for child in reaction["children"]:
                walk(child)

    for route in result["routes"]:
        walk(route["tree"])
    assert max(shared.values()) >= 2  # 确实有共用的一步，这个测试才有意义


def test_audit_is_off_by_default(client, account, ctx):
    analysis = start_analysis(client, account, IBUPROFEN)
    view = client.get(f"/api/analyses/{analysis['id']}/audit", headers=account["headers"]).json()
    assert view["status"] == "disabled" and view["approval_allowed"] is False
    assert _runs(ctx, analysis["id"]) == []
    # 每一轮运行都有执行编号
    with ctx.session_factory() as session:
        assert re.fullmatch(r"[0-9a-f]{32}", session.get(models.Analysis, analysis["id"]).execution_id)


def test_other_orgs_are_out_of_scope(client, account, ctx):
    ctx.settings.audit_mode = "embedded"
    ctx.settings.audit_demo_orgs = "some-other-org"
    analysis = start_analysis(client, account, IBUPROFEN)
    view = client.get(f"/api/analyses/{analysis['id']}/audit", headers=account["headers"]).json()
    assert view["status"] == "not_eligible" and view["reason"] == "OUT_OF_SCOPE"
    assert _runs(ctx, analysis["id"]) == []


def test_missing_sal_is_recorded_not_raised(client, account, ctx, tmp_path):
    """SAL 没装好：分析照样成功，审计记成 failed / PROVIDER_UNAVAILABLE。"""
    _enable(ctx, account, sal_path=str(tmp_path / "no-sal-here"))
    analysis = start_analysis(client, account, IBUPROFEN)
    assert analysis["status"] == "succeeded"
    view = client.get(f"/api/analyses/{analysis['id']}/audit", headers=account["headers"]).json()
    assert view["status"] == "failed" and view["reason"] == "PROVIDER_UNAVAILABLE"
    step = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1/audit", headers=account["headers"]).json()
    assert step["status"] == "failed" and step["step"] is None
    assert client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/99/audit",
                      headers=account["headers"]).status_code == 404


def test_export_context_fields(client, account, ctx):
    _enable(ctx, account, sal_path="")
    analysis = start_analysis(client, account, IBUPROFEN)
    with ctx.session_factory() as session:
        row = session.get(models.Analysis, analysis["id"])
        run = session.query(models.AuditRun).filter_by(analysis_id=row.id).one()
        context = export.build_context(session, ctx.settings, row, run)
        snapshot = export.build_snapshot(row)
    assert context["execution_id"] == row.execution_id == run.execution_id
    assert context["data_classification"] == "synthetic"
    assert HEX64.fullmatch(context["engine_fingerprint"])  # 演示引擎：data/demo_routes 的内容
    assert HEX64.fullmatch(context["config_fingerprint"])
    assert context["upstream_revision"] == "unknown" or re.fullmatch(r"[0-9a-f]{40}", context["upstream_revision"])
    assert context["retrieved_at"].endswith("Z")
    assert set(snapshot) == {"upstream_analysis_id", "upstream_project_id", "upstream_attempt", "result"}
    assert {"molecules", "reactions", "routes"} <= set(snapshot["result"])
    # 不是演示课题组的分析一律算客户数据
    ctx.settings.audit_demo_orgs = ""
    assert export.classification(ctx.settings, row) == export.CONFIDENTIAL


def test_superseded_when_analysis_reran(client, account, ctx):
    _enable(ctx, account, sal_path="")
    analysis = start_analysis(client, account, IBUPROFEN)
    with ctx.session_factory() as session:
        row = session.get(models.Analysis, analysis["id"])
        run = models.AuditRun(analysis_id=row.id, org_id=row.org_id, execution_id="old-execution",
                              observation_id="obs", mode="embedded", status="queued")
        session.add(run)
        session.commit()
        audit_id = run.id
    service.run_audit(ctx, audit_id)
    with ctx.session_factory() as session:
        assert session.get(models.AuditRun, audit_id).status == "superseded"


def test_queue_failure_is_recorded_not_raised(client, account, ctx, monkeypatch):
    """审计任务排不上队（比如 Redis 连不上）：分析照样成功，审计记成 failed / QUEUE_UNAVAILABLE。"""
    _enable(ctx, account, sal_path="")
    enqueue = ctx.queue.enqueue

    def flaky(job_type, payload):
        if job_type == "audit.run":
            raise ConnectionError("redis down")
        return enqueue(job_type, payload)

    monkeypatch.setattr(ctx.queue, "enqueue", flaky)
    analysis = start_analysis(client, account, IBUPROFEN)
    assert analysis["status"] == "succeeded"
    assert [(r["status"], r["reason"]) for r in _runs(ctx, analysis["id"])] == [("failed", "QUEUE_UNAVAILABLE")]


def test_restart_requeues_queued_and_fails_running(client, account, ctx):
    """服务重启：排着队的审计重新入队，跑到一半的记成 failed / INTERRUPTED，做完的不动。"""
    analysis = start_analysis(client, account, IBUPROFEN)
    ids = {}
    with ctx.session_factory() as session:
        row = session.get(models.Analysis, analysis["id"])
        for status in ("queued", "running", "done"):
            run = models.AuditRun(analysis_id=row.id, org_id=row.org_id, execution_id=row.execution_id,
                                  observation_id=status, mode="embedded", status=status)
            session.add(run)
            session.commit()
            ids[status] = run.id
    assert service.recover_interrupted(ctx) == [ids["queued"]]
    with ctx.session_factory() as session:
        assert session.get(models.AuditRun, ids["queued"]).status == "queued"
        interrupted = session.get(models.AuditRun, ids["running"])
        assert (interrupted.status, interrupted.reason) == ("failed", "INTERRUPTED")
        assert interrupted.finished_at is not None
        assert session.get(models.AuditRun, ids["done"]).status == "done"


# ---------------------------------------------------------------- http 模式（假的传输层）

DOCUMENT = {
    "document_id": "md_" + "a" * 64, "contract_version": "mapping/v0.1", "mapping_status": "current",
    "diagnostics": ["CHEMISTRY_IDENTITY_UNVERIFIED"],
    "routes": [{"upstream_route_label": "r1", "route_snapshot_id": "rs_1", "diagnostics": [],
                "steps": [{"upstream_step_index": 1, "upstream_step_key": "k1", "step_instance_id": "si_1",
                           "diagnostics": ["YIELD_TYPE_UNKNOWN"], "chemistry_approval_allowed": True,
                           "sources": [{"upstream_ref": "P1", "kind": "journal", "doi": "10.1/x", "source_id": "src_1",
                                        "verification_level": "unverified", "original_passage_verified": False,
                                        "verdict": "insufficient_evidence"}]}]}],
}


def _http(handler):
    return HttpProvider(settings=None, client=httpx.Client(base_url="http://audit.test", transport=httpx.MockTransport(handler)))


def test_http_provider_submit_and_errors():
    seen = {}

    def ok(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"audit_id": DOCUMENT["document_id"], "document": DOCUMENT,
                                         "lifecycle": {"mapping_status": "stale"}})

    outcome = _http(ok).submit(org_id="o", project_id="p", analysis_id="a", snapshot={"x": 1}, context={"y": 2})
    assert seen["path"] == "/v1/audits" and seen["body"]["snapshot"] == {"x": 1}
    summary = outcome["summary"]
    assert outcome["document_id"] == DOCUMENT["document_id"]
    assert summary["mapping_status"] == "stale" and summary["approval_allowed"] is False
    step = summary["routes"][0]["steps"][0]
    assert step["step_instance_id"] == "si_1" and step["sources"][0]["ref"] == "P1"
    assert step["sources"][0]["check"] == "unverified" and step["sources"][0]["lifecycle"] == "unknown"

    with pytest.raises(AuditError) as refused:
        _http(lambda r: httpx.Response(422, json={"code": "STRUCTURE_CONFLICT"})).submit(
            org_id="o", project_id="p", analysis_id="a", snapshot={}, context={})
    assert refused.value.code == "STRUCTURE_CONFLICT" and not refused.value.retryable

    with pytest.raises(AuditError) as down:
        _http(lambda r: httpx.Response(503, text="busy")).submit(org_id="o", project_id="p", analysis_id="a",
                                                                  snapshot={}, context={})
    assert down.value.code == "PROVIDER_ERROR" and down.value.retryable

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AuditError) as timeout:
        _http(slow).submit(org_id="o", project_id="p", analysis_id="a", snapshot={}, context={})
    assert timeout.value.code == "TIMEOUT"

    # 对方返回的代码不像代码（比如一段原文）时不原样保存
    with pytest.raises(AuditError) as odd:
        _http(lambda r: httpx.Response(400, json={"code": "a sentence with private text"})).submit(
            org_id="o", project_id="p", analysis_id="a", snapshot={}, context={})
    assert odd.value.code == "AUDIT_FAILED"


def test_summary_never_allows_approval():
    assert summarize(DOCUMENT, None, provider="x")["routes"][0]["steps"][0]["review_required"] is True
    assert summarize({**DOCUMENT, "chemistry_approval_allowed": True}, {}, provider="x")["approval_allowed"] is False


# ---------------------------------------------------------------- 端到端：embedded 模式，真的调 SAL


@needs_sal
@pytest.mark.parametrize("smiles", [ASPIRIN, IBUPROFEN])
def test_embedded_audit_end_to_end(client, account, ctx, smiles):
    _enable(ctx, account)
    analysis = start_analysis(client, account, smiles)
    view = client.get(f"/api/analyses/{analysis['id']}/audit", headers=account["headers"]).json()
    assert view["status"] == "done", view
    assert view["approval_allowed"] is False and view["document_id"].startswith("md_")
    assert view["provider"].startswith("sal-embedded")
    assert {route["route_id"] for route in view["routes"]} >= {"r1"}
    step = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1/audit", headers=account["headers"]).json()
    assert step["step"]["step_instance_id"].startswith("si_")
    assert step["route_snapshot_id"].startswith("rs_")
    assert "CHEMISTRY_IDENTITY_UNVERIFIED" in step["step"]["flags"]
    assert (Path(ctx.settings.var_dir) / "audit" / "mapping" / f"{analysis['id']}.sqlite3").is_file()


@needs_sal
def test_embedded_refuses_public_data(client, account, ctx):
    """默认的数据分类（public_non_sensitive）SAL v0.1 不收：记成 not_eligible，不算出错。"""
    _enable(ctx, account, classification="public_non_sensitive")
    analysis = start_analysis(client, account, IBUPROFEN)
    view = client.get(f"/api/analyses/{analysis['id']}/audit", headers=account["headers"]).json()
    assert view["status"] == "not_eligible" and view["reason"] == "RETENTION_POLICY_REQUIRED"


@needs_sal
def test_rescore_reaudits_with_parent(client, account, ctx):
    """B12 标记已有以后名次变了：同一轮运行再审计一次，用 parent_id 接上上一次。"""
    _enable(ctx, account)
    analysis = start_analysis(client, account, IBUPROFEN)
    response = client.post(f"/api/analyses/{analysis['id']}/mark-owned", headers=account["headers"],
                           json={"smiles": "CC(C)Cc1ccccc1"})
    assert response.status_code == 200, response.text
    runs = _runs(ctx, analysis["id"])
    assert [run["trigger"] for run in runs] == ["analysis", "rescore"]
    assert all(run["status"] == "done" for run in runs), runs
    assert runs[1]["parent_id"] == runs[0]["document_id"]
    assert runs[0]["execution_id"] == runs[1]["execution_id"]
