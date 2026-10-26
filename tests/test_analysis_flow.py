"""从“开始分析”到结果页、证据卡的整条流程（用内置的布洛芬演示路线）。

对应 PPT 里的那个例子：同一个分子，实验室条件不同，可选的路线就不同。
课题组没有加压设备、不用剧毒物质时，BHC 路线（氟化氢、加压）和 Boots 路线（氯乙酸乙酯）
被排除并写明原因，而不是悄悄消失；只剩格氏羧基化一条可选。

这个文件测的是什么
    全部是走接口的端到端测试：注册一个账号，提交目标分子，等后端把八个阶段跑完，再去调结果页、证据卡、
    问一问、放宽条件、排除、标记为我有、进度推送、重跑这些接口，看返回的内容。
    一次分析会经过的模块都在里面：M0（接口和权限，syntherisk/api/analyses.py）、MQ（任务队列和流水线，
    syntherisk/pipeline.py）、M3（逆合成，这里用演示引擎）、M4（可得性）、M5（文献先例，这里先例库是空的）、
    M6（危害和管制）、M7（操作条件）、M9（绿色化学）、M10（评分、排除、排序）、M11（写说明）、M12（问答）。

涉及的按钮和接口（接口函数都在 syntherisk/api/analyses.py）
    A16 开始分析          POST   /api/analyses                                    create_analysis
    A17 进度面板          GET    /api/analyses/{id}/events（SSE 进度推送）         stream_events
    A19 取消              POST   /api/analyses/{id}/cancel                        cancel_analysis
    失败或取消以后重跑    POST   /api/analyses/{id}/retry                         retry_analysis
    B1 排序、结果页       GET    /api/analyses/{id}/result?sort=                  get_result
    B4 路线卡片           GET    /api/analyses/{id}/routes/{route_id}             get_route
    B6 放宽条件           POST   /api/analyses/{id}/relax                         relax_constraints
    B8 B9 分叉、最薄弱环节  结果页返回值里的 graph 和每条路线的 weakest
    B10 证据卡            GET    /api/analyses/{id}/routes/{route_id}/steps/{n}   get_step_card
    B11 排除、撤销排除    POST / DELETE /api/analyses/{id}/exclusions             add_exclusion、remove_exclusion
    B12 标记为我有        POST   /api/analyses/{id}/mark-owned                    mark_owned
    C5 问一问             POST   …/steps/{n}/ask                                  ask_about_step
    分析列表、当前状态    GET    /api/analyses、GET /api/analyses/{id}            list_analyses、get_analysis

用到的演示数据：data/demo_routes/ibuprofen.json 里布洛芬的三条教科书路线。路线编号按文件里的先后给：
    r1 格氏羧基化（4 步）  ① 傅-克酰基化：异丁基苯 + 乙酸酐，三氯化铝催化，得到 4-异丁基苯乙酮
                           ② 羰基还原：硼氢化钠、甲醇，酮变成醇
                           ③ 氯代：盐酸，醇变成氯代物
                           ④ 格氏羧基化：镁屑、乙醚做成格氏试剂，和二氧化碳（干冰）反应得到布洛芬
    r2 Boots（6 步）       ① 同上（和 r1 是同一步，结果里只存一份）  ② Darzens 缩合：氯乙酸乙酯、乙醇钠
                           ③ 水解脱羧  ④ 成肟：盐酸羟胺  ⑤ 肟脱水成腈  ⑥ 腈水解
    r3 BHC（3 步）         ① 傅-克酰基化：同样的反应物，但用氟化氢  ② 催化加氢：氢气、雷尼镍
                           ③ 钯催化羰基化：一氧化碳、钯催化剂
    步骤的序号按实验顺序：第 1 步最先做，最后一步得到目标分子。

共用的夹具和辅助函数（都在 tests/conftest.py）
    client            FastAPI 的测试客户端：在测试进程里直接调接口，不起真的服务器
    account           已经注册好的一个账号：headers（带登录令牌的请求头）、project_id（默认项目）、access_token 等
    ctx               这个测试自己的运行时上下文（AppContext）；可以直接换掉里面的逆合成引擎
    register()        再注册一个账号（另一个课题组），用来测“别的组看不到”
    start_analysis()  提交一次分析（POST /api/analyses），断言返回 202，返回这次分析的状态
    测试环境和正式运行不一样的地方（conftest.py 的 make_settings）：
      - 任务队列是同步的：start_analysis 返回时八个阶段已经全部跑完，不用等；
      - 每个测试用一个新的临时数据库：先例库是空的，所以证据等级都是“未评估”，文献依据一项不计分；
      - 不联网：内置试剂表以外的物质（路线上的中间体）查不到危害，标“没有危害数据”；
      - 不调大模型：说明和回答都是按模板生成的；
      - 逆合成用演示引擎：只认布洛芬和阿司匹林。
    本文件自己的替身只有一个：BrokenEngine（一调用就抛错的假引擎），写在用它的那个测试里面。

本文件里有什么
    LAB_WITHOUT_PRESSURE   “没有加压设备、不用剧毒物质”的一组约束
    _result()、_by_name()、_events()   取结果页、按路线名取卡片、读进度推送的三个小函数
    test_all_eight_stages_run_and_report_progress
        八个阶段依次跑完，每个阶段留下一句结果；返回值里有目标分子的信息和约束标签
    test_excluded_routes_stay_visible_with_their_reasons
        被排除的路线留在结果里并写明原因；最薄弱环节、“证据等级未评估”、分叉信息都如实给出
    test_relaxing_constraints_brings_the_route_back_without_a_new_search
        临时放宽条件能让被排除的路线回来，要把挡住它的约束全部放宽才行，保存的结果不变
    test_a_fully_equipped_lab_gets_a_different_recommendation
        什么都不限制时三条路线都可选，可以按“最绿色”“最短”排序
    test_complexity_tier_limits_the_number_of_steps
        “简洁”档把超过 3 步的路线排除，原因里写明步数和上限
    test_evidence_card_has_sources_for_every_statement
        证据卡上各项内容齐全，说明里的每句话都带着卡里有的出处
    test_ask_answers_from_the_card_or_says_there_is_no_evidence
        问一问只根据证据卡回答；卡里没有依据的问题直接说没有
    test_marking_a_material_as_owned_turns_the_leaf_green_and_raises_coverage
        标记为我有以后，原料进库存、库存覆盖和分数上升、路线树上的叶子变成“库存”
    test_excluding_a_reagent_reranks_and_can_be_undone
        排除一种试剂以后用到它的路线被拿掉，可以撤销，重复排除被拒绝
    test_excluding_every_route_triggers_a_new_search
        把路线全排除以后自动重新搜索，之后看进度只收到新一轮的事件
    test_progress_events_stream_ends_when_the_analysis_is_done
        进度推送按顺序发完事件，最后是 done 和 end；令牌可以放在网址里；不带令牌是 401
    test_unknown_molecule_finishes_with_an_honest_no_routes_message
        找不到路线时分析照常结束并说明原因；结构不合法的目标直接 422
    test_cancelled_and_failed_analyses_can_be_retried
        失败的分析可以重跑，成功的不行；重跑以后看进度只收到新一轮的事件
    test_another_group_cannot_see_or_touch_this_groups_analysis
        别的课题组看不到、动不了这次分析；数据库里的目标结构和结果是加密的
"""

import json

from tests.conftest import IBUPROFEN, register, start_analysis

# 一个“没有加压设备、不用剧毒物质”的课题组在约束页上的选择，随 A16 开始分析一起提交（请求体的 constraints）：
#   complexity              A7 复杂度档位“平衡”：路线最多 6 步（data/scoring.toml 的 [complexity.balanced]）
#   unavailable_conditions  A10 做不了的条件：加压气体反应、手套箱
#   safety                  A11 安全要求：排除剧毒物质（GHS 急性毒性 1–2 类）
# 没写的项用默认值：原料来源“库存优先 + 可购买”，规模 mg 级。各项的定义在 syntherisk/constraints.py 的
# Constraints。本文件里要看“有约束时怎样”的测试都用它；要看“什么都不限制”时，start_analysis 不传 constraints。
LAB_WITHOUT_PRESSURE = {
    "complexity": "balanced",
    "unavailable_conditions": ["pressurized_gas", "glovebox"],
    "safety": {"exclude_acute_toxic": True},
}


def _result(client, account, analysis_id, **params):
    """取结果页的数据（GET /api/analyses/{id}/result），顺便确认接口返回了 200。

    参数：
        client、account  conftest.py 的夹具。
        analysis_id      分析编号（start_analysis 返回值里的 id）。
        **params         原样当作查询参数发出去，比如 sort="green"（B1 排序方式）。
    返回：
        接口返回的字典（syntherisk/api/views.py 的 result_view）。测试里用到的键：
        routes（可选的路线卡片，已经排好序）、excluded_routes（被排除的路线卡片）、graph（分叉信息）、warnings。
    谁在用：本文件的多个测试。
    """
    response = client.get(f"/api/analyses/{analysis_id}/result", headers=account["headers"], params=params)
    # assert 逗号后面的是断言失败时一起显示的内容：这里把接口返回的原文打出来，方便看出了什么错
    assert response.status_code == 200, response.text
    return response.json()


def _by_name(routes):
    """把路线卡片的列表变成 {路线名: 卡片}，断言时可以直接按名字取（“格氏羧基化”“Boots”“BHC”）。"""
    return {route["name"]: route for route in routes}


def _events(client, account, analysis_id, **params):
    """连上进度推送，读到结束为止；返回带编号的事件 [(事件类型, 内容), …]（最后那条 end 没有编号，不算）。

    调的是 GET /api/analyses/{id}/events（syntherisk/api/analyses.py 的 stream_events）。它返回的不是 JSON，
    而是 Server-Sent Events：一段一段的文本，段和段之间隔一个空行。每个事件三行：
        id: 事件编号（analysis_events 表的自增 id）
        event: 事件类型（status / stage / preliminary_ready / scored / done）
        data: 一段 JSON
    最后那条 end 只有 event 和 data 两行，不以 “id: ” 开头，被下面的筛选条件去掉了。
    参数：
        **params  原样当作查询参数，比如 last_event_id=1（从第 1 条事件之后接着发）。
    测试里分析在提交的时候就已经跑完，服务端把存着的事件发完、再发一条 end 就结束，所以这里不会一直等下去。
    谁在用：本文件里检查“重跑、重新搜索以后收到的是哪一轮事件”的两个测试。
    """
    url = f"/api/analyses/{analysis_id}/events"
    # client.stream(...) 用“流”的方式发请求：响应不是一次收完，可以边到边读；离开 with 时连接关闭。
    # iter_text() 一块一块地给出收到的文本，拼起来就是这次推送的全部内容
    with client.stream("GET", url, headers=account["headers"], params=params) as stream:
        text = "".join(stream.iter_text())
    # 按空行切成一个个事件，只留以 “id: ” 开头的；每个事件再按行切开，得到 [id 行, event 行, data 行]
    blocks = [block.split("\n") for block in text.split("\n\n") if block.startswith("id: ")]
    # removeprefix 去掉每行开头固定的字样；data 那一行是 JSON 文本，解析成字典
    return [(lines[1].removeprefix("event: "), json.loads(lines[2].removeprefix("data: "))) for lines in blocks]


def test_all_eight_stages_run_and_report_progress(client, account):
    """提交分析以后八个阶段依次跑完，每个阶段留下一句给用户看的结果。

    保证的行为：A16 开始分析（POST /api/analyses，syntherisk/api/analyses.py 的 create_analysis）把任务交给
    队列，流水线（syntherisk/pipeline.py 的 AnalysisRunner.run）把八个阶段逐个标成 done，每个阶段的 message
    写明这一阶段得到了什么；返回的状态里还有目标分子的基本信息和 R2 约束标签。
    进度面板（A17）上每个阶段后面显示的就是这些 message。
    怎么读：用 LAB_WITHOUT_PRESSURE 提交布洛芬。测试里队列是同步的，start_analysis 返回时分析已经跑完，
    所以直接在它的返回值上断言：先看状态，再逐个阶段看 message 里的关键字，最后看目标分子和约束标签。
    """
    analysis = start_analysis(client, account, constraints=LAB_WITHOUT_PRESSURE)
    # 失败的话把后端记下的失败原因（analysis["error"]）一起显示出来
    assert analysis["status"] == "succeeded", analysis["error"]
    # stages 是八个阶段的进度表（pipeline.py 的 fresh_stages 建的），每项有 index、name、status、message 等
    assert [stage["status"] for stage in analysis["stages"]] == ["done"] * 8
    # 第 1 阶段 读入约束：合并出 6 个约束标签，就是本测试最后一行核对的那 6 个。
    # 这句话是 syntherisk/constraints.py 的 EffectiveConstraints.tag_summary 拼的
    assert analysis["stages"][0]["message"].startswith("合并 6 条约束")
    # 第 2 阶段 逆合成搜索：演示引擎给出 data/demo_routes/ibuprofen.json 里的 3 条路线
    assert "3 条内置路线" in analysis["stages"][1]["message"]
    # 第 3 阶段 原料可得性：三条路线的起始原料都在内置试剂表（data/compounds.csv）里，并且都标着可购买，
    # 所以“买不到”的是 0 个
    assert "买不到 0" in analysis["stages"][2]["message"]
    # 第 4 阶段 文献先例：这个测试的数据库是新建的，先例库里一条数据都没有，所以不评证据等级
    assert "先例库还没有导入数据" in analysis["stages"][3]["message"]
    # 第 5 阶段 毒性与法规：数的是三条路线用到的全部物质里，属于“剧毒”（GHS 急性毒性 1–2 类）的有几种。
    # 两种都在 data/compounds.csv 里：氟化氢的 h_codes 有 H300、H310、H330，氯乙酸乙酯有 H310；
    # 哪些 H 编码算剧毒，见 syntherisk/modules/m06_safety.py 的 H_STATEMENTS（类别 acute_toxic_12）
    assert "剧毒 2" in analysis["stages"][4]["message"]  # 氟化氢、氯乙酸乙酯
    # 第 7 阶段 评分与排序：三条路线都有分数，其中两条违反了硬约束
    assert "可选 1 条，被排除 2 条" in analysis["stages"][6]["message"]
    # 第 8 阶段 写说明：测试里没有配置大模型，说明是 M11 按模板生成的
    assert "按模板生成" in analysis["stages"][7]["message"]
    # 目标分子：分子式是 RDKit 算的；名字取自内置试剂表（data/compounds.csv 里 key 为 ibuprofen 的那一行）
    assert analysis["target"]["formula"] == "C13H18O2" and analysis["target"]["name"] == "布洛芬"
    # R2 约束标签，顺序固定（constraints.py 的 merge_constraints）：复杂度档位、原料来源、做不了的条件、
    # 安全要求、规模。文字的来历：
    #   “平衡 · ≤ 6 步”             data/scoring.toml 的 [complexity.balanced]：label 和 max_steps
    #   “库存优先 + 可购买”          没有指定原料来源时的默认档位（constraints.py 的 STOCK_LABELS）
    #   “排除：加压气体 / 手套箱 / 剧毒”  data/vocabulary.toml 里这三项的 short
    #   “mg 级”                      默认的规模（constraints.py 的 SCALE_LABELS）
    labels = [tag["label"] for tag in analysis["constraint_tags"]]
    assert labels == ["平衡 · ≤ 6 步", "库存优先 + 可购买", "排除：加压气体", "排除：手套箱", "排除：剧毒", "mg 级"]


def test_excluded_routes_stay_visible_with_their_reasons(client, account):
    """被硬约束挡住的路线不会消失：留在结果里，逐条写明是哪一步、哪个物质、违反了哪条约束。

    保证的行为：结果页（GET /api/analyses/{id}/result，api/analyses.py 的 get_result，数据由 api/views.py 的
    result_view 整理）把路线分成 routes（可选）和 excluded_routes（被排除）两组。被排除的路线照样有分数，
    只是没有名次；每条排除原因带约束编号（constraint）和一句中文（label）。排不排除只看硬约束，和分数无关
    （syntherisk/modules/m10_scoring.py 的 exclusion_reasons、score_result）。
    同一份结果里还有：需要的特殊操作、最薄弱环节（B9）、先例库为空时如实标出的“证据等级未评估”、
    路线树的分叉信息（B8，syntherisk/routes.py 的 build_graph）。
    怎么读：用 LAB_WITHOUT_PRESSURE 提交布洛芬，取结果页。断言分五段：可选的只有格氏路线；Boots 和 BHC
    各自的排除原因；特殊操作和最薄弱环节；证据等级未评估；分叉。
    为什么重要：文件头说的那个例子靠的就是这一条。README 也把“被排除的路线都会留在结果里并写明是哪一步、
    哪个物质、哪条危险性说明”列为产品的行为；界面上 B6（被排除的路线：看原因、临时放宽条件）读的就是这些内容。
    """
    analysis = start_analysis(client, account, constraints=LAB_WITHOUT_PRESSURE)
    result = _result(client, account, analysis["id"])

    # ---- 可选的路线只有一条
    # 左边写成 (grignard,) 是“解包”：右边的列表必须正好有一个元素，多了少了都会报错
    (grignard,) = result["routes"]
    assert (grignard["name"], grignard["rank"], grignard["n_steps"]) == ("格氏羧基化", 1, 4)

    # ---- 被排除的两条：都还在结果里
    excluded = _by_name(result["excluded_routes"])
    assert set(excluded) == {"Boots", "BHC"} and excluded["Boots"]["n_steps"] == 6
    # 被排除的路线没有名次（rank 是 None），但分数照样算出来了
    assert all(route["rank"] is None and route["score"] is not None for route in excluded.values())
    # 排除看的是硬约束，不是分数：Boots 的分数比留下的那条还高
    # （按现在 data/ 下的数据，Boots 是 81.1 分，格氏羧基化是 77.8 分，README 里引用的也是这两个数）
    assert excluded["Boots"]["score"] > grignard["score"]
    # Boots 的第 2 步是 Darzens 缩合，反应物里有氯乙酸乙酯。它在 data/compounds.csv 里的 H 编码有 H310
    # （皮肤接触致命），属于剧毒一类，违反了“排除剧毒”。括号里只列把它归成剧毒的那一条说明
    # （m06_safety.py 的 statements_behind）；它另有 H301、H331，那两条属于“有毒”一档，不在这里列
    assert [(r["constraint"], r["label"]) for r in excluded["Boots"]["exclusion_reasons"]] == [
        ("safety:acute_toxic", "第 2 步用到剧毒物质 氯乙酸乙酯（H310 皮肤接触致命）")]
    # BHC 同时违反两条约束。原因列表里同一个编号可以出现几次，所以先做成 {编号: 文字} 看有哪几种编号。
    # 约束里还勾了“手套箱”，但三条路线没有哪一步命中手套箱的规则，所以这里没有 condition:glovebox
    bhc = excluded["BHC"]
    reasons = {reason["constraint"]: reason["label"] for reason in bhc["exclusion_reasons"]}
    assert set(reasons) == {"condition:pressurized_gas", "safety:acute_toxic"}
    # 第 1 步用氟化氢。表里它的 H 编码是 H300、H310、H314、H330：其中 H300、H310、H330 三条属于剧毒，
    # 全部列出；H314 是腐蚀，不列
    assert reasons["safety:acute_toxic"] == "第 1 步用到剧毒物质 氟化氢（H300 吞咽致命、H310 皮肤接触致命、H330 吸入致命）"
    # 加压气体这一条出现两次：第 2 步的反应物有氢气，第 3 步的反应物有一氧化碳，两步都命中
    # data/condition_rules.toml 里 id 为 pressurized_gas 的规则；两条原因编号相同，文字里的步骤序号不同
    assert sum(r["constraint"] == "condition:pressurized_gas" for r in bhc["exclusion_reasons"]) == 2  # 加氢和羰基化两步

    # ---- 特殊操作（R7 汇总条上的一项）
    # 格氏路线第 4 步用到镁屑，命中规则 anhydrous_organometallic（无水无氧操作）。这条规则对应 A10 里的
    # “无水无氧”，带这种对应关系的标签才算“特殊操作”（m10_scoring.py 的 _summary）。
    # 这个课题组没有勾“无水无氧”，所以它只是提示，不会把路线排除
    assert [op["text"] for op in grignard["special_operations"]] == ["无水无氧操作 1 步"]
    # Boots 的六步都没有命中这类规则
    assert excluded["Boots"]["special_operations"] == []
    # 最薄弱环节：格氏那一步（要无水操作，乙醚极易燃）
    # weakest 是这条路线里步骤分最低的一步（m10_scoring.py 的 _weakest_link）
    assert grignard["weakest"]["step_index"] == 4 and grignard["weakest"]["name"] == "格氏羧基化"
    assert grignard["weakest"]["score"] == min(step["score"] for step in grignard["steps"])
    # 建议的文字是 data/scoring.toml 里 [weakest_link] 的 suggestion，把步骤序号填进去
    assert grignard["weakest"]["suggestion"] == "建议先做 50 mg 小试验证第 4 步"
    # 先例库没导入：证据等级如实标“未评估”，文献依据不计分，而不是把每一步都当成没有先例
    # 置信度是“中”：每一步的证据等级都没评估时，整条路线按中等算（m10_scoring.py 的 _route_confidence）
    assert grignard["evidence_text"] == "证据等级未评估" and grignard["confidence_label"] == "中"
    # 每一步“文献依据”这一维的分数是 None（没评估），不是 0 分
    assert all(step["dimensions"]["literature"] is None for step in grignard["steps"])
    # 结果页的提示里也有一条说明（pipeline.py 的 _precedents 加的）
    assert any("先例库还没有导入" in warning for warning in result["warnings"])

    # 路线树的分叉：目标分子有三种做法，其中 BHC 和 Boots 那两支标为已排除
    # top_branches 是“得到目标分子的最后一步”的几种做法，每条路线的最后一步各是一种：
    # 格氏羧基化（r1）、腈水解（r2 Boots）、钯催化羰基化（r3 BHC）。分叉的名字是那一步的反应名
    branches = {branch["name"]: branch for branch in result["graph"]["top_branches"]}
    assert [name for name, branch in branches.items() if not branch["excluded"]] == ["格氏羧基化"]
    assert branches["钯催化羰基化"]["excluded"]
    # 分叉上的 score 是这个分叉里最好的那条路线的路线分（不是这一步自己的步骤分）
    assert branches["格氏羧基化"]["score"] == grignard["score"]
    # 三个分叉的标签是 A、B、C（画布上的分叉标签，按路线在搜索结果里的先后发）
    assert sorted(branch["label"] for branch in branches.values()) == ["A", "B", "C"]


def test_relaxing_constraints_brings_the_route_back_without_a_new_search(client, account):
    """B6 放宽条件：临时去掉几条约束，被排除的路线回到可选列表；不重新搜索，保存的结果也不变。

    保证的行为：POST /api/analyses/{id}/relax（api/analyses.py 的 relax_constraints）
      - 请求里给 route_id：放宽挡住这条路线的全部约束；给 constraints：只放宽列出的那几个编号；
      - 一条路线要它的每一条排除原因都被放宽才回来，回来的路线带 relaxed = true（界面上标“放宽后”）；
      - 重新过滤和排序是在结果的副本上做的（m10_scoring.py 的 score_result，参数 relaxed），数据库里那份不动。
    怎么读：先按路线放宽（r3 就是 BHC），三条路线都回来了；再分别只放宽一个约束编号，看谁回来、谁还被挡着；
    最后重新取一次结果页，被排除的仍然是 2 条。
    """
    analysis = start_analysis(client, account, constraints=LAB_WITHOUT_PRESSURE)
    # 按路线放宽。挡住 BHC 的有两个约束编号：加压气体、剧毒
    relaxed = client.post(f"/api/analyses/{analysis['id']}/relax", headers=account["headers"], json={"route_id": "r3"}).json()
    names = {route["name"]: route for route in relaxed["routes"]}
    assert set(names) == {"格氏羧基化", "Boots", "BHC"} and relaxed["excluded_routes"] == []
    # relaxed 只标在“靠放宽才回来”的路线上；格氏路线本来就可选，是 False
    assert names["BHC"]["relaxed"] and not names["格氏羧基化"]["relaxed"]
    # 返回值里列出这次实际放宽的约束编号（排过序）
    assert relaxed["relaxed_constraints"] == ["condition:pressurized_gas", "safety:acute_toxic"]
    # Boots 也是被“排除剧毒”挡住的，放宽这一项以后一起回来
    assert names["Boots"]["relaxed"]
    # 只放宽一项不够：BHC 还被另一项挡着
    # 只放宽“加压气体”：BHC 还有氟化氢（剧毒）这一条，Boots 的氯乙酸乙酯也是剧毒，两条都还被排除
    partial = client.post(f"/api/analyses/{analysis['id']}/relax", headers=account["headers"],
                          json={"constraints": ["condition:pressurized_gas"]}).json()
    assert {route["name"] for route in partial["excluded_routes"]} == {"Boots", "BHC"}
    # 只放宽“剧毒”：Boots 回来了，BHC 还被“加压气体”挡着
    only_toxic = client.post(f"/api/analyses/{analysis['id']}/relax", headers=account["headers"],
                             json={"constraints": ["safety:acute_toxic"]}).json()
    assert [route["name"] for route in only_toxic["excluded_routes"]] == ["BHC"]
    # 放宽是临时的，保存的结果没变
    assert len(_result(client, account, analysis["id"])["excluded_routes"]) == 2


def test_a_fully_equipped_lab_gets_a_different_recommendation(client, account):
    """什么都不限制时三条路线都可选；3 步的 BHC 路线原子经济性最高。

    保证的行为：不带任何约束提交时没有路线被排除，三条路线按路线分从高到低得到名次 1、2、3；
    结果页的 sort 参数（B1 排序，api/analyses.py 的 get_result，比较的办法在 api/views.py 的 _sort_key）
    可以换成按别的指标排：green 按“绿色化学”这一维各步的平均分，steps 按步数。
    怎么读：start_analysis 不传 constraints（全部用默认值）。先看默认的综合排序，再看 sort=green 和
    sort=steps 时排第一的是谁。
    和上面几个测试对照着看：同一个分子，条件不同，可选的路线和排在前面的路线就不同。
    """
    analysis = start_analysis(client, account)
    # 默认按综合分排：名次是 1、2、3，分数从高到低
    by_score = _result(client, account, analysis["id"])["routes"]
    assert [route["rank"] for route in by_score] == [1, 2, 3]
    assert [route["score"] for route in by_score] == sorted((route["score"] for route in by_score), reverse=True)
    # 按“最绿色”排。绿色化学分由原子经济性和溶剂评级合成（syntherisk/modules/m09_green.py）。
    # BHC 的三步里，加氢和羰基化都是把一个小分子整个加上去，原子经济性是 100%，所以它的平均分最高
    # （按现在 data/ 下的数据，三条路线这一维的平均分是 BHC 91.5、Boots 83.4、格氏羧基化 79.4）
    result = _result(client, account, analysis["id"], sort="green")
    assert result["excluded_routes"] == [] and len(result["routes"]) == 3
    assert result["routes"][0]["name"] == "BHC"
    # 按“最短”排：BHC 只有 3 步（另外两条是 4 步和 6 步）
    assert [route["name"] for route in _result(client, account, analysis["id"], sort="steps")["routes"]][0] == "BHC"


def test_complexity_tier_limits_the_number_of_steps(client, account):
    """A7 复杂度档位决定路线最多几步：选“简洁”档时，超过 3 步的路线被排除并写明步数。

    保证的行为：档位的步数上限来自 data/scoring.toml 的 [complexity.*]（简洁 3、平衡 6、探索 10），
    constraints.py 的 merge_constraints 把它放进实际执行的约束，m10_scoring.py 的 exclusion_reasons
    用它检查每条路线（约束编号 max_steps）。
    怎么读：只指定 complexity = concise 提交布洛芬。三条路线是 4 步、6 步、3 步，只有 3 步的 BHC 留下；
    另外两条的排除原因里写着各自的步数和上限。
    （“简洁”档还规定不允许有没有文献先例的步骤；先例库是空的时候这一条不检查，所以这里没有体现。）
    """
    analysis = start_analysis(client, account, constraints={"complexity": "concise"})
    result = _result(client, account, analysis["id"])
    assert [route["name"] for route in result["routes"]] == ["BHC"]
    # 把两条被排除路线的原因文字收到一个列表里；4 步的是格氏路线，6 步的是 Boots
    reasons = [reason["label"] for route in result["excluded_routes"] for reason in route["exclusion_reasons"]]
    assert "共 4 步，超过“简洁”档的 3 步上限" in reasons and "共 6 步，超过“简洁”档的 3 步上限" in reasons


def test_evidence_card_has_sources_for_every_statement(client, account):
    """B10 证据卡：一步反应的条件、产率、危害、原料、各维度分数都在卡上，说明里的每句话都有出处。

    保证的行为：GET /api/analyses/{id}/routes/{route_id}/steps/{n}（api/analyses.py 的 get_step_card）返回的卡
    由 syntherisk/cards.py 的 build_card 拼出来，各模块的评估结果都带着来源编号；
    M11 写的说明（syntherisk/modules/m11_explain.py）每一句都列出来源，而且只引用这张卡的 sources 里有的编号。
    没有数据的项目如实写“没有”：没有产率记录、文献依据没评估。
    怎么读：取格氏路线（r1）第 4 步格氏羧基化的卡。这一步的反应物是二氧化碳和上一步得到的氯代物，
    试剂是镁屑和乙醚（data/demo_routes/ibuprofen.json）。依次看：标题、条件和操作标签、产率、绿色化学、
    危害和管制、要准备的物料、六个维度、来源列表、说明；最后确认不存在的步骤返回 404。
    """
    analysis = start_analysis(client, account, constraints=LAB_WITHOUT_PRESSURE)
    card = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/4", headers=account["headers"]).json()

    # 标题是“第 N 步 · 反应名”；step_count 是这条路线一共几步
    assert card["title"] == "第 4 步 · 格氏羧基化" and card["step_count"] == 4
    # 条件说明是演示数据里写的那句概述，出处标成 DEMO（不是文献里查到的）
    assert card["conditions"]["text"].startswith("镁屑、无水乙醚") and card["conditions"]["source"] == "DEMO"
    # 操作标签（syntherisk/modules/m07_conditions.py）：这一步命中一条规则“无水无氧操作”，扣 22 分，
    # 操作分是 100 − 22 = 78，落在 50–79 的“中等”一档；“需无水”是这条规则的短名字。
    # 规则在 data/condition_rules.toml（id 是 anhydrous_organometallic），分档线在 data/scoring.toml 的 [operation]
    assert card["conditions"]["operation_label"] == "操作 中等 · 需无水"
    assert [tag["rule_id"] for tag in card["conditions"]["tags"]] == ["anhydrous_organometallic"]
    assert card["yield"]["label"] == "无产率记录"  # M8 没接入，也没有文献报道值：如实显示
    # 试剂里认出来的溶剂只有乙醚；它在 data/compounds.csv 里的溶剂评级是 highly_hazardous（高度有害）
    assert card["green"]["solvents"][0]["class"] == "highly_hazardous"  # 乙醚
    # 危害列表里找出乙醚这一项。next(生成器) 取第一个符合条件的
    ether = next(h for h in card["hazards"] if h["name"] == "乙醚")
    # 乙醚的 H 编码里有 H224（极易燃液体和蒸气）；它在中国、美国、加拿大三地的管制清单上都有
    # （data/controlled_substances.csv 里各有一行）
    assert "extremely_flammable" in ether["flags"] and {c["jurisdiction"] for c in ether["controlled"]} == {"CN", "US", "CA"}
    # 这一步要准备的物料：作为起始原料的反应物（二氧化碳）加上试剂（镁屑、乙醚）。
    # 另一个反应物氯代物是上一步做出来的中间体，不用准备，所以不在里面。三样都在内置试剂表里标着可购买
    assert {m["name"]: m["label"] for m in card["materials"]} == {"二氧化碳（干冰）": "可购买", "镁屑": "可购买", "乙醚": "可购买"}

    # 六个维度做成 {维度编号: 明细} 方便取
    dimensions = {d["key"]: d for d in card["dimensions"]}
    # 文献依据（先例库是空的）和产率（没有报道值）没有数据：分数是 None，不参加计分
    assert dimensions["literature"]["score"] is None and dimensions["yield"]["score"] is None
    # 操作 78 分：见上。可得性 70 分：三样物料都是“可购买”，在默认的“库存优先”档下，能买到的原料记 70 分
    # （data/scoring.toml 的 [availability] purchasable_inventory_first），平均还是 70
    assert dimensions["operation"]["score"] == 78 and dimensions["availability"]["score"] == 70

    # 这张卡可以引用的来源（cards.py 的 build_sources）。<= 用在集合上是“左边是右边的子集”
    source_ids = {source["id"] for source in card["sources"]}
    assert {"ROUTE", "RULE:anhydrous_organometallic", "GHS", "STOCK", "SCORE", "DEMO"} <= source_ids
    # 说明：没配大模型，由模板生成；至少三句
    explanation = card["explanation"]
    assert explanation["generated_by"] == "template" and len(explanation["sentences"]) >= 3
    # 每一句都有出处，而且出处都是这张卡的来源列表里有的编号
    for sentence in explanation["sentences"]:
        assert sentence["sources"] and set(sentence["sources"]) <= source_ids
    # 拼好的那段文字里，每句后面是方括号里的出处标记。RULE:anhydrous_organometallic 这个来源
    # 显示成“规则 · 气氛”（“气氛”是这条规则在 condition_rules.toml 里的 category）
    assert "[规则 · 气氛]" in explanation["text"]

    # 这条路线只有 4 步，要第 9 步返回 404
    missing = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/9", headers=account["headers"])
    assert missing.status_code == 404


def test_ask_answers_from_the_card_or_says_there_is_no_evidence(client, account):
    """C5 问一问：只根据这张证据卡回答，每句附出处；卡里没有依据的问题，直接说没有。

    保证的行为：POST …/steps/{n}/ask（api/analyses.py 的 ask_about_step，回答由 syntherisk/modules/m12_qa.py 的
    QuestionAnswerer 给出）。没有配置大模型时按问题里的关键词归类，只能答六类常见问题
    （分数、产率、文献、毒性、条件、原料），内容全部取自卡片；归不了类的问题返回固定的一句
    “证据卡里没有这项依据。”（m12_qa.py 的 NO_EVIDENCE），answerable 是 false。
    怎么读：对格氏路线第 4 步问两个问题。第一个问题里有“分”字，归到“分数”一类，能答；
    第二个问的是反应机理，卡上没有这类内容，也不属于那六类，答不了。
    """
    analysis = start_analysis(client, account, constraints=LAB_WITHOUT_PRESSURE)
    url = f"/api/analyses/{analysis['id']}/routes/r1/steps/4/ask"
    answer = client.post(url, headers=account["headers"], json={"question": "为什么这一步是这个分数？"}).json()
    # 模板的第一句是“这一步综合得分 …”，出处是 SCORE，在拼好的文字里显示成 [评分]
    assert answer["answerable"] and "综合得分" in answer["answer"] and "[评分]" in answer["answer"]
    # 回答拆成的每一句都带着出处
    assert all(sentence["sources"] for sentence in answer["sentences"])
    unknown = client.post(url, headers=account["headers"], json={"question": "这个反应的机理是什么？"}).json()
    assert not unknown["answerable"] and unknown["answer"] == "证据卡里没有这项依据。"


def test_marking_a_material_as_owned_turns_the_leaf_green_and_raises_coverage(client, account):
    """B12 标记为我有：原料记进库存，这次分析的库存覆盖和分数跟着更新，路线树上这个叶子变成“库存”。

    保证的行为：POST /api/analyses/{id}/mark-owned（api/analyses.py 的 mark_owned）
      - 往库存里加一条（syntherisk/modules/m04_availability.py 的 InventoryService.upsert，来源记 marked_owned），
        名字没给时用分析结果或内置试剂表里的；
      - 只给这一个分子重新判断可得性，然后重新评分（pipeline.py 的 rescore），不重新搜索；
      - 返回更新后的结果页数据；之后取路线详情（B4），路线树上这个起始原料的 status 是 inventory。
    怎么读：先记下格氏路线原来的库存覆盖（0/9），把第 1 步的原料异丁基苯（SMILES CC(C)Cc1ccccc1）标记为我有，
    再看返回的结果、路线树上的叶子、库存列表这三处。
    """
    analysis = start_analysis(client, account, constraints=LAB_WITHOUT_PRESSURE)
    before = _by_name(_result(client, account, analysis["id"])["routes"])["格氏羧基化"]
    # 4 个起始原料（异丁基苯、乙酸酐、盐酸、干冰）+ 5 个试剂（三氯化铝、硼氢化钠、甲醇、镁屑、乙醚）
    # inventory_count 是“库存里已有的 / 要准备的物料总数”。这个账号是新注册的，库存是空的，所以是 0/9
    assert before["inventory_count"] == "0/9" and before["inventory_coverage"] == 0

    response = client.post(f"/api/analyses/{analysis['id']}/mark-owned", headers=account["headers"],
                           json={"smiles": "CC(C)Cc1ccccc1", "quantity": "250 mL", "location": "试剂柜 B1"})
    assert response.status_code == 200, response.text
    body = response.json()
    # created：库存里原来没有这个结构，是新加的。请求里没给名字，用的是内置试剂表里的“异丁基苯”
    assert body["created"] and body["item"]["name"] == "异丁基苯"
    after = _by_name(body["result"]["routes"])["格氏羧基化"]
    # 库存覆盖变成 1/9。分数也高了：第 1 步的三样物料里，异丁基苯从“可购买”（70 分）变成“库存”（100 分），
    # 这一步的可得性分上去了，步骤分和路线分跟着上去（分值在 data/scoring.toml 的 [availability]）
    assert after["inventory_count"] == "1/9" and after["score"] > before["score"]

    # B4 路线详情里的路线树：根是目标分子，往下是得到它的反应和各个反应物，一直到起始原料（叶子）
    route = client.get(f"/api/analyses/{analysis['id']}/routes/r1", headers=account["headers"]).json()
    leaves = {}

    def collect(node):
        """从 node 往下走遍整棵路线树，把每个叶子（起始原料）的 名字 → 状态 记进外面的 leaves。

        分子节点的 children 是反应节点，反应节点的 children 又是分子节点，所以对每个孩子再调自己（递归）。
        """
        if node["type"] == "mol" and node["is_leaf"]:
            leaves[node["name"]] = node["status"]
        for child in node["children"]:
            collect(child)

    collect(route["tree"])
    # 异丁基苯现在是“库存”（界面上叶子变绿），同一步的另一个原料乙酸酐没动，还是“可购买”
    assert leaves["异丁基苯"] == "inventory" and leaves["乙酸酐"] == "purchasable"
    # 库存页上也查得到这一条，来源是 marked_owned（从结果页标记来的）
    inventory = client.get("/api/inventory/items", headers=account["headers"], params={"q": "异丁基苯"}).json()
    assert inventory["total"] == 1 and inventory["items"][0]["source"] == "marked_owned"
    assert inventory["items"][0]["updated_at"].endswith("+00:00")  # 重新从库里读出来的时间也带时区


def test_excluding_a_reagent_reranks_and_can_be_undone(client, account):
    """B11 排除一种试剂：用到它的路线被拿掉，其余的重新排序；可以撤销；重复排除被拒绝。

    保证的行为：POST /api/analyses/{id}/exclusions（api/analyses.py 的 add_exclusion）把排除记在这次分析上，
    用已有的评估结果重新过滤和排序（pipeline.py 的 rescore），不重新搜索；被这样排除的路线，原因的编号是
    user:<排除记录的 id>，并且 user 为 true（B6 放宽条件不会把它放回来，要用撤销）。
    同一种试剂再排除一次返回 409。DELETE …/exclusions/{id}（remove_exclusion）撤销以后路线回来。
    怎么读：不带约束提交布洛芬（三条路线都可选），排除盐酸羟胺（SMILES 写成 NO.Cl：羟胺和氯化氢两部分）。
    """
    analysis = start_analysis(client, account)
    url = f"/api/analyses/{analysis['id']}/exclusions"
    # 排除盐酸羟胺：只有 Boots 路线用到它
    # （它是 Boots 第 4 步“成肟”的反应物，另外两条路线里没有）
    response = client.post(url, headers=account["headers"], json={"type": "substance", "smiles": "NO.Cl"}).json()
    # research 是 False：还有可选的路线，不用重新搜索
    assert not response["research"]
    excluded = response["result"]["excluded_routes"]
    assert [route["name"] for route in excluded] == ["Boots"]
    # 排除原因只有一条。名字“盐酸羟胺”取自分析结果里这个分子的名字（内置试剂表）
    assert excluded[0]["exclusion_reasons"] == [
        {"constraint": f"user:{response['exclusion']['id']}", "label": "你排除了试剂：盐酸羟胺", "user": True}
    ]
    # 已经排除过的再排除一次：409
    assert client.post(url, headers=account["headers"], json={"type": "substance", "smiles": "NO.Cl"}).status_code == 409

    # 撤销：用排除时返回的那个 id。撤销以后没有被排除的路线了
    undone = client.delete(f"{url}/{response['exclusion']['id']}", headers=account["headers"]).json()
    assert undone["result"]["excluded_routes"] == []


def test_excluding_every_route_triggers_a_new_search(client, account):
    """B11 把可选的路线全部排除以后，后端自动重新搜索；这时去看进度，收到的是重新搜索这一轮的事件。

    保证的行为：add_exclusion 发现“原来有可选路线，排除以后一条都不剩”，就把这次分析改回排队状态、
    记一条 status: queued 事件、重新放进队列，返回 research = true 和分析的当前状态。
    进度推送（stream_events）对新连上来的前端只发最近一轮的事件（api/analyses.py 的 _round_start）。
    怎么读：不带约束提交，排除异丁基苯。演示引擎不会换路线，重新搜索回来的还是那三条，仍然全被排除，
    所以最后可选路线是空的。然后用 _events 读一遍进度推送，看事件的种类和个数。
    为什么重要：上一轮的事件还留在库里。要是从头发，前端一收到上一轮的 done 就会以为这次分析已经结束
    （api/analyses.py 的 stream_events 里说明了这一点）。
    """
    analysis = start_analysis(client, account)
    # 三条路线的第 1 步都要用异丁基苯：排除它以后一条可选的都不剩，于是重新搜索
    response = client.post(f"/api/analyses/{analysis['id']}/exclusions", headers=account["headers"],
                           json={"type": "substance", "smiles": "CC(C)Cc1ccccc1"}).json()
    assert response["research"] is True
    assert response["analysis"]["status"] == "succeeded"  # 测试里队列是同步的，重新搜索已经跑完
    result = _result(client, account, analysis["id"])
    assert result["routes"] == [] and len(result["excluded_routes"]) == 3
    # 这时去看进度，收到的是重新搜索这一轮：从“又排上队了”开始，只有一条 done
    # 第一轮也有一条 done 和一条 scored；这里各只收到一条，说明第一轮的事件没有被重放
    kinds = [kind for kind, _data in _events(client, account, analysis["id"])]
    assert kinds[0] == "status" and kinds.count("done") == 1 and kinds.count("scored") == 1


def test_progress_events_stream_ends_when_the_analysis_is_done(client, account):
    """A17 进度推送：事件按顺序发出，分析结束以后发 done 和 end，连接随之结束；令牌可以放在网址里。

    保证的行为：GET /api/analyses/{id}/events（api/analyses.py 的 stream_events）
      - 返回 text/event-stream（Server-Sent Events）；
      - 第一条是 status，然后是各阶段的 stage 事件，中间有 preliminary_ready（可以先看初步结果，A18）
        和 scored（结果页可以打开了），最后是 done 和 end；
      - 浏览器自带的 EventSource 不能加请求头，所以令牌可以写成查询参数 ?access_token=；
      - 不带令牌返回 401。
    怎么读：分析已经跑完，这里把存着的事件完整读一遍。这次没有带 Authorization 请求头，令牌放在 params 里。
    读回来的文本按空行切成事件，再从里面挑出事件类型和 stage 事件的内容来断言。
    """
    analysis = start_analysis(client, account)
    with client.stream("GET", f"/api/analyses/{analysis['id']}/events", params={"access_token": account["access_token"]}) as stream:
        assert stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream")
        text = "".join(stream.iter_text())
    # 每个事件是一段文本，段与段之间隔一个空行
    events = [block for block in text.split("\n\n") if block.strip()]
    # 每段里以 “event: ” 开头的那一行是事件类型；split(": ", 1) 只在第一个冒号处切一刀，取后半截
    types = [line.split(": ", 1)[1] for block in events for line in block.split("\n") if line.startswith("event: ")]
    # 第一条是 status（分析开始跑了），最后两条是 done（结束）和 end（推送到此为止，可以关连接）
    assert types[0] == "status" and types[-2:] == ["done", "end"]
    # preliminary_ready 在第 2 阶段完成时发，scored 在第 7 阶段完成时发，所以前者在前
    assert types.index("preliminary_ready") < types.index("scored")
    # 把 stage 事件的 data（JSON）都解析出来
    stages = [json.loads(block.split("data: ", 1)[1]) for block in events if "event: stage" in block]
    # 第一条 stage 事件：第 1 阶段开始，这时还没有 message
    assert stages[0] == {"index": 1, "status": "running", "message": ""}
    # 八个阶段各有一条 done，按 1 到 8 的顺序
    assert [stage["index"] for stage in stages if stage["status"] == "done"] == [1, 2, 3, 4, 5, 6, 7, 8]

    # 请求头和网址里都没有令牌：401
    assert client.get(f"/api/analyses/{analysis['id']}/events").status_code == 401


def test_unknown_molecule_finishes_with_an_honest_no_routes_message(client, account):
    """找不到路线的分子：分析照常“成功”结束，进度里写明原因，后面的阶段标成跳过；结构不合法的目标直接拒绝。

    保证的行为：
      - 第 2 阶段一条路线都没找到时，第 3–8 阶段标 skipped，分析的状态仍是 succeeded，route_count 是 0
        （pipeline.py 的 AnalysisRunner.run 和 _search）；第 2 阶段的 message 带上引擎给的提示；
      - 目标分子的结构读不出来时，POST /api/analyses 返回 422，不建任务（api/analyses.py 的 create_analysis）。
    怎么读：提交咖啡因（CN1C=NC2=C1C(=O)N(C(=O)N2C)C）。演示引擎只有布洛芬和阿司匹林的路线，
    对别的分子返回空结果和一句提示。然后提交 C1CC：数字 1 表示成环，这里只开了头、没有闭合，不是合法的 SMILES。
    """
    analysis = start_analysis(client, account, smiles="CN1C=NC2=C1C(=O)N(C(=O)N2C)C")
    assert analysis["status"] == "succeeded" and analysis["route_count"] == 0
    # 提示来自演示引擎（syntherisk/modules/m03_retrosynthesis.py 的 DemoEngine.search）
    assert "还没有接入 AiZynthFinder" in analysis["stages"][1]["message"]
    # stages[2:] 是第 3 到第 8 阶段，一共 6 个，全部跳过
    assert [stage["status"] for stage in analysis["stages"][2:]] == ["skipped"] * 6

    bad = client.post("/api/analyses", headers=account["headers"],
                      json={"project_id": account["project_id"], "target": {"smiles": "C1CC"}})
    assert bad.status_code == 422


def test_cancelled_and_failed_analyses_can_be_retried(client, account, ctx):
    """失败或取消的分析可以原样重跑；成功的不能重跑。重跑以后看进度，只收到新一轮的事件。

    保证的行为：POST /api/analyses/{id}/retry（api/analyses.py 的 retry_analysis）只接受状态是 failed 或
    cancelled 的分析，重跑时清掉上一次的错误信息，分析的 id 不变。流水线里任何一个阶段抛了异常，
    分析都记成 failed，原因写进 error，出错的那个阶段标 failed（pipeline.py 的 run_analysis）。
    重跑会先记一条 status: queued 事件；新连上来的进度推送从这条事件开始发（_round_start），
    前端自己带了 last_event_id 时则照它说的位置接着发。
    怎么读：先正常跑一次（成功），确认它不能重跑（409）；再把逆合成引擎换成一个一调用就抛错的替身，
    跑出一次失败的分析；换回正常的引擎以后重跑，状态变成 succeeded；最后读两遍进度推送。
    这个测试只做了“失败以后重跑”，取消以后重跑没有在这里做。
    """
    analysis = start_analysis(client, account)
    # 成功的分析不能重跑：409 表示“现在不能做这个操作”
    assert client.post(f"/api/analyses/{analysis['id']}/retry", headers=account["headers"]).status_code == 409

    class BrokenEngine:
        """一调用就抛错的假逆合成引擎，用来造出一次“失败的分析”。

        顶替的是 ctx.retro_engine（测试里本来是 m03_retrosynthesis.py 的 DemoEngine）。真的引擎返回路线，
        它在 search() 里直接抛异常，相当于“模型文件找不到”这类故障。只在这个测试里用。
        """

        name = "broken"

        def search(self, request, should_cancel=None):
            """和真引擎的 search 同样的参数；不搜索，直接抛 RuntimeError。"""
            raise RuntimeError("模型文件找不到")

    # 一行里同时做两件事：把原来的引擎存到 working，再把上下文里的引擎换成坏的。
    # ctx 和接口用的是同一个上下文对象，所以接下来提交的分析用的就是 BrokenEngine
    working, ctx.retro_engine = ctx.retro_engine, BrokenEngine()
    failed = start_analysis(client, account)
    # error 的格式是“异常类型: 说明”，所以里面有引擎抛出的那句话
    assert failed["status"] == "failed" and "模型文件找不到" in failed["error"]
    # 出错的是第 2 阶段（逆合成搜索），stages 的下标从 0 开始
    assert failed["stages"][1]["status"] == "failed"

    # 换回正常的引擎，重跑。返回的是同一次分析（id 不变）的当前状态
    ctx.retro_engine = working
    retried = client.post(f"/api/analyses/{failed['id']}/retry", headers=account["headers"]).json()
    assert retried["status"] == "succeeded" and retried["error"] is None

    # 重跑以后新连上来看进度：只收到这一轮的事件，不会先收到上一轮那条“失败”的 done
    events = _events(client, account, failed["id"])
    # 这一轮的头两条：重跑时记的 queued，然后是真正开始跑时记的 running
    assert events[0] == ("status", {"status": "queued"}) and events[1] == ("status", {"status": "running"})
    assert [data for kind, data in events if kind == "done"] == [{"status": "succeeded", "error": None}]
    # 前端自己说了从哪条事件接着发，就照它说的来：从第 1 条之后发，两轮的事件都在
    # 事件编号是整张事件表统一往下排的：编号 1 是本测试开头那次成功的分析的第一条事件，
    # 这一次分析（failed）的事件编号都比 1 大，所以它两轮的事件全都会发出来
    everything = _events(client, account, failed["id"], last_event_id=1)
    assert [data["status"] for kind, data in everything if kind == "done"] == ["failed", "succeeded"]


def test_another_group_cannot_see_or_touch_this_groups_analysis(client, account):
    """数据按课题组隔离：别的课题组看不到、也动不了这次分析；目标结构和结果在数据库里是加密的。

    保证的行为：
      - 接口取分析时都按登录者的课题组过滤（syntherisk/api/deps.py 的 load_analysis）。别的组的分析一律当作
        不存在，返回 404，而不是 403，不透露“有但你看不了”；分析列表里也没有它；
      - 不带令牌返回 401；
      - analyses 表的 target、result 两列是加密后存的（syntherisk/security.py 的 EncryptedJSON），
        直接查数据库看不到结构和路线内容。
    怎么读：账号甲提交一次分析；再注册一个属于另一个课题组的账号乙，用乙的令牌去调四个接口，都应该是 404；
    最后绕过接口，用原始 SQL 把这两列读出来，确认里面找不到明文。
    为什么重要：README 写明数据按课题组隔离，目标结构和分析结果在应用层加密以后再存。
    """
    analysis = start_analysis(client, account)
    other = register(client, username="other@example.com", org="隔壁课题组")
    # 四个接口各试一次：看状态、看结果页、看证据卡、取消
    for method, path in [
        ("get", f"/api/analyses/{analysis['id']}"),
        ("get", f"/api/analyses/{analysis['id']}/result"),
        ("get", f"/api/analyses/{analysis['id']}/routes/r1/steps/1"),
        ("post", f"/api/analyses/{analysis['id']}/cancel"),
    ]:
        # getattr(client, "get") 就是 client.get：按名字取出方法再调用
        assert getattr(client, method)(path, headers=other["headers"]).status_code == 404
    # 乙的分析列表是空的
    assert client.get("/api/analyses", headers=other["headers"]).json() == []
    # 完全不带令牌：401
    assert client.get(f"/api/analyses/{analysis['id']}").status_code == 401

    # 数据库里看不到明文：目标结构和结果都是加密后存的
    # text 把一段原始 SQL 包成可以执行的语句。这里直接用数据库引擎查表，不经过加密列的自动解密
    from sqlalchemy import text

    # client.app.state.ctx 就是后端的运行时上下文；.one() 表示结果必须正好一行
    with client.app.state.ctx.engine.connect() as connection:
        raw = connection.execute(text("SELECT target, result FROM analyses")).one()
    # raw[0] 是 target 列的密文：里面找不到布洛芬的 SMILES 和名字；raw[1] 是 result 列的密文：找不到路线名
    assert IBUPROFEN not in raw[0] and "布洛芬" not in raw[0] and "格氏" not in raw[1]
