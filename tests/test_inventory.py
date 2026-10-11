"""实验室库存：导入、确认、搜索、共享（E1–E5），以及库存对分析的影响（A8）。

这个文件测的是什么
    库存页上的按钮对应的接口（都在 syntherisk/api/inventory.py），以及库存怎样影响一次分析：
        E1 导入 Excel / CSV   POST   /api/inventory/imports                       import_inventory
        E2 确认匹配           POST   /api/inventory/imports/{id}/rows/{row_id}    resolve_import_row
        E3 手动添加           POST   /api/inventory/items                         add_item
        E4 搜索               GET    /api/inventory/items?q=&substructure=        list_items
        E5 共享给团队         POST   /api/inventory/share                         share_inventory
        已导入库存 N 种       GET    /api/inventory/summary                       inventory_summary
        改、删一条库存        PATCH / DELETE /api/inventory/items/{id}            update_item、delete_item
        A8 原料来源“仅用库存”  随开始分析提交，由 M10 执行（syntherisk/modules/m10_scoring.py）
    背后的模块是 M1（把名称、CAS 号变成结构：syntherisk/modules/m01_resolver.py）和
    M4（库存的存取和匹配：syntherisk/modules/m04_availability.py）；读表格、逐行识别在
    syntherisk/inventory_import.py。库存内容加密存放，按课题组隔离，这两点也在这里测。

导入的结果分三栏（inventory_import.py 的文件头有完整说明）
    已匹配（matched）             认准了是哪个物质，直接进库存
    需确认（needs_confirmation）  有一个猜测但不够确定，等用户确认、改结构或者忽略
    未识别（unrecognized）        名称、CAS 号、SMILES 都没认出来
    用户处理过的行变成 confirmed（进了库存，计数时并入“已匹配”）或 ignored（忽略）。

共用的夹具和辅助函数（tests/conftest.py）
    client、account    测试客户端、已注册的账号（“测试课题组”的组长）
    register()         再注册一个别的课题组的账号
    start_analysis()   提交一次分析；测试里队列是同步的，返回时已经跑完
    测试环境不联网：名称和 CAS 号只在内置试剂表（data/compounds.csv）里查，查不到的就是“未识别”。
    本文件没有替身类；表格内容是手写的（下面的 CSV，和一个在内存里现做的 Excel 文件）。

本文件里有什么
    CSV        一张故意做得不整齐的试剂清单
    _upload()  上传一个表格文件，返回这次导入的记录
    test_import_sorts_rows_into_matched_needs_confirmation_and_unrecognized
        导入把每一行分到已匹配、需确认、未识别三栏；只有已匹配的进库存
    test_confirming_fixing_and_ignoring_uncertain_rows
        不确定的行可以确认、改结构或忽略；处理过的行不能再处理
    test_excel_import_and_duplicate_rows_update_instead_of_adding
        Excel 也能导入；同一个物质出现两次只更新那一条；不支持的文件、没有表头的表格被拒绝
    test_manual_add_search_edit_and_delete
        手动添加（名称或 SMILES 任给一个）、按名称 / CAS 号 / 子结构搜索、修改、删除
    test_inventory_is_encrypted_and_private_items_are_hidden_from_teammates
        库存在数据库里是密文；设成私有以后同组的人看不到；别的课题组始终看不到；普通成员不能加人
    test_inventory_only_mode_excludes_routes_until_materials_are_in_stock
        “仅用库存”档：原料不在库存里的路线被排除，把原料加进库存以后路线回来
"""

import io

from openpyxl import Workbook
from sqlalchemy import text

from tests.conftest import register, start_analysis

# 一张故意做得不整齐的试剂清单，导入测试用。表头是 名称、CAS、SMILES、数量、位置 五列，下面每一行考察一种情况
# （识别的规则在 syntherisk/modules/m01_resolver.py 的 NameResolver.resolve_row）：
#   第 1 行  乙酸酐 + 正确的 CAS 号            按 CAS 号在内置试剂表里找到                        → 已匹配
#   第 2 行  没有名称，只有 CAS 号 538-93-2    这是异丁基苯的 CAS 号，名字用表里的“异丁基苯”      → 已匹配
#   第 3 行  “某个新化合物” + SMILES           SMILES 读得出来就直接用（结构是 4-异丁基苯乙酮），
#                                              名字用这一行自己填的                              → 已匹配
#   第 4 行  Mg turnings                       是镁屑的别名，不是表里的正式名称                  → 需确认
#   第 5 行  Pd/C 10%                          去掉规格“10%”以后对上钯碳的别名 Pd/C；
#                                              名称里有 “/” 和 “%”，按混合物提示                 → 需确认
#   第 6 行  乙醚 + CAS 号 60-29-8             CAS 号的最后一位（校验位）写错了，表里乙醚的是 60-29-7；
#                                              名称对得上，但前面有了疑点                        → 需确认
#   第 7 行  自制中间体 B-7                    名称、CAS 号、SMILES 都认不出来                   → 未识别
#   第 8 行  五格全空                          当空行跳过，不算一行数据
# 所以一共 7 行数据：已匹配 3、需确认 3、未识别 1。
CSV = """名称,CAS,SMILES,数量,位置
乙酸酐,108-24-7,,500 mL,试剂柜 A3
,538-93-2,,250 mL,试剂柜 B1
某个新化合物,,CC(C)Cc1ccc(cc1)C(C)=O,5 g,冰箱
Mg turnings,,,100 g,药品柜 1
Pd/C 10%,,,1 g,干燥器
乙醚,60-29-8,,1 L,防爆柜
自制中间体 B-7,,,2 g,冰箱
,,,,
"""


def _upload(client, account, content: bytes, filename: str):
    """上传一个表格文件（E1，POST /api/inventory/imports），确认返回 201，返回这次导入的记录。

    参数：
        client、account  conftest.py 的夹具。
        content          文件内容（字节）。
        filename         文件名。后端按它的扩展名决定怎么读（.csv、.xlsx 等）。
    返回：
        接口返回的字典（inventory_import.py 的 import_view）：id、status、total_rows、
        counts（各栏的行数）、rows（需确认和未识别两栏的明细）。
    这里的表格都很小（不超过 200 行），在这次请求里就处理完了，所以返回时 status 已经是 done。
    """
    # files=… 是按网页上传文件的方式（multipart/form-data）发请求：字段名 file，值是 (文件名, 内容)
    response = client.post("/api/inventory/imports", headers=account["headers"], files={"file": (filename, content)})
    assert response.status_code == 201, response.text
    return response.json()


def test_import_sorts_rows_into_matched_needs_confirmation_and_unrecognized(client, account):
    """E1 导入表格：每一行分到已匹配、需确认、未识别三栏之一，并说明原因；只有已匹配的直接进库存。

    保证的行为：POST /api/inventory/imports（api/inventory.py 的 import_inventory）读表格、认表头
    （inventory_import.py 的 parse_table），逐行交给 m01_resolver.py 的 NameResolver.resolve_row 识别
    （process_import）。认准的行直接写进库存；拿不准的行给出猜测的结构和要人确认的原因；
    认不出来的行没有猜测。空行不计入行数。
    怎么读：上传文件开头的那张 CSV（每一行考察什么见 CSV 上方的注释）。先看总行数和三栏的计数，
    再逐个看需确认、未识别的行给出的原因，最后看库存里实际多了哪几条。
    """
    result = _upload(client, account, CSV.encode("utf-8"), "试剂清单.csv")
    assert result["status"] == "done" and result["total_rows"] == 7  # 空行不算
    assert result["counts"] == {"matched": 3, "needs_confirmation": 3, "unrecognized": 1, "ignored": 0, "pending": 0}

    # rows 里只有需确认和未识别的行。做成 {表格里的名称: 这一行}，下面按名称取
    by_name = {row["raw"]["name"]: row for row in result["rows"]}
    # Mg turnings：系统猜它是镁（SMILES 是 [Mg]）；猜测里的名称保留表格里填的写法
    assert by_name["Mg turnings"]["proposal"]["name"] == "Mg turnings" and by_name["Mg turnings"]["proposal"]["smiles"] == "[Mg]"
    # 要人确认的原因：它对上的是内置试剂表里“镁屑”的别名，不是正式的中文名或英文名
    assert "缩写、别名或商品名" in by_name["Mg turnings"]["reason"]
    # Pd/C 10%：按主要成分（钯碳）匹配，提示它是混合物
    assert "混合物" in by_name["Pd/C 10%"]["reason"]
    assert "CAS 号的校验位不对" in by_name["乙醚"]["reason"]  # CAS 写错一位：名称对上了，但要人确认
    # 自制中间体：没有任何猜测
    assert by_name["自制中间体 B-7"]["bucket"] == "unrecognized" and by_name["自制中间体 B-7"]["proposal"] is None

    # 库存里现在只有已匹配的那 3 行
    items = client.get("/api/inventory/items", headers=account["headers"]).json()
    assert items["total"] == 3
    # 第 2 行没有填名称，库存里用的是内置试剂表里的名字“异丁基苯”
    assert {item["name"] for item in items["items"]} == {"乙酸酐", "异丁基苯", "某个新化合物"}
    # 新建分析页上“已导入库存 N 种”读的就是这个接口
    assert client.get("/api/inventory/summary", headers=account["headers"]).json() == {"count": 3}


def test_confirming_fixing_and_ignoring_uncertain_rows(client, account):
    """E2 确认匹配：需确认和未识别的行，可以接受系统的猜测、自己给结构，或者忽略；处理过的行不能再处理。

    保证的行为：POST /api/inventory/imports/{id}/rows/{row_id}（api/inventory.py 的 resolve_import_row，
    具体处理在 inventory_import.py 的 confirm_row），请求体里的 action 有三种：
      - confirm：用系统猜的结构记进库存，这一行变成 confirmed，计数并入“已匹配”；
      - set_structure：用请求里给的 SMILES 记进库存；SMILES 读不出来返回 422，这一行不变；
      - ignore：不进库存，这一行变成 ignored。
    只有还在“需确认”“未识别”两栏的行可以处理，其余的返回 409。
    怎么读：还是那张 CSV。导入以后有 3 行需确认、1 行未识别；这里处理其中三行，乙醚那一行留着不动。
    """
    result = _upload(client, account, CSV.encode("utf-8"), "list.csv")
    # {表格里的名称: 行记录的编号}，下面拼接口地址用
    rows = {row["raw"]["name"]: row["id"] for row in result["rows"]}
    base = f"/api/inventory/imports/{result['id']}/rows"

    # 接受系统的猜测：Mg turnings 就是镁。原来已匹配 3 行，加上这一行是 4
    confirmed = client.post(f"{base}/{rows['Mg turnings']}", headers=account["headers"], json={"action": "confirm"}).json()
    assert confirmed["row"]["bucket"] == "confirmed" and confirmed["import"]["counts"]["matched"] == 4

    # 给未识别的那一行指定结构。先给一个读不出来的 SMILES（C1CC 的环没有闭合）：422
    bad = client.post(f"{base}/{rows['自制中间体 B-7']}", headers=account["headers"], json={"action": "set_structure", "smiles": "C1CC"})
    assert bad.status_code == 422
    # 再给一个合法的结构（这里随便用了水杨酸的 SMILES）：这一行进了库存，“未识别”一栏清空
    fixed = client.post(f"{base}/{rows['自制中间体 B-7']}", headers=account["headers"],
                        json={"action": "set_structure", "smiles": "OC(=O)c1ccccc1O"}).json()
    assert fixed["import"]["counts"]["unrecognized"] == 0

    # 忽略 Pd/C 那一行
    ignored = client.post(f"{base}/{rows['Pd/C 10%']}", headers=account["headers"], json={"action": "ignore"}).json()
    assert ignored["import"]["counts"]["ignored"] == 1
    # 已经忽略的行再去确认：409（这一行已经处理过了）
    again = client.post(f"{base}/{rows['Pd/C 10%']}", headers=account["headers"], json={"action": "confirm"})
    assert again.status_code == 409

    # 库存里一共 5 条：导入时直接匹配的 3 条，加上刚确认的镁和指定了结构的自制中间体
    assert client.get("/api/inventory/summary", headers=account["headers"]).json() == {"count": 5}


def test_excel_import_and_duplicate_rows_update_instead_of_adding(client, account):
    """E1 导入 Excel；同一个物质在表里出现两次，库存里只有一条（后面的更新前面的）；读不了的文件被拒绝。

    保证的行为：
      - .xlsx 文件读第一个工作表；表头可以是中文的各种叫法（inventory_import.py 的 _HEADERS：
        “试剂名称”算名称，“CAS号”算 CAS，“库存量”算数量，“存放位置”算位置）；
      - 进库存用的是 m04_availability.py 的 InventoryService.upsert：库存里已经有同一个结构时不新建，
        只把这次给的数量、位置更新到那一条上；
      - 扩展名不支持的文件、找不到表头的表格，返回 422（inventory_import.py 的 TableFormatError）。
    怎么读：用 openpyxl 在内存里做一个两行的 Excel：乙酸酐（带 CAS 号）和醋酸酐（乙酸酐的别名）。
    第一行直接匹配进了库存；第二行是别名，要人确认。确认以后库存里还是一条，数量和位置是第二行的。
    """
    # Workbook() 新建一个工作簿，active 是它自带的第一张表，append 往表里加一行
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["试剂名称", "CAS号", "库存量", "存放位置"])
    sheet.append(["乙酸酐", "108-24-7", "500 mL", "A3"])
    sheet.append(["醋酸酐", "", "1 L", "B2"])  # 同一个东西的另一种叫法
    # 不往磁盘上写文件：io.BytesIO 是内存里的一个“文件”，存进去以后用 getvalue() 取出全部字节
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = _upload(client, account, buffer.getvalue(), "库存.xlsx")
    assert result["total_rows"] == 2
    # result["rows"] 里只有要人处理的行，这里就是“醋酸酐”那一行；确认它
    assert client.post(f"/api/inventory/imports/{result['id']}/rows/{result['rows'][0]['id']}",
                       headers=account["headers"], json={"action": "confirm"}).status_code == 200
    items = client.get("/api/inventory/items", headers=account["headers"]).json()
    # 两行是同一个结构，库存里只有一条；数量和位置被第二行的值（1 L、B2）盖掉了
    assert items["total"] == 1 and items["items"][0]["quantity"] == "1 L" and items["items"][0]["location"] == "B2"

    # 扩展名是 .pdf：不是支持的表格格式
    assert client.post("/api/inventory/imports", headers=account["headers"],
                       files={"file": ("notes.pdf", b"%PDF")}).status_code == 422
    # 是 CSV，但表头里没有名称、CAS、SMILES 中的任何一列，认不出表头
    assert client.post("/api/inventory/imports", headers=account["headers"],
                       files={"file": ("bad.csv", "颜色,价格\n红,3\n".encode("utf-8"))}).status_code == 422


def test_manual_add_search_edit_and_delete(client, account):
    """E3 手动添加、E4 搜索，以及修改和删除一条库存。

    保证的行为（syntherisk/api/inventory.py）：
      - POST /api/inventory/items（add_item）：名称、CAS 号、SMILES 任给一个都行，后端去认它是什么物质，
        认出来就补上 CAS 号和结构；认不出结构的也可以先记下来（混合物、缓冲液）；
        给了 SMILES 却读不出来，或者三样都没给，返回 422；
      - GET /api/inventory/items（list_items）：q 按名称或 CAS 号搜，substructure 按子结构搜；
        子结构读不出来返回 422；
      - PATCH /api/inventory/items/{id}（update_item）只改带上的字段；DELETE（delete_item）返回 204。
    怎么读：加三条库存——只给名称的水杨酸、只给 SMILES 的乙醚（CCOCC）、认不出结构的“缓冲液 A”；
    然后用四种办法搜；最后改水杨酸的数量、再把它删掉。
    """
    added = client.post("/api/inventory/items", headers=account["headers"],
                        json={"name": "水杨酸", "quantity": "100 g", "location": "药品柜 2"}).json()
    # 只给了名称：CAS 号和结构是从内置试剂表（data/compounds.csv）里补上的；SMILES 是 RDKit 的规范写法
    assert added["created"] and added["item"]["cas"] == "69-72-7" and added["item"]["smiles"] == "O=C(O)c1ccccc1O"
    # 只给 SMILES：名字用内置试剂表里的“乙醚”（下面按子结构搜的时候会看到）
    client.post("/api/inventory/items", headers=account["headers"], json={"smiles": "CCOCC"})
    # 认不出结构的东西照样记进库存，只是没有结构
    client.post("/api/inventory/items", headers=account["headers"], json={"name": "缓冲液 A"})
    # 给了 SMILES 但读不出来：422，不会拿别的信息去猜
    assert client.post("/api/inventory/items", headers=account["headers"], json={"smiles": "C1CC"}).status_code == 422
    # 名称、CAS 号、SMILES 一个都没给：422
    assert client.post("/api/inventory/items", headers=account["headers"], json={}).status_code == 422

    def search(**params):
        """按给的查询参数搜库存（q=… 或 substructure=…），返回搜到的条目的名称列表。"""
        return [item["name"] for item in client.get("/api/inventory/items", headers=account["headers"], params=params).json()["items"]]

    # 按名称搜：名称里包含“水杨”就算
    assert search(q="水杨") == ["水杨酸"]
    # 按 CAS 号搜
    assert search(q="69-72-7") == ["水杨酸"]
    assert search(substructure="c1ccccc1") == ["水杨酸"]  # 含苯环的
    # COC 是“碳-氧-碳”，也就是醚键：三条库存里只有乙醚有。没有结构的“缓冲液 A”不参与子结构搜索
    assert search(substructure="COC") == ["乙醚"]
    assert client.get("/api/inventory/items", headers=account["headers"], params={"substructure": "C1CC"}).status_code == 422

    item_id = added["item"]["id"]
    # 只改数量：没带的字段（位置）保持原样
    updated = client.patch(f"/api/inventory/items/{item_id}", headers=account["headers"], json={"quantity": "50 g"}).json()
    assert updated["quantity"] == "50 g" and updated["location"] == "药品柜 2"
    # 删掉水杨酸，库存里剩乙醚和缓冲液 A 两条
    assert client.delete(f"/api/inventory/items/{item_id}", headers=account["headers"]).status_code == 204
    assert client.get("/api/inventory/summary", headers=account["headers"]).json() == {"count": 2}


def test_inventory_is_encrypted_and_private_items_are_hidden_from_teammates(client, account):
    """库存在数据库里是密文；E5 设成“只有自己可见”以后同组的人看不到；别的课题组始终看不到。

    保证的行为：
      - inventory_items 表的 payload 列（名称、CAS、SMILES、数量、位置）整个加密后存
        （syntherisk/security.py 的 EncryptedJSON）；structure_index 列是结构的“盲索引”：
        用密钥算出来的 64 个十六进制字符，同一个课题组里同一个结构总是同一串，看不出是什么结构
        （security.py 的 Cipher.blind_index）；
      - 新加的库存默认全组可见；POST /api/inventory/share（api/inventory.py 的 share_inventory）
        把自己名下的全部库存改成 private 或 team；private 的条目只有添加它的人看得到
        （m04_availability.py 的 InventoryService._visible）；
      - 库存按课题组隔离；
      - 给组里加成员要管理员权限，普通成员去加返回 403（syntherisk/api/deps.py 的 require_admin）。
    怎么读：组长加一条水杨酸，直接查数据库看存的是什么；再给组里开一个普通成员的账号，
    比较共享前后他看到的库存条数；最后用另一个课题组的账号看一眼。
    为什么重要：README 写明客户库存在应用层加密后再存，按结构匹配靠盲索引，数据按课题组隔离。
    """
    client.post("/api/inventory/items", headers=account["headers"], json={"name": "水杨酸", "location": "药品柜 2"})
    # 绕过接口直接查表。text() 把一段原始 SQL 包成可以执行的语句；这样读到的是列里存的原样，没有经过自动解密。
    # 表里只有刚加的这一行，.one() 取出它
    with client.app.state.ctx.engine.connect() as connection:
        stored = connection.execute(text("SELECT payload, structure_index FROM inventory_items")).one()
    # payload 是密文：名称、位置、结构（苯环的写法 c1ccccc1）都找不到
    assert "水杨酸" not in stored[0] and "药品柜" not in stored[0] and "c1ccccc1" not in stored[0]
    assert len(stored[1]) == 64  # 结构的盲索引，不是结构本身

    # 组长给组里的同学开一个账号（默认角色是普通成员），同学登录拿到自己的令牌
    member = client.post("/api/org/members", headers=account["headers"],
                         json={"email": "labmate@example.com", "password": "another-password-1"})
    assert member.status_code == 201
    login = client.post("/api/auth/login", json={"email": "labmate@example.com", "password": "another-password-1"}).json()
    mate = {"Authorization": f"Bearer {login['access_token']}"}
    # 默认全组可见：同学看得到组长加的那一条
    assert client.get("/api/inventory/summary", headers=mate).json() == {"count": 1}

    # 组长把自己名下的库存改成只有自己可见；updated 是处理了几条
    assert client.post("/api/inventory/share", headers=account["headers"], json={"visibility": "private"}).json()["updated"] == 1
    # 同学看不到了，组长自己还看得到
    assert client.get("/api/inventory/summary", headers=mate).json() == {"count": 0}
    assert client.get("/api/inventory/summary", headers=account["headers"]).json() == {"count": 1}

    # 别的课题组的人看不到这个组的库存
    outsider = register(client, email="outsider@example.com", org="别的课题组")
    assert client.get("/api/inventory/summary", headers=outsider["headers"]).json() == {"count": 0}
    # 普通成员不能给组里加人：403（没有权限）
    assert client.post("/api/org/members", headers=mate, json={"email": "x@example.com", "password": "password-123"}).status_code == 403


def test_inventory_only_mode_excludes_routes_until_materials_are_in_stock(client, account):
    """A8 原料来源选“仅用库存”：起始原料不在库存里的路线被排除并写明缺什么；补齐库存以后路线回来。

    保证的行为：constraints.stock_source = inventory_only 时，m10_scoring.py 的 exclusion_reasons 检查每条路线
    的起始原料（路线树的叶子），只要有一个不在库存里就排除，原因里列出缺的原料（约束编号 stock）。
    试剂不在检查范围内。库存里有没有，是分析的第 3 阶段（pipeline.py 的 _availability）按结构到库存里对出来的。
    怎么读：目标分子是阿司匹林（这里直接写了它的 SMILES）。演示数据里它有三条路线，第一条“乙酸酐酰化”只有一步：
    水杨酸 + 乙酸酐，浓硫酸催化。先在空库存下分析，三条路线全被排除；再把水杨酸和乙酸酐加进库存重新分析，
    第一条路线回来了，另外两条还缺别的原料。
    """
    analysis = start_analysis(client, account, smiles="CC(=O)Oc1ccccc1C(=O)O", constraints={"stock_source": "inventory_only"})
    result = client.get(f"/api/analyses/{analysis['id']}/result", headers=account["headers"]).json()
    # 库存是空的：一条可选的路线都没有
    assert result["routes"] == []
    # 把三条被排除路线的原因文字收到一个列表里；第一条路线缺的是它的两个起始原料
    labels = [reason["label"] for route in result["excluded_routes"] for reason in route["exclusion_reasons"]]
    assert "原料不在库存里：水杨酸、乙酸酐" in labels

    # 把水杨酸和乙酸酐加进库存（E3 手动添加，只给 SMILES），再提交一次同样的分析
    for smiles in ("OC(=O)c1ccccc1O", "CC(=O)OC(C)=O"):
        client.post("/api/inventory/items", headers=account["headers"], json={"smiles": smiles})
    again = start_analysis(client, account, smiles="CC(=O)Oc1ccccc1C(=O)O", constraints={"stock_source": "inventory_only"})
    # 第 3 阶段的进度里报了“库存 2”：三条路线的起始原料里，有两个在库存里
    assert "库存 2" in again["stages"][2]["message"]
    result = client.get(f"/api/analyses/{again['id']}/result", headers=account["headers"]).json()
    assert [route["name"] for route in result["routes"]] == ["乙酸酐酰化"]
    # 这条路线要准备的物料一共 3 样：两个起始原料在库存里，试剂浓硫酸不在。
    # “仅用库存”只管起始原料，所以缺试剂不影响它可选
    assert result["routes"][0]["inventory_count"] == "2/3"  # 催化用的浓硫酸不在库存里
