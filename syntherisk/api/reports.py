"""路线对比、导出报告、回填实验结果的接口（M10、M13、M14）。

对应按钮：
    D1  六个权重滑杆：前端按 /api/scoring/config 的公式自己重算；
        也可以把权重发给后端算                    POST /api/analyses/{id}/compare
    D4  导出对比                                POST /api/analyses/{id}/compare/export
    D5  差异步骤（对比结果里每一步带 shared 标记，前端据此灰显共同步骤）
    F1 F2 F3  选内容、选格式、生成并下载          POST /api/analyses/{id}/reports
                                               GET  /api/reports/{id}/download
    G1 G2 G3 G4  记录实验结果并保存              POST /api/analyses/{id}/routes/{route_id}/steps/{n}/experiments
    G2  附 TLC、NMR 照片                        POST /api/experiments/{id}/attachments
    G3  撤回或补上授权                          PATCH /api/experiments/{id}

另外四个接口没有单独的按钮编号：
    物料清单预览（按规模折算用量）                GET  /api/analyses/{id}/reports/materials
    这次分析下的全部实验记录                     GET  /api/analyses/{id}/experiments
    一条实验记录的附件列表                       GET  /api/experiments/{id}/attachments
    下载一个附件                                GET  /api/experiments/attachments/{id}
本文件的接口都要登录。

这个文件做三件事：
    路线对比（D）  读已经存好的评分结果；请求里给了权重，就在副本上让 M10 按新权重重算一遍。
                  导出时由 M13 生成 xlsx 或 csv 文件。
    导出报告（F）  用已经算好的结果和 M11 写好的说明，由 M13 拼成 PDF、Word，或者导出结构文件（zip）。
                  不重新分析。
    回填（G）      把某一步实际做出来的结果记进 experiment_records 表（M14），并挂到分析结果里的那一步上，
                  路线树和证据卡上才会显示“已做”。

文件怎么存：
    报告、对比表、实验附件都由 storage.save_file 加密以后写到 var/files 目录下，文件名是随机的；
    数据库里（reports、experiment_attachments 两张表）只记这个随机名、给用户看的文件名、大小等。
    下载时 storage.load_file 读出来并解密，storage.download_response 组装成带文件名的下载响应。

它依赖谁：
    api/deps.py、api/schemas.py、api/views.py（iso：时间的格式）
    cards.py                 find_route：按编号找路线
    constraints.py           Constraints、merge_constraints：把提交时的约束合并成实际执行的那一份
    modules/m10_scoring.py   compare_routes（对比表的数据）、score_result（换权重重算）、DIMENSIONS（六个维度）
    modules/m11_explain.py   Explainer.explain_route：报告里缺推荐理由时现写一段
    modules/m13_reports.py   拼报告内容、渲染 PDF / Word、导出结构文件、对比表、物料清单、文件名
    modules/m14_backfill.py  保存实验记录、把结果挂到分析结果上、记录的 JSON 样子
    storage.py               save_file、load_file、read_upload、download_response
谁调用它：前端。syntherisk/main.py 把本文件的 router 挂到 /api 下。
读写的表：reports（写）、experiment_records（写）、experiment_attachments（写）、
          analyses（回填时改写 result）；配了大模型时，补写推荐理由会在 llm_calls 表里记一次用量。

本文件里有什么（按阅读顺序）：
    MEDIA_TYPES、ATTACHMENT_TYPES   文件格式对应的 Content-Type；允许上传的附件类型
    _report_view          报告记录的 JSON 样子
    _store_report         把生成的文件存盘，并记进 reports 表
    _weighted_result      按请求里的权重在副本上重算，或者直接用存好的结果
    compare               POST  /api/analyses/{id}/compare                    D1、D5
    export_comparison     POST  /api/analyses/{id}/compare/export             D4
    create_report         POST  /api/analyses/{id}/reports                    F1–F3
    download_report       GET   /api/reports/{id}/download                    F3
    preview_materials     GET   /api/analyses/{id}/reports/materials
    _load_record          取本课题组的一条实验记录
    record_experiment     POST  /api/analyses/{id}/routes/{route_id}/steps/{n}/experiments   G1–G4
    list_experiments      GET   /api/analyses/{id}/experiments
    update_experiment     PATCH /api/experiments/{id}                         G3
    add_attachment        POST  /api/experiments/{id}/attachments             G2
    list_attachments      GET   /api/experiments/{id}/attachments
    download_attachment   GET   /api/experiments/attachments/{id}
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from syntherisk import i18n, models
from syntherisk.api import schemas
from syntherisk.api.deps import (
    Principal, get_ctx, get_db, get_principal, load_analysis, load_finished_analysis, load_scored_analysis,
)
from syntherisk.api.views import iso
from syntherisk.cards import find_route
from syntherisk.constraints import Constraints, merge_constraints
from syntherisk.context import AppContext
from syntherisk.modules import m13_reports, m14_backfill
from syntherisk.modules.m10_scoring import DIMENSIONS, compare_routes, score_result
from syntherisk.modules.m11_explain import Explainer
from syntherisk.storage import download_response, load_file, read_upload, save_file

# 本文件的接口分属“对比”“报告”“回填”三组，所以 tags 写在每个接口上，这里不统一指定
router = APIRouter()

# 文件格式 → 下载时响应头里的 Content-Type（浏览器靠它知道这是什么文件）。
# 键是 reports.format 这一列里存的值：pdf、docx、sdf 是报告的格式，xlsx、csv 是对比表的格式。
# sdf 对应的是 zip：结构文件是 route.sdf 加上每一步的 .rxn，打成一个压缩包
MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "sdf": "application/zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}
# 实验附件允许的类型：上传时声明的 Content-Type 要以其中一个开头，也就是各种图片（image/…）和 PDF
ATTACHMENT_TYPES = ("image/", "application/pdf")


def _report_view(report: models.Report) -> dict[str, Any]:
    """把 reports 表里的一行整理成返回给前端的字典。

    参数：
        report  models.Report。
    返回：
        字典：
            id            报告编号
            analysis_id   它是哪一次分析的
            kind          report 报告 / comparison 对比表
            format        pdf / docx / sdf / xlsx / csv
            filename      下载时用的文件名
            size_bytes    文件大小（字节，加密之前的大小）
            created_at    生成时间，带时区的 ISO 8601 字符串
            download_url  下载地址 /api/reports/{id}/download；下载时同样要带登录令牌
    谁在用：
        本文件的 create_report。
        项目历史（api/core.py 的 project_history）没有调它，在那边另外拼了一份字段相同的字典。
    """
    return {
        "id": report.id,
        "analysis_id": report.analysis_id,
        "kind": report.kind,
        "format": report.format,
        "filename": report.filename,
        "size_bytes": report.size_bytes,
        "created_at": iso(report.created_at),
        # 下载地址是一个以 /api 开头的路径，不带域名；对应本文件的 download_report 接口
        "download_url": f"/api/reports/{report.id}/download",
    }


def _store_report(ctx, db, analysis, principal, *, kind: str, fmt: str, filename: str, data: bytes, options: dict) -> models.Report:
    """把生成好的文件存起来，并在 reports 表里记一条。项目历史里的下载记录就是这张表。

    参数：
        ctx        运行时上下文（AppContext），文件目录和加密用的密钥在里面。
        db         数据库会话。
        analysis   这份文件是哪一次分析的（models.Analysis）；课题组和项目从它身上取。
        principal  当前登录的人，记作生成者（created_by）。
        kind       "report" 报告 / "comparison" 对比表。
        fmt        文件格式：pdf / docx / sdf / xlsx / csv。
        filename   下载时用的文件名。
        data       文件内容（字节）。
        options    生成时用的选项（哪条路线、哪些部分、权重等），原样存进 reports.options 这一列。
    返回：
        新建的 models.Report（已经提交，id 可以用了）。
    副作用：
        写文件：storage.save_file 把内容加密以后写到 var/files/<随机名>，返回这个随机名。
        写数据库：reports 表加一行，并且在这个函数里就 db.commit() 了。
    谁在用：
        本文件的 export_comparison、create_report。
    """
    report = models.Report(
        org_id=analysis.org_id,
        project_id=analysis.project_id,
        analysis_id=analysis.id,
        kind=kind,
        format=fmt,
        filename=filename,
        options=options,
        # 先把文件写到磁盘上，表里只记它的随机存储名；下载时凭这个名字找文件
        storage_name=save_file(ctx, data),
        size_bytes=len(data),
        created_by=principal.user_id,
    )
    db.add(report)
    db.commit()
    return report


def _weighted_result(ctx: AppContext, analysis: models.Analysis, weights: dict[str, float] | None) -> dict[str, Any]:
    """换了权重就在副本上重新算一遍；没换就用存好的结果。

    参数：
        ctx       运行时上下文，用到 ctx.reference（评分配置）。
        analysis  已经评过分的一次分析。
        weights   请求里给的权重 {维度编号: 权重}；None 或者空字典表示不换权重。
    返回：
        一份分析结果（字典）。
        没给权重：就是 analysis.result 本身，调用方不要去改它。
        给了权重：是深拷贝以后重新评分的副本，里面的分数、名次、weights 都是按新权重算的；不保存。
    抛出：
        HTTPException 422：权重里有不认识的维度、有负数，或者加起来不大于 0。
    谁在用：
        本文件的 compare、export_comparison。
    """
    if not weights:
        return analysis.result
    # 检查权重：键只能是六个维度的编号（DIMENSIONS）；值不能为负；不能全是 0。
    # set(weights) 取的是字典的键，两个集合相减得到“不认识的键”
    unknown = set(weights) - set(DIMENSIONS)
    if unknown or any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        raise HTTPException(422, i18n.tr("权重只能包含这几项，且不能为负：{dims}",
                                         "Weights may only contain these keys, none negative: {dims}",
                                         dims=i18n.join(DIMENSIONS)))
    # 深拷贝：重新评分会直接改写传进去的字典，在副本上做才不会动到存着的结果
    result = copy.deepcopy(analysis.result)
    effective = merge_constraints(Constraints.model_validate(analysis.constraints), ctx.reference)
    # 请求里没写的维度按 0 算（weights.get(d, 0)），也就是这一项不参与评分。
    # 硬约束和用户的排除照旧生效；按新权重重算的是步骤分、路线分、名次，以及跟权重有关的
    # coverage、置信度和最薄弱环节（m10_scoring.evaluate_route）
    score_result(result, effective, ctx.reference, user_exclusions=analysis.user_exclusions or [],
                 weights={d: float(weights.get(d, 0)) for d in DIMENSIONS})
    return result


# ---------------------------------------------------------------- 路线对比（D）


@router.post("/analyses/{analysis_id}/compare", tags=["对比"])
def compare(analysis_id: str, body: schemas.CompareRequest, principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """R9：排序、雷达图数据、指标表、差异步骤。给了 weights 就按这组权重重算（不保存）。

    D1、D5。需要登录。请求体见 `CompareRequest`。返回：

    - `dimension_labels`：六个维度的中文名 `{编号: 名称}`
    - `routes`：要对比的路线，按路线分从高到低排。每条：`route_id`、`name`、`score`、`n_steps`、
      `literature_coverage`（文献覆盖，百分数）、`inventory_coverage`（库存覆盖，百分数）、
      `special_operations`（特殊操作的文字；没有是“无”）、`green`（绿色化学各步的平均分）、
      `material_cost`（原料成本，第一版是“待询价”）、`confidence`（置信度：高 / 中 / 低）、
      `radar`（雷达图数据：六个维度各步的平均分）、
      `steps`（每一步 `{index, key, name, shared}`；`shared` 是 `true` 表示这一步在被对比的每条路线里都有）
    - `weights`：这次用的权重

    `route_ids` 里不存在的编号会被跳过。出错：404 没有这个分析，或者要对比的路线一条都没找到；
    409 这次分析还没有评分结果；422 权重不对。
    """
    analysis = load_scored_analysis(db, principal, analysis_id)
    # 没给权重：直接用存好的结果；给了：在副本上按新权重重算
    result = _weighted_result(ctx, analysis, body.weights)
    # M10 整理出对比表的数据：每条路线一行指标，并标出哪些步骤是这几条路线共有的（D5）
    comparison = compare_routes(result, body.route_ids, ctx.reference.scoring)
    if not comparison["routes"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有找到要对比的路线", "None of the routes to compare were found"))
    comparison["weights"] = result.get("weights")
    # 按路线分从高到低排：键取负数，sort 从小到大排，分高的就在前面；分数是 None 的当 0
    comparison["routes"].sort(key=lambda r: -(r["score"] or 0))
    return comparison


@router.post("/analyses/{analysis_id}/compare/export", tags=["对比"])
def export_comparison(analysis_id: str, body: schemas.CompareExportRequest, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """D4 导出对比表（R11 下载文件），同时记进项目历史。

    需要登录。请求体见 `CompareExportRequest`。直接返回文件内容，不是 JSON：`xlsx` 是 Excel 文件，
    里面有“路线对比”和“权重”两张表；`csv` 是带 BOM 的 UTF-8 文本（Excel 直接打开不乱码）。
    文件名在响应头 `Content-Disposition` 里，形如“目标分子_路线对比_日期.xlsx”。
    表里各条路线的先后和 `route_ids` 的顺序一致。

    同一份文件会存进项目历史，之后可以从 `GET /api/projects/{id}/history` 给的地址再次下载。

    出错：同 `POST /api/analyses/{id}/compare`。
    """
    analysis = load_scored_analysis(db, principal, analysis_id)
    result = _weighted_result(ctx, analysis, body.weights)
    comparison = compare_routes(result, body.route_ids, ctx.reference.scoring)
    if not comparison["routes"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有找到要对比的路线", "None of the routes to compare were found"))
    # “权重”那张表里写哪组权重：结果里记着的那一组（重算过的就是新权重）；结果里没有时用配置里的默认权重
    weights = result.get("weights") or ctx.reference.scoring["weights"]
    # M13 生成文件内容（字节）：xlsx 用 openpyxl，csv 用标准库
    data = m13_reports.comparison_xlsx(comparison, weights) if body.format == "xlsx" else m13_reports.comparison_csv(comparison)
    # 文件名里的目标分子：有名字用名字，没有就用分子式
    target = analysis.target.get("name") or analysis.target["formula"]
    filename = m13_reports.default_filename(target, "路线对比", body.format)
    # 存盘并记进 reports 表（kind="comparison"），然后把同样的内容直接作为下载响应返回
    _store_report(ctx, db, analysis, principal, kind="comparison", fmt=body.format, filename=filename, data=data,
                  options={"route_ids": body.route_ids, "weights": weights})
    return download_response(data, filename, MEDIA_TYPES[body.format])


# ---------------------------------------------------------------- 导出报告（F）


@router.post("/analyses/{analysis_id}/reports", status_code=status.HTTP_201_CREATED, tags=["报告"])
def create_report(analysis_id: str, body: schemas.ReportCreate, principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """F3 生成：用已经算好的结果和写好的说明拼报告，不重新分析。生成后存进项目历史，返回下载地址。

    需要登录。请求体见 `ReportCreate`。成功返回 201：

    - `id`、`analysis_id`
    - `kind`：固定是 `report`
    - `format`：`pdf` / `docx` / `sdf`
    - `filename`：文件名。选 `sdf` 时文件是一个 zip，里面是 `route.sdf`（路线上的全部分子）、
      每一步的 `step_N.rxn` 和一个 `README.txt`
    - `size_bytes`、`created_at`
    - `download_url`：下载地址。再 `GET` 它（同样要带登录令牌）才拿到文件

    出错：404 没有这个分析，或者没有这条路线；409 这次分析还没有评分结果；422 请求体不对。
    """
    analysis = load_scored_analysis(db, principal, analysis_id)
    result = analysis.result
    route = find_route(result, body.route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这条路线", "Route not found"))
    # 规模档位（mg / 百 mg / g）在提交分析时的约束里，报告的物料清单按它折算
    constraints = Constraints.model_validate(analysis.constraints)

    if body.format == "sdf":
        # 结构文件：不拼报告，直接把路线上的分子和每一步反应导出成 zip。
        # 右边是一个二元组 (文件内容, 扩展名)，一次赋给 data 和 extension 两个变量
        data, extension = m13_reports.export_structures_zip(result, route), "zip"
    else:
        if not route.get("explanation"):
            # 这条路线不在前几名里，还没有写过推荐理由：现在补上（不存回去，免得和后台任务抢着写）
            # {**route, "explanation": …} 是复制出一个新字典再加上 explanation，result 里的那条路线没有被改
            route = {**route, "explanation": Explainer(ctx.llm).explain_route(route, org_id=principal.org_id, analysis_id=analysis.id)}
        # 报告内容先拼成和格式无关的“块”（标题、段落、表格、图片……），再交给 PDF 或 Word 的渲染函数
        blocks = m13_reports.build_blocks(result, route, ctx.reference, sections=list(body.sections),
                                          scale=constraints.scale, target_mg=body.target_mg)
        if body.format == "pdf":
            # PDF 要嵌中文字体：report_font_path 是配置里指定的字体文件，留空就自动找系统里的中文字体
            data, extension = m13_reports.render_pdf(blocks, ctx.settings.report_font_path), "pdf"
        else:
            data, extension = m13_reports.render_docx(blocks), "docx"

    target = analysis.target.get("name") or analysis.target["formula"]
    # 文件名：用户给了就用用户的（safe_filename 去掉文件名里不能用的字符），否则是“目标分子_路线名_日期.扩展名”
    filename = m13_reports.safe_filename(body.filename) if body.filename else m13_reports.default_filename(target, route["name"], extension)
    # 用户给的文件名可能没带扩展名，或者带的不对：补上正确的
    if not filename.lower().endswith(f".{extension}"):
        filename += f".{extension}"
    # fmt 存的是请求里的格式（sdf），不是文件的扩展名（zip）；下载时按 MEDIA_TYPES["sdf"] 给出 zip 的类型
    report = _store_report(ctx, db, analysis, principal, kind="report", fmt=body.format, filename=filename, data=data,
                           options={"route_id": body.route_id, "sections": list(body.sections), "target_mg": body.target_mg})
    return _report_view(report)


@router.get("/reports/{report_id}/download", tags=["报告"])
def download_report(report_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                    ctx: AppContext = Depends(get_ctx)):
    """下载一份生成过的报告或对比表。

    需要登录，同组的人都能下载。返回文件内容，`Content-Type` 按文件格式而定，
    文件名在响应头 `Content-Disposition` 里。

    出错：404 没有这份报告（别的课题组的报告也是 404），或者文件已经不在磁盘上了。
    """
    report = db.get(models.Report, report_id)
    if report is None or report.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这份报告", "Report not found"))
    # load_file 按存储名读出文件并解密（文件不在了它会抛 404）；
    # download_response 加上 Content-Disposition 头，浏览器就会当作下载，并用上中文文件名
    return download_response(load_file(ctx, report.storage_name), report.filename, MEDIA_TYPES[report.format])


@router.get("/analyses/{analysis_id}/reports/materials", tags=["报告"])
def preview_materials(analysis_id: str, route_id: str, target_mg: float | None = None,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                      ctx: AppContext = Depends(get_ctx)):
    """物料清单预览：按规模折算每个起始原料的用量。

    需要登录。

    - 查询参数 `route_id`：哪条路线，必填
    - 查询参数 `target_mg`：想得到多少毫克目标物。不带（或者是 0）时按这次分析的规模档位：
      现在的配置是 mg 级 10、百 mg 级 100、g 级 1000

    返回：

    - `target_mg`：实际按多少毫克折算的
    - `rows`：物料的数组。每项 `{name, role, steps, mol_weight, mmol, mg, source}`：`role` 是“起始原料”
      或“试剂”；`steps` 是用在第几步；`source` 是来源（库存 / 可购买 / 买不到 / 未知）。
      试剂现在没有用量数据，`mmol` 和 `mg` 是 `null`
    - `assumed_full_yield_steps`：哪几步没有产率记录、按 100% 算的
    - `note`：估算方法的说明

    出错：404 没有这个分析，或者没有这条路线；409 这次分析还没有评分结果；422 目标产量要大于 0。
    """
    analysis = load_scored_analysis(db, principal, analysis_id)
    route = find_route(analysis.result, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这条路线", "Route not found"))
    scale = Constraints.model_validate(analysis.constraints).scale
    # target_mg 没给时用规模档位对应的默认产量（data/scoring.toml 的 [scale]）。
    # 注意 Python 里 0 也算“假”，所以 target_mg=0 同样走默认值；只有负数会走到下面的 422
    amount = float(target_mg or ctx.reference.scoring["scale"][scale])
    if amount <= 0:
        raise HTTPException(422, i18n.tr("目标产量要大于 0", "The target amount must be greater than 0"))
    # M13 从目标产量倒推每个起始原料要多少：各步按 1:1 投料，有报道产率的按产率放大，没有的按 100%
    return m13_reports.bill_of_materials(analysis.result, route, amount)


# ---------------------------------------------------------------- 回填实验结果（G）


def _load_record(db: Session, principal: Principal, record_id: str) -> models.ExperimentRecord:
    """取本课题组的一条实验记录。

    只按课题组检查：同组的人都能看、能改别人记的实验结果。

    参数：
        db         数据库会话。
        principal  当前登录的人。
        record_id  实验记录的编号（experiment_records.id）。
    返回：
        models.ExperimentRecord。
    抛出：
        HTTPException 404 “没有这条实验记录”：编号不存在，或者是别的课题组的。
    谁在用：
        本文件的 update_experiment、add_attachment、list_attachments。
    """
    record = db.get(models.ExperimentRecord, record_id)
    if record is None or record.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这条实验记录", "Experiment record not found"))
    return record


@router.post("/analyses/{analysis_id}/routes/{route_id}/steps/{step_index}/experiments",
             status_code=status.HTTP_201_CREATED, tags=["回填"])
def record_experiment(analysis_id: str, route_id: str, step_index: int, body: schemas.ExperimentCreate,
                      principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """G4 保存：记录只存在本课题组的项目里；保存后路线树上这一步显示“已做 · 实际 78%”（R12）。

    需要登录，并且分析已经跑完。请求体见 `ExperimentCreate`。同一步可以记多次，每次都是一条新记录，
    路线树上显示最后保存的那一次。

    成功返回 201，是这条记录：

    - `id`、`analysis_id`、`route_id`、`step_index`、`step_key`、`step_name`
    - `outcome`、`outcome_label`（成功 / 部分成功 / 失败）
    - `actual_yield`、`condition_changes`、`notes`
    - `consent_for_training`：是否授权用于改进模型
    - `label`：路线树上显示的文字，如“已做 · 实际 78%”
    - `created_at`、`updated_at`

    出错：404 没有这个分析，或者没有这一步；409 分析还没评分或者还在进行中；
    422 请求体不对（比如产率超过 100）。
    """
    analysis = load_finished_analysis(db, principal, analysis_id)
    route = find_route(analysis.result, route_id)
    if route is None or not 1 <= step_index <= len(route["steps"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这一步", "Step not found"))
    # M14：在 experiment_records 表里加一行（内容加密；同时存下系统当时对这一步的评估，校准时拿来对比），
    # 并把这次结果挂到 analysis.result 里的那一步上，路线树和证据卡才会显示“已做”
    record = m14_backfill.save_record(
        db, analysis, route, step_index, principal.user_id,
        outcome=body.outcome, actual_yield=body.actual_yield, condition_changes=body.condition_changes,
        notes=body.notes, consent_for_training=body.consent_for_training,
    )
    # 一次提交，把新记录和改过的 result 一起写进数据库
    db.commit()
    return m14_backfill.record_view(record)


@router.get("/analyses/{analysis_id}/experiments", tags=["回填"])
def list_experiments(analysis_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """这次分析下记录过的全部实验结果，最新的在前。

    需要登录。返回一个数组，每项的字段同 `POST …/experiments` 的返回。出错：404 没有这个分析。
    """
    analysis = load_analysis(db, principal, analysis_id)
    records = db.scalars(
        select(models.ExperimentRecord)
        .where(models.ExperimentRecord.analysis_id == analysis.id)
        .order_by(models.ExperimentRecord.created_at.desc())
    )
    return [m14_backfill.record_view(record) for record in records]


@router.patch("/experiments/{record_id}", tags=["回填"])
def update_experiment(record_id: str, body: schemas.ExperimentUpdate, principal: Principal = Depends(get_principal),
                      db: Session = Depends(get_db)):
    """改记录，或者撤回 / 补上“授权用于改进模型”（G3，可随时撤回）。

    需要登录，同组的人都能改。请求体见 `ExperimentUpdate`：只带要改的字段。
    返回修改后的记录，字段同 `POST …/experiments` 的返回。路线树上这一步的“已做”标记会换成这条记录的内容。

    出错：404 没有这条实验记录；422 请求体不对。
    """
    record = _load_record(db, principal, record_id)
    # exclude_unset=True：只拿前端这次真正带上的字段，没带的字段不在字典里。
    # 这样才能区分“没带（不改）”和“带了 null（清空）”
    changes = body.model_dump(exclude_unset=True)
    if "consent_for_training" in changes and changes["consent_for_training"] is not None:
        record.consent_for_training = changes["consent_for_training"]
        # 记下授权状态最后一次改动的时间
        record.consent_updated_at = models.utcnow()
    if changes.get("outcome"):
        record.outcome = changes["outcome"]
    # 产率、条件变化、备注在加密的 payload 里：复制一份，改完再整个赋值回去
    payload = dict(record.payload)
    for field in ("actual_yield", "condition_changes", "notes"):
        if field in changes:
            # 带了 null：产率清空成 None，另外两项文字清空成空字符串
            payload[field] = changes[field] if changes[field] is not None or field == "actual_yield" else ""
    record.payload = payload
    # 结果或产率可能变了：把分析结果里这一步的“已做”标记换成这条记录的内容（会改写 analyses.result）
    analysis = db.get(models.Analysis, record.analysis_id)
    if analysis is not None and analysis.result:
        m14_backfill.apply_to_result(analysis, record)
    db.commit()
    return m14_backfill.record_view(record)


@router.post("/experiments/{record_id}/attachments", status_code=status.HTTP_201_CREATED, tags=["回填"])
async def add_attachment(record_id: str, file: UploadFile = File(...), principal: Principal = Depends(get_principal),
                         db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """G2：附上 TLC、NMR 照片或谱图（图片或 PDF）。文件加密保存。

    需要登录。请求是 `multipart/form-data`，文件放在 `file` 字段里。类型要是图片（`image/…`）或 PDF，
    按上传时声明的类型判断。一条实验记录可以传多个附件。

    成功返回 201：`{id, filename, content_type, size_bytes, download_url}`；`download_url` 是下载这个附件的地址。

    出错：404 没有这条实验记录；413 文件超过大小上限（默认 20 MB）；415 附件只支持图片和 PDF；
    422 文件是空的。
    """
    record = _load_record(db, principal, record_id)
    # 浏览器上传时会声明文件的类型；没声明的当作“未知的二进制”，下面的检查会把它拒掉
    content_type = file.content_type or "application/octet-stream"
    # str.startswith 可以接受一个元组：以其中任意一个开头就算符合
    if not content_type.startswith(ATTACHMENT_TYPES):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, i18n.tr("附件只支持图片和 PDF", "Attachments must be images or PDF files"))
    # 这个接口是 async def，因为读上传的文件要用 await；读的时候顺便检查大小（storage.read_upload）
    data = await read_upload(ctx, file)
    # safe_filename 去掉文件名里不能用的字符；save_file 把内容加密后写盘，表里只记随机的存储名
    attachment = models.ExperimentAttachment(
        record_id=record.id, org_id=record.org_id, filename=m13_reports.safe_filename(file.filename or "attachment"),
        content_type=content_type, storage_name=save_file(ctx, data), size_bytes=len(data),
    )
    db.add(attachment)
    db.commit()
    return {"id": attachment.id, "filename": attachment.filename, "content_type": content_type, "size_bytes": len(data),
            "download_url": f"/api/experiments/attachments/{attachment.id}"}


@router.get("/experiments/{record_id}/attachments", tags=["回填"])
def list_attachments(record_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """一条实验记录的全部附件。

    需要登录。返回一个数组，每项 `{id, filename, content_type, size_bytes, download_url}`。
    出错：404 没有这条实验记录。
    """
    record = _load_record(db, principal, record_id)
    attachments = db.scalars(select(models.ExperimentAttachment).where(models.ExperimentAttachment.record_id == record.id))
    return [
        {"id": a.id, "filename": a.filename, "content_type": a.content_type, "size_bytes": a.size_bytes,
         "download_url": f"/api/experiments/attachments/{a.id}"}
        for a in attachments
    ]


@router.get("/experiments/attachments/{attachment_id}", tags=["回填"])
def download_attachment(attachment_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
                        ctx: AppContext = Depends(get_ctx)):
    """下载一个实验附件。

    需要登录，同组的人都能下载。返回文件内容，`Content-Type` 是上传时声明的类型，
    文件名在响应头 `Content-Disposition` 里。

    出错：404 没有这个附件（别的课题组的附件也是 404），或者文件已经不在磁盘上了。
    """
    attachment = db.get(models.ExperimentAttachment, attachment_id)
    # 附件表里自己记着 org_id，所以不用先去查它属于哪条实验记录
    if attachment is None or attachment.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这个附件", "Attachment not found"))
    return download_response(load_file(ctx, attachment.storage_name), attachment.filename, attachment.content_type)
