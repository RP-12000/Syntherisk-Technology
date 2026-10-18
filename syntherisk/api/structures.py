"""M1 结构工具和 M2 约束解析的接口。

对应按钮：
    A1  画结构（编辑器导出 MOL 或 SMILES，边画边校验）  POST /api/structures/validate
    A2  粘贴 SMILES                                    POST /api/structures/validate
    A3  名称或 CAS 号                                  GET  /api/structures/lookup
    A4  上传 MOL / SDF 文件                            POST /api/structures/upload
    A5  指定起始骨架（校验结构，随约束一起提交）          POST /api/structures/validate
    A14 一句话补充要求                                 POST /api/constraints/parse
    C2  相似先例的结构差异                              POST /api/structures/diff
    C4  原料标签（库存、在售、价格）                     GET  /api/materials/availability

另有一个没有对应按钮编号的接口：GET /api/structures/depict，把一个 SMILES 画成 SVG 图片。
本文件的接口都要登录。

这个文件只是薄薄的一层：收请求、调模块、返回结果。真正干活的代码在：
    modules/m01_structure.py     解析和校验结构、画图、比较结构（RDKit）。本文件里简写成 chem
    modules/m01_resolver.py      名称或 CAS 号 → 结构：内置试剂表 → PubChem → OPSIN。通过 ctx.resolver 使用
    modules/m02_constraints.py   一句话要求 → 约束标签：配了大模型用大模型，没配用关键词规则
    modules/m04_availability.py  库存（InventoryService）和可得性判断（AvailabilityChecker）
    storage.py                   read_upload：读上传的文件，顺便限制大小
谁调用它：前端。syntherisk/main.py 把本文件的 router 挂到 /api 下。

数据从哪来到哪去：
    这些接口都不写业务数据，结果当场算出来返回。
    /materials/availability 读 inventory_items（本课题组的库存）和 catalog_compounds（在售目录）两张表。
    /structures/lookup 可能联网查 PubChem、OPSIN；/constraints/parse 配了大模型时会调大模型，
    并且由大模型调用层在 llm_calls 表里记一条用量。联不联网取决于 .env 里的配置。

本文件里有什么：
    _with_name             给结构校验结果补上内置试剂表里的名字
    validate_structure     POST /api/structures/validate      A1、A2、A5
    upload_structure       POST /api/structures/upload        A4
    lookup_structure       GET  /api/structures/lookup        A3
    depict_structure       GET  /api/structures/depict
    diff_structures        POST /api/structures/diff          C2
    parse_requirement      POST /api/constraints/parse        A14
    material_availability  GET  /api/materials/availability   C4
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from syntherisk import i18n
from syntherisk.api import schemas
from syntherisk.api.deps import Principal, get_ctx, get_db, get_principal
from syntherisk.context import AppContext
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m02_constraints import ConstraintParser
from syntherisk.modules.m04_availability import AvailabilityChecker, InventoryService
from syntherisk.storage import read_upload

# 本文件的接口登记在这个 router 上；每个接口的 tags 决定它在接口文档页面上归到“结构”“约束”还是“库存”一组
router = APIRouter()


def _with_name(info: chem.StructureInfo, ctx: AppContext) -> dict:
    """把结构校验的结果变成字典，并补上内置试剂表里的名字。

    参数：
        info  chem.validate_structure() 的结果（StructureInfo）。
        ctx   运行时上下文，用到 ctx.reference.compounds（从 data/compounds.csv 读进来的内置试剂表）。
    返回：
        字典，StructureInfo 的全部字段加一个 name：
            valid             结构是否合法。False 时原因在 errors 里，下面的结构信息是 None 或默认值
            input_format      实际按哪种格式读的：smiles / mol / sdf
            smiles            规范 SMILES（同一个分子不管怎么写，规范以后都一样）
            inchikey、key14   InChIKey 和它的前 14 位。后端到处用 key14 当“这个分子”的编号
            formula、mol_weight   分子式、分子量
            heavy_atoms       重原子（氢以外的原子）个数
            components        有几个不相连的部分；盐或混合物会大于 1
            stereocenters、unspecified_stereocenters   立体中心的个数、其中没有指定构型的个数
            formal_charge     净电荷
            messages、warnings、errors   提示、警告、错误，都是中文句子的列表
            svg               结构图（SVG 文本）
            name              内置试剂表里的名字；表里没有、或者结构不合法时是 None
    谁在用：
        本文件的 validate_structure、upload_structure。
    """
    data = info.to_dict()
    # 结构不合法时 info.smiles 是 None，没法查名字
    data["name"] = ctx.reference.compounds.name_for(info.smiles) if info.valid else None
    return data


@router.post("/structures/validate", tags=["结构"])
def validate_structure(body: schemas.StructureInput, ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """R1 结构预览和校验：四种输入方式最后都走这里，有问题当场提示。

    A1 画结构、A2 粘贴 SMILES、A5 起始骨架调这个接口；A4 上传文件调 `/api/structures/upload`，
    做的是同一种校验，返回的内容也相同。需要登录。请求体见 `StructureInput`。

    结构合不合法都返回 200，要看 `valid` 字段：

    - `valid`：结构是否合法。`false` 时原因在 `errors` 里，下面的结构信息是 `null` 或默认值
    - `input_format`：实际按哪种格式读的（`smiles` / `mol` / `sdf`）
    - `smiles`：规范 SMILES，后面提交分析时用它
    - `inchikey`、`key14`：InChIKey 和它的前 14 位
    - `formula`、`mol_weight`、`heavy_atoms`：分子式、分子量、重原子个数
    - `components`：有几个不相连的部分（盐或混合物会大于 1）
    - `stereocenters`、`unspecified_stereocenters`：立体中心的个数、其中没有指定构型的个数
    - `formal_charge`：净电荷
    - `messages`、`warnings`、`errors`：提示、警告、错误，都是中文句子的数组，可以直接显示
    - `svg`：结构图（SVG 文本）
    - `name`：内置试剂表里的名字，没有就是 `null`

    出错：401 没登录；422 请求体格式不对（比如 `input` 超过 20 万个字符）。
    """
    # 这里的 chem.validate_structure 是 modules/m01_structure.py 里的函数，和本接口函数同名但不是同一个。
    # 它用 RDKit 解析并检查结构，不查数据库、不联网
    return _with_name(chem.validate_structure(body.input, body.format), ctx)


@router.post("/structures/upload", tags=["结构"])
async def upload_structure(file: UploadFile = File(...), ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """A4 上传一个 MOL 或 SDF 文件，返回和 `/api/structures/validate` 一样的校验结果。

    需要登录。请求是 `multipart/form-data`，文件放在 `file` 字段里。格式按文件内容自动判断；
    文件里有多个结构时只读第一个，`warnings` 里会说明。

    出错：401 没登录；413 文件超过大小上限（默认 20 MB）；422 文件是空的。
    """
    # File(...) 里的三个点表示这个字段必填。这个接口是 async def，因为读上传的文件要用 await。
    # read_upload 读的时候顺便检查大小：超过 MAX_UPLOAD_MB 报 413，空文件报 422（见 storage.py）
    data = await read_upload(ctx, file)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        # 不是 UTF-8 的文件改用 latin-1 解码：latin-1 能解开任何字节，不会再报错。
        # MOL 文件里要紧的内容（原子、键）都是 ASCII 字符，两种解码出来是一样的
        text = data.decode("latin-1")
    return _with_name(chem.validate_structure(text, "auto"), ctx)


@router.get("/structures/lookup", tags=["结构"])
def lookup_structure(q: str = Query(min_length=1, max_length=200), limit: int = Query(5, ge=1, le=10),
                     ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """按名称或 CAS 号查候选结构：先查内置试剂表，再查 PubChem。

    A3。需要登录。前两处都没查到、输入全是英文字符又不是 CAS 号时，再请 OPSIN 按系统命名解析。

    - 查询参数 `q`：名称（中文、英文、别名都行）或 CAS 号，1–200 个字符
    - 查询参数 `limit`：最多返回几个候选，1–10，默认 5

    返回：

    - `query`：原样返回查询的文字
    - `candidates`：候选结构的数组。每项：`name`、`name_en`、`cas`、`smiles`、`key14`、`formula`、
      `mol_weight`、`source`（从哪查到的：`内置试剂表` / `PubChem` / `OPSIN`）、`cid`（PubChem 编号）、
      `svg`（结构图）。查不到的项是 `null`
    - `warnings`：提示的数组，比如“PubChem 暂时连不上，这次没有查它，结果可能不全”

    没查到时 `candidates` 是空数组，不算出错。出错：401 没登录；422 `q` 是空的或太长，或者 `limit` 超出范围。
    """
    # Query(...) 声明这是网址里 ? 后面的查询参数，并写明限制；不符合时 FastAPI 直接返回 422。
    # ctx.resolver 是 m01_resolver.NameResolver：返回 (候选列表, 提示列表)。
    # PubChem、OPSIN 连不上时不报错，只在提示里说明，这次只给本地查到的结果
    candidates, warnings = ctx.resolver.lookup(q, limit)
    return {"query": q, "candidates": [c.to_dict() for c in candidates], "warnings": warnings}


@router.get("/structures/depict", tags=["结构"])
def depict_structure(smiles: str, width: int = Query(320, ge=60, le=1200), height: int = Query(220, ge=60, le=1200),
                     _: Principal = Depends(get_principal)):
    """把一个 SMILES 画成结构图。

    需要登录。返回的不是 JSON，而是一张 SVG 图片（`Content-Type: image/svg+xml`）。

    - 查询参数 `smiles`：要画的结构
    - 查询参数 `width`、`height`：图片的宽和高（像素），各 60–1200，默认 320 × 220

    出错：401 没登录；422 结构读不出来，或者宽高超出范围。
    """
    # chem.mol_to_svg 用 RDKit 画图，返回 SVG 文本；SMILES 读不出来时返回 None
    svg = chem.mol_to_svg(smiles, width, height)
    if svg is None:
        raise HTTPException(422, i18n.tr("结构读不出来", "The structure could not be read"))
    # 用 Response 直接返回文本并指明类型；不这样写的话 FastAPI 会把字符串当成 JSON 返回
    return Response(content=svg, media_type="image/svg+xml")


@router.post("/structures/diff", tags=["结构"])
def diff_structures(body: schemas.StructureDiffRequest, _: Principal = Depends(get_principal)):
    """两个结构的公共部分和各自多出来的原子。

    C2 相似先例的结构差异。需要登录。请求体见 `StructureDiffRequest`（两个 SMILES）。返回：

    - `similarity`：两个结构的相似度，0–1
    - `identical`：是不是同一个分子（不区分立体异构）
    - `common_atoms`：最大公共部分有几个原子
    - `query_only_atoms`、`reference_only_atoms`：各自多出来的原子的序号（从 0 数起）
    - `query_only_fragments`、`reference_only_fragments`：各自多出来的部分，写成 SMILES 片段的数组
    - `query_svg`、`reference_svg`：两张结构图，多出来的原子已经高亮

    出错：401 没登录；422 有一个结构读不出来。
    """
    # chem.structure_diff 用 RDKit 找两个分子的最大公共子结构；有一个 SMILES 读不出来时返回 None
    result = chem.structure_diff(body.query, body.reference)
    if result is None:
        raise HTTPException(422, i18n.tr("有一个结构读不出来", "One of the structures could not be read"))
    return result


@router.post("/constraints/parse", tags=["约束"])
def parse_requirement(body: schemas.RequirementParseRequest, ctx: AppContext = Depends(get_ctx),
                      principal: Principal = Depends(get_principal)):
    """R2：把一句话要求变成约束标签。返回的标签要用户确认后，放进提交分析时的 constraints.requirements。

    A14。需要登录。请求体见 `RequirementParseRequest`。这个接口只解析，不保存任何约束。返回：

    - `tags`：解析出来的约束标签。每项：`kind`（约束的类型）、`value`（词表里的编号、物质名或步数）、
      `hardness`（`hard` 硬约束 / `soft` 软偏好）、`label`（显示的文字）、`source_text`（对应的原话）、
      `smiles`（`kind` 是 `exclude_substance` 时那个物质的结构，其他类型是 `null`）
    - `unsupported`：系统执行不了的要求，原话的数组，应当如实告诉用户
    - `generated_by`：`llm` 是大模型解析的，`rules` 是关键词规则解析的
    - `note`：补充说明，比如“未配置大模型，用关键词规则解析”
    - `provider`、`model`：用的是哪家的哪个模型（只有 `generated_by` 是 `llm` 时才有这两项）

    出错：401 没登录；422 `text` 超过 1000 个字符。
    """
    # ConstraintParser（M2）要三样东西：ctx.reference 里有约束词表（data/vocabulary.toml），标签只能从词表里选；
    # ctx.llm 是大模型调用层，没配大模型时解析器自动改用关键词规则；
    # ctx.resolver 用来把“氯仿”这样点名的具体物质变成结构（可能联网查 PubChem）
    parser = ConstraintParser(ctx.reference, ctx.llm, ctx.resolver)
    # org_id 只用来在 llm_calls 表里记下这次大模型调用是哪个课题组的
    return parser.parse(body.text, org_id=principal.org_id)


@router.get("/materials/availability", tags=["库存"])
def material_availability(smiles: str, ctx: AppContext = Depends(get_ctx), principal: Principal = Depends(get_principal),
                          db: Session = Depends(get_db)):
    """R6 原料浮层：库存里有没有、目录里有没有在售。价格和货期第一版显示“待询价”。

    C4。需要登录。

    - 查询参数 `smiles`：要查的物质

    返回：

    - `smiles`：规范 SMILES
    - `name`：内置试剂表里的名字，没有就是 `null`
    - `status`：`inventory` 库存里有 / `purchasable` 可购买 / `unavailable` 买不到
    - `label`：`status` 的中文（库存 / 可购买 / 买不到）
    - `inventory`：库存里的那一条 `{item_id, name, quantity, location}`；库存里没有是 `null`
    - `purchasable`：能不能买到
    - `catalog_source`：在哪个在售目录里查到的（如 `seed` 内置试剂表、`zinc`）；没查到是 `null`
    - `ubiquitous`：是不是随手可得、不用准备的物质（内置试剂表里现在只有水）
    - `suppliers`、`price`、`lead_time`：供应商、价格、货期，第一版都是 `null`
    - `price_label`：固定是“待询价”

    出错：401 没登录；422 结构读不出来。
    """
    # chem.mol_record 解析 SMILES，给出规范 SMILES、key14 等；读不出来返回 None
    record = chem.mol_record(smiles)
    if record is None:
        raise HTTPException(422, i18n.tr("结构读不出来", "The structure could not be read"))
    # 第一步：在本课题组的库存里按结构找（只看当前登录者看得见的条目）。
    # 库存内容是加密的，这里靠“盲索引”匹配，不用把整张表解密（见 m04_availability.InventoryService）
    inventory = InventoryService(db, principal.org_id, principal.user_id)
    item = inventory.find_by_structure(record.smiles)
    # 第二步：AvailabilityChecker 要一份 {key14: 库存条目} 的索引。这里只查一个分子，所以索引里最多一项
    index = {record.key14: InventoryService.to_dict(item)} if item else {}
    # check() 先看库存索引，再查在售目录（catalog_compounds 表），给出 status 等字段
    availability = AvailabilityChecker(db, ctx.reference, index).check(record.smiles)
    # **availability 把可得性的各项展开，和 smiles、name 合成一个字典
    return {"smiles": record.smiles, "name": ctx.reference.compounds.name_for(record.smiles), **availability}
