"""M0 应用服务（账号、项目、草稿、预设、配置）、结构接口，以及任务的取消、恢复和命令行工具。

这个文件测的是什么
    四块内容，按文件里的先后：
    1. M0 应用服务的日常接口（syntherisk/api/core.py）：注册、登录、成员、项目和项目历史、
       草稿（A6）、团队预设（A15、D2、D3）、前端启动时要读的配置。
    2. M1 结构工具的接口（syntherisk/api/structures.py）：校验结构（A1、A2）、上传 MOL 文件（A4）、
       按名称或 CAS 号查结构（A3）、画结构图、比较两个结构（C2）；以及先例列表接口（C1、C2，
       syntherisk/api/analyses.py 的 get_step_precedents）怎样标出先例和这一步的结构差异。
    3. MQ 任务队列和流水线在“不顺利”时的行为（syntherisk/pipeline.py、syntherisk/taskqueue.py、
       syntherisk/inventory_import.py）：排队中取消（A19）、搜索途中取消、先看初步结果（A18）、
       服务重启以后恢复没跑完的分析、大表格的库存导入交给后台。
    4. 命令行工具（syntherisk/cli.py）：status、seed-demo、load-precedents、calibrate、export-consented。

共用的夹具和辅助函数（tests/conftest.py）
    client、account、ctx   测试客户端、已注册的账号、运行时上下文（说明见 conftest.py）
    register()             再注册一个账号（另一个课题组）
    start_analysis()       提交一次分析；测试里队列默认是同步的，返回时分析已经跑完
    IBUPROFEN、ASPIRIN     布洛芬、阿司匹林的 SMILES；演示引擎只有这两个分子的路线
    TEST_PASSWORD          测试账号用的口令，只存在于临时测试库里
pytest 自带的夹具（本文件用到三个）
    monkeypatch   临时改掉某个模块或对象的属性，测试结束时自动改回去
    capsys        把测试期间打印到屏幕上的内容截下来，capsys.readouterr().out 取出截到的文字
    tmp_path      这个测试专用的临时文件夹（pathlib.Path）
本文件自己的替身
    HeldQueue     只收任务、不执行的假队列：让分析和导入停在“排队中”
    SlowEngine    假的逆合成引擎：搜索到一半时“用户点了取消”（写在用它的那个测试里面）

本文件里有什么
    test_login_and_account_basics
        注册、登录、查当前用户；邮箱不分大小写；口令太短或不对被拒；库里存的是口令的哈希
    test_projects_and_history
        建项目、看项目历史、按项目筛分析；别的课题组看不到这个项目，也不能往里提交分析
    test_draft_is_saved_updated_and_removed_once_the_analysis_starts
        草稿能保存、覆盖、列出；别人改不了；用它开始分析以后草稿自动删掉
    test_team_presets_for_constraints_and_weights
        约束预设和权重预设：保存时补全默认值、同名覆盖、内容不对被拒、全组共用、可以删除
    test_frontend_configuration_endpoints
        评分公式的参数、约束页的选项、各模块的现状、数据来源登记表这四个配置接口
    test_structure_endpoints
        校验、上传、名称查询、画图、结构差异五个结构接口
    test_precedent_list_marks_structural_differences
        先例列表给相似先例标出结构差异；产率只用同一反应的报道值
    test_precedent_links_saved_by_older_analyses_are_rebuilt_when_read
        旧分析里存着的打不开的先例链接，打开证据卡和先例列表时按专利号重新拼
    HeldQueue
        只收任务、不执行的假队列
    test_queued_analysis_can_be_cancelled_and_never_runs
        还在排队的分析可以取消，取消以后队列再轮到它也不会跑
    test_cancel_during_search_stops_before_the_next_stage
        搜索途中取消：搜索完成后、下一阶段开始前停下，已经搜到的初步路线树还能看
    test_interrupted_analyses_are_requeued_once_after_a_restart
        服务重启后没跑完的分析重新排队一次；已经重试过的直接记为失败
    test_large_inventory_files_are_processed_by_the_queue
        行数多的库存表格不在请求里处理，交给任务队列，处理完能查到结果
    test_command_line_tools
        命令行的 status、seed-demo、load-precedents、calibrate、export-consented
"""

import copy
import json

import pytest
from sqlalchemy import select

# 除了走接口，这个文件还直接调后端的几个模块：cli（命令行入口）、inventory_import 和 pipeline
# （任务队列的两个处理函数，测试里手动调用来模拟“队列轮到这个任务了”）、models（直接查数据库）
from syntherisk import cli, inventory_import, models, pipeline
from syntherisk.modules.m03_retrosynthesis import SearchResult
from syntherisk.modules.m05_precedents import precedent_from_reaction
from tests.conftest import ASPIRIN, IBUPROFEN, TEST_PASSWORD, register, start_analysis


# ---------------------------------------------------------------- 账号、项目


def test_login_and_account_basics(client, account):
    """注册、登录、查当前用户、成员列表这几个账号接口的基本行为。

    保证的行为（接口都在 syntherisk/api/core.py）：
      - GET /health 不用登录，返回 status = ok（syntherisk/main.py 的 health）；
      - 注册的人是新课题组的组长：GET /api/auth/me（me）返回他的邮箱、课题组名和角色 owner；
      - 邮箱不分大小写：同一个邮箱换一种大小写再注册是 409，换一种大小写登录能登上（register、login）；
      - 口令不足 8 位注册不了，返回 422（api/schemas.py 的 RegisterRequest）；
      - 口令不对、令牌是乱写的，都是 401（login；api/deps.py 的 authenticate）；
      - 数据库里存的是口令的 scrypt 哈希，不是明文（syntherisk/security.py 的 hash_password）。
    怎么读：account 夹具已经用 anna@example.com 注册好了一个账号和“测试课题组”（conftest.py 的 register）。
    这里依次调各个接口看状态码和返回内容，最后直接到数据库里看 users 表存的是什么。
    """
    # 健康检查不用带令牌
    assert client.get("/health").json()["status"] == "ok"
    me = client.get("/api/auth/me", headers=account["headers"]).json()
    assert me["user"]["email"] == "anna@example.com" and me["org"]["name"] == "测试课题组" and me["role"] == "owner"

    # 同一个邮箱，只是大小写不同，再注册一次
    again = client.post("/api/auth/register", json={"email": "Anna@Example.com", "password": TEST_PASSWORD, "org_name": "x"})
    assert again.status_code == 409  # 邮箱不分大小写
    # 换一个没用过的邮箱，但口令不够 8 位：请求体没通过格式检查
    weak = client.post("/api/auth/register", json={"email": "b@example.com", "password": "short", "org_name": "x"})
    assert weak.status_code == 422

    # 登录时邮箱写成大写也可以；返回的内容里带着课题组
    login = client.post("/api/auth/login", json={"email": "ANNA@example.com", "password": TEST_PASSWORD})
    assert login.status_code == 200 and login.json()["org"]["name"] == "测试课题组"
    # 口令不对：401
    assert client.post("/api/auth/login", json={"email": "anna@example.com", "password": "wrong-password"}).status_code == 401
    # 令牌不是后端签发的：401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    # 课题组里现在只有注册的这一个人，角色是 owner
    assert [m["role"] for m in client.get("/api/org/members", headers=account["headers"]).json()] == ["owner"]

    # 密码不是明文存的
    # client.app.state.ctx 是后端的运行时上下文；session_factory() 开一个数据库会话，离开 with 时关闭。
    # users 表里现在只有一行（上面两次注册都没成功），.one() 取出它
    with client.app.state.ctx.session_factory() as session:
        stored = session.query(models.User).one().password_hash
    # 存的字符串以 “scrypt$” 开头（后面是参数、盐和哈希值），里面找不到口令原文
    assert TEST_PASSWORD not in stored and stored.startswith("scrypt$")


def test_projects_and_history(client, account):
    """项目：新建、列出、看项目历史、按项目筛选分析；别的课题组看不到，也不能往这个项目里提交分析。

    保证的行为（syntherisk/api/core.py）：
      - POST /api/projects（create_project）返回 201；GET /api/projects（list_projects）能看到注册时
        自动建的“默认项目”和新建的项目；
      - GET /api/projects/{id}/history（project_history）列出这个项目下的分析和生成过的报告；
      - GET /api/analyses?project_id=（api/analyses.py 的 list_analyses）只列这个项目下的分析；
      - 项目按课题组隔离（api/deps.py 的 load_project）：别的组查这个项目的历史是 404，
        拿这个项目的编号去提交分析也是 404。
    怎么读：account["project_id"] 是注册时自动建的“默认项目”。先再建一个项目，然后在默认项目下跑一次分析，
    看两个项目各自的历史和分析列表；最后用另一个课题组的账号试两次。
    """
    created = client.post("/api/projects", headers=account["headers"], json={"name": "布洛芬衍生物", "description": "第一批"})
    assert created.status_code == 201
    names = [project["name"] for project in client.get("/api/projects", headers=account["headers"]).json()]
    assert set(names) == {"默认项目", "布洛芬衍生物"}

    # start_analysis 把分析放在 account["project_id"]（默认项目）下面
    start_analysis(client, account)
    history = client.get(f"/api/projects/{account['project_id']}/history", headers=account["headers"]).json()
    # 默认项目的历史：一次分析（目标是布洛芬），还没有生成过报告
    assert len(history["analyses"]) == 1 and history["analyses"][0]["target"]["name"] == "布洛芬" and history["reports"] == []
    listed = client.get("/api/analyses", headers=account["headers"], params={"project_id": created.json()["id"]}).json()
    assert listed == []  # 另一个项目下没有分析

    # 另一个课题组的账号：看不到这个项目的历史
    other = register(client, email="other@example.com", org="隔壁课题组")
    assert client.get(f"/api/projects/{account['project_id']}/history", headers=other["headers"]).status_code == 404
    # 也不能把分析提交到别人的项目里：对他来说这个项目不存在
    not_mine = client.post("/api/analyses", headers=other["headers"],
                           json={"project_id": account["project_id"], "target": {"smiles": IBUPROFEN}})
    assert not_mine.status_code == 404


# ---------------------------------------------------------------- 草稿（A6）、预设（A15、D2、D3）


def test_draft_is_saved_updated_and_removed_once_the_analysis_starts(client, account):
    """A6 草稿：能新建、覆盖保存、列出；只有本人能改；用它开始分析以后草稿自动删掉。

    保证的行为（syntherisk/api/core.py）：
      - POST /api/drafts（create_draft）新建，PUT /api/drafts/{id}（save_draft）把内容整个换成新的，
        没给 project_id 时保持原来的项目；GET /api/drafts（list_drafts）只列自己的草稿；
      - 草稿是个人的：别人拿这个编号去改，得到 404（_load_draft）；
      - 开始分析时带上 draft_id，分析建好以后那份草稿被删掉（api/analyses.py 的 create_analysis）。
    怎么读：草稿的内容（payload）由前端决定，后端不检查里面有什么，这里随便放了“停在第几步”和目标分子。
    """
    payload = {"step": 1, "target": {"smiles": IBUPROFEN}}
    draft = client.post("/api/drafts", headers=account["headers"], json={"project_id": account["project_id"], "payload": payload}).json()
    # 覆盖保存：{**payload, …} 是在原来内容的基础上改 step、加 constraints，得到一个新的字典。
    # 这次没带 project_id，草稿应当还挂在原来的项目下
    updated = client.put(f"/api/drafts/{draft['id']}", headers=account["headers"],
                         json={"payload": {**payload, "step": 2, "constraints": {"complexity": "concise"}}}).json()
    assert updated["payload"]["step"] == 2 and updated["project_id"] == account["project_id"]
    # 这个账号的草稿列表里只有这一份
    assert [d["id"] for d in client.get("/api/drafts", headers=account["headers"]).json()] == [draft["id"]]

    # 另一个课题组的人去改这份草稿：404
    other = register(client, email="other@example.com", org="隔壁课题组")
    assert client.put(f"/api/drafts/{draft['id']}", headers=other["headers"], json={"payload": {}}).status_code == 404

    # 用这份草稿开始分析：请求里带 draft_id。202 表示任务已经接受
    started = client.post("/api/analyses", headers=account["headers"],
                          json={"project_id": account["project_id"], "target": {"smiles": IBUPROFEN}, "draft_id": draft["id"]})
    assert started.status_code == 202
    # 草稿已经没有了
    assert client.get("/api/drafts", headers=account["headers"]).json() == []


def test_team_presets_for_constraints_and_weights(client, account):
    """团队预设：约束预设（A15）和权重预设（D2、D3）的保存、覆盖、校验、共用和删除。

    保证的行为（syntherisk/api/core.py 的 save_preset、list_presets、delete_preset）：
      - 约束预设保存时用 Constraints 校验一遍并补全默认值，存进去的是一份完整的约束；
      - 同一个课题组里种类和名字都相同的预设只有一份，再存一次就是覆盖；
      - GET /api/presets?kind= 可以只要一种，不带 kind 两种都返回；
      - 内容不对返回 422：权重里有不认识的维度、有负数；约束里有不合法的取值；
      - 预设是全组共用的，组里别的成员也看得到；
      - DELETE 成功是 204，再删一次是 404。
    怎么读：先存一个约束预设，再把同名的权重预设存两次；然后试三种不合格的内容；
    接着由组长给组里加一个成员（POST /api/org/members，add_member），用他的账号看预设；最后删除。
    """
    constraints = {"complexity": "balanced", "unavailable_conditions": ["pressurized_gas"], "safety": {"exclude_cmr": True}}
    saved = client.post("/api/presets", headers=account["headers"],
                        json={"kind": "constraints", "name": "课题组默认", "payload": constraints, "is_default": True}).json()
    # 存进去的是补全了默认值的完整约束，套用时一次填好整页
    # 提交的内容里没有写原料来源，返回的 payload 里有，是默认的 inventory_first（库存优先 + 可购买）
    assert saved["payload"]["stock_source"] == "inventory_first" and saved["payload"]["safety"]["exclude_cmr"] is True
    assert saved["is_default"]

    # 权重预设：六个维度各给一个数。不要求加起来等于 1 或 100，评分时按它们之间的比例算
    weights = {"literature": 20, "operation": 15, "safety": 40, "availability": 15, "yield": 5, "green": 5}
    client.post("/api/presets", headers=account["headers"], json={"kind": "weights", "name": "安全优先", "payload": weights})
    # 同名再存一次（把安全的权重改成 45）：覆盖原来的那一份
    replaced = client.post("/api/presets", headers=account["headers"],
                           json={"kind": "weights", "name": "安全优先", "payload": {**weights, "safety": 45}}).json()
    assert replaced["payload"]["safety"] == 45
    # 权重预设仍然只有一个；两种预设加起来一共 2 个
    assert [p["name"] for p in client.get("/api/presets", headers=account["headers"], params={"kind": "weights"}).json()] == ["安全优先"]
    assert len(client.get("/api/presets", headers=account["headers"]).json()) == 2

    # 三种不合格的内容，都应该是 422：
    #   权重里有六个维度以外的键；权重是负数；约束的复杂度档位不是 concise / balanced / explore 之一
    for bad in (
        {"kind": "weights", "name": "x", "payload": {"颜值": 1}},
        {"kind": "weights", "name": "x", "payload": {"safety": -1}},
        {"kind": "constraints", "name": "x", "payload": {"complexity": "随便"}},
    ):
        assert client.post("/api/presets", headers=account["headers"], json=bad).status_code == 422

    # 组里的同学也能用
    # 组长（account）给同学开一个账号，同学登录以后拿到自己的令牌，用它去读约束预设
    client.post("/api/org/members", headers=account["headers"], json={"email": "mate@example.com", "password": "another-password-1"})
    mate = client.post("/api/auth/login", json={"email": "mate@example.com", "password": "another-password-1"}).json()
    mine = client.get("/api/presets", headers={"Authorization": f"Bearer {mate['access_token']}"}, params={"kind": "constraints"}).json()
    assert [p["name"] for p in mine] == ["课题组默认"]

    # 删除：第一次 204（成功，没有返回内容），第二次这个预设已经不在了，404
    assert client.delete(f"/api/presets/{saved['id']}", headers=account["headers"]).status_code == 204
    assert client.delete(f"/api/presets/{saved['id']}", headers=account["headers"]).status_code == 404


# ---------------------------------------------------------------- 配置


def test_frontend_configuration_endpoints(client, account):
    """前端启动时读的四个配置接口：评分公式的参数、约束页的选项、各模块的现状、数据来源登记表。

    保证的行为（syntherisk/api/core.py）：这些接口把 data/ 下配置文件里的内容原样交给前端，
    前端不用把权重、选项写死在自己的代码里。
      - GET /api/scoring/config（scoring_config）：B1、B8、B9、D1 在前端重算分数用的权重和系数；
      - GET /api/meta/options（constraint_options）：约束页各个选项的编号和显示文字；
      - GET /api/meta/modules（modules）：各模块现在接的是真引擎、演示数据，还是没配置；
      - GET /api/meta/data-sources（data_sources）：数据来源和许可登记表；
      - 这些接口都要登录，不带令牌是 401。
    怎么读：逐个接口取回来，和 data/scoring.toml、data/vocabulary.toml、data/data_sources.toml 里的值对。
    这里的期望值是照着这几个文件现在的内容写的：改了文件里的权重、档位或选项，这个测试要跟着改。
    """
    config = client.get("/api/scoring/config", headers=account["headers"]).json()
    # data/scoring.toml 的 [weights]：六个维度的权重，加起来是 1
    assert config["weights"] == {"literature": 0.3, "operation": 0.2, "safety": 0.2, "availability": 0.15, "yield": 0.1, "green": 0.05}
    # [route]：路线分 = 0.6 × 各步平均分 + 0.4 × 最弱一步
    assert config["route"] == {"mean_coefficient": 0.6, "weakest_coefficient": 0.4}
    # [evidence.levels.E1] 的 stars 是 3（文献直接报道，三颗星）；[complexity.concise] 的 max_steps 是 3
    assert config["evidence_levels"]["E1"]["stars"] == 3 and config["complexity"]["concise"]["max_steps"] == 3

    options = client.get("/api/meta/options", headers=account["headers"]).json()
    # A7 复杂度三档，顺序和 scoring.toml 里写的一样：简洁、平衡、探索
    assert [o["key"] for o in options["complexity"]] == ["concise", "balanced", "explore"]
    # A10 做不了的条件：data/vocabulary.toml 里 [condition.*] 的五项
    assert {o["key"] for o in options["unavailable_conditions"]} == {"pressurized_gas", "anhydrous_anaerobic", "glovebox", "cryogenic", "photo_electro"}
    # A12 规模：g 级对应的目标产量是 1000 mg（scoring.toml 的 [scale]），物料清单默认按它折算
    assert {"key": "g", "label": "g 级", "target_mg": 1000} in options["scale"]

    modules = client.get("/api/meta/modules", headers=account["headers"]).json()
    # 测试环境的配置（conftest.py 的 make_settings）：M3 用演示引擎；没有配置大模型，所以 M2 显示 offline。
    # M8 产率模型还没有接入：上下文里的产率预测器现在固定是占位实现，所以 enabled 是 False
    # （syntherisk/context.py 的 describe_modules）
    assert modules["M3"]["engine"] == "demo" and modules["M8"]["enabled"] is False and modules["M2"]["provider"] == "offline"
    # 把来源列表做成 {来源编号: 这一项} 方便取
    sources = {s["id"]: s for s in client.get("/api/meta/data-sources", headers=account["headers"]).json()}
    # data/data_sources.toml：USPTO 专利反应允许使用；学校的 Reaxys、SciFinder 账号是学术许可，不允许
    assert sources["uspto"]["allowed"] and not sources["reaxys_scifinder"]["allowed"]
    assert client.get("/api/scoring/config").status_code == 401


# ---------------------------------------------------------------- 结构接口（A1–A4、C2）


def test_structure_endpoints(client, account):
    """M1 结构工具的五个接口：校验结构、上传 MOL 文件、按名称或 CAS 号查结构、画结构图、比较两个结构。

    保证的行为（接口在 syntherisk/api/structures.py，计算在 syntherisk/modules/m01_structure.py）：
      - POST /api/structures/validate（A1 画结构、A2 粘贴 SMILES）：结构合不合法都返回 200，看 valid 字段；
        合法时给出规范 SMILES、分子式、结构图，内置试剂表里有的还给出名字；不合法时原因在 errors 里；
      - POST /api/structures/upload（A4）：上传一个 MOL 文件，得到和上面一样的校验结果；
      - GET /api/structures/lookup?q=（A3）：按 CAS 号或名称查候选结构，先查内置试剂表
        （syntherisk/modules/m01_resolver.py 的 NameResolver.lookup；测试里不联网，只有内置表）；
      - GET /api/structures/depict：返回一张 SVG 图片，不是 JSON；结构读不出来是 422；
      - POST /api/structures/diff（C2）：两个结构各自多出来的部分。
    怎么读：五个接口各调一两次，用的都是很小的分子。
    """
    headers = account["headers"]
    # 校验布洛芬：名字来自内置试剂表（data/compounds.csv），分子式是 RDKit 算的，svg 是结构图的文本
    valid = client.post("/api/structures/validate", headers=headers, json={"input": IBUPROFEN}).json()
    assert valid["valid"] and valid["name"] == "布洛芬" and valid["formula"] == "C13H18O2" and "<svg" in valid["svg"]
    # C1CC：数字 1 表示一个环，这里只开了头、没有闭合，不是合法的 SMILES。接口仍然返回 200，valid 是 false
    invalid = client.post("/api/structures/validate", headers=headers, json={"input": "C1CC"}).json()
    assert not invalid["valid"] and invalid["errors"]

    # 手写的一个 MOL 文件：两个原子（碳和氧）、一根单键，也就是甲醇。
    # MOL 文件的格式：前三行是标题和说明；第四行开头两个数是原子数和键数，行尾是版本 V2000；
    # 接着每个原子一行（三个坐标和元素符号），每根键一行（两个原子的序号和键级）；最后一行是 M  END
    molfile = "\n  test\n\n  2  1  0  0  0  0  0  0  0  0999 V2000\n" \
              "    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0\n" \
              "    1.5000    0.0000    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0\n  1  2  1  0\nM  END\n"
    # files=… 是按网页上传文件的方式（multipart/form-data）发请求：字段名 file，后面是 (文件名, 文件内容)
    uploaded = client.post("/api/structures/upload", headers=headers, files={"file": ("methanol.mol", molfile.encode())}).json()
    # 读出来的规范 SMILES 是 CO（甲醇），内置试剂表里有它的名字
    assert uploaded["valid"] and uploaded["smiles"] == "CO" and uploaded["name"] == "甲醇"

    # 按 CAS 号查：15687-27-1 是内置试剂表里布洛芬的 CAS 号
    lookup = client.get("/api/structures/lookup", headers=headers, params={"q": "15687-27-1"}).json()
    assert [c["name"] for c in lookup["candidates"]] == ["布洛芬"] and lookup["candidates"][0]["source"] == "内置试剂表"
    # 按英文名查也能查到同一个物质
    by_alias = client.get("/api/structures/lookup", headers=headers, params={"q": "ibuprofen"}).json()
    assert by_alias["candidates"][0]["cas"] == "15687-27-1"

    # 画一个苯环，指定图的宽和高。返回的是图片：Content-Type 是 image/svg+xml，内容是 SVG 文本
    picture = client.get("/api/structures/depict", headers=headers, params={"smiles": "c1ccccc1", "width": 100, "height": 80})
    assert picture.headers["content-type"].startswith("image/svg+xml") and b"<svg" in picture.content
    assert client.get("/api/structures/depict", headers=headers, params={"smiles": "C1CC"}).status_code == 422

    # 比较两个结构：query 是 1-(4-氯苯基)乙醇，reference 是 1-苯乙醇，两者只差苯环上的一个氯。
    # 所以 query 多出来的片段是 Cl，reference 没有多出来的原子
    diff = client.post("/api/structures/diff", headers=headers, json={"query": "CC(O)c1ccc(Cl)cc1", "reference": "CC(O)c1ccccc1"}).json()
    assert diff["query_only_fragments"] == ["Cl"] and diff["reference_only_atoms"] == []


def test_precedent_list_marks_structural_differences(client, account, ctx):
    """C1 文献列表、C2 相似先例：每条先例带原文链接；不是同一反应的先例标出它和这一步产物的结构差异。

    保证的行为：GET …/steps/{n}/precedents（syntherisk/api/analyses.py 的 get_step_precedents）
      - 同一反应的先例排在前面，exact 为 true，相似度是 1.0，不带 structure_diff；
      - 相似先例带 structure_diff（m01_structure.py 的 structure_diff）：query 一侧是这一步的产物，
        reference 一侧是先例的产物；
      - 原文链接：有 DOI 的给 doi.org 的地址；没有 DOI 的专利，专利号认得的给 Google Patents 的地址，
        认不出来的（比如这里现编的 TEST-0001）不给链接（syntherisk/modules/m05_precedents.py 的 document_url）；
      - 证据卡上的产率只用同一反应的报道值（m05_precedents.py 的 _yield_info）。
    怎么读：先往这个测试的先例库里放两条现编的先例，再提交阿司匹林的分析（证据等级是分析的第 4 阶段评的，
    所以先例要在分析之前放进去）。演示路线 r1 只有一步：水杨酸 + 乙酸酐 → 阿司匹林。
    然后取这一步的先例列表和证据卡。
    """
    # 两条现编的先例（文献号以 TEST- 开头）：一条和这一步完全相同，一条是氯代的类似物
    # 反应 SMILES 的写法是 “反应物>试剂>产物”，这里中间的试剂一栏是空的：
    #   TEST-0001  水杨酸 + 乙酸酐 → 阿司匹林。来源 uspto，没指定类型就算专利，2001 年，产率 90%
    #   TEST-0002  苯环上多一个氯的水杨酸 + 乙酸酐 → 多一个氯的阿司匹林。来源 ord，期刊论文，带 DOI，产率 70%
    # precedent_from_reaction 把一条反应算成先例库的一行（反应编号、指纹等），session.add 之后 commit 才入库
    with ctx.session_factory() as session:
        session.add(precedent_from_reaction(f"OC(=O)c1ccccc1O.CC(=O)OC(C)=O>>{ASPIRIN}", source="uspto",
                                            document_id="TEST-0001", year=2001, yield_pct=90))
        session.add(precedent_from_reaction("OC(=O)c1cc(Cl)ccc1O.CC(=O)OC(C)=O>>CC(=O)Oc1ccc(Cl)cc1C(=O)O", source="ord",
                                            document_id="TEST-0002", doc_type="journal", doi="10.1000/test", yield_pct=70))
        session.commit()
    analysis = start_analysis(client, account, smiles=ASPIRIN)
    listing = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1/precedents", headers=account["headers"]).json()
    # 库里有反应物、产物都相同的记录，所以这一步是 E1（文献直接报道）
    assert listing["evidence"]["level"] == "E1"
    # 列表里正好两条，解包成两个变量：同一反应的那条在前
    exact, similar = listing["precedents"]
    # TEST-0001 不是专利号的写法，拼不出能打开的链接，所以不给链接（以前会给一个打不开的 Google Patents 地址）
    assert exact["exact"] and exact["similarity"] == 1.0 and exact["url"] is None
    # 同一反应的先例产物和这一步一样，没有差异可标
    assert "structure_diff" not in exact
    # 第二条：不是同一反应；类型显示成“期刊论文”；有 DOI，所以链接是 doi.org 的地址
    assert not similar["exact"] and similar["doc_type_label"] == "期刊论文" and similar["url"] == "https://doi.org/10.1000/test"
    # 先例的产物比这一步的产物多一个氯，这一步的产物没有多出来的原子
    assert similar["structure_diff"]["reference_only_fragments"] == ["Cl"] and similar["structure_diff"]["query_only_atoms"] == []
    # 产率只用同一反应的报道值（90%），不把氯代类似物的 70% 混进来
    card = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1", headers=account["headers"]).json()
    assert card["yield"]["label"] == "文献报道 90%（1 个先例）"


def test_precedent_links_saved_by_older_analyses_are_rebuilt_when_read(client, account, ctx):
    """旧分析里存着的先例链接打不开时，打开证据卡和先例列表看到的是按专利号重新拼的链接。

    保证的行为：
      - 分析时（M5，m05_precedents.py 的 _to_dict）存进结果的链接已经是 Google Patents 能打开的写法：
        USPTO 的 US06171520B2 去掉补位的 0 和类型代码，成为 US6171520（m05_precedents.py 的 document_url）；
      - 读结果的时候不用存着的 url，按文献号重新拼（cards.py 的 build_card、build_sources 调
        m05_precedents.py 的 precedent_url）。先例列表、证据卡上的先例、卡上的来源 P1 都是这样，
        报告里的文献列表也从 build_card 取先例。
    为什么重要：旧版本把 US06171520B2 原样接在地址后面，Google Patents 上是 404；那些分析的结果还存在库里，
    重新分析之前也要能打开。
    怎么读：放一条和阿司匹林这一步完全相同的先例（专利号用 USPTO 数据里的写法），提交阿司匹林的分析；
    再把存好的结果里先例的 url 改回旧写法，冒充旧版本跑出来的分析，然后读先例列表和证据卡。
    """
    with ctx.session_factory() as session:
        session.add(precedent_from_reaction(f"OC(=O)c1ccccc1O.CC(=O)OC(C)=O>>{ASPIRIN}", source="uspto",
                                            document_id="US06171520B2", year=2001, yield_pct=90))
        session.commit()
    analysis = start_analysis(client, account, smiles=ASPIRIN)
    step = f"/api/analyses/{analysis['id']}/routes/r1/steps/1"
    fixed = "https://patents.google.com/patent/US6171520"
    # 新跑的分析：存进去的就是能打开的写法
    listing = client.get(f"{step}/precedents", headers=account["headers"]).json()
    assert [p["url"] for p in listing["precedents"]] == [fixed]

    # 冒充旧版本跑出来的分析：把结果里每条先例的 url 改成旧写法（专利号原样接在地址后面）。
    # result 是加密的 JSON 列，要整体重新赋值才会写回数据库（security.py 的 EncryptedJSON），所以先深拷贝一份再改
    with ctx.session_factory() as session:
        stored = session.get(models.Analysis, analysis["id"])
        result = copy.deepcopy(stored.result)
        for reaction in result["reactions"].values():
            for precedent in reaction["evidence"]["precedents"]:
                precedent["url"] = "https://patents.google.com/patent/US06171520B2"
        stored.result = result
        session.commit()

    # 先例列表、证据卡上的先例、卡上的来源 P1，给的都是重新拼的链接
    listing = client.get(f"{step}/precedents", headers=account["headers"]).json()
    assert [p["url"] for p in listing["precedents"]] == [fixed]
    card = client.get(step, headers=account["headers"]).json()
    assert [p["url"] for p in card["precedents"]] == [fixed]
    assert [s["url"] for s in card["sources"] if s["id"] == "P1"] == [fixed]


# ---------------------------------------------------------------- 任务：先看初步结果、取消、恢复


class HeldQueue:
    """只收任务、不执行：用来模拟“还在排队”。

    顶替的是 ctx.queue。测试里它本来是同步模式的 InProcessQueue（syntherisk/taskqueue.py）：
    接口一调 enqueue，任务当场跑完。换成 HeldQueue 以后，enqueue 只把 (任务类型, 参数) 记进 self.jobs，
    什么都不执行，于是刚提交的分析停在 queued，刚上传的大表格停在排队状态。
    之后测试自己调处理函数（pipeline.run_analysis、inventory_import.run_import），相当于“队列后来轮到它了”。
    它只有任务队列约定的两个方法 enqueue 和 close（taskqueue.py 的 TaskQueue）。
    谁在用：本文件里“排队中取消”“重启恢复”“大表格导入”三个测试。
    """

    def __init__(self):
        """从一个空的任务列表开始。jobs 里每一项是 (任务类型, 参数字典)，测试可以直接看它。"""
        self.jobs = []

    def enqueue(self, job_type, payload):
        """记下任务，不执行。真的队列会把它交给线程池、Redis，或者当场跑完。"""
        self.jobs.append((job_type, payload))

    def close(self):
        """什么都不用关。留着这个方法是因为 conftest.py 的 ctx 夹具在测试结束时会调 ctx.queue.close()。"""
        pass


def test_queued_analysis_can_be_cancelled_and_never_runs(client, account, ctx):
    """A19 取消还在排队的分析：立刻变成 cancelled；之后队列再轮到这个任务，它也不会跑。

    保证的行为：
      - 分析还没开始时，结果页和初步结果都取不到，返回 409（api/deps.py 的 load_scored_analysis、
        api/analyses.py 的 get_preliminary）；状态里的 preliminary_ready 是 false；
      - POST /api/analyses/{id}/cancel（api/analyses.py 的 cancel_analysis）对排队中的分析直接改状态；
      - 任务的处理函数 pipeline.run_analysis 开头会看状态：不是 queued 或 running 就直接返回。
    怎么读：把队列换成 HeldQueue，提交的分析就停在 queued。取消以后，手动调一次 run_analysis，
    模拟“队列稍后才轮到它”，再确认状态没有变回去。
    为什么重要：取消只改了数据库里的状态，队列里的那个任务并没有被撤回；
    处理函数不检查状态的话，用户已经取消的分析会被照样跑一遍。
    """
    ctx.queue = HeldQueue()
    queued = start_analysis(client, account)
    assert queued["status"] == "queued" and queued["preliminary_ready"] is False
    # 还没有评分结果，也还没有初步路线树：409（现在不能做这个操作）
    assert client.get(f"/api/analyses/{queued['id']}/result", headers=account["headers"]).status_code == 409
    assert client.get(f"/api/analyses/{queued['id']}/preliminary", headers=account["headers"]).status_code == 409

    cancelled = client.post(f"/api/analyses/{queued['id']}/cancel", headers=account["headers"]).json()
    assert cancelled["status"] == "cancelled"
    pipeline.run_analysis(ctx, queued["id"])  # 队列稍后才轮到它：已经取消了，不应该再跑
    assert client.get(f"/api/analyses/{queued['id']}", headers=account["headers"]).json()["status"] == "cancelled"


def test_cancel_during_search_stops_before_the_next_stage(client, account, ctx):
    """A19 取消正在跑的分析：在下一个检查点停下；已经搜到的初步路线树保留，A18 还能看。

    保证的行为（syntherisk/pipeline.py）：
      - 取消一个正在跑的分析只是在数据库里立一个标记 cancel_requested；流水线在每个阶段开始之前查它
        （AnalysisRunner._check_cancel），查到就停，状态记成 cancelled；
      - 流水线交给逆合成引擎的 should_cancel 回调（AnalysisRunner._cancelled）每次都重新查数据库，
        所以搜索途中立的标记它看得到；
      - 第 2 阶段已经存下的初步路线树不会丢：GET /api/analyses/{id}/preliminary
        （api/analyses.py 的 get_preliminary）照样返回，只是路线还没有分数。
    怎么读：测试里分析是在提交的那个请求里同步跑完的，没法同时再发一个取消请求。所以用一个假引擎
    SlowEngine：它在搜索途中直接把数据库里的 cancel_requested 改成 True（相当于用户这时点了取消），
    然后照常返回演示路线。提交以后看分析停在哪里，再取初步结果。
    """
    class SlowEngine:
        """假的逆合成引擎：搜索途中“用户点了取消”，搜索本身照常返回结果。

        顶替的是 ctx.retro_engine。真的 AiZynthFinder 引擎搜索要几十秒，其间会反复调 should_cancel() 问
        要不要停；这个替身不搜索，只做三件事：立取消标记、确认 should_cancel() 看得到它、
        把演示引擎（下面的 ctx_demo）的路线原样交回去。只在这个测试里用。
        """

        name = "aizynthfinder"

        def search(self, request, should_cancel=None):
            """和真引擎的 search 同样的参数。request 是搜索请求，should_cancel 是流水线传进来的回调。"""
            # 搜索进行到一半，用户点了取消（A19）
            # 另开一个数据库会话来改，和取消接口的做法一样（它也是另一个请求、另一个会话）。
            # 这个测试的库里只有一次分析，.one() 取到的就是它
            with ctx.session_factory() as session:
                analysis = session.query(models.Analysis).one()
                analysis.cancel_requested = True
                session.commit()
            # 流水线的回调这时应当已经看到标记了
            assert should_cancel() is True
            # 路线用演示引擎的（三条布洛芬路线），包成一个 SearchResult 返回
            return SearchResult(engine=self.name, version=None, routes=ctx_demo.search(request).routes)

    # 一行做两件事：原来的演示引擎留在 ctx_demo 里（SlowEngine.search 要用），上下文里的引擎换成 SlowEngine
    ctx_demo, ctx.retro_engine = ctx.retro_engine, SlowEngine()
    analysis = start_analysis(client, account)
    assert analysis["status"] == "cancelled"
    statuses = [stage["status"] for stage in analysis["stages"]]
    assert statuses[:3] == ["done", "done", "pending"]  # 搜索完成后、下一阶段开始前停下
    # 已经搜到的初步路线树还在，可以看
    preliminary = client.get(f"/api/analyses/{analysis['id']}/preliminary", headers=account["headers"]).json()
    # 三条路线都在；评分阶段没有跑，所以 score 是 None
    assert len(preliminary["routes"]) == 3 and preliminary["routes"][0]["score"] is None
    # 路线树的根是目标分子。第一条是 4 步的格氏路线，根下面那一步反应是最后一步，所以 step_index 是 4
    assert preliminary["routes"][0]["tree"]["name"] == "布洛芬" and preliminary["routes"][0]["tree"]["children"][0]["step_index"] == 4


def test_interrupted_analyses_are_requeued_once_after_a_restart(client, account, ctx):
    """服务重启以后：没跑完的分析重新排队一次；已经重试过、又被打断的，直接记为失败。

    保证的行为：syntherisk/pipeline.py 的 recover_interrupted 找出状态还是 queued 或 running 的分析，
    按它已经被跑过几次（attempts）分两种处理：
      - 少于 2 次：改回 queued，记一条 status: queued 事件，编号放进返回值，由调用方重新入队；
      - 已经 2 次：不再重试，记成 failed，error 里说明是服务重启时被中断的。
    正式运行时它由 syntherisk/main.py 在服务启动时调用（只有进程内队列需要：任务只存在内存里，
    进程一退出就没人管了）；这个测试直接调它。
    怎么读：用 HeldQueue 提交两次分析，两次都停在 queued。把第二次手工改成“正在跑、已经是第 2 次尝试”，
    模拟它重试途中又被打断。调 recover_interrupted，看返回值、第二次分析的状态、第一次分析的事件。
    """
    ctx.queue = HeldQueue()
    first, second = start_analysis(client, account), start_analysis(client, account)
    # 直接改数据库里的那一行。session.get(模型, 主键) 按主键取一行
    with ctx.session_factory() as session:
        stuck = session.get(models.Analysis, second["id"])
        stuck.status, stuck.attempts = "running", 2  # 已经重试过一次，又被打断
        session.commit()
    # 只有第一次分析需要重新入队
    assert pipeline.recover_interrupted(ctx) == [first["id"]]
    failed = client.get(f"/api/analyses/{second['id']}", headers=account["headers"]).json()
    assert failed["status"] == "failed" and "服务重启" in failed["error"]
    # 重新排队的那一个记了一条“新的一轮开始”的事件，进度推送据此不再重放被打断的那一轮
    # 第一次分析从来没有真正跑过，所以它的事件只有这一条。
    # session.scalars(select(模型).where(…)).all() 查出符合条件的行，每行是一个模型对象
    with ctx.session_factory() as session:
        events = session.scalars(select(models.AnalysisEvent).where(models.AnalysisEvent.analysis_id == first["id"])).all()
    assert [(event.type, event.payload) for event in events] == [("status", {"status": "queued"})]


def test_large_inventory_files_are_processed_by_the_queue(client, account, ctx, monkeypatch):
    """E1 导入库存：行数多的表格不在请求里处理，交给任务队列；处理完以后能查到三栏结果。

    保证的行为：
      - 行数超过 inventory_import.SYNC_ROW_LIMIT（正式是 200）时，POST /api/inventory/imports
        （syntherisk/api/inventory.py 的 import_inventory）只建导入记录、把 "inventory.import" 任务入队，
        立刻返回 201，status 是 queued，所有行都在 pending（还没处理）一栏；
      - 任务的处理函数 inventory_import.run_import 逐行识别，认准的进库存，处理完 status 变成 done；
      - GET /api/inventory/imports/{id}（get_import）可以看进度和各栏的行数。
    怎么读：用 monkeypatch 把上限临时改成 3，这样 5 行的小表格就算“大表格”；队列换成 HeldQueue，
    任务不会被执行。上传以后看返回值和队列里记下的任务，再手动调 run_import，模拟后台处理完了。
    """
    # monkeypatch.setattr(对象, "属性名", 新值)：临时改掉这个属性，测试结束时自动改回原值
    monkeypatch.setattr(inventory_import, "SYNC_ROW_LIMIT", 3)
    ctx.queue = HeldQueue()
    # 一张只有“名称”一列的 CSV：表头下面 5 行。前四个是内置试剂表里的正式中文名，最后一个什么都不是
    rows = "名称\n" + "\n".join(["乙醚", "甲苯", "丙酮", "乙酸乙酯", "看不懂的东西"]) + "\n"
    response = client.post("/api/inventory/imports", headers=account["headers"], files={"file": ("大表.csv", rows.encode("utf-8"))})
    pending = response.json()
    assert response.status_code == 201 and pending["status"] == "queued" and pending["counts"]["pending"] == 5
    # 队列里记下了一个任务：类型是 inventory.import，参数里只有这次导入的编号
    assert ctx.queue.jobs == [("inventory.import", {"import_id": pending["id"]})]

    inventory_import.run_import(ctx, pending["id"])  # Worker 取到任务
    done = client.get(f"/api/inventory/imports/{pending['id']}", headers=account["headers"]).json()
    assert done["status"] == "done" and done["processed_rows"] == 5
    # 四个认得出来的进了“已匹配”，“看不懂的东西”进了“未识别”
    assert done["counts"] == {"matched": 4, "needs_confirmation": 0, "unrecognized": 1, "ignored": 0, "pending": 0}


# ---------------------------------------------------------------- 命令行


def test_command_line_tools(client, account, ctx, monkeypatch, capsys, tmp_path):
    """命令行工具（python -m syntherisk.cli …）的五条命令：status、seed-demo、load-precedents、calibrate、
    export-consented。

    保证的行为（syntherisk/cli.py）：
      - status（cmd_status）打印一段 JSON：各模块现在接的是什么，库里有多少数据；
      - seed-demo（cmd_seed_demo）没配演示账号的邮箱和口令时拒绝运行；配了以后建课题组、账号、项目，
        往库存里放 8 种试剂（cli.py 的 DEMO_INVENTORY）；再运行一次不会重复建；
      - load-precedents（cmd_load_precedents）拒绝登记为不允许使用的数据来源（syntherisk/loaders.py 的
        _check_source），以一段给人看的中文说明退出；
      - calibrate（cmd_calibrate）和 export-consented（cmd_export_consented）在没有任何授权记录时
        给出“0 条”的结果，而不是出错。
    怎么读：cli.main(参数列表) 就是命令行入口，测试里直接调它，参数列表相当于命令行上敲的那几个词。
    cli.main 本来会自己建一份运行时上下文（读 .env、连正式的数据库）；第一行用 monkeypatch 把
    cli.build_context 换成“直接返回这个测试的 ctx”，命令操作的就是临时的测试库。
    命令的输出是打印到屏幕上的，用 capsys 截下来看。
    """
    # lambda: ctx 是一个不带参数、调用时返回 ctx 的小函数
    monkeypatch.setattr(cli, "build_context", lambda: ctx)
    cli.main(["status"])
    # readouterr() 取出到目前为止截到的输出（同时清空），.out 是正常输出的那一部分
    status = json.loads(capsys.readouterr().out)
    # 在售目录里来源是 seed 的条目：建上下文时从内置试剂表写进去的可购买物质（syntherisk/db.py 的 init_db）。
    # data/compounds.csv 现在有一百多行，都标着可购买
    assert status["modules"]["M3"]["engine"] == "demo" and status["data"]["catalog"]["seed"] > 100

    # pytest.raises(异常类型, match=文字)：with 里面的代码必须抛出这种异常，并且异常的说明里找得到这段文字
    # （按正则表达式查找），否则测试失败。命令主动退出时抛的是 SystemExit。
    # 这时还没有配置演示账号，seed-demo 应当拒绝运行，并提示要设置 DEMO_USER_EMAIL
    with pytest.raises(SystemExit, match="DEMO_USER_EMAIL"):
        cli.main(["seed-demo"])
    # 正式运行时这两项写在 .env 里；这里直接改上下文里的配置对象
    ctx.settings.demo_user_email, ctx.settings.demo_user_password = "demo@example.com", TEST_PASSWORD
    cli.main(["seed-demo"])
    assert "库存里放了 8 种试剂" in capsys.readouterr().out
    # 再运行一次：账号已经在了，只提示一句，不重复建
    cli.main(["seed-demo"])
    assert "已经存在" in capsys.readouterr().out
    # 用演示账号走接口登录，确认它真的能用，库存里是 8 种
    demo = client.post("/api/auth/login", json={"email": "demo@example.com", "password": TEST_PASSWORD}).json()
    headers = {"Authorization": f"Bearer {demo['access_token']}"}
    assert client.get("/api/inventory/summary", headers=headers).json() == {"count": 8}

    # reaxys_scifinder 在 data/data_sources.toml 里登记的是不允许使用（学术许可）。“没登记”和“登记为不允许”
    # 报的是同一句话，里面有“没有登记”这几个字。
    # 来源的检查排在文件的检查前面，所以这里给一个并不存在的文件路径也没关系
    with pytest.raises(SystemExit, match="没有登记"):
        cli.main(["load-precedents", "--source", "reaxys_scifinder", "--file", str(tmp_path / "x.rsmi")])

    # 校准统计：这个库里没有任何授权过的实验回填记录，用到的记录数是 0
    cli.main(["calibrate"])
    assert json.loads(capsys.readouterr().out)["n_records"] == 0
    # 导出已授权的匿名数据：写出来的文件是一个空列表
    output = tmp_path / "consented.json"
    cli.main(["export-consented", "--output", str(output)])
    assert json.loads(output.read_text(encoding="utf-8")) == []
