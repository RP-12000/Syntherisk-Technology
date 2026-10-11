"""M4 可得性与库存：每个原料是库存里有、能买到，还是买不到。

库存按组织隔离，内容加密；按结构匹配靠“盲索引”，不用解密整张表。
价格和货期第一版没有数据，一律显示“待询价”。

对应 PPT 里的模块 M4。这个文件有两个类：
    InventoryService     管一个课题组（组织）的库存：列出、按结构查、搜索、添加和去重。
    AvailabilityChecker  拿着库存和在售目录，回答“这个分子从哪来”：库存 / 可购买 / 买不到。

界面上哪些地方用到它（接口都在 syntherisk/api/ 下）：
    A8  原料来源三档      这里只给出每个原料的状态；按档位怎么打分、怎么排除路线，是 M10 的事
    A9  已导入库存 N 种   GET  /api/inventory/summary         -> InventoryService.count
    B12 标记为我有        POST /api/analyses/{id}/mark-owned  -> InventoryService.upsert，再重算这个分子的状态
    C4  原料标签浮层      GET  /api/materials/availability    -> find_by_structure + check
    E1 E2 导入、确认匹配  syntherisk/inventory_import.py      -> InventoryService.upsert
    E3  手动添加          POST /api/inventory/items           -> InventoryService.upsert
    E4  搜索              GET  /api/inventory/items           -> InventoryService.search / list_items
    E5  共享给团队        POST /api/inventory/share           -> 接口直接改 visibility 列；_visible() 按它过滤

在一次分析里的位置（syntherisk/pipeline.py 的 AnalysisRunner）：
    第 2 阶段  _search 用 list_items() 把库存里每样东西的 InChIKey 交给逆合成引擎：搜到库存里有的就不再往下拆。
    第 3 阶段  _availability 用 index_by_key14() 建库存索引，再对结果里的每个分子调 AvailabilityChecker.check()，
               结果写进 result["molecules"][key14]["availability"]。
    第 4 阶段  从先例借来的试剂是新出现的分子，_adopt_precedent_reagents 对它们再调一次 check()。

数据从哪来、到哪去：
    读写  inventory_items 表（models.InventoryItem）。名称、CAS、SMILES、数量、位置、备注都在加密的 payload 列里；
          structure_index 列是结构的盲索引。
    只读  catalog_compounds 表（models.CatalogCompound，在售目录）。服务启动时 db.init_db 把内置试剂表里
          能买到的物质写进去（source 是 seed）；python -m syntherisk.cli load-catalog 可以再导入别的目录（比如 zinc）。
    只读  organizations 表的 settings 列里的 inventory_default_visibility（新库存默认给谁看）。
    只读  参考数据 ReferenceData.compounds（data/compounds.csv）：roles 里带 ubiquitous 的物质算“随手可得”，
          现在表里只有水是这样。
    不联网，不调大模型。这个文件里的代码都不提交事务（不调 commit），由调用它的接口或流水线提交。

贯穿全文件的三个概念：
    key14     InChIKey 的前 14 位（m01_structure.key14_of）。只看原子怎么连，不分立体异构，
              所以库存里同一个分子的不同立体异构体算同一样东西。
    盲索引    HMAC-SHA256(密钥, "组织编号:key14")，64 个十六进制字符。同一个组织里同一个结构算出来的值永远一样，
              所以在数据库里用 “structure_index 等于某个值” 就能按结构找，不用把每一行解密出来比；
              从这个值反推不出结构。密钥由 DATA_ENCRYPTION_KEY 派生（security.Cipher.blind_index）。
    可见范围  visibility 列：team 全组可见，private 只有添加它的人（owner_id）可见。

本文件里有什么（按阅读顺序）：
    STATUS_LABELS、PRICE_PENDING   三种状态的中文显示文字；“待询价”
    InventoryService               一个组织的库存
        _visible                   “这个用户看得见的库存”这条查询（还没执行）
        list_items / count         全部库存；库存条数
        structure_index            算一个结构的盲索引
        find_by_structure          库存里有没有这个结构
        index_by_key14             全部库存按 key14 做成字典，一次分析用一次
        search                     E4：按名称、CAS 号或子结构搜
        upsert                     加一条库存；结构已经有了就更新那一条
        to_dict                    一条库存 -> 返回给前端的字典
    AvailabilityChecker            判断一个分子的可得性
        preload                    把一批分子在不在在售目录里一次查好
        check                      一个分子：库存 / 可购买 / 买不到
        _result                    拼出 availability 字典
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from syntherisk import models
from syntherisk.modules import m01_structure as chem
from syntherisk.reference import ReferenceData, normalize_name
from syntherisk.security import get_cipher

# 三种可得性状态 -> 界面上显示的文字。键就是存进分析结果里的 status；评分（M10）按键判断，不按文字判断
STATUS_LABELS = {"inventory": "库存", "purchasable": "可购买", "unavailable": "买不到"}
# 价格的显示文字。第一版没有供应商报价，所有价格都显示它；M10 路线汇总条里的“物料成本”也用这个常量
PRICE_PENDING = "待询价"


class InventoryService:
    """一个组织（课题组）的库存：查、搜、加。每个请求、每次分析各自新建一个，用完就丢，本身不保存状态。

    库存是客户数据，所以这里的每一条查询都带着“哪个组织、哪个用户”：
    别的组织的库存永远查不到，同组里别人设成 private 的也查不到。

    属性：
        session  SQLAlchemy 的数据库会话。这个类只读写、不提交，提交由调用方负责。
        org_id   组织编号（organizations.id）。
        user_id  当前用户编号（users.id）；None 表示不代表任何人，这时只看得到全组可见的库存。
    """

    def __init__(self, session: Session, org_id: str, user_id: str | None = None):
        """记下要用的数据库会话，以及“以谁的身份看库存”。不查库，没有副作用。

        参数：
            session  数据库会话，由调用方建好传进来（接口里是 Depends(get_db) 给的，流水线里是任务自己的会话）。
            org_id   组织编号。只会读写这个组织的库存。
            user_id  用户编号。给了：看得到全组可见的，加上这个人自己的 private 条目；新建的条目归这个人。
                     不给（None）：只看得到全组可见的。
        谁在用：
            api/inventory.py、api/structures.py、api/analyses.py 的接口（传登录用户），
            pipeline.py 的 _search 和 _availability（传发起这次分析的人），inventory_import.py，cli.py 的 seed-demo。
        """
        self.session = session
        self.org_id = org_id
        self.user_id = user_id

    # ------------------------------------------------------------ 读

    def _visible(self):
        """拼出“这个用户看得见的库存”这条查询，还没有执行。

        返回：
            SQLAlchemy 的 Select 对象。调用它的方法在后面再接 .where() / .order_by() / .limit()，
            然后交给 session.scalars() 执行。读库存的方法都从这里起步，所以隔离规则只写在这一处。
        规则：
            一定是本组织的（org_id 相同）；并且
            给了 user_id：全组可见的（visibility 是 team），或者是这个人自己的（owner_id 是他）；
            没给 user_id：只要全组可见的。
        谁在用：
            本类的 list_items()、find_by_structure()。
        """
        # select(模型) 是 SQLAlchemy 2 的写法，相当于 SELECT * FROM inventory_items；.where() 加筛选条件
        query = select(models.InventoryItem).where(models.InventoryItem.org_id == self.org_id)
        if self.user_id:
            # or_(a, b) 生成 SQL 里的 “a OR b”
            return query.where(
                or_(models.InventoryItem.visibility == "team", models.InventoryItem.owner_id == self.user_id)
            )
        return query.where(models.InventoryItem.visibility == "team")

    def list_items(self) -> list[models.InventoryItem]:
        """这个用户看得见的全部库存，最近改过的排在前面。

        返回：
            InventoryItem 对象的列表，可能是空的。读出来时 payload 已经自动解密成字典
            （这一列的类型是 security.EncryptedJSON）。
        副作用：
            查一次数据库，把看得见的库存整个读进内存并解密。
        谁在用：
            本类的 count()、index_by_key14()、search()；GET /api/inventory/items（E4 不带搜索条件时）；
            pipeline.py 的 _search（把库存的 InChIKey 交给逆合成引擎）。
        """
        # session.scalars(查询) 执行查询，每一行只取第一列——这里第一列就是整个 InventoryItem 对象。
        # updated_at.desc() 表示按更新时间从新到旧排
        return list(self.session.scalars(self._visible().order_by(models.InventoryItem.updated_at.desc())))

    def count(self) -> int:
        """这个用户看得见的库存一共有几条。A9 “已导入库存 N 种”显示的就是它。

        做法是把全部库存读出来再数个数（list_items），没有用 SQL 的 COUNT。
        谁在用：
            GET /api/inventory/summary。
        """
        return len(self.list_items())

    def structure_index(self, smiles: str | None) -> str | None:
        """算一个结构在本组织里的盲索引，用来在不解密的情况下按结构找库存。

        参数：
            smiles  结构的 SMILES；可以是 None 或空字符串（没有结构的条目，比如混合物、缓冲液）。
        返回：
            64 个十六进制字符的字符串；smiles 是空的、或者读不出来时返回 None。
        算法：
            先取 key14（InChIKey 前 14 位，不分立体异构），再算 HMAC-SHA256(密钥, "组织编号:key14")。
            组织编号也算在里面，所以两个组织的同一种试剂得到的值不同。
        抛出：
            RuntimeError  加密组件还没初始化（没有先调 context.build_context()）。正常运行时不会遇到。
        谁在用：
            本类的 find_by_structure()、upsert()；PATCH /api/inventory/items/{id} 改了结构以后用它重算这一列。
        """
        key = chem.key14_of(smiles) if smiles else None
        return get_cipher().blind_index(self.org_id, key) if key else None

    def find_by_structure(self, smiles: str) -> models.InventoryItem | None:
        """库存里有没有这个结构；有就返回那一条。

        参数：
            smiles  要找的结构。写法不必规范，立体构型不同也算同一个（按 key14 比）。
        返回：
            找到的 InventoryItem。结构读不出来、库存里没有、或者只有别人设成 private 的，都返回 None。
            同一个结构有好几条时只返回其中一条（查询没有指定顺序）。
        副作用：
            查一次数据库。按 structure_index 列直接定位（表上有 org_id + structure_index 的索引），
            不用解密别的行。
        谁在用：
            本类的 upsert()（去重）；GET /api/materials/availability（C4）。
        """
        index = self.structure_index(smiles)
        if index is None:
            return None
        # .limit(1) 只要一行；.first() 取结果的第一行，一行都没有时得到 None
        return self.session.scalars(
            self._visible().where(models.InventoryItem.structure_index == index).limit(1)
        ).first()

    def index_by_key14(self) -> dict[str, dict[str, Any]]:
        """分析开始时调用一次：这个组织的全部库存，按结构建索引。

        返回：
            {key14: 库存条目字典}。值的样子见 to_dict()（id、name、cas、smiles、quantity、location……）。
            没有结构的条目（混合物）进不了这个字典；同一个结构有几条时留最近更新的那一条。
        为什么需要它：
            一次分析要判断几十到几百个分子在不在库存里。先把库存整个读出来做成字典，
            之后每个分子查一下字典就行，不用一个分子问一次数据库。
        副作用：
            查一次数据库（list_items）。
        谁在用：
            pipeline.py 的 _availability，把结果交给 AvailabilityChecker。
        """
        index: dict[str, dict[str, Any]] = {}
        for item in self.list_items():
            # 用条目里存的 SMILES 重新算 key14；没有 SMILES 的条目算出来是 None，跳过
            key = chem.key14_of(item.payload.get("smiles") or "")
            if key:
                # setdefault：这个 key 还没有才放进去。list_items 是从新到旧给的，所以留下的是最新的一条
                index.setdefault(key, self.to_dict(item))
        return index

    def search(self, text: str | None = None, substructure: str | None = None) -> list[models.InventoryItem]:
        """E4：按名称、CAS 号或子结构搜。库存是加密的，所以在内存里过滤。

        参数：
            text          搜索词，在名称和 CAS 号里找“包含”关系。比较前两边都用 normalize_name 处理
                          （转小写，去掉空格、连字符、括号等），所以搜 “69727” 也能找到 CAS 号 69-72-7。
            substructure  子结构。先按 SMILES 读（用户画出来的结构），读不了再按 SMARTS 读。
            两个都给时要同时满足（先按文字筛，再按子结构筛）；都不给时返回全部库存。
        返回：
            符合条件的 InventoryItem 列表，顺序和 list_items() 一样（最近改过的在前）。
        抛出：
            ValueError  substructure 两种读法都读不出来。接口把它转成 422 返回给前端。
        副作用：
            查一次数据库（读出全部看得见的库存）。数据库里存的是密文，没法用 SQL 的 LIKE，只能读出来再比。
        谁在用：
            GET /api/inventory/items（带 q 或 substructure 参数时）。
        """
        items = self.list_items()
        # needle 是处理过的搜索词。text 是 None 或者全是空格时它是空字符串，下面这一段就跳过
        needle = normalize_name(text or "")
        if needle:
            # 名称或 CAS 号里包含搜索词的留下；条目里没有这个键时当作空字符串
            items = [
                item for item in items
                if needle in normalize_name(item.payload.get("name", ""))
                or needle in normalize_name(item.payload.get("cas", ""))
            ]
        if substructure:
            # 把子结构解析成 RDKit 的查询分子；得到 None 说明 SMILES 和 SMARTS 两种读法都失败了
            pattern = chem.parse_query(substructure)
            if pattern is None:
                raise ValueError("子结构读不出来，请检查画的结构或 SMILES")
            # 逐条做子结构匹配（RDKit 的 HasSubstructMatch）；没有 SMILES 的条目不算匹配
            items = [i for i in items if i.payload.get("smiles") and chem.has_substructure(i.payload["smiles"], pattern)]
        return items

    # ------------------------------------------------------------ 写

    def upsert(
        self,
        *,
        name: str,
        cas: str | None = None,
        smiles: str | None = None,
        quantity: str | None = None,
        location: str | None = None,
        note: str | None = None,
        source: str = "manual",
        import_id: str | None = None,
        visibility: str | None = None,
    ) -> tuple[models.InventoryItem, bool]:
        """加一条库存；结构已经在库存里就更新那一条（去重）。返回 (条目, 是不是新建的)。

        参数（都必须用关键字传，比如 upsert(name="乙醚", smiles="CCOCC")）：
            name        显示名称。只在新建时用；更新已有条目时不改名字。
            cas         CAS 号，可以不给。
            smiles      结构，可以不给（混合物、缓冲液这类没有单一结构的东西照样能记进库存）。
                        给了会先转成规范写法再存；读不出来时当作没给。
            quantity    数量，原样保存的一段文字，比如 “500 mL”。
            location    存放位置，原样保存的一段文字，比如 “试剂柜 A3”。
            note        备注。
            source      这一条是怎么来的：manual 手动添加、import 表格导入、marked_owned 在路线树上“标记为我有”。
            import_id   表格导入时填那次导入的编号（inventory_imports.id），其他来源不填。
            visibility  team 或 private。不给时用组织设置里的 inventory_default_visibility，没有设置就是 team。
        返回：
            (条目, created)。created 是 True 表示新建了一条，False 表示更新了已有的一条。
        去重规则：
            有结构时，按盲索引在“这个用户看得见的库存”里找同一个结构（find_by_structure）。找到了就只更新那一条的
            数量、位置、备注和 CAS 号，而且只用这次给了值的那几项去覆盖，没给的保持原样。
            没有结构的条目不去重，每调用一次就多一条。别人设成 private 的同结构条目看不见，所以不算重复。
        副作用：
            新建：把条目加进会话并 flush（向数据库发 INSERT）。新条目的 owner_id 是构造本对象时给的 user_id，
            表里这一列不允许为空，所以没给 user_id 时新建会被数据库拒绝。
            更新：改写已有条目的 payload。
            两种情况都不提交事务，调用方要自己 commit。
        谁在用：
            POST /api/inventory/items（E3）、POST /api/analyses/{id}/mark-owned（B12）、
            inventory_import.py 的 process_import 和 confirm_row（E1、E2）、cli.py 的 seed-demo。
        """
        # 结构统一成规范 SMILES 再存、再比。smiles 没给或者读不出来时 canonical 是 None，下面就不去重
        canonical = chem.canonical_smiles(smiles) if smiles else None
        existing = self.find_by_structure(canonical) if canonical else None
        if existing is not None:
            # 已经有这个结构：在旧内容的副本上改，只有这次给了非空值的项才覆盖
            payload = dict(existing.payload)
            for key, value in (("quantity", quantity), ("location", location), ("note", note), ("cas", cas)):
                if value:
                    payload[key] = value
            # 加密列必须整个重新赋值才会被保存，只改字典里的一项不行（见 security.EncryptedJSON 的说明）
            existing.payload = payload
            return existing, False

        if visibility is None:
            # session.get(模型, 主键) 按主键取一行，没有这一行时得到 None。settings 是组织自己的设置（JSON）
            org = self.session.get(models.Organization, self.org_id)
            visibility = (org.settings or {}).get("inventory_default_visibility", "team") if org else "team"
        item = models.InventoryItem(
            org_id=self.org_id,
            owner_id=self.user_id,
            visibility=visibility,
            # 盲索引；没有结构时是 None
            structure_index=self.structure_index(canonical),
            # 这个字典整体加密后存进 payload 列。没给的文字项存成空字符串，没有结构时 smiles 是 None
            payload={
                "name": name,
                "cas": cas or "",
                "smiles": canonical,
                "quantity": quantity or "",
                "location": location or "",
                "note": note or "",
            },
            source=source,
            import_id=import_id,
        )
        self.session.add(item)
        # flush 把 INSERT 发给数据库但不提交：这样 item.id、updated_at 这些自动生成的值马上就有，调用方接着就能用
        self.session.flush()
        return item, True

    @staticmethod
    def to_dict(item: models.InventoryItem, *, with_svg: bool = False) -> dict[str, Any]:
        """把一条库存变成返回给前端的字典；AvailabilityChecker 用的库存索引里放的也是它。

        参数：
            item      库存条目。
            with_svg  True 时多给一个 svg 键：结构的小图（120 × 80 的 SVG 文本）。没有结构的条目不给。
        返回的键：
            id          条目编号
            name        名称
            cas         CAS 号，没有是空字符串
            smiles      规范 SMILES，没有结构是 None
            quantity    数量（文字）
            location    位置（文字）
            note        备注
            source      manual / import / marked_owned
            visibility  team / private
            updated_at  最后修改时间，ISO 8601 格式的字符串；没有时是 None
            svg         只在 with_svg=True 并且有结构时才有
        谁在用：
            api/inventory.py 的各个接口、api/analyses.py 的 mark_owned、
            api/structures.py 的 material_availability，以及本类的 index_by_key14()。
            它是静态方法（@staticmethod），不用先建 InventoryService 对象就能调。
        """
        payload = item.payload
        out = {
            "id": item.id,
            "name": payload.get("name", ""),
            "cas": payload.get("cas", ""),
            "smiles": payload.get("smiles"),
            "quantity": payload.get("quantity", ""),
            "location": payload.get("location", ""),
            "note": payload.get("note", ""),
            "source": item.source,
            "visibility": item.visibility,
            "updated_at": models.iso(item.updated_at),
        }
        if with_svg and payload.get("smiles"):
            # 用 RDKit 画结构图；后两个参数是宽和高（像素）
            out["svg"] = chem.mol_to_svg(payload["smiles"], 120, 80)
        return out


class AvailabilityChecker:
    """给定库存索引和在售目录，回答“这个原料从哪来”。

    判断的先后（见 check）：库存里有 -> “库存”；否则，在售目录里有、或者逆合成引擎说在售、
    或者是水这样随手可得的 -> “可购买”；都不是 -> “买不到”。

    属性：
        session          数据库会话，只用来查 catalog_compounds 表（在售目录）。
        reference        参考数据。这里只用 reference.compounds.by_key14 看一个物质是不是“随手可得”。
        inventory_index  {key14: 库存条目字典}，由调用方准备好：整次分析用 InventoryService.index_by_key14()
                         的结果；只查一个分子的接口给只有一项（或者空）的字典。
        _catalog         在售目录的查询结果缓存 {key14: 目录来源或 None}。None 表示查过了、目录里没有，
                         这样同一个分子不会问数据库两次。只在这个对象存在期间有效。
    """

    def __init__(self, session: Session, reference: ReferenceData, inventory_index: dict[str, dict[str, Any]]):
        """记下三样东西，并建一个空的在售目录缓存。不查库。

        参数：
            session          数据库会话。
            reference        参考数据（AppContext.reference）。
            inventory_index  {key14: 库存条目字典}，见类的说明。
        谁在用：
            pipeline.py 的 _availability；api/analyses.py 的 mark_owned；
            api/structures.py 的 material_availability。
        """
        self.session = session
        self.reference = reference
        self.inventory_index = inventory_index
        self._catalog: dict[str, str | None] = {}

    def preload(self, key14s: Iterable[str]) -> None:
        """把一批分子在不在在售目录里一次查好，存进 _catalog，免得 check() 一个分子查一次库。

        参数：
            key14s  要查的 key14，可以有重复。pipeline 直接把 result["molecules"] 这个字典传进来
                    （遍历字典得到的就是它的键，也就是 key14）。
        返回：
            None。结果在 self._catalog 里：目录里有的记它的 source（比如 seed、zinc），没有的记 None。
        副作用：
            查 catalog_compounds 表，每 500 个 key 一条查询。已经查过的 key 不再查。
        谁在用：
            pipeline.py 的 _availability（整批）；本类的 check()（碰到没查过的单个分子时）。
        """
        # 去掉重复的和已经查过的
        missing = [k for k in set(key14s) if k not in self._catalog]
        # 分批查，每批最多 500 个：数据库对一条语句能带的参数个数有上限（老版本的 SQLite 是 999 个）
        for start in range(0, len(missing), 500):
            chunk = missing[start : start + 500]
            # 只取两列。session.execute 返回的每一行是一个 (key14, source) 元组；.in_(列表) 就是 SQL 的 IN (...)
            rows = self.session.execute(
                select(models.CatalogCompound.key14, models.CatalogCompound.source).where(
                    models.CatalogCompound.key14.in_(chunk)
                )
            )
            # dict(一串二元组) 得到 {key14: source}
            found = dict(rows.all())
            # 这一批里没查到的也记下来（记成 None），表示“查过了，目录里没有”
            for key in chunk:
                self._catalog[key] = found.get(key)

    def check(self, smiles: str, *, engine_in_stock: bool | None = None) -> dict[str, Any]:
        """判断一个分子的可得性：库存、可购买，还是买不到。

        参数：
            smiles           分子的 SMILES。
            engine_in_stock  逆合成引擎自己的在售清单（AiZynthFinder 的 ZINC）给出的判断。
                             True：引擎说在售。False 或 None：引擎说不在售，或者没有这项信息（演示路线、单独查询）。
                             只有我们自己的在售目录里查不到时，才用它来补。
        返回：
            一个字典，各个键见 _result()。其中 status 是三选一：
                inventory    库存里有（即使它同时也买得到）
                purchasable  库存里没有，但在售目录里有，或者引擎说在售，或者它是随手可得的物质
                unavailable  以上都不是；SMILES 读不出来也算这一种
        副作用：
            这个分子没有预先查过在售目录时，查一次数据库（preload）。不改传进来的东西。
        谁在用：
            pipeline.py 的 _availability（每个分子一次）和 _adopt_precedent_reagents（从先例借来的试剂）；
            POST /api/analyses/{id}/mark-owned（B12）；GET /api/materials/availability（C4）。
            结果存在 result["molecules"][key14]["availability"]，M10 评分、M13 物料清单、证据卡都读它。
        """
        key = chem.key14_of(smiles)
        if key is None:
            # 结构读不出来，没法查：按买不到算
            return self._result("unavailable")
        if key not in self._catalog:
            self.preload([key])
        # 在售目录里的来源（seed、zinc……）；目录里没有是 None
        catalog_source = self._catalog.get(key)
        if catalog_source is None and engine_in_stock:
            # 我们的目录里没有，但引擎自己的在售清单里有：也算能买到，来源记成 engine_stock
            catalog_source = "engine_stock"

        # 内置试剂表里 roles 带 ubiquitous 的物质（现在只有水）：不用买也不用查库存，随手就有
        compound = self.reference.compounds.by_key14.get(key)
        ubiquitous = bool(compound and "ubiquitous" in compound.roles)
        item = self.inventory_index.get(key)
        # 三种状态按这个先后判断：库存优先于可购买
        if item:
            status = "inventory"
        elif catalog_source or ubiquitous:
            status = "purchasable"
        else:
            status = "unavailable"
        return self._result(status, item=item, catalog_source=catalog_source, ubiquitous=ubiquitous)

    @staticmethod
    def _result(status: str, *, item: dict | None = None, catalog_source: str | None = None, ubiquitous: bool = False):
        """拼出 availability 字典。存进分析结果、返回给前端的就是这个样子。

        参数：
            status          inventory / purchasable / unavailable。
            item            库存条目字典（InventoryService.to_dict 的结果）；库存里没有就是 None。
            catalog_source  在售目录的来源；不在售是 None。
            ubiquitous      是不是随手可得的物质。
        返回的键：
            status          三种状态之一
            label           状态的中文：库存 / 可购买 / 买不到
            inventory       库存里有时是 {item_id, name, quantity, location}，否则是 None
            purchasable     买不买得到（True / False）。和 status 无关：库存里有的东西如果目录里也有，这里同样是 True
            catalog_source  在售的依据：seed（内置试剂表）、导入目录时填的来源名（比如 zinc）、
                            engine_stock（引擎的在售清单）；不在售是 None
            ubiquitous      True 的物质不算“需要准备的原料”，流水线和评分统计原料时会跳过它
            suppliers       供应商，现在一律是 None
            price           价格，现在一律是 None
            price_label     “待询价”
            lead_time       货期，现在一律是 None
        谁在用：
            本类的 check()。
        """
        return {
            "status": status,
            "label": STATUS_LABELS[status],
            "inventory": (
                {"item_id": item["id"], "name": item["name"], "quantity": item["quantity"], "location": item["location"]}
                if item else None
            ),
            "purchasable": bool(catalog_source) or ubiquitous,
            "catalog_source": catalog_source,
            "ubiquitous": ubiquitous,
            # 第一版没有供应商数据：几家在售、多少钱、多久到货都还不知道
            "suppliers": None,
            "price": None,
            "price_label": PRICE_PENDING,
            "lead_time": None,
        }
