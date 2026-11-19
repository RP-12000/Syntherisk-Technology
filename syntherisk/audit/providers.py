"""审计的两种实现（embedded、http），以及它们共用的统一格式 summarize()。

两种实现的方法一样，输入输出也一样：
    submit(org_id, project_id, analysis_id, snapshot, context, parent_id)
        送一次分析去审计。返回 {"document_id": SAL 的文档编号, "summary": 统一格式}。
    source_changed(org_id, project_id, analysis_id, document_id, source_id, reason, event_id)
        某个来源被更正 / 撤稿时通知 SAL，返回更新后的 {"document_id", "summary"}（mapping_status 变成 stale）。
    health()
        自检，返回 {"mode", "ok", "detail"}。
出错一律抛 AuditError，code 是一个短代码（大写字母、数字、下划线），不带任何分析内容。

统一格式（前端和接口只认这个，换实现时不变）
    {"provider", "contract", "document_id", "mapping_status": current / stale,
     "approval_allowed": false, "review_required": true, "flags": [...],
     "routes": [{"route_id": "r1", "route_snapshot_id": "rs_…", "flags": [...],
                 "steps": [{"step_index": 1, "step_key", "step_instance_id": "si_…", "flags": [...],
                            "review_required": true,
                            "sources": [{"ref": "P1", "kind": journal / patent, "doi", "source_id": "src_…",
                                         "check": unverified / metadata_checked / passage_checked,
                                         "passage_verified": bool, "verdict", "lifecycle"}]}]}]}

http 模式的接口形状（我们提给 SAL 作者的提议，作者还没确认）
    POST {AUDIT_BASE_URL}/v1/audits           请求体 {org_id, project_id, analysis_id, snapshot, context, parent_id}，
                                              返回 {audit_id, document, lifecycle}；4xx 时返回 {code}
    POST …/v1/audits/{audit_id}/source-events 请求体 {source_id, reason, event_id}，返回同上
    GET  …/v1/health                          返回 {ok, contract}
    鉴权：Authorization: Bearer <AUDIT_TOKEN>。租户由 SAL 按令牌决定，请求体里的编号不授予任何权限。
"""

from __future__ import annotations

import re
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from syntherisk.config import Settings

# SAL 拒收、而且是因为数据政策（不是出错）的代码：记成 not_eligible，不算失败
POLICY_CODES = frozenset({"RETENTION_POLICY_REQUIRED"})
# 给 SAL 的租户和操作者编号用的命名空间（固定值）。同一个课题组永远得到同一个租户 UUID
_NAMESPACE = uuid.UUID("6f1c1f0e-5a43-5b6e-9a43-7e0a7c2b9d11")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,59}")


class AuditError(Exception):
    """审计没做成。

    code       短代码，比如 RETENTION_POLICY_REQUIRED（SAL 不收这一类数据）、STRUCTURE_CONFLICT（结果对不上）、
               JSON_LIMIT_EXCEEDED（超过 2 MiB）、TIMEOUT、PROVIDER_UNAVAILABLE。会原样存进 audit_runs.reason。
    retryable  True 表示过一会儿再试可能成功（服务暂时连不上、超时）。
    """

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else "AUDIT_FAILED"
        self.retryable = retryable
        super().__init__(self.code)


def summarize(document: dict[str, Any], lifecycle: dict[str, Any] | None, *, provider: str) -> dict[str, Any]:
    """把 SAL 的 Mapping 文档整理成统一格式（字段见本文件开头）。

    approval_allowed 和 review_required 不看文档，固定是 false / true：审计记录永远不等于批准。
    """
    routes = []
    for route in document.get("routes") or []:
        steps = []
        for step in route.get("steps") or []:
            sources = []
            for source in step.get("sources") or []:
                sources.append({
                    "ref": source.get("upstream_ref"),
                    "kind": source.get("kind"),
                    "doi": source.get("doi"),
                    "source_id": source.get("source_id"),
                    "check": source.get("verification_level") or "unverified",
                    "passage_verified": bool(source.get("original_passage_verified")),
                    "verdict": source.get("verdict"),
                    "lifecycle": source.get("lifecycle") or "unknown",
                })
            steps.append({
                "step_index": step.get("upstream_step_index"),
                "step_key": step.get("upstream_step_key"),
                "step_instance_id": step.get("step_instance_id"),
                "flags": list(step.get("diagnostics") or []),
                "review_required": True,
                "sources": sources,
            })
        routes.append({
            "route_id": route.get("upstream_route_label"),
            "route_snapshot_id": route.get("route_snapshot_id"),
            "flags": list(route.get("diagnostics") or []),
            "steps": steps,
        })
    return {
        "provider": provider,
        "contract": document.get("contract_version"),
        "document_id": document.get("document_id"),
        "mapping_status": (lifecycle or {}).get("mapping_status") or document.get("mapping_status") or "current",
        "approval_allowed": False,
        "review_required": True,
        "flags": list(document.get("diagnostics") or []),
        "routes": routes,
    }


def tenant_id(org_id: str) -> str:
    """课题组在 SAL 那里的租户编号：由课题组编号算出来的固定 UUID。"""
    return str(uuid.uuid5(_NAMESPACE, f"org:{org_id}"))


# 我们的后端在 SAL 那里的操作者编号（所有审计都以它的名义写）
ACTOR_ID = str(uuid.uuid5(_NAMESPACE, "actor:syntherisk-backend"))


# ---------------------------------------------------------------- embedded：进程内调用 SAL Mapping

_LOAD_LOCK = threading.Lock()
_LOADED: dict[str, SimpleNamespace] = {}


def _load_sal(path: str) -> SimpleNamespace:
    """从 AUDIT_SAL_PATH 导入 SAL 的 Mapping 库（同一个路径只导入一次）。

    AUDIT_SAL_PATH 是 SAL 源码的根目录（里面有 sal_core/ 和 syntherisk_mapping/）。
    它加在 sys.path 的最后面：SAL 的包名都不和我们的重名，放最后也不会盖住后端自己的模块。
    抛出：AuditError("PROVIDER_UNAVAILABLE")：路径不对，或者导入失败（比如缺依赖）。
    """
    with _LOAD_LOCK:
        if path in _LOADED:
            return _LOADED[path]
        root = Path(path)
        if not path or not (root / "syntherisk_mapping").is_dir() or not (root / "sal_core").is_dir():
            raise AuditError("PROVIDER_UNAVAILABLE", retryable=True)
        if str(root) not in sys.path:
            sys.path.append(str(root))
        try:
            from sal_core.auth import Identity
            from syntherisk_mapping import CONTRACT_VERSION
            from syntherisk_mapping.access import MappingAccess, ProjectGrant
            from syntherisk_mapping.errors import MappingError
            from syntherisk_mapping.service import MappingService
            from syntherisk_mapping.store import MappingStore
        except Exception:  # noqa: BLE001 - 缺依赖、版本不对都按“审计暂不可用”处理
            raise AuditError("PROVIDER_UNAVAILABLE", retryable=True) from None
        loaded = SimpleNamespace(Identity=Identity, MappingAccess=MappingAccess, ProjectGrant=ProjectGrant,
                                 MappingError=MappingError, MappingService=MappingService, MappingStore=MappingStore,
                                 contract=CONTRACT_VERSION)
        _LOADED[path] = loaded
        return loaded


class EmbeddedProvider:
    """embedded 模式：在后端进程里直接调用 SAL 的 Mapping 库。

    每次分析一个 SQLite 文件：<var_dir>/audit/mapping/<分析编号>.sqlite3。同一次分析的几次审计
    （比如排除路线以后重新导出）放在同一个文件里，parent_id 才能串起来；SAL 给每个文件设了 64 MB 上限，
    一次分析用不到。不联网，不写 SAL Core。
    """

    def __init__(self, settings: Settings):
        self._sal = _load_sal(settings.audit_sal_path)
        self._store_dir = Path(settings.var_dir) / "audit" / "mapping"
        self.label = f"sal-embedded {self._sal.contract}"

    def _service(self, org_id: str, project_id: str, analysis_id: str):
        sal = self._sal
        tenant = tenant_id(org_id)
        identity = sal.Identity(tenant_id=tenant, actor_id=ACTOR_ID, scopes=frozenset({"getEvidence", "createEvidence"}))
        # 服务器决定的绑定：我们的项目 → SAL 的项目。快照里的任何字段都不授予权限（这是 SAL 的要求）
        grant = sal.ProjectGrant(project_id, f"syntherisk-{project_id}", True)
        access = sal.MappingAccess({(tenant, ACTOR_ID, project_id): grant})
        self._store_dir.mkdir(parents=True, exist_ok=True)
        store = sal.MappingStore(self._store_dir / f"{analysis_id}.sqlite3")
        return identity, sal.MappingService(store, access)

    def submit(self, *, org_id: str, project_id: str, analysis_id: str, snapshot: dict[str, Any],
               context: dict[str, Any], parent_id: str | None = None) -> dict[str, Any]:
        try:
            identity, service = self._service(org_id, project_id, analysis_id)
            record = service.map(identity, snapshot, context, parent_id=parent_id)
            lifecycle = service.status(identity, record["document_id"])
        except self._sal.MappingError as exc:
            raise AuditError(exc.code) from None
        return {"document_id": record["document_id"], "summary": summarize(record["document"], lifecycle, provider=self.label)}

    def source_changed(self, *, org_id: str, project_id: str, analysis_id: str, document_id: str, source_id: str,
                       reason: str, event_id: str) -> dict[str, Any]:
        try:
            identity, service = self._service(org_id, project_id, analysis_id)
            service.source_changed(identity, document_id, source_id, reason=reason, event_id=event_id)
            record = service.get(identity, document_id)
            lifecycle = service.status(identity, document_id)
        except self._sal.MappingError as exc:
            raise AuditError(exc.code) from None
        return {"document_id": document_id, "summary": summarize(record["document"], lifecycle, provider=self.label)}

    def health(self) -> dict[str, Any]:
        return {"mode": "embedded", "ok": True, "detail": self._sal.contract}


# ---------------------------------------------------------------- http：调审计接口


class HttpProvider:
    """http 模式：调 AUDIT_BASE_URL 上的审计接口（接口形状见本文件开头）。

    参数 client：测试时传一个带假传输层的 httpx.Client；正式运行时按配置新建。
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if client is None:
            if not settings.audit_base_url:
                raise AuditError("PROVIDER_UNAVAILABLE", retryable=True)
            headers = {"Accept": "application/json"}
            if settings.audit_token:
                headers["Authorization"] = f"Bearer {settings.audit_token}"
            client = httpx.Client(base_url=settings.audit_base_url.rstrip("/"), headers=headers,
                                  timeout=settings.audit_timeout)
        self._client = client
        self.label = "sal-http"

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, json=body)
        except httpx.TimeoutException:
            raise AuditError("TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            raise AuditError("PROVIDER_UNAVAILABLE", retryable=True) from None
        if response.status_code >= 500:
            raise AuditError("PROVIDER_ERROR", retryable=True)
        try:
            data = response.json()
        except ValueError:
            raise AuditError("PROVIDER_ERROR") from None
        if response.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            raise AuditError(code if isinstance(code, str) else f"HTTP_{response.status_code}")
        if not isinstance(data, dict):
            raise AuditError("PROVIDER_ERROR")
        return data

    def _outcome(self, data: dict[str, Any]) -> dict[str, Any]:
        document = data.get("document")
        if not isinstance(document, dict):
            raise AuditError("PROVIDER_ERROR")
        document_id = data.get("audit_id") or document.get("document_id")
        if not isinstance(document_id, str) or not document_id:
            raise AuditError("PROVIDER_ERROR")
        return {"document_id": document_id, "summary": summarize(document, data.get("lifecycle"), provider=self.label)}

    def submit(self, *, org_id: str, project_id: str, analysis_id: str, snapshot: dict[str, Any],
               context: dict[str, Any], parent_id: str | None = None) -> dict[str, Any]:
        data = self._call("POST", "/v1/audits", {"org_id": org_id, "project_id": project_id, "analysis_id": analysis_id,
                                                 "snapshot": snapshot, "context": context, "parent_id": parent_id})
        return self._outcome(data)

    def source_changed(self, *, org_id: str, project_id: str, analysis_id: str, document_id: str, source_id: str,
                       reason: str, event_id: str) -> dict[str, Any]:
        data = self._call("POST", f"/v1/audits/{document_id}/source-events",
                          {"source_id": source_id, "reason": reason, "event_id": event_id})
        return self._outcome(data)

    def health(self) -> dict[str, Any]:
        try:
            data = self._call("GET", "/v1/health")
        except AuditError as exc:
            return {"mode": "http", "ok": False, "detail": exc.code}
        return {"mode": "http", "ok": bool(data.get("ok")), "detail": data.get("contract")}


def build_provider(settings: Settings):
    """按 AUDIT_MODE 建一种实现。off 时抛 AuditError("DISABLED")。"""
    if settings.audit_mode == "embedded":
        return EmbeddedProvider(settings)
    if settings.audit_mode == "http":
        return HttpProvider(settings)
    raise AuditError("DISABLED")
