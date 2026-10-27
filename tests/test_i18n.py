"""中英文切换：请求带 ?lang=en（或请求头 X-Language: en）时返回英文，不带时和以前一样是中文。

这个文件测的是什么
    后端产出的文字（标签、原因、进度说明、报错、模板说明）默认是中文。机制在 syntherisk/i18n.py：
    结果里存的始终是中文，每一处拼文字的地方用 i18n.tr("中文", "English") 记下一句“中文 → 英文”的对照；
    返回之前，syntherisk/main.py 的 language 中间件看这次请求要不要英文，要的话把返回的 JSON 走一遍
    i18n.localize()，凡是和对照表里某句中文完全相同的字符串都换成英文。
    这里全部是走接口的端到端测试，看的是前端实际拿到的内容：
      - 不带 lang：一字不差还是中文；
      - 带 lang=en：一次完整的布洛芬演示分析，各个接口返回的 JSON 里不应该再有汉字
        （结构图 svg 和用户自己输入的文字除外）；
      - 进度推送（SSE）、重新评分以后新拼的句子、各种 4xx 报错，同样跟着语言走。

涉及的接口
    语言怎么传           查询参数 ?lang=en，或请求头 X-Language: en（main.py 的 language 中间件）
    项目列表             GET    /api/projects                                   core.list_projects
    评分配置、选项       GET    /api/scoring/config、/api/meta/options、/api/meta/modules
    开始分析、当前状态   POST   /api/analyses、GET /api/analyses/{id}            analyses.create_analysis、get_analysis
    进度推送             GET    /api/analyses/{id}/events                        analyses.stream_events
    初步结果、结果页     GET    /api/analyses/{id}/preliminary、/result          analyses.get_preliminary、get_result
    路线详情、证据卡     GET    /api/analyses/{id}/routes/{route_id}[/steps/{n}] analyses.get_route、get_step_card
    先例列表、问一问     GET    …/steps/{n}/precedents、POST …/steps/{n}/ask
    对比、放宽条件       POST   /api/analyses/{id}/compare、/relax               reports.compare、analyses.relax_constraints
    排除                 POST   /api/analyses/{id}/exclusions                    analyses.add_exclusion
    重跑                 POST   /api/analyses/{id}/retry                         analyses.retry_analysis

共用的夹具和辅助函数（都在 tests/conftest.py）
    client、account、ctx   测试客户端、注册好的账号、运行时上下文
    start_analysis()       提交一次分析；测试用同步队列，返回时八个阶段已经跑完
    测试环境：不联网、不调大模型（说明按模板生成）、逆合成用演示引擎、先例库是空的。

本文件里有什么
    HAN、EN                       判断有没有汉字的正则；英文请求用的查询参数
    USER_TEXT                     测试里“用户自己输入”的文字，检查剩余汉字时不算
    _han_left()                   把一份返回的 JSON 走一遍，列出里面还带汉字的字符串
    _events()                     读进度推送，返回 [(事件类型, 内容), …]
    test_default_stays_chinese_and_lang_en_or_header_gives_english
        不带 lang 是中文；?lang=en、X-Language: en 都是英文；查询参数优先于请求头
    test_english_ibuprofen_analysis_has_no_chinese_left
        布洛芬演示分析的各个接口用 ?lang=en 取回来，JSON 里没有汉字
    test_progress_events_follow_the_language
        进度推送带 ?lang=en 时进度说明是英文；不带时事件的格式和以前一样（没有 message_en）
    test_exclusion_reason_written_at_rescoring_is_english_too
        排除一个试剂（重新评分）以后，新拼出来的排除原因也有英文，而且随结果保存下来了
    test_errors_follow_the_language
        英文请求的 401、404、409、422 报错是英文；不带 lang 时还是原来的中文
    test_french_is_translated_from_the_english
        lang=fr：项目名、评分说明、报错是法文；按模板翻的句子（参数挨在一起的）切得对
    test_every_english_text_has_french
        代码里每一句 tr() 的英文在法文对照表 syntherisk/locales/fr.json 里都有，参数写法一致
"""

import ast
import json
import re
import string
from pathlib import Path

import pytest

from syntherisk import i18n
from tests.conftest import IBUPROFEN, start_analysis

# 有没有汉字：和 syntherisk/i18n.py 里的 _HAN 是同一个范围（CJK 统一表意文字的基本区）
HAN = re.compile(r"[一-鿿]")
# 要英文的请求统一带这个查询参数
EN = {"lang": "en"}
# 测试里“用户自己输入”的文字：问一问的两个问题。它们会原样出现在返回里（比如回答里带着问题），
# 不是后端产出的文字，检查剩余汉字时跳过以它们开头的字符串
USER_TEXT = ("操作上要注意什么？", "机理是什么？")


def _han_left(where: str, value, path: str = "") -> list[str]:
    """把一份返回的 JSON 整个走一遍，列出里面还带汉字的字符串。

    参数：
        where  这份数据是哪个接口返回的，只用来写进结果里方便定位。
        value  解析好的 JSON：字典、列表、字符串、数字……
        path   当前走到的字段路径，递归时一层层拼出来，最外层调用时不用传。
    返回：
        字符串的列表，每项形如 “接口 字段路径 | 那段文字”。一个汉字都不剩时是空列表。
    不算在内的：
        字段路径里带 svg 的（结构图是 RDKit 画的 SVG 文本，不是给人读的文字）；
        USER_TEXT 里的用户输入。
    谁在用：本文件的 test_english_ibuprofen_analysis_has_no_chinese_left。
    """
    if isinstance(value, str):
        if HAN.search(value) and "svg" not in path and not value.startswith(USER_TEXT):
            return [f"{where} {path} | {value[:80]}"]
        return []
    if isinstance(value, list):
        # 列表里的每一项路径都写成 []：同一个字段的问题只按字段归类，不区分是第几项
        return [found for item in value for found in _han_left(where, item, f"{path}[]")]
    if isinstance(value, dict):
        return [found for key, item in value.items() for found in _han_left(where, item, f"{path}.{key}")]
    return []


def _events(client, account, analysis_id, **params):
    """连上进度推送，读到结束为止；返回带编号的事件 [(事件类型, 内容), …]（最后那条 end 没有编号，不算）。

    调的是 GET /api/analyses/{id}/events（syntherisk/api/analyses.py 的 stream_events），返回的是
    Server-Sent Events：每个事件三行（id、event、data），事件之间隔一个空行；data 那一行是一段 JSON。
    参数：
        **params  原样当作查询参数，比如 lang="en"。
    测试里分析在提交的时候就已经跑完，服务端把存着的事件发完、再发一条 end 就结束，不会一直等下去。
    谁在用：本文件的 test_progress_events_follow_the_language。
    """
    with client.stream("GET", f"/api/analyses/{analysis_id}/events", headers=account["headers"], params=params) as stream:
        text = "".join(stream.iter_text())
    # 按空行切成一个个事件，只留以 “id: ” 开头的；每个事件再按行切开，得到 [id 行, event 行, data 行]
    blocks = [block.split("\n") for block in text.split("\n\n") if block.startswith("id: ")]
    return [(lines[1].removeprefix("event: "), json.loads(lines[2].removeprefix("data: "))) for lines in blocks]


def test_default_stays_chinese_and_lang_en_or_header_gives_english(client, account):
    """不带 lang 时返回的是原来的中文；带 ?lang=en 或请求头 X-Language: en 时是英文。

    拿三处文字来看，它们分别是三种来源：
      - 注册时自动建的“默认项目”的名字（存在数据库里的是中文，返回时才换）；
      - 评分配置里的公式说明（接口函数里现拼的固定文字）；
      - 结果页的排序方式（模块级的常量字典 views.SORT_OPTIONS）。
    """
    headers = account["headers"]

    # ---- 不带 lang：和以前一样是中文 ----
    assert client.get("/api/projects", headers=headers).json()[0]["name"] == "默认项目"
    config = client.get("/api/scoring/config", headers=headers).json()
    assert config["formula"]["route"] == "路线分 = mean_coefficient × 各步平均分 + weakest_coefficient × 最弱一步"
    assert config["status"] == "权重和系数都是初稿，待用历史案例回测和化学家盲评校准"
    # 明确写 lang=zh，或者写了一个不认识的语言，也都是中文
    assert client.get("/api/projects", headers=headers, params={"lang": "zh"}).json()[0]["name"] == "默认项目"
    assert client.get("/api/projects", headers=headers, params={"lang": "de"}).json()[0]["name"] == "默认项目"

    # ---- 查询参数 ?lang=en ----
    assert client.get("/api/projects", headers=headers, params=EN).json()[0]["name"] == "Default project"
    config = client.get("/api/scoring/config", headers=headers, params=EN).json()
    assert config["formula"]["route"] == "Route score = mean_coefficient × mean step score + weakest_coefficient × weakest step"
    assert not HAN.search(config["formula"]["step"]) and not HAN.search(config["status"])
    # en-US 这样带地区的写法也算英文（i18n.normalize_lang）
    assert client.get("/api/projects", headers=headers, params={"lang": "en-US"}).json()[0]["name"] == "Default project"

    # ---- 请求头 X-Language: en ----
    english = {**headers, "X-Language": "en"}
    assert client.get("/api/projects", headers=english).json()[0]["name"] == "Default project"
    # 两个都带时以查询参数为准：请求头说英文、网址里说中文，返回中文
    assert client.get("/api/projects", headers=english, params={"lang": "zh"}).json()[0]["name"] == "默认项目"

    # ---- 结果页的排序方式：同一次分析，两种语言各取一次 ----
    analysis = start_analysis(client, account)
    chinese = client.get(f"/api/analyses/{analysis['id']}/result", headers=headers).json()
    assert chinese["sort_options"] == {"score": "综合", "steps": "最短", "safety": "最安全", "availability": "原料最好拿",
                                       "literature": "文献最充分", "green": "最绿色"}
    # 先例库是空的，所以卡片上概括文献依据的那句话是“证据等级未评估”（views._evidence_text）
    assert chinese["routes"][0]["evidence_text"] == "证据等级未评估"
    assert chinese["routes"][0]["title"].startswith("路线 1 · ")
    translated = client.get(f"/api/analyses/{analysis['id']}/result", headers=headers, params=EN).json()
    assert translated["sort_options"]["score"] == "Overall" and translated["sort_options"]["green"] == "Greenest"
    assert translated["routes"][0]["evidence_text"] == "Evidence level not assessed"
    assert translated["routes"][0]["title"].startswith("Route 1 · ")
    # 目标分子的立体化学提示带数字，英文是提交时存在 target 里的（messages_en）：
    # 中文返回里没有这个键，格式和以前一样；英文返回里它顶替了 messages，自己也不出现
    status = client.get(f"/api/analyses/{analysis['id']}", headers=headers).json()
    assert status["target"]["messages"] == ["1 个立体中心未指定，按外消旋处理；要的是某一个立体异构体的话请补上构型，不然搜索认不出带构型的原料"]
    assert set(status["target"]) == {"smiles", "key14", "name", "formula", "mol_weight", "messages"}
    status = client.get(f"/api/analyses/{analysis['id']}", headers=headers, params=EN).json()
    assert len(status["target"]["messages"]) == 1 and not HAN.search(status["target"]["messages"][0])
    assert "messages_en" not in status["target"]
    # 只换文字，不换数据：两种语言下路线的编号、分数、名次完全一样
    assert [(r["id"], r["score"], r["rank"]) for r in translated["routes"]] == \
           [(r["id"], r["score"], r["rank"]) for r in chinese["routes"]]
    # 英文的返回里不带存着的对照表（result["_en"] 只在后端用）
    assert "_en" not in translated

    # ---- 接口文档页顶部说明了这个参数（不是必须的，所以没有逐个接口列出来）----
    assert "lang=en" in client.get("/openapi.json").json()["info"]["description"]


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_english_ibuprofen_analysis_has_no_chinese_left(client, account, lang):
    """跑一次布洛芬的演示分析，各个接口用 ?lang=en（和 ?lang=fr）取回来，返回的 JSON 里不应该再有汉字。

    约束选的是“简洁档、只用库存、做不了加压和无水无氧、排除剧毒 / CMR / 管制、不用钯、尽量不过柱”：
    这样三条路线里有被排除的，排除原因、约束标签、软偏好这些文字都会出现。
    走到的接口：提交分析（返回当前状态）、当前状态、初步结果、结果页、每条路线的详情、每一步的证据卡、
    先例列表、问一问、物料清单、对比、放宽条件，以及几种报错。
    不算在内的：svg（结构图）和用户自己输入的文字（问一问的问题）。

    注意：这个测试要等负责数据表、证据卡、评分、说明的几组文件也都配好英文才会通过。
    失败时断言信息里列出的每一条，就是某个接口的某个字段还剩的一句中文。
    """
    headers = account["headers"]
    left: list[str] = []

    def get(url, **params):
        """用英文（法文）取一个接口，把返回里还带汉字的字符串记进 left，返回解析好的 JSON。"""
        response = client.get(url, headers=headers, params={"lang": lang, **params})
        left.extend(_han_left(f"GET {url}", response.json()))
        return response.json()

    def post(url, body):
        """同上，POST。"""
        response = client.post(url, headers=headers, params={"lang": lang}, json=body)
        left.extend(_han_left(f"POST {url}", response.json()))
        return response.json()

    constraints = {
        "complexity": "concise",
        "stock_source": "inventory_only",
        "unavailable_conditions": ["pressurized_gas", "glovebox", "anhydrous_anaerobic"],
        "safety": {"exclude_acute_toxic": True, "exclude_cmr": True, "exclude_controlled": True},
        "requirements": [
            {"kind": "exclude_reagent_class", "value": "palladium", "hardness": "hard"},
            {"kind": "avoid_purification", "value": "column", "hardness": "soft"},
        ],
    }
    # 提交分析（同步队列：返回时八个阶段已经跑完）。返回的是分析的当前状态，里面有阶段名、阶段结果、约束标签
    analysis = post("/api/analyses", {"project_id": account["project_id"], "target": {"smiles": IBUPROFEN},
                                      "constraints": constraints})
    assert analysis["status"] == "succeeded", analysis
    base = f"/api/analyses/{analysis['id']}"
    get(base)
    get(f"{base}/preliminary")
    result = get(f"{base}/result")
    routes = result["routes"] + result["excluded_routes"]
    # 演示数据里布洛芬有三条路线；这组约束下至少有一条被排除
    assert len(routes) == 3 and result["excluded_routes"]
    for route in routes:
        get(f"{base}/routes/{route['id']}")
        # 每一步的证据卡（没写过说明的步骤，这时由 M11 按请求的语言现写）
        for number in range(1, route["n_steps"] + 1):
            get(f"{base}/routes/{route['id']}/steps/{number}")
        get(f"{base}/routes/{route['id']}/steps/1/precedents")
        for question in USER_TEXT:
            post(f"{base}/routes/{route['id']}/steps/1/ask", {"question": question})
        get(f"{base}/reports/materials", route_id=route["id"])
    post(f"{base}/compare", {"route_ids": [route["id"] for route in routes]})
    # 放宽条件：把挡住第一条被排除路线的约束都放宽
    post(f"{base}/relax", {"route_id": result["excluded_routes"][0]["id"]})
    # 几种报错：没有这个分析（404）、没有这条路线（404）、成功的分析不能重跑（409）、
    # 请求体没给目标分子（422，Pydantic 校验）、目标结构读不出来（422）、没登录（401）
    get("/api/analyses/nope")
    get(f"{base}/routes/nope")
    post(f"{base}/retry", {})
    post("/api/analyses", {"project_id": account["project_id"], "target": {}})
    post("/api/analyses", {"project_id": account["project_id"], "target": {"smiles": "C1CC"}})
    left.extend(_han_left("GET /api/projects（没登录）", client.get("/api/projects", params={"lang": lang}).json()))
    # 几个不属于某次分析的配置接口
    for url in ("/api/scoring/config", "/api/meta/options", "/api/meta/modules", "/api/projects"):
        get(url)

    # 同一句话可能在很多接口里出现：按文字去重以后再报，失败信息才看得过来
    distinct = sorted({item.split(" | ", 1)[1]: item for item in left}.values())
    assert not distinct, f"{lang} 返回里还剩 {len(distinct)} 句中文：\n" + "\n".join(distinct)


def test_progress_events_follow_the_language(client, account):
    """进度推送：带 ?lang=en 时进度说明是英文；不带时事件的格式和以前一样（没有 message_en）。

    进度事件是分析跑的时候存进 analysis_events 表的，那时还不知道以后谁用哪种语言来看，
    所以事件里中文、英文各存一份（message 和 message_en），推送时按请求的语言只发一份
    （analyses.stream_events）：英文请求拿到的 message 是英文；中文请求里 message_en 被去掉。
    """
    analysis = start_analysis(client, account)

    def keys(value) -> set[str]:
        """一份事件内容里出现过的全部键（字典套字典、列表里的字典都算）。"""
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()

    # ---- 不带 lang：和以前一样 ----
    chinese = _events(client, account, analysis["id"])
    assert chinese and chinese[-1][0] == "done"
    # 任何一条事件里都没有以 _en 结尾的键
    assert not [key for _, payload in chinese for key in keys(payload) if key.endswith("_en")]
    # 阶段事件的进度说明是中文
    messages = [payload["message"] for kind, payload in chinese if kind == "stage" and payload.get("message")]
    assert messages and all(HAN.search(message) for message in messages)

    # ---- 带 ?lang=en ----
    english = _events(client, account, analysis["id"], lang="en")
    # 事件还是那些：条数、类型、先后都一样，只是文字换了
    assert [kind for kind, _ in english] == [kind for kind, _ in chinese]
    # 英文那一份顶替了 message，message_en 自己不再出现
    assert not [key for _, payload in english for key in keys(payload) if key.endswith("_en")]
    translated = [payload["message"] for kind, payload in english if kind == "stage" and payload.get("message")]
    assert len(translated) == len(messages)
    assert not [message for message in translated if HAN.search(message)]
    # 除了文字，别的内容（第几阶段、状态）没有变
    assert [(p.get("index"), p.get("status")) for _, p in english] == [(p.get("index"), p.get("status")) for _, p in chinese]


def test_exclusion_reason_written_at_rescoring_is_english_too(client, account):
    """排除一个试剂以后（会重新评分），用英文取结果，新拼出来的排除原因也是英文。

    排除原因这句话不是分析跑的时候写的，而是排除那一刻由 pipeline.rescore 重新评分时现拼的。
    要让之后的英文请求也查得到它的英文，这句话的对照必须记进结果自己的对照表（result["_en"]）
    并随结果一起保存：deps._owned 取出分析时用 i18n.add_memory 把那张表接进当前请求，就是为了这个。
    所以这里特意分成两个请求：先用中文排除（不带 lang），再另发一个英文请求取结果。
    """
    headers = account["headers"]
    # 不带约束：三条路线一开始都可选
    analysis = start_analysis(client, account)
    base = f"/api/analyses/{analysis['id']}"
    before = client.get(f"{base}/result", headers=headers).json()
    assert len(before["routes"]) == 3 and not before["excluded_routes"]

    # 排除乙醚（格氏羧基化路线第 4 步的溶剂）。这个请求不带 lang，返回的是中文
    response = client.post(f"{base}/exclusions", headers=headers, json={"type": "substance", "smiles": "CCOCC"})
    assert response.status_code == 200, response.text
    assert response.json()["research"] is False
    chinese_reasons = [reason for route in response.json()["result"]["excluded_routes"]
                       for reason in route["exclusion_reasons"] if reason["user"]]
    assert chinese_reasons and all(HAN.search(reason["label"]) for reason in chinese_reasons)

    # 另发一个英文请求取结果：用户排除造成的那条原因是英文
    english = client.get(f"{base}/result", headers=headers, params=EN).json()
    english_reasons = [reason for route in english["excluded_routes"]
                       for reason in route["exclusion_reasons"] if reason["user"]]
    assert len(english_reasons) == len(chinese_reasons)
    assert not [reason["label"] for reason in english_reasons if HAN.search(reason["label"])]
    # 分析状态里的排除记录（user_exclusions）上的物质名也是英文
    exclusions = client.get(base, headers=headers, params=EN).json()["user_exclusions"]
    assert len(exclusions) == 1 and not HAN.search(exclusions[0]["name"])

    # 反过来也一样：直接用英文请求去排除，当场返回的原因就是英文
    response = client.post(f"{base}/exclusions", headers=headers, params=EN, json={"type": "substance", "smiles": "CO"})
    assert response.status_code == 200, response.text
    reasons = [reason["label"] for route in response.json().get("result", {}).get("excluded_routes", [])
               for reason in route["exclusion_reasons"] if reason["user"]]
    assert not [label for label in reasons if HAN.search(label)]
    # 存下来的仍然是中文：不带 lang 再取，原因还是中文
    chinese = client.get(f"{base}/result", headers=headers).json() if not response.json()["research"] else None
    if chinese is not None:
        assert all(HAN.search(reason["label"]) for route in chinese["excluded_routes"]
                   for reason in route["exclusion_reasons"] if reason["user"])


def test_errors_follow_the_language(client, account):
    """英文请求的 401、404、409、422 报错是英文；不带 lang 时还是原来的中文。

    报错有三种写法，都要覆盖到：
      - 固定的一句话：HTTPException(409, i18n.tr("…", "…"))；
      - 带参数的模板：deps._owned 的 “没有这个{what}”，参数“项目”“分析”自己也有英文；
      - Pydantic 校验方法抛的 ValueError：出现在 422 的 detail[].msg 里，前面带着 Pydantic 加的
        "Value error, "。对照是按整句匹配的，所以 api/schemas.py 的 _invalid 把带前缀的整句也登记了。
    """
    headers = account["headers"]
    analysis = start_analysis(client, account)
    base = f"/api/analyses/{analysis['id']}"

    def detail(method, url, *, lang=None, auth=True, **kwargs):
        """发一个会出错的请求，返回 (状态码, detail)。lang 给了就带上 ?lang=…；auth=False 时不带登录令牌。"""
        response = client.request(method, url, headers=headers if auth else None,
                                  params={"lang": lang} if lang else None, **kwargs)
        return response.status_code, response.json()["detail"]

    # ---- 401：没登录 ----
    assert detail("GET", "/api/projects", auth=False) == (401, "请先登录")
    assert detail("GET", "/api/projects", auth=False, lang="en") == (401, "Please sign in")
    # 请求头的写法对报错同样有效
    response = client.get("/api/projects", headers={"X-Language": "en"})
    assert (response.status_code, response.json()["detail"]) == (401, "Please sign in")

    # ---- 404：带参数的模板，参数也换成英文 ----
    assert detail("GET", "/api/analyses/nope") == (404, "没有这个分析")
    assert detail("GET", "/api/analyses/nope", lang="en") == (404, "Analysis not found")
    assert detail("GET", "/api/projects/nope/history") == (404, "没有这个项目")
    assert detail("GET", "/api/projects/nope/history", lang="en") == (404, "Project not found")
    # 固定的一句话
    assert detail("GET", f"{base}/routes/nope") == (404, "没有这条路线")
    assert detail("GET", f"{base}/routes/nope", lang="en") == (404, "Route not found")
    assert detail("GET", f"{base}/routes/r1/steps/99", lang="en") == (404, "This route has no such step")

    # ---- 409：成功的分析不能重跑；同一种试剂不能排除两次 ----
    assert detail("POST", f"{base}/retry") == (409, "只有失败或已取消的分析可以重跑")
    assert detail("POST", f"{base}/retry", lang="en") == (409, "Only failed or cancelled analyses can be rerun")
    body = {"type": "substance", "smiles": "CCOCC"}
    assert client.post(f"{base}/exclusions", headers=headers, json=body).status_code == 200
    assert detail("POST", f"{base}/exclusions", json=body) == (409, "已经排除过了")
    assert detail("POST", f"{base}/exclusions", lang="en", json=body) == (409, "Already excluded")
    # 注册一个已经有的邮箱（这个接口不用登录）
    again = {"username": "anna@example.com", "password": "another-password-1", "display_name": "A", "org_name": "B"}
    assert detail("POST", "/api/auth/register", auth=False, json=again) == (409, "这个用户名已经有人用了")
    assert detail("POST", "/api/auth/register", auth=False, lang="en", json=again) == (409, "This username is already taken")

    # ---- 422：接口自己抛的 ----
    bad = {"type": "substance", "smiles": "C1CC"}  # 环没有闭合，读不出来
    assert detail("POST", f"{base}/exclusions", json=bad) == (422, "结构读不出来")
    assert detail("POST", f"{base}/exclusions", lang="en", json=bad) == (422, "The structure could not be read")
    # 权重里有不认识的维度：说明里列着六个维度的编号，中文用顿号连，英文用逗号
    weights = {"route_ids": ["r1"], "weights": {"nonsense": 1}}
    code, text = detail("POST", f"{base}/compare", json=weights)
    assert code == 422 and text.startswith("权重只能包含这几项，且不能为负：literature、operation、")
    code, text = detail("POST", f"{base}/compare", lang="en", json=weights)
    assert code == 422 and text.endswith(": literature, operation, safety, availability, yield, green") and not HAN.search(text)
    # 目标结构读不出来：detail 是一个字典，message 是接口写的那一句
    target = {"project_id": account["project_id"], "target": {"smiles": "C1CC"}}
    assert detail("POST", "/api/analyses", json=target)[1]["message"] == "目标分子的结构有问题"
    assert detail("POST", "/api/analyses", lang="en", json=target)[1]["message"] == "The target structure is invalid"

    # ---- 422：Pydantic 校验方法抛的，msg 前面带 "Value error, " ----
    empty = {"project_id": account["project_id"], "target": {}}
    code, errors = detail("POST", "/api/analyses", json=empty)
    assert code == 422 and errors[0]["msg"] == "Value error, 需要给出目标分子的 smiles 或 molblock"
    code, errors = detail("POST", "/api/analyses", lang="en", json=empty)
    assert code == 422 and errors[0]["msg"] == "Value error, Provide the target molecule as smiles or molblock"
    code, errors = detail("POST", f"{base}/exclusions", lang="en", json={"type": "branch"})
    assert code == 422 and errors[0]["msg"] == "Value error, Excluding a branch requires route_id and step_index"
    code, errors = detail("POST", "/api/inventory/items", lang="en", json={})
    assert code == 422 and errors[0]["msg"] == "Value error, Provide at least one of name, CAS number or SMILES"
    # 英文的 422 里一个汉字都不剩（包括 Pydantic 附带的 ctx、input）
    assert not _han_left("422", errors)


def test_french_is_translated_from_the_english(client, account):
    """lang=fr：后端先得到英文，再查法文对照表（i18n.fr_of）。

    看三类文字：存在数据库里的中文（默认项目名）、接口里拼的固定文字（评分公式）、报错；
    再看按模板翻的句子：参数挨在一起、参数里又带空格时（“Steps 2, 3 have no precedent”）也要切对。
    """
    headers = account["headers"]
    french = {"lang": "fr"}
    assert client.get("/api/projects", headers=headers, params=french).json()[0]["name"] == "Projet par défaut"
    formula = client.get("/api/scoring/config", headers=headers, params=french).json()["formula"]["route"]
    english = client.get("/api/scoring/config", headers=headers, params=EN).json()["formula"]["route"]
    assert formula == i18n.fr_of(english) != english
    # 报错：没登录、用户名或密码不对
    assert client.get("/api/projects", params=french).json()["detail"] == i18n.fr_of("Please sign in") != "Please sign in"
    login = client.post("/api/auth/login", params=french, json={"username": "nobody", "password": "wrong-password"})
    assert login.json()["detail"] == "Nom d'utilisateur ou mot de passe incorrect"
    # 按模板翻：按个数选的词（Step / Steps、has / have）是参数，也要翻
    assert i18n.fr_of("Step 2 has no precedent") == "Étape 2 n'a aucun précédent"
    assert i18n.fr_of("Steps 2, 3 have no precedent") == "Étapes 2, 3 n'ont aucun précédent"
    # 认不出来的英文原样留着
    assert i18n.fr_of("Something nobody wrote") == "Something nobody wrote"


def test_every_english_text_has_french(ctx):
    """法文对照表 syntherisk/locales/fr.json 里有代码里每一句 tr() 的英文，参数的名字和格式都一样；
    data/ 下各表登记的英文名称（启动时记进固定对照的）也都翻得出来。

    新加一句 tr() 时，在 fr.json 里补上它的法文：键是英文模板原样，参数照抄（{name}、{score:g} 这些不能改）。
    法文和英文本来就一样的（SMILES、Format 这类）也要写进去，写成和英文一样。
    """
    table = json.loads((Path(i18n.__file__).parent / "locales" / "fr.json").read_text(encoding="utf-8"))

    def fields(text: str) -> list[tuple[str, str]]:
        return sorted((field, spec) for _, field, spec, _ in string.Formatter().parse(text) if field is not None)

    missing = []
    for path in sorted(Path(i18n.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = getattr(node.func, "attr", getattr(node.func, "id", None)) if isinstance(node, ast.Call) else None
            # tr() 和 schemas.py 的 _invalid()（校验报错）都是“中文, 英文”两个参数
            if name in ("tr", "_invalid") and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and isinstance(node.args[1].value, str) and node.args[1].value not in table:
                missing.append(f"{path.name}:{node.lineno} {node.args[1].value}")
    assert not missing, "fr.json 里缺这几句的法文：\n" + "\n".join(missing)
    assert [english for english, french in table.items() if fields(english) != fields(french)] == []
    # 固定对照要整句在表里（校验报错带的 "Value error, " 前缀不算）；带数字的（立体中心个数、净电荷这类按模板生成的提示）按模板翻得出来也行
    untranslated = sorted({english for english in i18n._STATIC.values() if english.removeprefix("Value error, ") not in table
                           and not (re.search(r"\d", english) and i18n.fr_of(english) != english)})
    assert not untranslated, "这些固定对照的英文在 fr.json 里没有法文：\n" + "\n".join(untranslated)
