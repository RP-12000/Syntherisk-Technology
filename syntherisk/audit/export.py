"""把一次分析导出成 SAL Mapping 要的两样东西：快照（snapshot）和上下文（context）。

快照
    {"upstream_analysis_id", "upstream_project_id", "upstream_attempt", "result"}。
    result 是数据库里存的原始 Analysis.result（解密后的字典），不是 GET …/result 整理过的样子：
    整理过的缺分子表和反应表，SAL 会拒收。也不调证据卡、先例接口（它们会补写说明、联网查 DOI）。

上下文（SAL mapping/v0.1 的 Context，八个键都必须有）
    execution_id        这一次运行的编号：run_analysis 每次开跑时生成，存在 analyses.execution_id
    observation_id      这一次导出的编号：每条 audit_runs 记录一个
    source_version      先例库的版本（library_versions 表里 precedents 的版本号）；没导入过是 "unknown"
    retrieved_at        导出时间，UTC，结尾是 Z
    engine_fingerprint  逆合成引擎和模型文件的 SHA-256（见 engine_fingerprint）；算不出是 "unknown"
    config_fingerprint  评分规则和影响搜索的配置项的 SHA-256（见 config_fingerprint）
    upstream_revision   后端代码的 git commit（40 位）；拿不到是 "unknown"
    data_classification synthetic / public_non_sensitive / confidential（见 classification）

谁在用
    service.py 的 run_audit()。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from syntherisk import models
from syntherisk.config import PROJECT_ROOT, Settings

# SAL 的数据分类（mapping/v0.1 Context.data_classification 的取值）。SAL v0.1 只接受 synthetic
SYNTHETIC = "synthetic"
PUBLIC = "public_non_sensitive"
CONFIDENTIAL = "confidential"

# 影响搜索、评分和说明的配置项。改了其中任何一项，同一个分子可能得到不同的结果，所以都算进 config_fingerprint
_RESULT_SETTINGS = (
    "m3_engine", "aizynth_stock", "aizynth_expansion_policy", "aizynth_filter_policy",
    "aizynth_iteration_limit", "aizynth_time_limit", "aizynth_cutoff_number", "aizynth_cutoff_cumulative",
    "controlled_jurisdictions", "explain_top_routes", "llm_provider", "llm_model",
)
# data/ 下面影响结果的规则文件
_RULE_FILES = ("scoring.toml", "condition_rules.toml", "vocabulary.toml", "data_sources.toml", "compounds.csv",
               "controlled_substances.csv")
_SHA1 = re.compile(r"[0-9a-f]{40}")


def demo_org_ids(settings: Settings) -> set[str]:
    """AUDIT_DEMO_ORGS 里列的课题组编号（逗号隔开），去掉空白。"""
    return {part.strip() for part in settings.audit_demo_orgs.split(",") if part.strip()}


def in_scope(settings: Settings, analysis: models.Analysis) -> bool:
    """这次分析在不在审计范围里。

    AUDIT_SCOPE=demo_only（默认）：只有 AUDIT_DEMO_ORGS 里的课题组算；all：所有课题组都算
    （真实用户的分析会被 SAL v0.1 按数据分类拒收，见 classification）。
    """
    if settings.audit_scope == "all":
        return True
    return analysis.org_id in demo_org_ids(settings)


def classification(settings: Settings, analysis: models.Analysis) -> str:
    """这次分析在 SAL 那里算哪一类数据。

    演示课题组的分析按 AUDIT_DEMO_CLASSIFICATION 算（默认 public_non_sensitive：公开分子、公开先例，
    但不是人造的合成数据。SAL 作者确认可以按 synthetic 送之前，不要改成 synthetic）；
    其余课题组的分析都是 confidential（客户数据）。
    """
    if analysis.org_id in demo_org_ids(settings):
        return settings.audit_demo_classification
    return CONFIDENTIAL


def build_snapshot(analysis: models.Analysis) -> dict[str, Any]:
    """SAL Mapping 的输入快照（SnapshotEnvelope）。result 原样放进去，SAL 自己会做一份有界的副本。"""
    return {
        "upstream_analysis_id": analysis.id,
        "upstream_project_id": analysis.project_id,
        "upstream_attempt": int(analysis.attempts or 0),
        "result": analysis.result,
    }


def build_context(session: Session, settings: Settings, analysis: models.Analysis, run: models.AuditRun) -> dict[str, Any]:
    """SAL Mapping 的上下文（Context）。各个键的含义见本文件开头。"""
    engine_name = ((analysis.result or {}).get("engine") or {}).get("name") or analysis.engine or "unknown"
    return {
        "execution_id": run.execution_id,
        "observation_id": run.observation_id,
        "source_version": _source_version(session),
        "retrieved_at": models.utcnow().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine_fingerprint": engine_fingerprint(settings, engine_name),
        "config_fingerprint": config_fingerprint(settings),
        "upstream_revision": upstream_revision(settings),
        "data_classification": classification(settings, analysis),
    }


def _source_version(session: Session) -> str:
    """先例库的版本：library_versions 表里 "precedents" 那一行的版本号（每导入一次先例就换一个）。

    没导入过先例时是 "unknown"。
    """
    row = session.get(models.LibraryVersion, "precedents")
    return f"precedents-{row.version}" if row is not None and row.version else "unknown"


@lru_cache(maxsize=256)
def _file_sha256(path: str, size: int, mtime_ns: int) -> str:
    """一个文件内容的 SHA-256。按（路径、大小、修改时间）缓存：模型文件有几百 MB，不用每次都重新算。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    info = path.stat()
    return _file_sha256(str(path.resolve()), info.st_size, info.st_mtime_ns)


def _combine(parts: dict[str, Any]) -> str:
    """把一组（名字 → 值）稳定地合成一个 SHA-256：按名字排序、写成紧凑的 JSON 再算。"""
    text = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def engine_fingerprint(settings: Settings, engine_name: str) -> str:
    """逆合成引擎的指纹：同一个指纹 = 同一套路线数据或模型文件。

    demo           data/demo_routes/ 下所有文件的内容
    aizynthfinder  config.yml 的内容，加上它里面提到的每个文件（模型、模板、库存表）的内容
    其他或者文件读不到时返回 "unknown"（SAL 会把这一项记成“未核实”）。
    """
    try:
        if engine_name == "demo":
            folder = Path(settings.data_dir) / "demo_routes"
            files = {p.name: _hash_file(p) for p in sorted(folder.glob("*")) if p.is_file()}
            return _combine({"engine": "demo", "files": files}) if files else "unknown"
        if engine_name == "aizynthfinder" and settings.aizynth_config:
            config = Path(settings.aizynth_config)
            files = {"config.yml": _hash_file(config)}
            for referenced in _referenced_files(config):
                files[referenced.name] = _hash_file(referenced)
            return _combine({"engine": "aizynthfinder", "files": files})
    except OSError:
        return "unknown"
    return "unknown"


def _referenced_files(config: Path) -> list[Path]:
    """AiZynthFinder 的 config.yml 里写到的文件（模型、模板、库存表）。相对路径按 config.yml 所在目录算。"""
    import yaml  # PyYAML 随 AiZynthFinder 装在同一个环境里；只有用到这个引擎时才导入

    found: list[Path] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and len(value) < 1024:
            candidate = Path(value) if os.path.isabs(value) else config.parent / value
            if candidate.is_file() and candidate.resolve() not in {p.resolve() for p in found}:
                found.append(candidate)

    walk(yaml.safe_load(config.read_text(encoding="utf-8")))
    return found


def config_fingerprint(settings: Settings) -> str:
    """评分和搜索规则的指纹：data/ 下的规则文件，加上影响结果的配置项（_RESULT_SETTINGS）。"""
    try:
        files = {name: _hash_file(Path(settings.data_dir) / name) for name in _RULE_FILES
                 if (Path(settings.data_dir) / name).is_file()}
    except OSError:
        return "unknown"
    values = {name: getattr(settings, name, None) for name in _RESULT_SETTINGS}
    return _combine({"files": files, "settings": values})


def upstream_revision(settings: Settings) -> str:
    """后端代码的 git commit（40 位十六进制）。

    先看配置项 APP_REVISION（部署脚本可以写进去）；没有就读项目目录下的 .git（不需要装 git）；
    都拿不到返回 "unknown"。
    """
    configured = (settings.app_revision or "").strip().lower()
    if _SHA1.fullmatch(configured):
        return configured
    return _git_head(PROJECT_ROOT) or "unknown"


def _git_head(root: Path) -> str | None:
    """不调 git 命令，直接读 .git/HEAD 找到当前 commit。读不到返回 None。"""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if _SHA1.fullmatch(head):
            return head
        if not head.startswith("ref: "):
            return None
        ref = head[5:].strip()
        loose = git / ref
        if loose.is_file():
            value = loose.read_text(encoding="utf-8").strip()
            return value if _SHA1.fullmatch(value) else None
        packed = git / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(" ")
                if len(parts) == 2 and parts[1] == ref and _SHA1.fullmatch(parts[0]):
                    return parts[0]
    except OSError:
        return None
    return None
