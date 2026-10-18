"""实验室库存的接口（M1 + M4）。

对应按钮：
    E1  导入 Excel / CSV          POST /api/inventory/imports
    E2  确认匹配                  POST /api/inventory/imports/{id}/rows/{row_id}
    E3  手动添加                  POST /api/inventory/items
    E4  搜索（名称、CAS、子结构）   GET  /api/inventory/items?q=…&substructure=…
    E5  共享给团队                POST /api/inventory/share
    A8 下面的“已导入库存 N 种”     GET  /api/inventory/summary

另外三个接口没有单独的按钮编号，是库存表（R10）上的操作：
    修改一条库存                     PATCH  /api/inventory/items/{id}
    删除一条库存                     DELETE /api/inventory/items/{id}
    查看一次导入的进度和三栏结果      GET    /api/inventory/imports/{id}
本文件的接口都要登录。

库存是什么：课题组手头有的试剂清单。分析的时候，库存里有的原料算“库存”（最好拿的一档）；
逆合成搜索碰到库存里的分子就不再往下拆（pipeline.py 的第 2、3 阶段）。

库存怎么存（inventory_items 表，见 syntherisk/models.py）：
    payload          名称、CAS 号、SMILES、数量、位置、备注，整个加密后存（EncryptedJSON 列）。
                     要改它，得把整个字典重新赋值；原地改里面的某一项不会被保存。
    structure_index  结构的“盲索引”：用密钥对“课题组编号 + 结构的 key14”算出来的一串哈希（64 个十六进制字符）。
                     从数据库里看不出是什么结构，但同一个课题组里同一个结构算出来总是同一串，
                     所以不用解密就能按结构查重、匹配。没有结构的条目（比如混合物）这一列是空的。
    visibility       可见范围：team 全组可见 / private 只有自己可见。
    owner_id         是谁加的。
    source           怎么来的：import 导入 / manual 手动添加 / marked_owned 结果页上“标记为我有”。
因为内容是加密的，按名称搜索、子结构搜索和分页，都是把看得见的库存全部读出来以后在内存里做的。

谁看得见（_own_item 和 m04_availability.InventoryService._visible 是同一条规则）：
    本课题组里可见范围是 team 的条目，加上自己名下的条目（不管是 team 还是 private）。
    看得见的就能改、能删，不要求是自己加的。

它依赖谁：
    api/deps.py、api/schemas.py
    inventory_import.py           读表格、逐行识别、三栏结果（E1、E2 的主要逻辑都在那里）
    modules/m04_availability.py   InventoryService：库存的查、增、改
    modules/m01_structure.py      规范 SMILES（本文件里简写成 chem）
    modules/m01_resolver.py       名称或 CAS 号 → 结构，通过 ctx.resolver 使用
    storage.py                    read_upload：读上传的文件，顺便限制大小
谁调用它：前端。syntherisk/main.py 把本文件的 router 挂到 /api 下。
读写的表：inventory_items、inventory_imports、inventory_import_rows；另外只读 organizations
（新条目默认的可见范围）。手动添加和导入在识别名称、CAS 号时可能联网查 PubChem、OPSIN，取决于配置。

本文件里有什么：
    _own_item            取一条当前登录者看得见的库存
    _own_import          取本课题组的一次导入
    inventory_summary    GET    /api/inventory/summary
    list_items           GET    /api/inventory/items                        E4
    add_item             POST   /api/inventory/items                        E3
    update_item          PATCH  /api/inventory/items/{id}
    delete_item          DELETE /api/inventory/items/{id}
    share_inventory      POST   /api/inventory/share                        E5
    import_inventory     POST   /api/inventory/imports                      E1
    get_import           GET    /api/inventory/imports/{id}
    resolve_import_row   POST   /api/inventory/imports/{id}/rows/{row_id}   E2
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from syntherisk import i18n, inventory_import, models
from syntherisk.api import schemas
from syntherisk.api.deps import Principal, get_ctx, get_db, get_principal
from syntherisk.context import AppContext
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m04_availability import InventoryService
from syntherisk.storage import read_upload

# 本文件的所有接口都登记在这个 router 上，在接口文档页面（/docs）上归到“库存”一组
router = APIRouter(tags=["库存"])


def _own_item(db: Session, principal: Principal, item_id: str) -> models.InventoryItem:
    """取一条当前登录者看得见的库存。

    看得见 = 属于本课题组，并且（可见范围是 team，或者是他自己加的）。

    参数：
        db         数据库会话。
        principal  当前登录的人。
        item_id    库存条目的编号（inventory_items.id）。
    返回：
        models.InventoryItem。
    抛出：
        HTTPException 404 “库存里没有这一条”：编号不存在、是别的课题组的，
        或者是同组其他人设成只有自己可见的。
    谁在用：
        本文件的 update_item、delete_item。
    """
    item = db.get(models.InventoryItem, item_id)
    # and 后面的括号里是“全组可见，或者是我自己的”；item 是 None 时 and 直接短路，不会去读它的属性
    visible = item is not None and item.org_id == principal.org_id and (
        item.visibility == "team" or item.owner_id == principal.user_id
    )
    if not visible:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("库存里没有这一条", "Inventory item not found"))
    return item


def _own_import(db: Session, principal: Principal, import_id: str) -> models.InventoryImport:
    """取本课题组的一次导入记录。

    只按课题组检查：同组的人都能查看、处理别人上传的那一次导入。

    参数：
        db         数据库会话。
        principal  当前登录的人。
        import_id  导入记录的编号（inventory_imports.id）。
    返回：
        models.InventoryImport。
    抛出：
        HTTPException 404 “没有这次导入”：编号不存在，或者是别的课题组的。
    谁在用：
        本文件的 get_import、resolve_import_row。
    """
    record = db.get(models.InventoryImport, import_id)
    if record is None or record.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这次导入", "Import not found"))
    return record


@router.get("/inventory/summary")
def inventory_summary(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """库存里一共有多少种（新建分析页上的“已导入库存 N 种”用）。

    需要登录。返回 `{"count": N}`：当前登录者看得见的库存条数，
    也就是全组可见的条目，加上他自己设成只有自己可见的条目。
    """
    # InventoryService.count() 是把看得见的条目全部读出来再数个数
    return {"count": InventoryService(db, principal.org_id, principal.user_id).count()}


@router.get("/inventory/items")
def list_items(q: str | None = None, substructure: str | None = None, svg: bool = False,
               limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
               principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """R10 库存表。按更新时间排序；q 按名称或 CAS 号搜，substructure 按子结构搜。

    E4。需要登录。最近更新的排在前面，只列出当前登录者看得见的条目。

    - 查询参数 `q`：按名称或 CAS 号搜，包含这段文字就算匹配（不分大小写，忽略空格、连字符、括号）
    - 查询参数 `substructure`：按子结构搜，给一个 SMILES 或 SMARTS。和 `q` 同时给时，两个条件都要满足
    - 查询参数 `svg`：是否给每条附上结构图，默认不附
    - 查询参数 `limit`、`offset`：分页。`limit` 是这一页最多几条（1–500，默认 100），
      `offset` 是跳过前面几条（默认 0）

    返回：

    - `total`：符合条件的总条数（不受分页影响）
    - `items`：这一页的库存。每条
      `{id, name, cas, smiles, quantity, location, note, source, visibility, updated_at}`，要了结构图的
      再加 `svg`。`source` 是来源：`import` 导入 / `manual` 手动添加 / `marked_owned` 标记为我有

    出错：422 子结构读不出来。
    """
    inventory = InventoryService(db, principal.org_id, principal.user_id)
    try:
        # 有搜索条件就搜，没有就全部列出。两种都是先把看得见的库存全读出来：
        # 内容是加密的，没法让数据库去筛，只能读出来以后在内存里比
        items = inventory.search(q, substructure) if (q or substructure) else inventory.list_items()
    except ValueError as exc:
        # InventoryService.search 在子结构读不出来时抛 ValueError；这里转成 422，把它的说明原样给前端
        raise HTTPException(422, str(exc)) from exc
    # 分页也是在内存里做：对整个列表切片
    page = items[offset : offset + limit]
    return {"total": len(items), "items": [InventoryService.to_dict(item, with_svg=svg) for item in page]}


@router.post("/inventory/items", status_code=status.HTTP_201_CREATED)
def add_item(body: schemas.InventoryItemCreate, principal: Principal = Depends(get_principal),
             db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """E3 手动添加：和新建分析一样，名称、CAS 号、SMILES 任给一个都行。认不出结构也可以先记下来。

    需要登录。请求体见 `InventoryItemCreate`。后端按 SMILES → CAS 号 → 名称 的顺序去认这是什么物质
    （先查内置试剂表，必要时联网查 PubChem、OPSIN）。库存里已经有同一个结构时不新建，
    只更新那一条的数量、位置、备注。

    成功返回 201：

    - `item`：库存里的这一条，字段同库存表（`GET /api/inventory/items`）里的一条
    - `created`：`true` 是新加的，`false` 是更新了已有的
    - `match`：系统是怎么认出来的。`status`：`matched` 认准了 / `needs_confirmation` 是按别名、
      去掉规格说明等办法猜的（照样入库）/ `unrecognized` 没认出来（这一条没有结构）；
      另有 `name`、`cas`、`smiles`、`source`（靠什么认出来的）、`reason`、`notes`

    出错：422 给了 SMILES 但读不出来，或者名称、CAS 号、SMILES 一个都没给。
    """
    # 认结构（m01_resolver.NameResolver.resolve_row）：SMILES 读得出来就直接用；
    # 否则拿 CAS 号、名称去内置试剂表、PubChem、OPSIN 找。这一步可能联网。
    # body.name or ""：没填的字段是 None，这里换成空字符串再传
    resolution = ctx.resolver.resolve_row(name=body.name or "", cas=body.cas or "", smiles=body.smiles or "")
    # 用户明确给了 SMILES，结果却不是靠 SMILES 认出来的，说明这个 SMILES 读不出来：直接报错，不拿名称去猜
    if body.smiles and resolution.source != "SMILES":
        raise HTTPException(422, i18n.tr("SMILES 读不出来", "The SMILES could not be read"))
    inventory = InventoryService(db, principal.org_id, principal.user_id)
    # upsert：库存里已经有同一个结构就更新那一条，没有才新建；返回 (条目, 是不是新建的)
    item, created = inventory.upsert(
        # 名称的先后：用户填的 → 认出来的名字 → CAS 号 → “未命名”
        name=body.name or resolution.name or body.cas or i18n.tr("未命名", "Unnamed"),
        cas=resolution.cas or body.cas,
        # 没认出来时 resolution.smiles 是 None：照样入库，只是这一条没有结构，参与不了按结构的匹配
        smiles=resolution.smiles,
        quantity=body.quantity,
        location=body.location,
        note=body.note,
        source="manual",
    )
    db.commit()
    return {"item": InventoryService.to_dict(item), "created": created, "match": resolution.to_dict()}


@router.patch("/inventory/items/{item_id}")
def update_item(item_id: str, body: schemas.InventoryItemUpdate, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)):
    """修改一条库存的名称、数量、位置、备注或结构。

    需要登录。能改的是全组可见的库存和自己名下的库存。请求体见 `InventoryItemUpdate`，只带要改的字段。
    返回修改后的这一条，字段同库存表里的一条。

    出错：404 库存里没有这一条（别人设成只有自己可见的也是 404）；422 SMILES 读不出来。
    """
    item = _own_item(db, principal, item_id)
    # payload 是加密的 JSON 列：先复制成一个新字典，改完再整个赋值回去；原地改不会被保存
    payload = dict(item.payload)
    for field in ("name", "quantity", "location", "note"):
        # getattr(body, "name") 等于 body.name。值是 None 表示这次不改这一项
        value = getattr(body, field)
        if value is not None:
            payload[field] = value
    if body.smiles is not None:
        canonical = chem.canonical_smiles(body.smiles)
        if canonical is None:
            raise HTTPException(422, i18n.tr("SMILES 读不出来", "The SMILES could not be read"))
        payload["smiles"] = canonical
        # 结构变了，盲索引也要跟着重算，否则以后按结构匹配会对不上
        item.structure_index = InventoryService(db, principal.org_id, principal.user_id).structure_index(canonical)
    item.payload = payload
    db.commit()
    return InventoryService.to_dict(item)


@router.delete("/inventory/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """删除一条库存。

    需要登录。能删的范围和能改的一样：全组可见的库存和自己名下的库存。
    成功返回 204，没有内容。出错：404 库存里没有这一条。
    """
    db.delete(_own_item(db, principal, item_id))
    db.commit()


@router.post("/inventory/share")
def share_inventory(body: schemas.InventoryShare, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """E5：把自己加的库存设成全组可见（team）或只有自己可见（private）。只改权限，不动数据。

    需要登录。请求体见 `InventoryShare`。作用于当前登录者名下的全部库存，不能只改其中几条。
    返回 `{"updated": 处理了几条, "visibility": 现在的可见范围}`。
    """
    # 找出 owner_id 是自己的全部条目（不管现在是 team 还是 private），逐条改 visibility。
    # .all() 把结果一次取成列表，下面既要循环又要数个数
    items = db.scalars(
        select(models.InventoryItem).where(
            models.InventoryItem.org_id == principal.org_id, models.InventoryItem.owner_id == principal.user_id
        )
    ).all()
    # 逐条改可见范围；条目的内容（payload）不动
    for item in items:
        item.visibility = body.visibility
    db.commit()
    # updated 是处理的条数：自己名下有几条就是几（原来就是这个可见范围的也算在内）
    return {"updated": len(items), "visibility": body.visibility}


# ---------------------------------------------------------------- 导入


@router.post("/inventory/imports", status_code=status.HTTP_201_CREATED)
async def import_inventory(file: UploadFile = File(...), principal: Principal = Depends(get_principal),
                           db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """E1：小表格当场处理完；大表格放到后台，用 GET /api/inventory/imports/{id} 看进度。

    需要登录。请求是 `multipart/form-data`，文件放在 `file` 字段里。支持 `.xlsx`、`.xlsm`（读第一个工作表）
    和 `.csv`、`.tsv`、`.txt`，按文件的扩展名判断。表头里要有名称、CAS、SMILES 至少一列；一次最多 5000 行。
    不超过 200 行的表格在这次请求里就处理完；更大的先返回 `status` 是 `queued` 的记录，由后台处理。

    成功返回 201，是这次导入的记录：

    - `id`、`filename`
    - `status`：`queued` 排队 / `running` 处理中 / `done` 完成 / `failed` 失败；`error` 是失败原因
    - `total_rows`、`processed_rows`：一共多少行、已经处理了多少行
    - `counts`：各栏的行数 `{matched, needs_confirmation, unrecognized, ignored, pending}`，
      依次是已匹配、需确认、未识别、已忽略、还没处理
    - `rows`：需确认和未识别的行的明细。每行 `{id, row_number, bucket, raw, proposal, reason, item_id}`：
      `raw` 是表格里的原始内容，`proposal` 是系统猜的结构 `{name, cas, smiles, source}`（没猜出来是
      `null`），`reason` 是为什么要人确认

    已匹配的行直接进了库存。出错：413 文件超过大小上限（默认 20 MB）；422 文件是空的、格式不支持、
    读不了、找不到表头、没有数据行，或者超过 5000 行。
    """
    # 这个接口是 async def，因为读上传的文件要用 await；读的时候顺便检查大小（storage.read_upload）
    data = await read_upload(ctx, file)
    # 上传时没带文件名的，按 CSV 处理（表格的类型是看文件名的扩展名定的）
    filename = file.filename or "inventory.csv"

    def run() -> dict:
        """真正的导入，在线程池里跑：读表格 → 建导入记录 → 行数少就当场识别，行数多就交给任务队列。

        返回：inventory_import.import_view 的字典（这次导入的记录）。
        抛出：HTTPException 422，表格读不了或者里面找不到能用的列。
        副作用：写 inventory_imports、inventory_import_rows 表；行数少时还会写 inventory_items，
                并且可能联网查 PubChem、OPSIN。
        """
        try:
            # 读表格、认表头，得到每一行的 {name, cas, smiles, quantity, location, note, row_number}
            rows = inventory_import.parse_table(filename, data)
        except inventory_import.TableFormatError as exc:
            raise HTTPException(422, str(exc)) from exc
        # 建一条导入记录，再给每一行建一条记录（这时每一行都在 pending 一栏）。
        # 先提交：行数多时后台任务要能读到它们
        record = inventory_import.create_import(db, principal.org_id, principal.user_id, filename, rows)
        db.commit()
        if len(rows) <= inventory_import.SYNC_ROW_LIMIT:
            # 不超过 SYNC_ROW_LIMIT（200）行：当场逐行识别，认准了的直接进库存
            inventory_import.process_import(ctx, db, record)
        else:
            # 大表格：交给任务队列，后台由 inventory_import.run_import 处理；前端轮询进度
            ctx.queue.enqueue("inventory.import", {"import_id": record.id})
            db.refresh(record)
        return inventory_import.import_view(db, record)

    # 识别结构可能要查 PubChem，放到线程池里，不堵住其他请求
    return await run_in_threadpool(run)


@router.get("/inventory/imports/{import_id}")
def get_import(import_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """R10：已匹配 / 需确认 / 未识别三栏的数量，以及后两栏的明细。

    需要登录。同组的人都能看。大表格导入时轮询这个接口看进度（`status`、`processed_rows`）。
    返回的字段同 `POST /api/inventory/imports`。出错：404 没有这次导入。
    """
    return inventory_import.import_view(db, _own_import(db, principal, import_id))


@router.post("/inventory/imports/{import_id}/rows/{row_id}")
def resolve_import_row(import_id: str, row_id: str, body: schemas.ImportRowAction,
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """E2：处理一条不确定的行。

    需要登录。请求体见 `ImportRowAction`：`confirm` 接受系统猜的结构并记进库存（没有结构的混合物也可以记）；
    `set_structure` 用自己给的结构记进库存；`ignore` 不要这一行。
    只能处理“需确认”和“未识别”两栏里的行。

    返回：

    - `row`：处理后的这一行。`bucket` 变成 `confirmed` 或 `ignored`；`item_id` 是进了库存的那一条的编号
    - `import`：整次导入最新的计数和剩下的明细，字段同 `GET /api/inventory/imports/{id}`

    出错：404 没有这次导入，或者这次导入里没有这一行；409 这一行已经处理过了；422 给的结构读不出来。
    """
    record = _own_import(db, principal, import_id)
    row = db.get(models.InventoryImportRow, row_id)
    # 这一行必须属于网址里的那一次导入，不能拿别的导入的行编号来凑
    if row is None or row.import_id != record.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("这次导入里没有这一行", "This import has no such row"))
    # bucket 是这一行所在的栏。只有“需确认”“未识别”两栏的行可以处理；
    # 已匹配、已确认、已忽略的（以及还在排队、没处理到的）都返回 409
    if row.bucket not in ("needs_confirmation", "unrecognized"):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("这一行已经处理过了", "This row has already been handled"))
    try:
        # confirm_row 按 action 处理：记进库存（或者只标成忽略），并更新这一行的 bucket 和 item_id
        inventory_import.confirm_row(db, row, principal.user_id, action=body.action, smiles=body.smiles, name=body.name)
    except ValueError as exc:
        # 给的结构读不出来时 confirm_row 抛 ValueError
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return {"row": inventory_import.row_view(row), "import": inventory_import.import_view(db, record)}
