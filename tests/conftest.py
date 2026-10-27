"""测试共用的准备工作。

每个测试用自己的临时 SQLite 库；任务队列用同步模式（提交分析时当场跑完）；
不联网（PubChem、OPSIN 关掉），不调大模型，逆合成用内置演示路线。

---- 这个文件是什么 ----

conftest.py 是 pytest 约定的文件名：放在 tests/ 目录下，pytest 在收集测试之前会自动导入它。
里面用 @pytest.fixture 定义的“夹具”对 tests/ 下所有的测试文件都可用，不需要 import。

夹具是“测试开始前准备好、测试结束后收拾掉”的东西。测试函数只要把夹具的名字写在参数里，
比如 def test_xxx(client, account)，pytest 就会先把这两样建好，再调测试函数；
夹具的参数里也可以写别的夹具，pytest 会按依赖的先后一个一个建。每个测试拿到的都是新建的一份。

本文件里另外几个普通的函数和常量（make_settings、register、start_analysis、IBUPROFEN……）不是夹具，
别的测试文件用 from tests.conftest import … 引入。

---- 测试环境和正式运行有什么不同（都由 make_settings 定下来）----

    数据库    每个测试一个全新的 SQLite 文件，建在 pytest 给的临时目录里。测试之间互不影响，
              也碰不到 var/ 下正式的数据库。
    任务队列  同步模式（QUEUE_BACKEND=sync）：POST /api/analyses 返回的时候，八个阶段已经当场跑完了，
              所以测试里提交完分析马上就能查结果，不用等，也不用轮询。
    外部接口  PubChem、OPSIN 关掉（ctx.pubchem、ctx.opsin 是 None），测试不发网络请求。
              后果是：内置试剂表（data/compounds.csv）以外的物质没有危害数据，按名称也只查得到表里的。
    大模型    不调（LLM_PROVIDER=offline）：一句话要求按关键词规则解析，说明按模板生成。
    逆合成    内置演示引擎（M3_ENGINE=demo）：只有布洛芬和阿司匹林两个分子的路线（data/demo_routes/）。
    先例库    空的（新库里 reaction_precedents 表没有数据）：证据等级是“未评估”，文献依据这一维不计分。
    配置文件  不读项目根目录的 .env（_env_file=None），所以这台电脑上的 .env 不影响测试。
              没有在 make_settings 里明确指定的配置项，仍然会读操作系统的环境变量。
    密钥      APP_ENV=test：两把密钥每次临时随机生成，只留在内存里，不写 var/dev_secrets.json。

要测“接上了 PubChem / AiZynthFinder / 大模型 / Redis”的情形时，由各个测试自己把对应的部件换成替身：
直接给 ctx.retro_engine、ctx.pubchem、ctx.llm、ctx.queue 赋值，或者调 build_context 时传 pubchem=…。

---- 夹具之间的关系 ----

    tmp_path（pytest 自带）    每个测试一个临时目录
      └─ ctx                   一份完整的运行时上下文（AppContext），数据库就在这个临时目录里
           └─ client           建在这份上下文上的 FastAPI 应用，和调它的测试客户端
                └─ account     通过注册接口建好的一个账号：登录用的请求头、默认项目的编号

    同一个测试里，参数 ctx 和 client 背后用的是同一个上下文对象。
    所以测试里改了 ctx（比如换掉逆合成引擎）以后，再通过 client 调接口，用的就是改过的。

---- 本文件里有什么 ----

    IBUPROFEN、ASPIRIN   两个目标分子的 SMILES（演示引擎只有这两个分子的路线）
    TEST_PASSWORD        测试账号统一用的口令
    make_settings        建一份测试用的配置
    ctx                  夹具：运行时上下文
    client               夹具：测试客户端
    register             通过接口注册一个账号（同时建课题组和默认项目），并替这个账号同意内测协议
    accept_agreement     替一个账号同意当前版本的内测协议
    account              夹具：一个注册好的账号
    start_analysis       通过接口提交一次分析
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from syntherisk.config import load_settings
from syntherisk.context import AppContext, build_context
from syntherisk.main import create_app
from syntherisk.taskqueue import build_queue

# 两个目标分子的 SMILES（SMILES 是用一行文字写一个分子的写法）。内置演示引擎只有这两个分子的路线，
# 所以要走完整条分析流程的测试都拿它们当目标分子。别的测试文件也从这里引入。
#
# 布洛芬。data/demo_routes/ibuprofen.json 里有三条路线：格氏羧基化（4 步）、Boots（6 步）、BHC（3 步）。
# 三条路线的第 1 步都是由异丁基苯和乙酸酐做 4-异丁基苯乙酮。
# 这个写法没有标明手性碳的构型，所以结构校验会提示“1 个立体中心未指定，按外消旋处理；……”。
IBUPROFEN = "CC(C)Cc1ccc(cc1)C(C)C(=O)O"
# 阿司匹林（乙酰水杨酸）。data/demo_routes/aspirin.json 里有三条路线：
# 乙酸酐酰化（1 步）、乙酰氯酰化（1 步）、从苯酚出发（Kolbe–Schmitt，2 步）。
ASPIRIN = "CC(=O)Oc1ccccc1C(=O)O"
# 注册测试账号、登录时统一用的口令：一个满足注册接口“至少 8 位”要求的固定字符串。
# 别的测试文件登录、核对“数据库里存的不是口令原文”时也引用这个常量。
TEST_PASSWORD = "test-password-123"  # 只存在于临时测试库里的账号


def make_settings(tmp_path, **overrides):
    """建一份测试用的配置（syntherisk/config.py 的 Settings）：数据都放在临时目录里，外部依赖全部关掉。

    顶替的是正式运行时“读环境变量和 .env 得到的配置”。
    参数：
        tmp_path   pytest 自带的 tmp_path 夹具给的临时目录（每个测试一个）。
                   数据库文件是 <tmp_path>/test.db，运行时数据（报告、附件等）放在 <tmp_path>/var 下面。
        overrides  要另外指定的配置项，写成 名字=值，名字是 Settings 的字段名。和下面的默认值重名时以它为准。
                   例：make_settings(tmp_path, m3_engine="auto", aizynth_config="config.yml")。
    返回：
        Settings 对象，两把密钥已经补齐（load_settings 的返回值）。
    副作用：
        建 <tmp_path>/var 目录（load_settings 做的）。数据库文件要到 build_context 时才建。
    谁在用：
        本文件的 ctx 夹具；tests/test_integrations.py 里自己建上下文、或者只测“按配置选引擎”的几个测试。
    """
    # 下面每一项对应 Settings 的一个字段（也就是 .env 里同名的大写变量），含义见 syntherisk/config.py：
    #   _env_file=None           这一次不读项目根目录的 .env（这是 pydantic-settings 的特殊参数，不是配置项）
    #   app_env="test"           测试环境：没配密钥就临时随机生成，只留在内存里
    #   database_url             SQLite 文件库的连接串；as_posix() 把 Windows 路径里的反斜杠换成正斜杠
    #   var_dir                  运行时数据的目录，指到临时目录下面，不碰项目里的 var/
    #   queue_backend="sync"     任务入队时当场跑完再返回（taskqueue.InProcessQueue 的同步模式）
    #   enable_pubchem / enable_opsin = False   不建这两个外部接口的客户端
    #   llm_provider="offline"   不调大模型
    #   m3_engine="demo"         逆合成用内置演示引擎，不去找 AiZynthFinder
    options = dict(
        _env_file=None,
        app_env="test",
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        var_dir=tmp_path / "var",
        queue_backend="sync",
        enable_pubchem=False,
        enable_opsin=False,
        llm_provider="offline",
        m3_engine="demo",
    )
    # 调用方指定的项盖掉上面的默认值
    options.update(overrides)
    # **options 把字典展开成关键字参数。load_settings 用这些值建 Settings（优先于环境变量）、建 var 目录、补密钥
    return load_settings(**options)


@pytest.fixture
def ctx(tmp_path) -> AppContext:
    """夹具：一份完整的运行时上下文（syntherisk/context.py 的 AppContext），测试结束时自动收拾。

    顶替的是正式运行时由 syntherisk/main.py 的 lifespan（API 进程）或 worker.py（Worker 进程）建的那一份。
    和正式的相比：
        数据库      这个测试独有的临时 SQLite 库。表已经建好，在售目录里已经写入内置试剂表里可购买的物质
                    （和正式启动时一样，db.init_db 做的），其余的表都是空的
        外部客户端  ctx.pubchem、ctx.opsin 是 None（make_settings 里关掉的）；ctx.literature 也是 None
                    （补文献题目的开关 ENABLE_LITERATURE_METADATA 默认是关的，make_settings 没有另外指定）
        大模型      没有配置，ctx.llm.enabled 是 False
        逆合成引擎  ctx.retro_engine 是演示引擎（m03_retrosynthesis.DemoEngine）
        任务队列    ctx.queue 是同步模式的进程内队列
        参考数据    ctx.reference 读的是项目里真实的 data/ 目录，和正式运行时一样
    怎么用：测试函数的参数里写 ctx 就能拿到它。常见的三种用法：
        直接调后端的函数，比如 loaders.load_precedents(ctx, …)、cli.cmd_check_hazards(ctx, …)；
        用 with ctx.session_factory() as session 开一个数据库会话，往库里放数据，或者查库核对结果；
        换掉其中的部件来模拟别的环境，比如 ctx.retro_engine = 假引擎、ctx.queue = Redis 队列。
    写法说明：
        @pytest.fixture 把这个函数登记成夹具。函数里用的是 yield 而不是 return：
        yield 之前的代码在测试开始前运行；yield 出去的值就是测试拿到的 ctx；
        yield 之后的代码在测试结束后运行（测试通过还是失败都会运行），用来收尾。
        参数 tmp_path 也是夹具（pytest 自带），pytest 会先建好临时目录再调这个函数。
    谁在用：
        本文件的 client 夹具；各个测试文件里参数带 ctx 的测试。
    """
    # build_context：读 data/ 下的参考数据、连数据库、建表、往在售目录里写入内置在售试剂、按配置选引擎
    context = build_context(make_settings(tmp_path))
    # 队列要拿着上下文才能执行任务，所以上下文建好以后再建队列、挂上去（正式运行时这一步由 lifespan 做）。
    # 配置里是 sync，得到的是同步模式的 InProcessQueue
    context.queue = build_queue(context)
    yield context
    # 收尾：关队列；dispose() 关掉连接池里的全部数据库连接，不再占着临时目录里的数据库文件
    context.queue.close()
    context.engine.dispose()


@pytest.fixture
def client(ctx) -> TestClient:
    """夹具：FastAPI 的测试客户端，用来像前端一样调接口。

    顶替的是“真的起一个 uvicorn 服务，再用浏览器或者 HTTP 库去请求它”。TestClient 在进程内直接调用应用，
    不占端口、不走网络，用法和 httpx 一样：
        client.get("/api/projects", headers=…)、client.post("/api/analyses", headers=…, json=…)；
        返回的 response 上有 status_code（状态码）、json()（解析好的 JSON）、text（原文）。
        client.stream("GET", …) 用来读进度推送（SSE）这种一段一段发回来的响应。
    应用是 create_app(ctx) 建的（syntherisk/main.py）：传了 ctx，应用就用这一份上下文，不再自己读配置、连数据库。
    所以通过 client 调接口时，读写的就是 ctx 夹具那个临时数据库。
    写法说明：
        with TestClient(...) as test_client：进入 with 时走一遍应用的启动流程（lifespan：把 ctx 放到 app.state 上），
        离开 with 时走关闭流程（关队列）。yield 写在 with 里面，所以整个测试期间应用都是“已启动”的状态。
    注意：
        接口地址都以 /api 开头。除了注册和登录，每个请求都要带登录的请求头：headers=account["headers"]。
    谁在用：
        本文件的 account 夹具；各个测试文件里参数带 client 的测试。
    """
    with TestClient(create_app(ctx)) as test_client:
        yield test_client


def register(client: TestClient, username: str = "anna@example.com", org: str = "测试课题组") -> dict:
    """通过注册接口建一个账号（同时建一个课题组和它的“默认项目”），返回后面调接口要用的东西。

    走的是真实的接口 POST /api/auth/register（syntherisk/api/core.py 的 register），不是直接往数据库里写，
    所以得到的令牌、课题组、项目，和前端注册以后拿到的一样。
    参数：
        client  测试客户端。
        username  登录用户名。同一个测试里需要第二个账号时（比如验证“别的课题组看不到我的数据”），换一个再调一次。
        org     课题组的名字。每注册一次就新建一个课题组，注册的人是组长（owner）。
    返回一个字典：
        headers      {"Authorization": "Bearer <令牌>"}。之后每个要登录的请求都带上它：headers=account["headers"]
        project_id   这个课题组的“默认项目”的编号。提交分析时必须指明放在哪个项目下
        其余的键     注册接口原样返回的内容：access_token、token_type、user（含 id）、org（含 id）、role
    注册没成功（状态码不是 201）时，下面的 assert 直接让测试失败，并显示接口返回的内容。
    谁在用：
        本文件的 account 夹具；需要第二个课题组的测试直接调它（tests/test_inventory.py、test_analysis_flow.py、
        test_app_service.py、test_reports_and_backfill.py）。
    """
    # 请求体见 api/schemas.py 的 RegisterRequest：display_name 是显示的名字，org_name 是要新建的课题组的名字
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": TEST_PASSWORD, "display_name": "测试", "org_name": org},
    )
    # assert 条件, 说明：条件不成立时，逗号后面的内容会显示在失败信息里。这里显示的是接口返回的原文
    assert response.status_code == 201, response.text
    data = response.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    # 同意内测协议：不同意的话，除 /api/auth/me 和协议的几个接口以外都返回 403
    accept_agreement(client, headers)
    # 注册接口会自动建一个“默认项目”。这时项目列表里只有它，取第一个
    # 最后一行的 **data 把注册接口返回的各项原样并进要返回的字典
    project = client.get("/api/projects", headers=headers).json()[0]
    return {"headers": headers, "project_id": project["id"], **data}


def accept_agreement(client: TestClient, headers: dict) -> dict:
    """替这个账号同意当前版本的内测协议，返回同意记录（POST /api/agreement/accept 的返回）。

    和前端一样：先 GET /api/agreement 拿版本、语言、哈希，再把每一个勾选项都勾上去同意。
    参数：
        client   测试客户端。
        headers  这个账号的登录请求头。
    谁在用：
        本文件的 register（注册的人）；管理员加的成员、seed-demo 的演示账号登录以后，测试里自己调一次
        （tests/test_app_service.py、test_inventory.py）。
    """
    current = client.get("/api/agreement", headers=headers).json()
    response = client.post("/api/agreement/accept", headers=headers, json={
        "version": current["version"], "language": current["language"], "sha256": current["sha256"],
        "checked": [box["key"] for box in current["content"]["checkboxes"]],
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def account(client) -> dict:
    """夹具：一个已经注册好的账号（邮箱 anna@example.com，课题组“测试课题组”，角色是组长）。

    就是 register(client) 的返回值，各个键见 register 的说明。最常用的两个：
        account["headers"]      调接口时的登录请求头
        account["project_id"]   默认项目的编号
    每个测试拿到的都是在它自己那个临时数据库里新注册的账号。
    谁在用：通过接口测试的函数，参数一般写成 (client, account) 或 (client, account, ctx)。
    """
    return register(client)


def start_analysis(client: TestClient, account: dict, smiles: str = IBUPROFEN, constraints: dict | None = None) -> dict:
    """提交一次分析（界面上的 A16“开始分析”），返回接口给出的分析状态。

    走的是 POST /api/analyses（syntherisk/api/analyses.py 的 create_analysis）。
    测试用的是同步队列，所以这个函数返回的时候八个阶段已经跑完了：返回值里的 status 已经是 succeeded
    （或者 failed），stages 里是每个阶段的结果说明。只有测试自己把 ctx.queue 换成了别的队列时，
    返回的才是刚入队的 queued 状态。
    参数：
        client       测试客户端。
        account      account 夹具或 register() 的返回值：用它的登录请求头和默认项目。
        smiles       目标分子，默认是布洛芬。演示引擎只有布洛芬和阿司匹林的路线；
                     传别的分子时分析照样以 succeeded 结束，只是一条路线都没有，第 3–8 阶段是 skipped。
        constraints  约束，也就是新建分析第 2 步的那些选项，键见 syntherisk/constraints.py 的 Constraints。
                     例：{"complexity": "explore"}、{"stock_source": "inventory_only"}。
                     不传就全部用默认值：复杂度“平衡”、原料来源“库存优先”，不勾任何“做不了的条件”和安全要求。
    返回：
        分析的当前状态，字段同 GET /api/analyses/{id}（api/views.py 的 analysis_view）。常用的有：
        id（后面的接口都用它）、status、error、current_stage、
        stages（八个阶段的列表，下标从 0 开始：stages[1] 是第 2 阶段）。
    接口没有返回 202 时，下面的 assert 让测试失败，并显示接口返回的内容。
    谁在用：几乎所有需要“先有一次分析”的测试。
    """
    # 目标分子只给 SMILES；constraints or {}：没传约束时发一个空字典，由接口补全默认值
    response = client.post(
        "/api/analyses",
        headers=account["headers"],
        json={"project_id": account["project_id"], "target": {"smiles": smiles}, "constraints": constraints or {}},
    )
    assert response.status_code == 202, response.text
    return response.json()
