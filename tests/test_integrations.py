"""和外部系统打交道的部分：PubChem、AiZynthFinder 的输出格式、Redis 队列、数据导入。

都不联网：PubChem 用假的 HTTP 传输，AiZynthFinder 用按它的格式手写的路线树，Redis 用 fakeredis。

---- 测的是哪些模块 ----

后端有四处要和“自己以外的东西”打交道。正式运行时它们分别是一个网站、另起的一个进程、另一个服务、
很大的数据文件；测试里都换成了替身，所以这个文件不联网，不用装 Redis，也不用下载模型和数据。

    PubChem（M1 名称查询、M6 危害分类）
        syntherisk/external.py               PubChemClient、collect_ghs、parse_ghs_codes、is_cas_number
        syntherisk/modules/m01_resolver.py   NameResolver.lookup（A3 按名称或 CAS 号查结构）
        syntherisk/modules/m06_safety.py     SafetyService（一次分析的第 5 阶段“毒性与法规”）
        syntherisk/cli.py                    cmd_check_hazards（命令行 check-hazards）
    AiZynthFinder（M3 逆合成搜索）
        syntherisk/routes.py                       build_skeleton、build_graph：把引擎的路线树整理成后端的结构
        syntherisk/modules/aizynth_bridge.py       run：真正调用 AiZynthFinder 的那段脚本
        syntherisk/modules/m03_retrosynthesis.py   build_engine、AiZynthEngine：选引擎、起子进程、报错
        syntherisk/pipeline.py                     引擎给的路线怎样走完可得性、先例、评分这些阶段
    Redis 队列（MQ）
        syntherisk/taskqueue.py 的 RedisQueue；syntherisk/worker.py 的 run_worker
    数据导入（M5 先例库、M4 在售目录）
        syntherisk/loaders.py                load_precedents、load_catalog、summarize
        syntherisk/db.py                     _add_missing_columns：给早先建的数据库补上后来加的列

用到的接口（都通过 client 夹具调）：
    POST /api/analyses                                   A16 开始分析（经 conftest 的 start_analysis）
    GET  /api/analyses/{id}                              A17 轮询进度
    GET  /api/analyses/{id}/events                       A17 进度推送（SSE）
    GET  /api/analyses/{id}/result                       结果页
    GET  /api/analyses/{id}/routes/{route_id}/steps/{n}  B10 证据卡
    POST /api/inventory/items                            E3 手动添加库存
    GET  /api/materials/availability                     C4 原料标签

---- 用到的夹具和替身 ----

来自 tests/conftest.py：
    ctx、client、account   临时数据库上的运行时上下文、测试客户端、一个注册好的账号
    make_settings          不用 ctx 夹具、自己建上下文或者只测配置的测试用它造配置
    start_analysis         提交一次分析。队列是同步的，返回时八个阶段已经跑完
    ASPIRIN                阿司匹林的 SMILES
pytest 自带的夹具：
    tmp_path               这个测试独有的临时目录
    capsys                 把测试期间打印到屏幕上的内容截下来
    monkeypatch            临时替换一个属性或字典里的一项，测试结束自动还原

本文件自己的替身（顶替的是什么、和真的差在哪）：
    httpx.MockTransport + pubchem_handler，以及各个测试里的 handler
        顶替“PubChem 网站”。PubChemClient 照常拼地址、发请求、解析响应，只是请求不出进程，
        由一个 Python 函数按地址决定回什么。所以客户端的限速、重试、解析都是真的在跑。
    ghs_page()、echa_summary() 和几个 *_PAGE 常量
        顶替 PubChem 的 “GHS Classification” 页面（一段 JSON）。结构照真实页面，内容缩减过。
    StubAiZynth（好几个测试里各有一个）
        顶替 ctx.retro_engine，也就是整个逆合成引擎：search() 不做搜索，直接返回事先写好的路线树。
        路线树由 _aizynth_tree() 按 AiZynthFinder 的输出格式手写。
    Stock、Policy、Finder，和塞进 sys.modules 的假 aizynthfinder 包
        顶替 AiZynthFinder 这个库本身，用来测桥接脚本 aizynth_bridge.run 调用它的先后次序。
    fakeredis.FakeRedis
        顶替 Redis 服务：一个进程内的假 Redis，RedisQueue 用到的命令它都支持。
    手写的 .rsmi 文件、InChIKey 清单
        顶替 USPTO 专利反应文件和在售清单文件，各只有两行数据。

---- 本文件里有什么（每个测试保证的行为）----

    PubChem
        test_cas_check_digit
            CAS 号要格式和校验位都对才算数
        test_ghs_page_is_read_by_source
            GHS 页面按来源读：欧盟统一分类加上多数企业申报；混合物条目、挂错的条目、口径很宽的来源不算
        test_name_lookup_falls_back_to_pubchem_and_survives_outages
            名称查询：内置试剂表没有的去问 PubChem；PubChem 连不上时不报错，只给一条提示
        test_pubchem_client_does_not_hammer_a_throttled_service
            被 PubChem 限流时只问一次、不重试；对方说负载偏高时自动放慢
        test_check_hazards_command_lists_where_the_table_and_pubchem_disagree
            check-hazards 命令列出内置试剂表和 PubChem 不一致的行；被限流时说清停在哪、怎么接着查
        test_pubchem_hazards_tell_no_data_apart_from_not_hazardous
            “查到了、不属于危险品”和“没有数据”是两回事；各种查询结果都缓存，第二次不再发请求
    AiZynthFinder
        test_aizynthfinder_route_trees_are_converted_and_deduplicated
            引擎格式的路线树整理成后端的结构；读不出来的路线跳过并提示，重复的路线只留一条
        test_pipeline_uses_the_engines_stock_flags_and_model_evidence
            库存和在售试剂表交给引擎当终点；引擎说在售的原料算可购买；模型依据进证据卡，但不算文献证据
        test_steps_with_atom_maps_are_matched_to_precedents_by_reaction_center
            带原子映射的步骤按反应中心认先例；先例库是旧版本导入的时退回按指纹比，并提醒重新导入
        test_incomplete_routes_from_the_engine_are_kept_and_reported
            引擎给的没拆到底的路线照样评估，进度里如实说明；一步都没拆的不算路线
        test_a_step_we_know_almost_nothing_about_is_not_scored_as_if_it_were_fine
            几乎没有数据的步骤不给“安全、操作简单”的满分，而是标成未评估、依据不足、低置信度
        test_bridge_adds_exclusions_after_aizynthfinder_has_reset_its_own_list
            用户排除的原料在 AiZynthFinder 清空自己的排除名单之后才加进去，确实生效
        test_engine_selection_and_bridge_error_reporting
            按配置选对引擎；配置文件、Python、依赖出问题时，报错里说清楚是哪一项
    队列与数据导入
        test_redis_queue_round_trips_jobs_in_order
            Redis 队列先进先出，空了以后取到 None
        test_api_enqueues_to_redis_and_a_separate_worker_runs_the_analysis
            API 只入队，Worker 取出来跑，进度经数据库回到 API（生产部署的形态）
        test_loading_precedents_changes_evidence_levels_in_the_next_analysis
            导入先例库：该拒绝的拒绝，统计数字对得上，多进程和单进程结果一样；
            导入以后，下一次分析的证据等级、产率、置信度、排名随之改变
        test_a_database_created_before_a_column_was_added_gets_it_on_startup
            早先建的数据库缺 center 这一列时，启动自动补上，原有的数据不动
        test_loading_a_purchasable_catalog
            导入在售目录以后，目录里的物质显示“可购买”；重复导入不会重复加
"""

import argparse
import sys
import threading
import types
from pathlib import Path

import fakeredis
import httpx
import pytest
from sqlalchemy import select, update

from syntherisk import cli, loaders, models
from syntherisk.context import build_context
from syntherisk.external import ExternalServiceError, PubChemClient, collect_ghs, is_cas_number, parse_ghs_codes
from syntherisk.modules import aizynth_bridge, m03_retrosynthesis
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m03_retrosynthesis import AiZynthEngine, EngineError, SearchRequest, SearchResult, build_engine
from syntherisk.modules.m05_precedents import precedent_from_reaction
from syntherisk.modules.m06_safety import SafetyService
from syntherisk.routes import build_graph, build_skeleton
from syntherisk.taskqueue import QUEUE_KEY, RedisQueue
from syntherisk.worker import run_worker
from tests.conftest import ASPIRIN, make_settings, start_analysis

# PubChem 的 GHS 页面上三种数据来源的机构名（页面里叫 SourceName）。external.collect_ghs 靠它们认来源：
#   HARMONIZED  欧盟统一分类（CLP 法规）。认的办法：名字里有 “1272/2008”
#   ECHA        欧洲化学品管理局汇总的企业申报。认的办法：名字以 “European Chemicals Agency” 开头
#   NITE        日本 NITE 的分类。在这里代表“其他来源”：前两种都没有时才用它
HARMONIZED = "Regulation (EC) No 1272/2008 of the European Parliament and of the Council"
ECHA = "European Chemicals Agency (ECHA)"
NITE = "NITE-CMC"


def echa_summary(reports: int, with_codes: int | None = None) -> list[str]:
    """造一段 ECHA“企业申报汇总”的文字，也就是页面上 “ECHA C&L Notifications Summary” 那一项的内容。

    external.collect_ghs 从这段文字里读两个数：这一组一共有多少份申报，其中多少份报了危害。
    页面上每条危险性说明后面的百分比只在“报了危害的申报”里算，
    要乘上 with_codes ÷ reports，才是占全部申报的比例。
    参数：
        reports     这一组一共有多少份申报。
        with_codes  其中报了危害（带 H 编码）的有多少份。None 表示页面上没有这句话，那就不折算。
    返回：
        一行或三行英文句子的列表，句式照着真实页面。句子里的 “27 notifications”“24 notifications”
        是固定写上去的数，解析时不取它们的值。
    谁在用：下面几个 *_PAGE 常量。
    """
    lines = [f"Aggregated GHS information provided per {reports} reports by companies from 27 notifications to the ECHA C&L Inventory."]
    # 给了 with_codes 才加后两句：多少份报的是“不属于危险品”，多少份带了危害编码
    if with_codes is not None:
        lines += [f"Reported as not meeting GHS hazard criteria per {reports - with_codes} of {reports} reports by companies.",
                  f"There are 24 notifications provided by {with_codes} of {reports} reports by companies with hazard statement code(s)."]
    return lines


def ghs_page(*blocks: tuple) -> dict:
    """照 PubChem “GHS Classification” 页面的结构造一页。每个来源一块：(来源, 条目名, 危险性说明, ECHA 汇总)。

    顶替的是 PubChemClient.ghs_page() 从 PubChem 取回来的那段 JSON。只造了解析时用得到的部分：
        Record.Reference   来源的列表：每个来源一个编号（ReferenceNumber）、机构名（SourceName）、条目名（Name）
        Record.Section     一层套一层的章节。最里面的 Information 列表里，每一项用 ReferenceNumber 指回来源；
                           Name 是 “GHS Hazard Statements” 的项是危险性说明，
                           Name 是 “ECHA C&L Notifications Summary” 的项是企业申报的汇总文字
    参数：
        blocks  每个来源一个元组，三项或者四项：
                来源        机构名，用上面的 HARMONIZED / ECHA / NITE
                条目名      这个来源给这条记录起的名字。统一分类靠它排除混合物条目；
                            ECHA 的条目名用来认这个物质自己叫什么
                危险性说明  字符串的列表，每条形如 “H225 (97.6%): Highly Flammable liquid and vapor [Danger …]”
                ECHA 汇总   可以不给；给的话是 echa_summary() 的返回值
    返回：
        和真实页面结构相同的字典。一个来源都不给（ghs_page()）得到的是“有页面的外壳、但没有任何分类”的空页。
        每个来源另外都带一项 “Pictogram(s)”（内容是一个空格）：这一项不是危险性说明，解析时应当被跳过。
    谁在用：下面几个 *_PAGE 常量；test_ghs_page_is_read_by_source 里直接调 ghs_page() 造空页。
    """
    references, information = [], []
    # enumerate(…, start=1)：来源编号从 1 数起。
    # (source, name, statements, *summary)：把元组拆开，*summary 接住第三项以后剩下的部分，没给第四项时是空列表
    for number, (source, name, statements, *summary) in enumerate(blocks, start=1):
        references.append({"ReferenceNumber": number, "SourceName": source, "Name": name})
        information.append({"ReferenceNumber": number, "Name": "Pictogram(s)", "Value": {"StringWithMarkup": [{"String": " "}]}})
        # 危险性说明、ECHA 汇总各加一项。字符串列表是空的就不加（比如“水”那个条目一条危险性说明都没有）
        for field, strings in (("GHS Hazard Statements", statements), ("ECHA C&L Notifications Summary", summary[0] if summary else [])):
            if strings:
                information.append({"ReferenceNumber": number, "Name": field,
                                    "Value": {"StringWithMarkup": [{"String": s} for s in strings]}})
    # 套上真实页面的外壳：Record → Section（Safety and Hazards）→ Section（GHS Classification）→ Information
    return {"Record": {"Section": [{"TOCHeading": "Safety and Hazards", "Section": [
        {"TOCHeading": "GHS Classification", "Information": information}]}], "Reference": references}}


# 下面几页是照 2026-10 实际抓到的页面缩写的
# 每一页针对读页面的一条规则；读出来应当是什么，都在 test_ghs_page_is_read_by_source 里核对。
#
# ISOPROPANOL_PAGE（异丙醇）：三种来源都有。
#   统一分类有两条：第一条是异丙醇自己的（H225、H319、H336）；第二条 “reaction mass of: ...” 是含异丙醇的混合物，
#   它的 H318、H373、H410 不能算在异丙醇头上。
#   ECHA 的企业申报（8845 份里 8842 份报了危害）给的 H225、H336，统一分类里本来就有。
#   NITE 另外报了 H361、H370，口径比前两个来源宽。
ISOPROPANOL_PAGE = ghs_page(
    (HARMONIZED, "propan-2-ol; isopropyl alcohol; isopropanol", [
        "H225: Highly Flammable liquid and vapor [Danger Flammable liquids]",
        "H319: Causes serious eye irritation [Warning Serious eye damage/eye irritation]",
        "H336: May cause drowsiness or dizziness [Warning Specific target organ toxicity, single exposure; Narcotic effects]"]),
    (HARMONIZED, "reaction mass of: ...", [
        "H318: Causes serious eye damage [Danger Serious eye damage/eye irritation]",
        "H373 **: May causes damage to organs through prolonged or repeated exposure [Warning Specific target organ toxicity, repeated exposure]",
        "H410: Very toxic to aquatic life with long lasting effects [Warning Hazardous to the aquatic environment, long-term hazard]"]),
    (ECHA, "Propan-2-ol (EC: 200-661-7)", [
        "H225 (> 99.9%): Highly Flammable liquid and vapor [Danger Flammable liquids]",
        "H336 (98.7%): May cause drowsiness or dizziness [Warning Specific target organ toxicity, single exposure; Narcotic effects]"],
     echa_summary(8845, 8842)),
    (NITE, "Isopropyl alcohol - FY2013 (Revised classification)", [
        "H361: Suspected of damaging fertility or the unborn child [Warning Reproductive toxicity]",
        "H370: Causes damage to organs [Danger Specific target organ toxicity, single exposure]"]),
)
# DMSO_PAGE（二甲基亚砜）：没有统一分类。ECHA 的 938 份申报里只有 327 份报了危害；
#   页面上写的 28.4%、33.6% 是在这 327 份里算的，折成占全部申报的比例只有 9.9%、11.7%。
DMSO_PAGE = ghs_page(
    (ECHA, "Dimethyl sulfoxide (EC: 200-664-3)", [
        "H315 (28.4%): Causes skin irritation [Warning Skin corrosion/irritation]",
        "H319 (33.6%): Causes serious eye irritation [Warning Serious eye damage/eye irritation]"],
     echa_summary(938, 327)),
    (NITE, "Dimethyl sulfoxide - FY2019 (New/original classication)", [
        "H371: May cause damage to organs [Warning Specific target organ toxicity, single exposure]"]),
)
# WATER_PAGE（水）：页面上有两组 ECHA 申报。第一组是挂在水的页面上的一个聚合物条目（39 份申报，H315 100%），
#   第二组才是水自己的（1876 份申报，一条危险性说明都没有）。应当只看申报数最多的那一组。
WATER_PAGE = ghs_page(
    (ECHA, "POLY{6-[4-(4'-CYANOPHENYL)PHENOXY]HEXYL METHACRYLATE} (EC: 634-210-4)", [
        "H315 (100%): Causes skin irritation [Warning Skin corrosion/irritation]"], echa_summary(39)),
    (ECHA, "Water (EC: 231-791-2)", [], echa_summary(1876, 10)),
)
# TOXIC_PAGE：编出来的一种物质，用来看几种写法都认得出来：带字母后缀的编码（H360FD）、
#   合并写法（H300+H310+H330，三个编码共用一个百分比）、够不上 30% 的编码（H319，14.1%）。
#   200 份申报里 190 份报了危害，所以页面上的百分比都要乘 190 ÷ 200 = 0.95。
TOXIC_PAGE = ghs_page(
    (HARMONIZED, "a made-up substance", [
        "H360FD: May damage fertility; May damage the unborn child [Danger Reproductive toxicity]"]),
    (ECHA, "A made-up substance (EC: 000-000-0)", [
        "H225 (97.6%): Highly Flammable liquid and vapor [Danger Flammable liquids]",
        "H300+H310+H330 (63.1%): Fatal if swallowed, in contact with skin or if inhaled [Danger Acute toxicity]",
        "H319 (14.1%): Causes serious eye irritation [Warning Serious eye damage/eye irritation]"],
     echa_summary(200, 190)),
)
# SALT_PAGE（氯化钠）：统一分类那一条其实是一种含氯化钠的染料（条目名里有 product-by-process、obtained by），
#   它的 H318、H412 不是氯化钠自己的。ECHA 的 2063 份申报里只有 223 份报了危害，H319 折算以后是 6.2%。
SALT_PAGE = ghs_page(
    (HARMONIZED, "product-by-process definition polyazodyestuff obtained by coupling diazotised ...; sodium chloride", [
        "H318: Causes serious eye damage [Danger Serious eye damage/eye irritation]",
        "H412: Harmful to aquatic life with long lasting effects [Hazardous to the aquatic environment, long-term hazard]"]),
    (ECHA, "Sodium chloride (EC: 231-598-3)", [
        "H319 (57%): Causes serious eye irritation [Warning Serious eye damage/eye irritation]"], echa_summary(2063, 223)),
)
# NITE_ONLY_PAGE：只有日本 NITE 一个来源。前两种来源都没有时，才退回用它。
NITE_ONLY_PAGE = ghs_page(
    (NITE, "Something - FY2009", ["H302: Harmful if swallowed [Warning Acute toxicity, oral]"]),
)
# TOXIC_PAGE 按默认规则（统一分类 + 占全部申报 30% 以上的企业申报）应当读出来的 H 编码：
#   H225 高度易燃液体和蒸气            97.6% × 0.95 = 92.7%
#   H300、H310、H330 吞咽、皮肤接触、吸入致命   63.1% × 0.95 = 59.9%
#   H360FD 生殖毒性                    来自统一分类，不看比例
# 页面上的 H319（14.1% × 0.95 = 13.4%）不到 30%，不在里面。
TOXIC_CODES = ["H225", "H300", "H310", "H330", "H360FD"]
# 咖啡因的 InChI（另一种用文字写结构的格式）。假的 PubChem 在“取性质”时把它当作结构返回，
# 名称查询把它读成分子，再自己算出分子式 C8H10N4O2。
CAFFEINE_INCHI = "InChI=1S/C8H10N4O2/c1-10-4-9-6-5(10)7(13)12(3)8(14)11(6)2/h4H,1-3H3"


def pubchem_handler(request: httpx.Request) -> httpx.Response:
    """假的 PubChem：只认识咖啡因一种物质。交给 httpx.MockTransport 用。

    顶替的是 PubChem 网站的两套接口（PUG REST 和 PUG View）。PubChemClient 每发一个请求，MockTransport 就调一次
    这个函数，把它返回的 httpx.Response 当作“网站的响应”，请求不会真的发出去。按请求的地址分几种情况：
        名称 → CID（POST …/compound/name/cids/JSON，名称在表单里）   名称里有 caffeine 回 CID 2519，否则回 404
        结构 → CID（…/compound/inchikey/…）                          不管问的是哪个结构，一律回 CID 2519
        取性质（…/property/…）                                       回标题 Caffeine、咖啡因的 InChI、分子式
        取别名（…/synonyms/JSON）                                    回三个别名，其中 58-08-2 是 CAS 号
        取 GHS 页面（…/pug_view/…）                                  回 TOXIC_PAGE（编的分类，不是咖啡因真实的）
        别的地址                                                     回 500：说明测试发了没料到的请求
    CID 是 PubChem 给每个化合物的编号。404 在 PubChemClient 里的意思是“查无此物”，返回空结果，不算故障。
    谁在用：test_name_lookup_falls_back_to_pubchem_and_survives_outages。
    """
    path = request.url.path
    if path.endswith("/compound/name/cids/JSON"):
        # POST 的表单内容是字节，decode() 转成文字（形如 name=caffeine）
        name = request.content.decode()
        return httpx.Response(200, json={"IdentifierList": {"CID": [2519]}}) if "caffeine" in name else httpx.Response(404)
    if "/compound/inchikey/" in path:
        return httpx.Response(200, json={"IdentifierList": {"CID": [2519]}})
    if "/property/" in path:
        return httpx.Response(200, json={"PropertyTable": {"Properties": [
            {"CID": 2519, "Title": "Caffeine", "InChI": CAFFEINE_INCHI, "MolecularFormula": "C8H10N4O2"}]}})
    if path.endswith("/synonyms/JSON"):
        return httpx.Response(200, json={"InformationList": {"Information": [{"Synonym": ["caffeine", "58-08-2", "Guaranine"]}]}})
    if "/pug_view/" in path:
        return httpx.Response(200, json=TOXIC_PAGE)
    return httpx.Response(500)


def test_cas_check_digit():
    """CAS 号要格式和校验位都对才算数：抄错一位的认得出来，不是 CAS 号的文字也不会被误认。

    保证的行为：syntherisk/external.py 的 is_cas_number。
    用在哪：A3 按名称或 CAS 号查结构、E1 导入库存时，用它判断一格文字是不是 CAS 号（m01_resolver.py）。
    导入库存时，校验位不对的 CAS 号不拿去按 CAS 号查，而是记一条“CAS 号的校验位不对”；
    这一行即使靠名称认出来了，也要用户确认（m01_resolver.NameResolver.resolve_row）。
    怎么读：一句断言里三个例子。
        15687-27-1   布洛芬的 CAS 号（data/compounds.csv 里 ibuprofen 那一行）。校验位的算法：
                     去掉连字符和最后一位得到 1568727，从右往左依次乘 1、2、3……再相加：
                     7×1 + 2×2 + 7×3 + 8×4 + 6×5 + 5×6 + 1×7 = 131，除以 10 的余数是 1，和最后一位相同
        15687-27-2   把最后一位改成了 2，对不上
        乙醚         根本不是“数字-数字-数字”的格式
    """
    assert is_cas_number("15687-27-1") and not is_cas_number("15687-27-2") and not is_cas_number("乙醚")


def test_ghs_page_is_read_by_source():
    """PubChem 的 GHS 页面按来源读：取欧盟统一分类，加上占全部企业申报 30% 以上的分类；别的都不算。

    保证的行为：syntherisk/external.py 的 collect_ghs（把页面按来源拆开）和 parse_ghs_codes（定出这个物质的 H 编码）。
    规则见 README“危害分类取哪个来源”；30 这个门槛是 data/scoring.toml 的 pubchem_min_share。
    用在哪：M6 给内置试剂表以外的物质查危害（m06_safety.SafetyService._from_pubchem），
    以及 check-hazards 命令拿内置试剂表和 PubChem 对账。
    怎么读：不发请求，直接把文件开头造好的几页交给两个函数。每一组断言上面原有的那行注释，说的是它针对哪条规则；
    各个数字是怎么来的，见每一页常量上方的说明。
    为什么重要：危害分类直接决定路线会不会被排除（勾了“排除剧毒”“排除致癌、致突变、生殖毒性物质”的时候）。
    读宽了，常用溶剂都会被筛掉（README 举的例子：日本的分类把乙醇列为致癌）；读窄了，剧毒物质会漏过去。
    另外，“没有数据”（None）和“不属于危险品”（空列表）必须分开：前者在界面上显示“没有危害数据”，不当作安全。
    """
    # 统一分类 + 多数企业申报；合并写法（H300+H310+H330）拆开；带后缀和星号的编码照样认
    # 第二句把门槛提到 70%：H300、H310、H330 折算后是 59.9%，不够了；H225 是 92.7%；统一分类的 H360FD 不看比例
    assert parse_ghs_codes(TOXIC_PAGE) == TOXIC_CODES
    assert parse_ghs_codes(TOXIC_PAGE, min_percent=70) == ["H225", "H360FD"]
    # 挂在同一页上的混合物条目、口径很宽的日本分类，都不算这个物质的
    # collect_ghs 返回的字典里，other 是“其他来源”给的编码：照样收着，只是 parse_ghs_codes 在这里不采用
    assert parse_ghs_codes(ISOPROPANOL_PAGE) == ["H225", "H319", "H336"]
    assert collect_ghs(ISOPROPANOL_PAGE)["other"] == ["H361", "H370"]
    # 页面上的百分比只在“报了危害的公司”里算：DMSO 有 65% 的申报是“不属于危险品”，折算后都不到 30%
    # 327 ÷ 938 ≈ 0.349：28.4% × 0.349 = 9.9%，33.6% × 0.349 = 11.7%；（938 − 327）÷ 938 ≈ 65% 就是上面说的那 65%。
    # 默认门槛下的结果是空列表而不是 None：有申报数据，结论是不属于危险品。把门槛降到 10%，H319（11.7%）才进来
    assert collect_ghs(DMSO_PAGE)["echa"] == {"H315": 9.9, "H319": 11.7}
    assert parse_ghs_codes(DMSO_PAGE) == [] and parse_ghs_codes(DMSO_PAGE, min_percent=10) == ["H319"]
    # 水的页面上挂着一个不相干的聚合物条目：只看申报数最多的那一组
    # 39 份申报的那一组带着 H315（100%）。要是把两组混在一起，水就会被读成“造成皮肤刺激”
    assert parse_ghs_codes(WATER_PAGE) == [] and collect_ghs(WATER_PAGE)["echa_reports"] == 1876
    # 氯化钠的页面上挂着一条含氯化钠的染料的统一分类（H318）：那不是氯化钠自己的
    # 企业申报的 H319 折算以后是 57% × 223 ÷ 2063 = 6.2%，也不采用，所以最后是空列表
    assert collect_ghs(SALT_PAGE)["harmonized"] == [] and parse_ghs_codes(SALT_PAGE) == []
    # 前两个来源都没有时才用其他来源；什么都没有是“没有数据”，不是“不属于危险品”
    # ghs_page() 是一张一个来源都没有的空页，{} 是连页面的外壳都没有：两种都返回 None
    assert parse_ghs_codes(NITE_ONLY_PAGE) == ["H302"]
    assert parse_ghs_codes(ghs_page()) is None and parse_ghs_codes({}) is None


def test_name_lookup_falls_back_to_pubchem_and_survives_outages(tmp_path):
    """名称查询：内置试剂表里没有的去问 PubChem；PubChem 给的危害分类存进数据库；PubChem 连不上时不报错。

    保证的行为（三件事，在同一份上下文上依次验证）：
        1. A3 按名称查结构（m01_resolver.NameResolver.lookup，接口是 GET /api/structures/lookup）：
           内置试剂表查不到的名称，用 PubChem 查到的结构、名字、CAS 号当候选；
           哪里都查不到时返回空列表，不算出错。
        2. M6 查危害（m06_safety.SafetyService.assess）：内置试剂表里没有的物质去问 PubChem，
           结果写进 substance_hazards 表当缓存。
        3. PubChem 连不上：客户端抛 ExternalServiceError；名称查询把它接住，返回空的候选和一句给用户看的提示。
    怎么读：这个测试不用 ctx 夹具（夹具建的上下文里 PubChem 是关着的），而是自己调 build_context，
    用 pubchem= 参数换上一个接着假传输的客户端；所以结尾要自己收尾（dispose）。
    先用认识咖啡因的假 PubChem（pubchem_handler）做第 1、2 件事，再换成一发请求就“连不上”的传输做第 3 件。
    为什么重要：PubChem 会限流、会连不上（README 记着这台电脑被它限流过两次）。它一出问题就让整个请求失败的话，
    名称查询和分析都会跟着用不了；现在的做法是降级成只用内置试剂表，并在页面上给出提示。
    """
    # httpx.MockTransport(函数)：httpx 自带的假传输层。客户端每发一个请求，就调一次这个函数拿响应，不走网络。
    # min_interval=0：两次请求之间不等待（正式运行时至少隔 0.34 秒）
    pubchem = PubChemClient(transport=httpx.MockTransport(pubchem_handler), min_interval=0)
    # 传了 pubchem=，上下文就用这个客户端，不看配置里 ENABLE_PUBCHEM 是开还是关
    ctx = build_context(make_settings(tmp_path), pubchem=pubchem)
    # ctx.resolver 每次访问都新建一个 NameResolver，用的是 ctx 上当前的 pubchem。
    # lookup 返回 (候选结构的列表, 提示的列表)；with_svg=False 是不给候选画结构小图
    candidates, warnings = ctx.resolver.lookup("caffeine", with_svg=False)
    assert warnings == [] and len(candidates) == 1
    # 内置试剂表里没有咖啡因，这个候选来自 PubChem：名字是它的标题，CAS 号是别名里第一个校验位正确的。
    # 分子式不是照抄 PubChem 的，是后端把它给的 InChI 读成分子以后自己算的
    assert (candidates[0].name, candidates[0].cas, candidates[0].source, candidates[0].formula) == ("Caffeine", "58-08-2", "PubChem", "C8H10N4O2")
    # 内置试剂表里有的：排第一的候选来自内置试剂表，CAS 号是 data/compounds.csv 里布洛芬那一行登记的。
    # （候选不到 5 个时 lookup 仍然会去问一次 PubChem 看有没有别的候选；这里的假 PubChem 对它回 404）
    local, _ = ctx.resolver.lookup("布洛芬", with_svg=False)
    assert local[0].source == "内置试剂表" and local[0].cas == "15687-27-1"
    # 表里没有，假 PubChem 也回 404（查无此物），OPSIN 在测试配置里是关着的：候选和提示都是空的，不抛异常
    assert ctx.resolver.lookup("不存在的东西", with_svg=False) == ([], [])

    # PubChem 查到的危害分类会缓存进数据库，下次不再请求
    # with ctx.session_factory() as session：开一个数据库会话，离开 with 时自动关闭
    with ctx.session_factory() as session:
        # 第四个参数是要看哪些地区的管制清单（这个测试用不到）。
        # 被评估的是咖啡因的 SMILES。假 PubChem 对任何结构都回 CID 2519，GHS 页面回的是编出来的 TOXIC_PAGE
        service = SafetyService(session, ctx.reference, pubchem, ["CA", "US", "CN", "TW"])
        hazard = service.assess("CN1C=NC2=C1C(=O)N(C(=O)N2C)C")
        # data_source 是 pubchem：数据来自 PubChem，不是内置表。flags 是 H 编码归出来的危害类别：
        # H300、H310、H330 归到 acute_toxic_12（剧毒），H360FD 归到 cmr_1（致癌、致突变或生殖毒性）
        assert hazard["data_source"] == "pubchem" and "acute_toxic_12" in hazard["flags"] and "cmr_1" in hazard["flags"]
        session.commit()
        # 缓存表 substance_hazards 的主键是 key14。这里核对的是缓存确实写进去了；
        # “下次不再请求”在 test_pubchem_hazards_tell_no_data_apart_from_not_hazardous 里验证
        assert session.get(models.SubstanceHazard, hazard["key14"]).h_codes == TOXIC_CODES

    def down(_request):
        """一发请求就抛“连不上”，模拟断网。httpx.ConnectError 是 httpx 自己的网络错误类型。"""
        raise httpx.ConnectError("no network")

    offline = PubChemClient(transport=httpx.MockTransport(down), min_interval=0)
    # pytest.raises(异常类型)：with 块里的代码必须抛出这种异常，测试才算通过；没抛反而算失败。
    # 客户端遇到网络错误会试 3 次，都不行才抛 ExternalServiceError
    with pytest.raises(ExternalServiceError):
        offline.cids_by_name("caffeine")
    # 把上下文里的客户端换成连不上的那个，再查一次同样的名称
    ctx.pubchem = offline
    candidates, warnings = ctx.resolver.lookup("caffeine", with_svg=False)
    # 完整的提示是“PubChem 暂时连不上，这次没有查它，结果可能不全”
    assert candidates == [] and "PubChem 暂时连不上" in warnings[0]
    # 这份上下文是测试自己建的，没有夹具替它收尾：关掉数据库连接
    ctx.engine.dispose()


def test_pubchem_client_does_not_hammer_a_throttled_service():
    """被 PubChem 限流时只问一次、不重试；对方报告负载偏高时，之后的请求自动放慢。

    保证的行为：syntherisk/external.py 的 _ThrottledClient（PubChemClient 建在它上面）：
        收到 429（请求太多，被限流）               不重试，直接抛 ExternalServiceError，消息里有“限流”两个字
        响应头 X-Throttling-Control 里出现 Yellow   两次请求之间的间隔调到至少 1 秒
    怎么读：造一个假传输：地址里带 BLOCKED 的回 429，别的回 200，并带一个“请求数到了 Yellow”的响应头；
    calls 记下它收到的每一个请求。先问一个会被限流的，数一数只发了一次请求；
    再问一个正常的，看客户端现在用的间隔变成了多少。
    为什么重要：README 记着这台电脑的 IP 被 PubChem 限流过两次。被限流以后马上重试，只会让限流持续得更久
    （external.py 里 request_json 的注释），所以宁可这一次查不到，让调用方降级。
    """
    # 假传输收到的每个请求的地址，按先后记在这里
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """假的 PubChem：地址里有 BLOCKED 就回 429；否则回 CID 1，并在响应头里说“请求数到了黄色”。"""
        calls.append(request.url.path)
        if "BLOCKED" in request.url.path:
            return httpx.Response(429, text="429 Too Many Requests")
        return httpx.Response(200, json={"IdentifierList": {"CID": [1]}}, headers={
            "X-Throttling-Control": "Request Count status: Yellow (60%), Request Time status: Green (9%), Service status: Green (20%)"})

    client = PubChemClient(transport=httpx.MockTransport(handler), min_interval=0)
    # 被限流时只问一次，不反复重试
    # match="限流"：除了异常的类型，还要求异常的消息里找得到这两个字（按正则表达式找）
    with pytest.raises(ExternalServiceError, match="限流"):
        client.cids_by_inchikey("BLOCKED")
    # 网络错误和 500 一类的错误，一次调用里最多会试 3 次；429 试一次就停
    assert len(calls) == 1
    # 对方说快到上限了：之后的请求放慢
    assert client.cids_by_inchikey("FINE") == [1]
    # _client 是 PubChemClient 里面的 _ThrottledClient，_interval 是它现在实际用的请求间隔（秒）。
    # 出现 Yellow 时的规则是 max(最小间隔 × 3, 1.0)；这里最小间隔是 0，所以是 1.0。
    # 测试到这里就结束了，不会真的去等这一秒
    assert client._client._interval >= 1.0


def test_check_hazards_command_lists_where_the_table_and_pubchem_disagree(ctx, capsys):
    """check-hazards 命令：逐个核对内置试剂表和 PubChem 的危害分类，只列出不一致的行；被限流时说清停在哪。

    保证的行为：syntherisk/cli.py 的 cmd_check_hazards（命令行 python -m syntherisk.cli check-hazards），
    比对本身由 m06_safety.compare_with_pubchem 做。界面上没有对应的按钮，是给维护 data/compounds.csv 的人用的。
        一致的行不打印；不一致的打印“表里没有 … / 表里多出 …”；PubChem 上没有分类的打印“没有数据”；然后是一行合计
        中途请求失败（比如被限流）：立刻停下，说明查到哪个物质时停的、已经查完的统计、接着查该用的命令
        --only 给了表里没有的 key：直接退出并说明
    怎么读：
        准备  从 ctx.reference（内置试剂表）里取异丙醇、甲醇、丙酮三行。造一个假 PubChem：异丙醇、甲醇各有一个 CID，
              取它们的 GHS 页面时一律回异丙醇的页面；丙酮一问就回 429；别的物质、以及按名称（CAS 号）查，都回 404。
        调用  不经过命令行解析，直接调 cmd_check_hazards(ctx, args, pubchem=假客户端)。
              args 用 argparse.Namespace 现造，相当于命令行里写了 --only …。
        断言  用 capsys 截下打印的内容，逐行核对；再看两种退出的情况。
    为什么重要：表里的危害编码直接决定路线会不会因为“剧毒”“致癌”被排除（README“危害数据会直接影响排除结果”），
    而这张表是人维护的，这个命令是发现它和 PubChem 对不上的办法。它一次要查上百个物质，又查得很慢，
    中途被限流以后必须能从停下的地方接着查。
    """
    # by_key：内置试剂表按 key 查（data/compounds.csv 的第一列），得到那一行的 Compound 对象
    compounds = ctx.reference.compounds.by_key
    # {完整的 InChIKey: CID}：假 PubChem 认识的两个物质，各给一个编号
    cids = {chem.mol_record(compounds[key].smiles).inchikey: cid for key, cid in (("isopropanol", 3776), ("methanol", 887))}
    # 丙酮的 InChIKey：请求的地址里出现它，假 PubChem 就回 429（被限流）
    blocked = chem.mol_record(compounds["acetone"].smiles).inchikey

    def handler(request: httpx.Request) -> httpx.Response:
        """假的 PubChem，按请求的地址分四种情况，见下面逐段的注释。"""
        path = request.url.path
        # 问到丙酮：限流
        if blocked in path:
            return httpx.Response(429, text="429 Too Many Requests")
        # 按结构查 CID：从地址里把 InChIKey 截出来，认识的回它的 CID，不认识的回 404（查无此物）
        if "/compound/inchikey/" in path:
            cid = cids.get(path.split("/compound/inchikey/")[1].split("/")[0])
            return httpx.Response(200, json={"IdentifierList": {"CID": [cid]}}) if cid else httpx.Response(404)
        # 按名称查 CID：按结构查不到分类时，命令会再拿 CAS 号当名称查一次。这里一律查不到
        if path.endswith("/compound/name/cids/JSON"):
            return httpx.Response(404)
        # 剩下的是取 GHS 页面
        return httpx.Response(200, json=ISOPROPANOL_PAGE)  # 两个物质都回异丙醇的页面：甲醇那一行就该对不上

    pubchem = PubChemClient(transport=httpx.MockTransport(handler), min_interval=0)
    # 相当于命令行：check-hazards --only isopropanol,methanol,4_isobutylacetophenone。
    # argparse.Namespace 是命令行解析结果的类型，各个选项是它的属性；这里直接造一个，跳过解析
    args = argparse.Namespace(only="isopropanol,methanol,4_isobutylacetophenone", start_from=None)
    cli.cmd_check_hazards(ctx, args, pubchem=pubchem)
    # capsys 是 pytest 自带的夹具：测试期间打印的内容被它截下来，readouterr().out 是打印到标准输出的全部文字
    lines = capsys.readouterr().out.splitlines()
    # 三个物质的结果（表里的编码见 data/compounds.csv 的 h_codes 一列）：
    #   异丙醇  表里是 H225;H319;H336，假 PubChem 的页面读出来也是这三个：一致，不打印
    #   甲醇    表里是 H225;H301;H311;H331;H370，页面读出来是 H225;H319;H336：
    #           表里没有的是 H319、H336，表里多出来的是 H301、H311、H331、H370
    #   4-异丁基苯乙酮  按结构、按 CAS 号都查不到：PubChem 上没有它的分类。表里写的是 H315;H317;H411
    assert lines[0] == "不一致    甲醇（methanol）：表里没有 H319、H336；表里多出 H301、H311、H331、H370。PubChem 是 H225;H319;H336"
    assert lines[1] == "没有数据  4-异丁基苯乙酮（4_isobutylacetophenone）：PubChem 上没有它的分类，表里写的是 H315;H317;H411"
    # 合计一行。它后面还打印了一行固定的提醒（“不一致不一定是表错了……”），这里没有核对
    assert lines[2] == "查了 3 个：一致 1 个，不一致 1 个，PubChem 没有数据 1 个。"

    # 中途被限流：说清楚停在哪、怎么接着查
    # 命令遇到这种情况是用 raise SystemExit(一段话) 退出的：正式运行时这段话会显示在命令行里。
    # pytest.raises(…) as stopped 把异常接住，stopped.value 就是那个异常，str() 取出它带的那段话。
    # 这一次的顺序是：异丙醇（一致）、丙酮（429，在这里停下）、甲醇（没有轮到）
    with pytest.raises(SystemExit) as stopped:
        cli.cmd_check_hazards(ctx, argparse.Namespace(only="isopropanol,acetone,methanol", start_from=None), pubchem=pubchem)
    assert "查到“丙酮”时停下了" in str(stopped.value) and "--start-from acetone" in str(stopped.value)
    # --only 给了表里没有的 key：还没发任何请求就退出，完整的话是“data/compounds.csv 里没有 key 为 nothing 的物质。”
    with pytest.raises(SystemExit, match="没有 key 为 nothing"):
        cli.cmd_check_hazards(ctx, argparse.Namespace(only="nothing", start_from=None), pubchem=pubchem)


def test_pubchem_hazards_tell_no_data_apart_from_not_hazardous(ctx):
    """查 PubChem 的四种结果要分清：有危害；查到了、不属于危险品；有这个物质但没有分类；根本没有这个物质。

    保证的行为：m06_safety.SafetyService.assess（里面是 _from_pubchem），一次分析的第 5 阶段“毒性与法规”用的就是它：
        同一个结构在 PubChem 上有几条记录时，依次看前 3 条，用第一条有分类数据的
        有分类数据、但一条危害都没有              data_source 是 "pubchem"，h_codes 是空列表：不属于危险品
        有这个物质但没有分类 / 根本没有这个物质    data_source 是 "none"：没有数据，不能当作安全
        没有名字的物质借用 PubChem 上的名字；PubChem 上也没有它的记录时，显示分子式
        四种结果都写进 substance_hazards 表，之后再问同一个物质不发请求
    怎么读：
        准备  四个结构相近、都不在内置试剂表里的分子（所以都会走查 PubChem 这条路）：theobromine 可可碱、
              theophylline 茶碱、paraxanthine 副黄嘌呤；unknown 是把可可碱的一个 N-甲基换成乙基的分子。
              假 PubChem 给它们各安排了一种情况（见下面的注释）；requested 记下它收到的每个请求的地址。
        调用  用同一个 SafetyService 逐个 assess 并提交；然后清空 requested，新建一个 SafetyService 再问一遍。
        断言  每种情况的 data_source、h_codes、name；第二遍 requested 是空的。
    为什么重要：把“没有数据”当成“不属于危险品”，一个没人查过的中间体就会被当作安全的；
    README 里定的规矩是查不到分类的物质标“没有危害数据”，不当作安全。
    缓存要是不起作用，每次分析都得把同样的物质再问一遍，很快就会碰到 PubChem 的限流（README）。
    """
    theobromine, theophylline, paraxanthine, unknown = (
        "CN1C=NC2=C1C(=O)NC(=O)N2C", "CN1C2=C(C(=O)N(C1=O)C)NC=N2", "CN1C=NC2=C1C(=O)N(C)C(=O)N2", "CN1C=NC2=C1C(=O)NC(=O)N2CC")
    # 假 PubChem 认识哪些结构：{完整的 InChIKey: 这个结构对应的 CID 列表}。
    # 可可碱有两条记录（11、12）。unknown 不在里面，查它会得到 404
    cids = {chem.mol_record(theobromine).inchikey: [11, 12], chem.mol_record(theophylline).inchikey: [21],
            chem.mol_record(paraxanthine).inchikey: [31]}
    # 哪些 CID 有 GHS 页面。可可碱的两条记录里只有第二条（12）有；茶碱（21）的页面是 WATER_PAGE：
    # 有申报、但一条危害都没有；副黄嘌呤的 31 没有页面
    pages = {12: TOXIC_PAGE, 21: WATER_PAGE}
    # 假 PubChem 收到的每个请求的地址
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        """假的 PubChem：按结构查 CID、按 CID 取标题、按 CID 取 GHS 页面；查不到的一律回 404。"""
        path = request.url.path
        requested.append(path)
        # 按结构查 CID
        if "/compound/inchikey/" in path:
            found = cids.get(path.split("/compound/inchikey/")[1].split("/")[0])
            return httpx.Response(200, json={"IdentifierList": {"CID": found}}) if found else httpx.Response(404)
        # 取性质：标题固定回 “Compound <CID>”，这样从名字就看得出后端用的是哪一条记录
        if "/property/" in path:
            cid = int(path.split("/compound/cid/")[1].split("/")[0])
            return httpx.Response(200, json={"PropertyTable": {"Properties": [{"CID": cid, "Title": f"Compound {cid}"}]}})
        # 剩下的是取 GHS 页面（地址形如 …/data/compound/<CID>/JSON）
        page = pages.get(int(path.split("/data/compound/")[1].split("/")[0]))
        return httpx.Response(200, json=page) if page else httpx.Response(404)

    pubchem = PubChemClient(transport=httpx.MockTransport(handler), min_interval=0)
    with ctx.session_factory() as session:
        # 这里的 ctx 是夹具给的，上面本来没有 PubChem；假客户端是直接交给 SafetyService 的。
        # 第四个参数是要看哪些地区的管制清单
        service = SafetyService(session, ctx.reference, pubchem, ["CN"])
        # 第一条记录没有分类数据，第二条有（硼氢化钠在 PubChem 上就是这样）
        first = service.assess(theobromine)
        assert (first["data_source"], first["h_codes"]) == ("pubchem", TOXIC_CODES)
        # 缓存里记的是真正用上的那条记录（12），不是排在第一的 11
        assert session.get(models.SubstanceHazard, first["key14"]).cid == 12
        # 内置表里没有名字的物质，借用 PubChem 上的名字；PubChem 也没有的，只能显示分子式
        assert (first["name"], first["pubchem_name"]) == ("Compound 12", "Compound 12")
        # 副黄嘌呤没有分类数据，但 PubChem 上有它这条记录（31），名字照样借得到；
        # unknown 连记录都没有，显示的是它的分子式 C8H10N4O2
        assert service.assess(paraxanthine)["name"] == "Compound 31" and service.assess(unknown)["name"] == "C8H10N4O2"
        # 查到了，而且结论是不属于危险品
        harmless = service.assess(theophylline)
        assert (harmless["data_source"], harmless["h_codes"], harmless["flags"]) == ("pubchem", [], [])
        # PubChem 有这个物质但没有分类数据；PubChem 根本没有这个物质：都是“没有数据”
        for smiles in (paraxanthine, unknown):
            assert service.assess(smiles)["data_source"] == "none"
        session.commit()

        # 四种结果都缓存下来，再问一遍不发请求
        # 新建一个 SafetyService，是为了绕开对象自己记在内存里的结果（_cache），确认这一遍用的是数据库里的缓存
        requested.clear()
        again = SafetyService(session, ctx.reference, pubchem, ["CN"])
        assert [again.assess(s)["data_source"] for s in (theobromine, theophylline, paraxanthine, unknown)] == [
            "pubchem", "pubchem", "none", "none"]
        assert requested == []


# ---------------------------------------------------------------- AiZynthFinder

# AiZynthFinder 4.4.1 给“水杨酸 + 乙酸酐 → 阿司匹林”这一步的真实输出
# 这是引擎放在反应节点 metadata 里的 mapped_reaction_smiles，格式是 “产物>>反应物1.反应物2”。
# 每个原子后面的 :数字 是原子映射号，两边编号相同的是同一个原子：
#   第一行  产物阿司匹林，1–13 号
#   第二行  乙酸酐（1–3 号是进入产物的那个乙酰基，14–17 号是掉下去的那一半）和水杨酸（4–13 号）
# 由它算出来的反应中心是 F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1：
# 酚羟基的氧（4 号）接到酰基碳（2 号）上，酰基碳上原来连着的乙酸根（14 号氧那一半）掉下去。
MAPPED_ACETYLATION = (
    "[CH3:1][C:2](=[O:3])[O:4][c:5]1[cH:6][cH:7][cH:8][cH:9][c:10]1[C:11](=[O:12])[OH:13]>>"
    "[CH3:1][C:2](=[O:3])[O:14][C:15]([CH3:16])=[O:17].[OH:4][c:5]1[cH:6][cH:7][cH:8][cH:9][c:10]1[C:11](=[O:12])[OH:13]"
)


def _aizynth_tree(first_step_template: str) -> dict:
    """按 AiZynthFinder 输出格式写的一条两步路线：苯酚 → 水杨酸 → 阿司匹林。

    顶替的是 AiZynthFinder 搜索以后给出的一棵路线树。树的格式是分子节点和反应节点一层套一层，
    根是目标分子，越往下越靠近原料（格式说明见 m03_retrosynthesis.py 的文件头）：
        阿司匹林（目标分子，in_stock 是 False）
          └─ 反应：O-酰化。引擎会给的各项 metadata 都带上了：模板编号、反应类别、模板出现的次数、
             单步模型给的概率和排名、模型名、带原子映射的反应
               ├─ 乙酸酐（in_stock 是 True：引擎自己的在售清单里有）
               └─ 水杨酸（in_stock 是 False）
                    └─ 反应：类别是 “0.0 Unrecognized”（引擎没认出是哪一类反应），没有原子映射
                         ├─ 苯酚（in_stock 是 True）
                         └─ 二氧化碳（in_stock 是 True）
    按实验顺序：第 1 步，苯酚和二氧化碳得到水杨酸；第 2 步，水杨酸和乙酸酐得到阿司匹林。
    参数：
        first_step_template  第 1 步（苯酚那一步）的模板编号 template_hash。传不同的值，
                             可以造出“反应完全一样、只是模板不同”的两条路线，用来测去重。
    返回：
        一个新的字典。每次调用都是新建的，所以测试可以放心地改它（删掉一层、换掉一个原料）。
    谁在用：本文件 AiZynthFinder 这一节的几个测试。
    """
    return {
        "type": "mol", "smiles": ASPIRIN, "is_chemical": True, "in_stock": False,
        # scores 和 metadata 是引擎加在根节点上的：这条路线的分数，和它认为原料是不是都买得到（is_solved）
        "scores": {"state score": 0.994}, "metadata": {"created_at_iteration": 3, "is_solved": True},
        "children": [{
            "type": "reaction", "is_reaction": True, "hide": False,
            "smiles": "[CH3:1][C:2](=[O:3])[O:4][c:5]1...",
            # classification 前面的 “1.7.9” 是引擎给反应类别编的号，后端取名时会去掉。
            # library_occurence 是 AiZynthFinder 自己的拼法（少一个 r），后端两种拼法都认
            "metadata": {"template_hash": "abc123", "classification": "1.7.9 O-Acylation", "library_occurence": 4161,
                         "policy_probability": 0.42, "policy_probability_rank": 0, "policy_name": "uspto",
                         # 引擎按“产物>>反应物”给的原子映射：每个原子从哪来、到哪去
                         "mapped_reaction_smiles": MAPPED_ACETYLATION},
            "children": [
                {"type": "mol", "smiles": "CC(=O)OC(C)=O", "is_chemical": True, "in_stock": True},
                {"type": "mol", "smiles": "O=C(O)c1ccccc1O", "is_chemical": True, "in_stock": False, "children": [{
                    "type": "reaction", "is_reaction": True,
                    "metadata": {"template_hash": first_step_template, "classification": "0.0 Unrecognized",
                                 "library_occurence": 12, "policy_probability": 0.08},
                    "children": [
                        {"type": "mol", "smiles": "Oc1ccccc1", "is_chemical": True, "in_stock": True},
                        {"type": "mol", "smiles": "O=C=O", "is_chemical": True, "in_stock": True},
                    ],
                }]},
            ],
        }],
    }


def test_aizynthfinder_route_trees_are_converted_and_deduplicated(ctx):
    """引擎格式的路线树整理成后端自己的结构：读不出来的跳过并提示，重复的只留一条，每一步该有的信息都在。

    保证的行为：syntherisk/routes.py 的 build_skeleton（流水线第 2 阶段“逆合成搜索”的收尾）和 build_graph：
        整理不了的路线（有结构读不出来）跳过，在 warnings 里记一句；别的路线照常处理
        用到的步骤完全相同的两条路线算同一条（反应一样、只是引擎用的模板不同），只留先出现的
        步骤按实验顺序编号：第 1 步最先做，最后一步得到目标分子
        反应名：引擎给了可用的反应类别就用它（去掉前面的编号）；类别是 Unrecognized 时按官能团变化猜
        模型依据（模板、概率等）存进每一步的 model；引擎给了原子映射的步骤算出反应中心 center
        分子的名字用内置试剂表里的中文名；引擎说在售的记在分子的 engine_in_stock 上
    怎么读：这里不跑整条流水线，也不经过接口：直接造一个 SearchResult（引擎的搜索结果），调 build_skeleton。
    参数里的 ctx 只是为了拿 ctx.reference（内置试剂表）。
    三条“路线”分别是：正常的一条；反应相同、只是第 1 步的模板编号不同的一条；根本读不出结构的一条。
    为什么重要：这一步的产出就是后面所有阶段和前端读的那份结果。同一条路线留下两份，路线列表里就会有重复的；
    一条坏路线要是让整次分析失败，其余的好路线也看不到了。
    """
    # SearchResult 是几种引擎共用的返回格式：引擎名、版本、若干条路线（树、引擎自己的分数、是否拆到底）、统计数字
    search = SearchResult(
        engine="aizynthfinder", version="4.4.1",
        routes=[{"tree": _aizynth_tree("t1"), "score": 0.994, "solved": True},
                {"tree": _aizynth_tree("t2"), "score": 0.9, "solved": True},  # 同样的反应，只是模板不同：算同一条路线
                {"tree": {"type": "mol", "smiles": "not-a-smiles"}, "score": 0.1}],
        stats={"number_of_nodes": 1240},
    )
    skeleton = build_skeleton(search, ASPIRIN, ctx.reference)
    # 写法 (route,) = 列表：要求列表里正好一项，并把它取出来；多一项、少一项都会报错。
    # 三条里只留下一条：t2 那条和 t1 用到的步骤完全相同，被去重；not-a-smiles 那条读不出来，被跳过
    (route,) = skeleton["routes"]
    # 完整的提示是“跳过了一条读不出来的路线：路线里有读不出来的结构：not-a-smiles”
    assert any("读不出来" in warning for warning in skeleton["warnings"])
    # 留下的是先出现的 t1 那条，所以引擎分数是它的 0.994；一共两步
    assert route["engine_score"] == 0.994 and len(route["step_keys"]) == 2

    # step_keys 是按实验顺序排好的步骤编号。到反应表 skeleton["reactions"] 里把这两步取出来
    first, last = (skeleton["reactions"][key] for key in route["step_keys"])
    # 实验顺序：先做羧基化，最后一步得到目标分子
    # 第 1 步的名字是 m07_conditions.name_reaction 猜的：二氧化碳是反应物，产物多出羧基，反应物里有酚
    assert first["name"] == "Kolbe–Schmitt 羧基化" and first["classification"] is None  # “Unrecognized” 不当名字用
    # 最后一步的类别 “1.7.9 O-Acylation” 去掉编号以后当反应名；引擎没给路线起名字，路线名借最后一步的反应名
    assert last["name"] == "O-Acylation" and route["name"] == "O-Acylation"
    # model 是逆合成模型自己给的依据。键名换成了后端的写法（library_occurrence、policy_rank），值是树里原样的
    assert last["model"] == {"template_hash": "abc123", "library_occurrence": 4161, "policy_probability": 0.42,
                             "policy_rank": 0, "policy_name": "uspto"}
    assert last["reagents"] == []  # AiZynthFinder 不给试剂和条件
    # 引擎给了原子映射的那一步，算出反应中心（酚羟基接到酰基碳上，乙酸根掉下去）；没给的那一步没有
    assert last["center"] == "F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1" and first["center"] is None
    # leaf_keys 是这条路线的起始原料（路线树的叶子）。名字来自 data/compounds.csv 的中文名一列
    names = {skeleton["molecules"][key]["name"] for key in route["leaf_keys"]}
    assert names == {"乙酸酐", "苯酚", "二氧化碳（干冰）"}
    # 树里苯酚的 in_stock 是 True，原样记在分子表里（分子表的键是 key14）。第 3 阶段判断可得性时会参考它
    assert skeleton["molecules"][chem.key14_of("Oc1ccccc1")]["engine_in_stock"] is True
    # build_graph 是结果页画布用的分叉视图。目标分子只有一种做法，所以只有一个分叉，标签是 A
    assert build_graph(skeleton)["top_branches"][0]["label"] == "A"


def test_pipeline_uses_the_engines_stock_flags_and_model_evidence(client, account, ctx):
    """接上 AiZynthFinder 时的整条流水线：库存和在售试剂表交给引擎，引擎说在售的原料算可购买，模型依据进证据卡。

    保证的行为：
        第 2 阶段（pipeline.AnalysisRunner._search）交给引擎的搜索请求（SearchRequest）里：
            步数上限来自复杂度档位，原料来源来自约束；课题组库存里各个物质的 InChIKey、
            内置试剂表里可购买的试剂的 InChIKey 都带上（搜索碰到这些分子就不再往下拆）
        第 2 阶段的进度说明里写出引擎探索了多少个中间体
        第 3 阶段（m04_availability.AvailabilityChecker.check）：我们自己的在售目录里没有、
            但引擎的在售清单里有的原料，也算“可购买”
        证据卡（GET …/routes/{route_id}/steps/{n}）带上模型依据，并且在来源里写明它“不是文献证据”
    怎么读：
        准备  StubAiZynth 顶替 ctx.retro_engine：search() 把收到的请求记下来，然后返回文件上面那条两步路线，
              只是把乙酸酐换成了乙酸丙酸酐 CC(=O)OC(=O)CC。这个分子不在内置试剂表里，树里给它标的是 in_stock True。
              另外通过接口往库存里加了苯酚。
        调用  提交阿司匹林的分析，复杂度选“探索”。队列是同步的，返回时已经跑完。
        断言  第 2、3 阶段的进度说明；引擎收到的请求；第 2 步的证据卡。
    为什么重要：库存不交给引擎，引擎就不知道哪些中间体手头已经有、可以不再往下拆；内置试剂表不交给引擎，
    碘甲烷这类引擎清单里没有的小分子试剂会被当成“原料买不到”（aizynth_bridge.py 的文件头）。
    模型给的概率不是文献证据，证据卡上必须分清：README 写得很明白，引擎给的路线要靠先例来把关。
    """
    class StubAiZynth:
        """假的逆合成引擎，顶替 ctx.retro_engine（真的是 m03_retrosynthesis.AiZynthEngine）。

        一个对象只要有 name、search、describe，就能当引擎用（m03_retrosynthesis.RetrosynthesisEngine）。
        和真引擎的区别：不起子进程，不加载模型，search() 立刻返回一条写死的路线，并把收到的请求记在 requests 里。
        """

        # 引擎的名字。流水线看到它不是 "demo"，第 2 阶段的进度说明就按真引擎的写法来
        name = "aizynthfinder"
        # 写在类上的列表，所有实例共用：测试最后通过 StubAiZynth.requests 取出引擎收到的请求
        requests = []

        def search(self, request, should_cancel=None):
            """顶替真引擎的 search()：先把收到的搜索请求记下来，再返回一条写死的路线，不做搜索。

            request 是流水线组好的 SearchRequest；should_cancel 是“用户要不要取消”的回调，这里用不到。
            """
            self.requests.append(request)
            tree = _aizynth_tree("t1")
            # 假设目录里没有的一个原料，但 AiZynthFinder 自己的在售清单里有
            # tree["children"][0] 是最后一步反应，它的 children[0] 原来是乙酸酐：换成乙酸丙酸酐
            tree["children"][0]["children"][0] = {"type": "mol", "smiles": "CC(=O)OC(=O)CC", "in_stock": True}
            return SearchResult(engine=self.name, version="4.4.1", routes=[{"tree": tree, "score": 0.9}],
                                stats={"number_of_nodes": 1240})

        def describe(self):
            """引擎约定要有的方法：GET /api/meta/modules 和命令行 status 靠它显示引擎的现状。跑分析时用不到。"""
            return {"engine": self.name}

    # 测试里可以直接换掉上下文上的部件：之后这份上下文跑分析，用的就是这个假引擎
    ctx.retro_engine = StubAiZynth()
    # E3 手动添加库存：只给 SMILES（苯酚）。它是这条路线第 1 步的原料
    client.post("/api/inventory/items", headers=account["headers"], json={"smiles": "Oc1ccccc1"})
    analysis = start_analysis(client, account, smiles=ASPIRIN, constraints={"complexity": "explore"})
    # 逗号后面是断言失败时显示的内容：分析没成功的话，把失败原因打出来
    assert analysis["status"] == "succeeded", analysis["error"]
    # stages 是八个阶段的列表，下标从 0 开始：stages[1] 是第 2 阶段“逆合成搜索”，stages[2] 是第 3 阶段“原料可得性”。
    # 1,240 来自假引擎给的统计 number_of_nodes（显示时加了千位分隔符）；
    # 这条路线没有被标成 solved=False，所以说的是“完整路线”
    assert analysis["stages"][1]["message"] == "已探索 1,240 个中间体，找到 1 条完整路线"
    # 三个起始原料：苯酚在库存里；二氧化碳在内置的在售试剂表里；乙酸丙酸酐只有引擎说在售，也算可购买
    assert analysis["stages"][2]["message"] == "3 个原料：库存 1 · 可购买 2 · 买不到 0"

    # 取出假引擎收到的请求（正好一个）
    (request,) = StubAiZynth.requests
    # “探索”档的步数上限是 10（data/scoring.toml 的 [complexity.explore]）；
    # 约束里没有指定原料来源，用的是默认的“库存优先”
    assert request.max_steps == 10 and request.stock_mode == "inventory_first"
    # ISWSIDIOOBJBQZ-UHFFFAOYSA-N 是苯酚完整的 InChIKey
    assert request.inventory_inchikeys == ["ISWSIDIOOBJBQZ-UHFFFAOYSA-N"]  # 库存里的苯酚：搜索碰到它就停
    # 内置在售试剂表也交给引擎当终点（比如碘甲烷）
    # INQOMBQAUSQDDS-UHFFFAOYSA-N 是碘甲烷的 InChIKey。data/compounds.csv 现在有 114 行，
    # purchasable 一列全是 1，所以交给引擎的超过 100 个
    assert "INQOMBQAUSQDDS-UHFFFAOYSA-N" in request.catalog_inchikeys and len(request.catalog_inchikeys) > 100

    # 路线 r1 的第 2 步（最后一步，O-酰化）的证据卡
    card = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/2", headers=account["headers"]).json()
    # 0.42 是树里写的 policy_probability：单步模型给这种拆法的概率
    assert card["model"]["policy_probability"] == 0.42
    # 这一步要准备的物料只有乙酸丙酸酐（水杨酸是上一步做出来的中间体，不算）：状态是可购买
    assert {m["status"] for m in card["materials"]} == {"purchasable"}
    # sources 是这张卡可以引用的全部出处；next(…) 取出编号是 MODEL 的那一项，看它的显示文字
    assert next(s for s in card["sources"] if s["id"] == "MODEL")["label"] == "逆合成模型依据（不是文献证据）"


def test_steps_with_atom_maps_are_matched_to_precedents_by_reaction_center(client, account, ctx):
    """引擎给的步骤带原子映射、先例库也带：同类反应按“改了哪些键”认，不按指纹。

    保证的行为（M5 文献先例：m05_precedents.PrecedentSearch.find 和 _class_rows，流水线第 4 阶段）：
        这一步有反应中心、先例库里也有带反应中心的先例时，按反应中心判断“是不是同类反应”：
        反应中心（归并以后）一样的才算同类先例；产物一样、成的键不一样的不算，也不列出来
        同类先例的底物相似度 ≥ 0.7 时，证据等级是 E2“高相似先例”，产率借它报道的值
        先例库里一条带反应中心的先例都没有（库是旧版本导入的）时，退回按指纹比，并在结果里提醒重新导入
    怎么读：
        准备  往先例库里直接放两条现编的先例（precedent_from_reaction 把一条反应 SMILES 算成数据库的一行），
              它们的产物都是 5-甲基阿司匹林。假引擎返回只有一步的路线：水杨酸 + 乙酸酐 → 阿司匹林，带着真实的原子映射。
        前半  提交分析，看第 1 步的证据卡：是按反应中心比的，只认第一条先例。
        后半  把库里所有先例的 center 清空来模拟旧库，再分析一次：退回了按指纹比，并给出提醒。
    为什么重要：README“几处和你预想的可能不一样的地方”记着按指纹比的毛病：分不清“产物相近、成的键不同”的反应，
    一条不靠谱的阿司匹林路线曾经靠着“水杨酸乙酰化”的专利排到第一。这里的第二条先例就是这一类情形
    （方向反过来：这一步是乙酰化，先例是取代芳环上的氟）。
    """
    with ctx.session_factory() as session:
        # 现编的两条先例。第一条和这一步是同一种转化（酚羟基被酸酐酰化），底物多一个甲基；
        # 第二条产物一样，但做法是“乙酸根取代芳环上的氟”，成的不是同一根键
        # 写法是专利数据的 “反应物>>产物”，带原子映射。两条各自算出来的反应中心：
        #   TEST-ACYL  F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1，和这一步的一模一样；
        #              它的产物和阿司匹林的结构相似度是 0.841
        #   TEST-SNAR  F:OH.acyl~c:1|L:c~F:1（羧酸的氧接到芳环碳上，芳环碳上掉了一个氟）；
        #              归并以后的同类、核心两把钥匙和这一步的都对不上
        # 两条先例各带一个不同的产率（88%、35%），后面从产率就能看出借的是哪一条
        session.add(precedent_from_reaction(
            "[OH:1][c:2]1[cH:3][cH:4][c:14]([CH3:15])[cH:6][c:7]1[C:8](=[O:9])[OH:10].[CH3:11][C:12](=[O:13])OC(C)=O"
            ">>[CH3:11][C:12](=[O:13])[O:1][c:2]1[cH:3][cH:4][c:14]([CH3:15])[cH:6][c:7]1[C:8](=[O:9])[OH:10]",
            source="uspto", document_id="TEST-ACYL", year=2001, yield_pct=88.0))
        session.add(precedent_from_reaction(
            "[CH3:11][C:12](=[O:13])[OH:1].F[c:2]1[cH:3][cH:4][c:14]([CH3:15])[cH:6][c:7]1[C:8](=[O:9])[OH:10]"
            ">>[CH3:11][C:12](=[O:13])[O:1][c:2]1[cH:3][cH:4][c:14]([CH3:15])[cH:6][c:7]1[C:8](=[O:9])[OH:10]",
            source="uspto", document_id="TEST-SNAR", year=2002, yield_pct=35.0))
        session.commit()

    class StubAiZynth:
        """假的逆合成引擎，顶替 ctx.retro_engine：返回只有最后一步（水杨酸 + 乙酸酐 → 阿司匹林）的一条路线。"""

        name = "aizynthfinder"

        def search(self, request, should_cancel=None):
            """顶替真引擎的 search()：不搜索，返回一条只有一步、带原子映射的路线。"""
            tree = _aizynth_tree("t1")
            # del 字典[键]：删掉水杨酸节点下面的 children，也就是去掉“苯酚 → 水杨酸”那一步，水杨酸成了起始原料
            del tree["children"][0]["children"][1]["children"]  # 只留最后一步
            return SearchResult(engine=self.name, version="4.4.1", routes=[{"tree": tree, "score": 0.9, "solved": True}])

    ctx.retro_engine = StubAiZynth()
    analysis = start_analysis(client, account, smiles=ASPIRIN)
    card = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1", headers=account["headers"]).json()
    # compared_by 是 center：这一步是按反应中心判断同类反应的。
    # 同类先例的底物相似度 0.841 ≥ 0.7（data/scoring.toml 的 high_similarity_threshold），所以是 E2
    assert card["evidence"]["compared_by"] == "center" and card["evidence"]["level"] == "E2"
    # 先例列表里只有 TEST-ACYL：它是同类反应（same_class），是按反应中心认定的。TEST-SNAR 没有列出来
    assert [(p["document_id"], p["same_class"], p["compared_by"]) for p in card["precedents"]] == [("TEST-ACYL", True, "center")]
    # E2 的产率取高相似的同类先例报道的值，标签写“相似先例报道”；88 是 TEST-ACYL 的产率
    assert card["yield"]["label"] == "相似先例报道 88%（1 个先例）"
    # 证据卡原样带出这一步的反应中心
    assert card["center"] == "F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1"
    result = client.get(f"/api/analyses/{analysis['id']}/result", headers=account["headers"]).json()
    # 库里的先例都带反应中心，所以结果的提示里没有“重新导入”这一条
    assert not any("重新导入" in warning for warning in result["warnings"])

    # 先例库如果是加入“反应中心”之前导入的（这里把那一列清空来模拟）：退回按指纹比，并在结果里提醒重新导入
    with ctx.session_factory() as session:
        # update(表).values(center=None)：一条 SQL，把所有行的 center 列改成空
        session.execute(update(models.ReactionPrecedent).values(center=None))
        # 再加一条不相干的先例（苯乙酮还原成 1-苯乙醇，没有原子映射）。内存里的先例索引靠“行数和版本号变没变”
        # 判断要不要重新加载（m05_precedents.PrecedentIndex.refresh）；上面那句直接改表，这两样都不变，
        # 多了这一行、行数变了，下一次分析才会把索引重新读一遍
        session.add(precedent_from_reaction("CC(=O)c1ccccc1>>CC(O)c1ccccc1", source="uspto", document_id="TEST-MORE"))
        session.commit()
    again = start_analysis(client, account, smiles=ASPIRIN)
    card = client.get(f"/api/analyses/{again['id']}/routes/r1/steps/1", headers=account["headers"]).json()
    # 库里一条带反应中心的先例都没有了：这一步虽然自己有反应中心，也只能按指纹比
    assert card["evidence"]["compared_by"] == "fingerprint"
    result = client.get(f"/api/analyses/{again['id']}/result", headers=account["headers"]).json()
    # 提醒是 pipeline.AnalysisRunner._precedents 加的，结尾是“重新导入一遍先例库即可”
    assert any("重新导入一遍先例库" in warning for warning in result["warnings"])


def test_incomplete_routes_from_the_engine_are_kept_and_reported(client, account, ctx):
    """AiZynthFinder 凑不够完整路线时会返回没拆到底的：照样评估，但进度里要说清楚。

    保证的行为：
        引擎标了 solved=False 的路线（有原料不在它自己的在售清单里）照样进入后面的阶段；
            第 2 阶段的进度说明里写明有几条是这样的（pipeline.AnalysisRunner._search）
        只有目标分子、一步都没拆的“路线”不算路线，直接丢掉（routes.build_skeleton）
        引擎认为买不到的原料，只要我们自己的在售目录里有，路线就不会因为“原料买不到”被排除
    怎么读：假引擎返回三条：完整的两步路线；停在水杨酸的一步路线（树里水杨酸的 in_stock 是 False，
    路线标着 solved=False）；只有阿司匹林本身的一条。提交分析以后看第 2 阶段的说明，再看结果页。
    为什么重要：pipeline.py 里记着，AiZynthFinder 至少返回 5 条路线，凑不够完整的就拿没拆到底的补。
    把这些路线悄悄丢掉，用户能选的路线就少了（它的原料我们自己的库存和在售表里可能有）；
    不加说明地留着，用户又会以为每一条都拆到了买得到的原料。
    """
    partial = _aizynth_tree("t1")
    del partial["children"][0]["children"][1]["children"]  # 停在水杨酸，引擎认为它买不到

    class StubAiZynth:
        """假的逆合成引擎，顶替 ctx.retro_engine：返回一条完整的、一条没拆到底的、一条一步都没拆的。"""

        name = "aizynthfinder"

        def search(self, request, should_cancel=None):
            """顶替真引擎的 search()：不搜索，返回三条事先准备好的“路线”。"""
            # 每条路线的 solved 是引擎自己的判断：这条路线的原料是不是都在它的在售清单里
            routes = [{"tree": _aizynth_tree("t1"), "score": 0.99, "solved": True},
                      {"tree": partial, "score": 0.52, "solved": False},
                      {"tree": {"type": "mol", "smiles": ASPIRIN, "in_stock": False}, "score": 0.05, "solved": False}]
            return SearchResult(engine=self.name, version="4.4.1", routes=routes, stats={"number_of_nodes": 5})

    ctx.retro_engine = StubAiZynth()
    analysis = start_analysis(client, account, smiles=ASPIRIN)
    # 只有目标分子、一步都没拆的那条不算路线
    # “5 个中间体”来自假引擎的统计 number_of_nodes；留下 2 条路线，其中标着 solved=False 的有 1 条
    assert analysis["stages"][1]["message"] == "已探索 5 个中间体，找到 2 条路线，其中 1 条没有拆到在售原料"
    result = client.get(f"/api/analyses/{analysis['id']}/result", headers=account["headers"]).json()
    # 引擎说水杨酸买不到，但我们自己的在售表里有：这条一步的路线照样可选
    # 水杨酸在 data/compounds.csv 里 purchasable 是 1，启动时就写进了在售目录。
    # routes 是没被排除的路线，步数是 1 和 2（排好序再比，不依赖名次）；excluded_routes 是空的：没有路线被排除
    assert sorted(route["n_steps"] for route in result["routes"]) == [1, 2] and result["excluded_routes"] == []


def test_a_step_we_know_almost_nothing_about_is_not_scored_as_if_it_were_fine(client, account, ctx):
    """逆合成引擎只给反应物和产物。查不到危害、不知道条件的时候，不能按“安全、操作简单”给满分。

    保证的行为（README“没有数据的维度不计分”；评分规则在 m10_scoring.py 的文件头）：
        这一步投进去的物质都查不到危害数据   安全这一维是 None（没有评估），不是 100 分（m10_scoring.base_dimensions）
        不知道试剂和条件、又没有命中规则     操作这一维是 None，标签是“操作 未评估”
                                           （m07_conditions.ConditionEvaluator.evaluate）
        有数据的维度权重加起来不到 0.3       这一步标 thin（依据不足），按低置信度处理（m10_scoring.evaluate_route）
        证据卡的依据和按模板写的说明里，把“没有评估”如实说出来（m11_explain.Explainer._step_template）
    怎么读：假引擎返回一条只有一步的路线：阿司匹林甲酯 COC(=O)c1ccccc1OC(C)=O → 阿司匹林。
    这个反应物不在内置试剂表里，测试环境又没有开 PubChem，所以没有危害数据；引擎不给试剂和条件；
    先例库是空的，文献依据没有评估，也就没有文献产率。六个维度里只剩可得性和绿色化学有数据。
    提交分析以后，先看结果页里这一步的各项，再看它的证据卡。
    为什么重要：没有数据的维度要是按满分算，一个什么都不知道的步骤会拿到很高的分数，
    排到有真凭实据的路线前面去。
    """

    class StubAiZynth:
        """假的逆合成引擎，顶替 ctx.retro_engine：返回一条只有一步的路线，除了模型给的概率，什么依据都没有。"""

        name = "aizynthfinder"

        def search(self, request, should_cancel=None):
            """顶替真引擎的 search()：不搜索，返回一条只有一步的路线。"""
            # 手写的最小路线树：阿司匹林 ← 一步反应（只带模型概率和模板编号）← 阿司匹林甲酯（引擎说在售）
            tree = {"type": "mol", "smiles": ASPIRIN, "in_stock": False, "children": [{
                "type": "reaction", "metadata": {"policy_probability": 0.3, "template_hash": "x"},
                "children": [{"type": "mol", "smiles": "COC(=O)c1ccccc1OC(C)=O", "in_stock": True}]}]}
            return SearchResult(engine=self.name, version="4.4.1", routes=[{"tree": tree, "score": 0.9, "solved": True}])

    ctx.retro_engine = StubAiZynth()
    analysis = start_analysis(client, account, smiles=ASPIRIN)
    (route,) = client.get(f"/api/analyses/{analysis['id']}/result", headers=account["headers"]).json()["routes"]
    (step,) = route["steps"]
    # 只有可得性（15%）和绿色化学（5%）有数据：分数照算，但标明依据不足，按低置信度处理
    # 上面两行的 (x,) = 列表：要求结果页里正好一条路线、这条路线正好一步。
    # dimensions 里值是 None 的维度就是“没有评估”
    assert step["dimensions"]["safety"] is None and step["dimensions"]["operation"] is None
    # coverage 0.2 = 0.15 + 0.05（data/scoring.toml 的 [weights]）；它低于 [confidence] 的 min_assessed_weight（0.3），
    # 所以 thin 是 True；thin 的步骤置信度固定是 low
    assert (step["coverage"], step["thin"], step["confidence"], step["operation_label"]) == (0.2, True, "low", "操作 未评估")
    # 整条路线：唯一的一步是低置信步骤，占比 1.0 超过了 low_fraction_for_low（0.34），所以路线的置信度是“低”
    assert (route["confidence_label"], route["confidence_reason"]) == ("低", "第 1 步多数评分项没有数据")

    card = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1", headers=account["headers"]).json()
    # 证据卡的 dimensions 是六个维度的列表，每项有 key（维度的英文名）和 basis（一句话的依据）。
    # 这里做成 {维度: 依据} 的字典，方便按名字取
    basis = {d["key"]: d["basis"] for d in card["dimensions"]}
    assert basis["safety"] == "这一步用到的物质都没有危害数据，安全这一项没有评估"
    assert basis["operation"] == "不知道这一步用什么试剂和条件，操作复杂度没有评估"
    # explanation 是这一步的说明。测试环境没有配大模型，它是按模板生成的：
    # 模板会把没有评估的两项说出来，并提醒这个分数只算了少数几项、参考价值有限
    text = card["explanation"]["text"]
    assert "操作复杂度没有评估" in text and "安全性没有评估" in text and "参考价值有限" in text


def test_bridge_adds_exclusions_after_aizynthfinder_has_reset_its_own_list(monkeypatch):
    """AiZynthFinder 建搜索树时会清空排除名单：用户排除的原料必须在那之后加，否则等于没排除。

    保证的行为：syntherisk/modules/aizynth_bridge.py 的 run（真正调用 AiZynthFinder 的那段脚本）：
        先 prepare_tree()，再把 excluded_smiles 里的原料逐个加进排除名单，然后才 tree_search()
        搜索的终点 = 引擎自己的在售清单 + 我们的在售试剂表 + 客户库存；“仅用库存”时只有客户库存
        单步模型、过滤模型和三个搜索参数（最多几步、最多几轮、最长多少秒）照请求里给的设
        返回值里每条路线带上分数和“是否拆到底”，统计数字原样带回
    对应按钮 B11“右键 · 排除试剂”：路线被排光以后重新搜索时，排除的试剂通过 excluded_smiles 交给引擎。
    怎么读：这里没有用真的 AiZynthFinder。测试写了三个假类（Stock、Policy、Finder），照着真库的接口，
    但只是把“被怎么调用了”记下来；再用 monkeypatch 把它们塞进 sys.modules，于是 run() 里的
    from aizynthfinder.… import … 拿到的就是这些假类。然后调 run(request)，检查假 Finder 上留下的记录。
    关键在假的 Finder.tree_search：它把“搜索开始那一刻”的排除名单抄了一份（excluded_during_search）。
    为什么重要：次序反了程序不会报任何错，只是用户排除的试剂照样会被当成可用的原料。
    """

    class Stock:
        """假的在售清单管理器，顶替 AiZynthFinder 对象上的 stock。只把收到的调用记下来：

        loaded 记 load(清单内容, 名字) 加载过的清单，selected 记 select() 选中的清单名，
        excluded 记 exclude() 排除掉的分子的 SMILES。
        """

        def __init__(self):
            """三样记录都从空的开始：加载过的清单、选中的清单、排除名单。"""
            self.loaded, self.selected, self.excluded = {}, [], set()

        def load(self, source, key):
            """真的是“把一份清单加载进来，起名叫 key”；这里只记下 {名字: 清单内容}。"""
            self.loaded[key] = source

        def select(self, keys):
            """真的是“搜索时把哪几份清单当终点”；这里只记下选中的名字。"""
            self.selected = list(keys)

        def exclude(self, molecule):
            """真的是“把这个分子记进排除名单”；这里记下它的 SMILES。"""
            self.excluded.add(molecule.smiles)

    class Policy:
        """假的模型选择器，顶替 expansion_policy（单步模型）和 filter_policy（过滤模型）：只记下选了哪一个。"""

        selected = None

        def select(self, key):
            """记下 run() 选的是哪个模型（名字）。"""
            self.selected = key

    class Finder:
        """假的 AiZynthFinder，顶替 aizynthfinder.aizynthfinder.AiZynthFinder 这个类。

        和真的一样的地方：有 config.search、stock、expansion_policy、filter_policy、target_smiles 这些属性，
        有 prepare_tree、tree_search、build_routes、extract_statistics 这几个方法，路线从 routes.dict_with_extra 取。
        不一样的地方：不加载模型，不做搜索；routes 里永远是一条只有目标分子的“路线”，统计数字固定是 7 个节点。
        last 记着最近建的那个实例：run() 是在函数里面自己建 Finder 的，测试靠 Finder.last 拿到它。
        """

        last = None

        def __init__(self, configfile=None):
            """照真的 AiZynthFinder(configfile=…) 的样子，把 run() 要用到的属性都准备好。configfile 用不到。"""
            # types.SimpleNamespace 是一个可以随便挂属性的空对象。run() 会往 config.search 上写三个搜索参数
            self.config = types.SimpleNamespace(search=types.SimpleNamespace())
            self.stock, self.expansion_policy, self.filter_policy = Stock(), Policy(), Policy()
            self.tree = self.target_smiles = None
            # 一棵只有根节点的路线树，带着分数 0.9 和“已拆到底”的标记。
            # routes.dict_with_extra(…) 是 run() 取路线用的方法：这里用 lambda 顶替，不管参数是什么都返回这一棵树
            tree = {"type": "mol", "smiles": ASPIRIN, "scores": {"state score": 0.9}, "metadata": {"is_solved": True}}
            self.routes = types.SimpleNamespace(dict_with_extra=lambda include_metadata, include_scores: [tree])
            Finder.last = self

        def prepare_tree(self):  # 清空名单，再把目标分子自己排除掉（和真的差在哪，见下面的说明）
            """建搜索树。和真的一样有一个副作用：排除名单先被清空，在这之前加进去的都没有了。

            真的 prepare_tree（AiZynthFinder 4.4.1）清空以后，只在 exclude_target_from_stock 开着（默认是开的）、
            并且目标分子在在售清单里的时候，才把目标分子排除掉；这里不看这个条件，一律把目标分子放进名单。
            """
            self.stock.excluded = {self.target_smiles}
            # 随便放一个对象，表示“树已经建好了”
            self.tree = object()

        def tree_search(self):
            """“搜索”。不真的搜，只记下搜索开始这一刻排除名单里有什么。"""
            # 和真的一样：树还没建就先建（那样会清空名单）。run() 已经先调过 prepare_tree，所以这里不会再清一次
            if not self.tree:
                self.prepare_tree()
            # 把搜索开始这一刻的排除名单抄一份，留给测试核对
            self.excluded_during_search = set(self.stock.excluded)

        def build_routes(self):
            """真的是从搜索树里挑出路线；这里什么都不用做，路线在 __init__ 里已经写死了。"""
            pass

        def extract_statistics(self):
            """真的是给出这次搜索的统计数字；这里固定回 7 个节点。"""
            return {"number_of_nodes": 7}

    # 假的 aizynthfinder 包：键是模块的完整名字，值是顶替它的对象。run() 里写的是
    #     from aizynthfinder.aizynthfinder import AiZynthFinder
    #     from aizynthfinder.context.stock import InMemoryInchiKeyQuery
    #     from aizynthfinder.chem import Molecule
    # 所以这三个子模块上各放一个同名的假货；两个包本身（aizynthfinder、aizynthfinder.context）放空模块占位。
    # 真的 InMemoryInchiKeyQuery 是“从文件读一份清单”；这里换成把文件内容直接读成行的列表，
    # 这样 Stock.loaded 里存下的就是 run() 写进临时文件的那些 InChIKey，方便核对。
    # 真的 Molecule 是分子对象；这里只要一个带 smiles 属性的东西
    modules = {
        "aizynthfinder": types.ModuleType("aizynthfinder"),
        "aizynthfinder.aizynthfinder": types.SimpleNamespace(AiZynthFinder=Finder),
        "aizynthfinder.chem": types.SimpleNamespace(Molecule=lambda smiles: types.SimpleNamespace(smiles=smiles)),
        "aizynthfinder.context": types.ModuleType("aizynthfinder.context"),
        "aizynthfinder.context.stock": types.SimpleNamespace(
            InMemoryInchiKeyQuery=lambda path: Path(path).read_text(encoding="ascii").splitlines()),
    }
    # sys.modules 是 Python 记“已经导入了哪些模块”的字典：往里放了假货，之后的 import 拿到的就是它。
    # monkeypatch.setitem(字典, 键, 值)：临时改字典里的一项，测试结束时自动恢复原样
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    # 交给桥接脚本的请求，也就是 request.json 的内容（各个键的含义见 aizynth_bridge.py 的文件头）。
    # 库存里是苯酚，在售试剂表里是碘甲烷（各用它的 InChIKey），用户排除的是乙酰氯 CC(=O)Cl
    request = {"config": "config.yml", "target_smiles": ASPIRIN, "stock": "zinc", "expansion_policy": "uspto",
               "filter_policy": "uspto", "max_steps": 4, "iteration_limit": 50, "time_limit": 60, "stock_mode": "inventory_first",
               "inventory_inchikeys": ["ISWSIDIOOBJBQZ-UHFFFAOYSA-N"], "catalog_inchikeys": ["INQOMBQAUSQDDS-UHFFFAOYSA-N"],
               "excluded_smiles": ["CC(=O)Cl"]}
    result = aizynth_bridge.run(request)
    finder = Finder.last
    # 搜索开始时名单里有两样：目标分子自己（prepare_tree 加的）和用户排除的乙酰氯。
    # run() 要是在 prepare_tree 之前加的乙酰氯，它会被清掉，这里就只剩阿司匹林
    assert finder.excluded_during_search == {ASPIRIN, "CC(=O)Cl"}
    # 终点 = 引擎自己的在售清单 + 我们的在售试剂表（碘甲烷这类小分子引擎的清单里没有）+ 客户库存
    assert finder.stock.selected == ["zinc", "catalog", "inventory"]
    # 两份额外的清单是 run() 先写成临时文件、再让（假的）InMemoryInchiKeyQuery 读回来的，内容就是请求里给的
    assert finder.stock.loaded["inventory"] == ["ISWSIDIOOBJBQZ-UHFFFAOYSA-N"]
    assert finder.stock.loaded["catalog"] == ["INQOMBQAUSQDDS-UHFFFAOYSA-N"]
    assert (finder.expansion_policy.selected, finder.filter_policy.selected) == ("uspto", "uspto")
    search = finder.config.search
    # 请求里的 max_steps 在 AiZynthFinder 那边叫 max_transforms
    assert (search.max_transforms, search.iteration_limit, search.time_limit) == (4, 50, 60)
    # 没有出任何岔子，所以没有提示；统计数字原样带回
    assert result["warnings"] == [] and result["stats"] == {"number_of_nodes": 7}
    # 每条路线的分数取路线树 scores 里的第一个值，solved 取 metadata 里的 is_solved
    assert [(route["score"], route["solved"]) for route in result["routes"]] == [(0.9, True)]

    # {**request, "stock_mode": …}：把 request 抄一份，只改 stock_mode 这一项，再跑一次
    aizynth_bridge.run({**request, "stock_mode": "inventory_only"})
    assert Finder.last.stock.selected == ["inventory"]  # “仅用库存”：在售清单不算终点


def test_engine_selection_and_bridge_error_reporting(tmp_path, monkeypatch):
    """按配置选对逆合成引擎；AiZynthFinder 用不了的时候，报错里说清楚是哪一项、该怎么修。

    保证的行为：
        m03_retrosynthesis.build_engine（服务启动时按 M3_ENGINE 等配置选引擎）：
            demo 用演示引擎；auto 时，配了 AIZYNTH_CONFIG 并且找得到 aizynthfinder 就用 AiZynthFinder，
            否则退回演示引擎；明确指定 aizynth_bridge 而配置不全，当场报错
        AiZynthEngine.describe 和 search：配置文件不存在、Python 解释器不存在，搜索之前就用中文说明是哪个配置项；
            配置里的相对路径按项目根目录算；没填 AIZYNTH_PYTHON 时用后端自己的 Python
        AiZynthEngine._run_subprocess：子进程失败时，报错里带上它自己的输出；认出 pyarrow 和 NumPy 的版本冲突并指路
        aizynth_bridge._json_safe：把结果里 JSON 存不了的值换成存得了的
    和界面的关系：GET /api/meta/modules 和命令行 status 里 M3 一栏显示的就是 describe() 的内容；
    搜索失败时，EngineError 的这段文字会成为这次分析的失败原因（analyses 表的 error 列）。
    怎么读：这个测试不建数据库，也不调接口，只用 make_settings(tmp_path, …) 造各种配置，一段一段地试。
    中间用了两处 monkeypatch：把“环境里装没装 aizynthfinder”的探测函数换成固定回 True / False；
    把桥接脚本的路径换成临时写的两个小脚本，让真的子进程照我们要的方式失败。
    为什么重要：AiZynthFinder 装起来有不少坑（README“踩过的坑”）。报错要是只有一句“搜索失败”，
    维护的人不知道该去改配置、下载模型，还是换 pyarrow 的版本。
    """
    # make_settings 默认的 m3_engine 是 "demo"
    assert build_engine(make_settings(tmp_path)).name == "demo"
    # auto，并且指了一个 Python、给了配置文件名：条件满足，选的是 AiZynthFinder（另起进程的方式，mode 是 bridge）。
    # build_engine 只看配置项填没填，不看文件在不在
    configured = make_settings(tmp_path, m3_engine="auto", aizynth_python="C:/no/such/python.exe", aizynth_config="config.yml")
    engine = build_engine(configured)
    assert isinstance(engine, AiZynthEngine) and engine.describe()["mode"] == "bridge"
    # 模型还没下载（配置文件不存在）、Python 的位置写错：搜索之前就说清楚是哪一项
    # 这里的 config.yml 是相对路径，按项目根目录算，那里没有这个文件。describe() 把问题写在 problem 里，不抛异常
    assert engine.describe()["ready"] is False and "AIZYNTH_CONFIG" in engine.describe()["problem"]
    # 真去搜索时则抛 EngineError。SearchRequest 里只有目标分子是必填的
    with pytest.raises(EngineError, match="找不到 AiZynthFinder 的配置文件"):
        engine.search(SearchRequest(target_smiles=ASPIRIN))
    # 在临时目录里放一个真的配置文件（只检查它在不在，内容无所谓）。这一关过了，轮到那个不存在的 Python 报错
    config = tmp_path / "config.yml"
    config.write_text("expansion: {}\n", encoding="utf-8")
    engine = build_engine(make_settings(tmp_path, m3_engine="auto", aizynth_python="C:/no/such/python.exe", aizynth_config=str(config)))
    with pytest.raises(EngineError, match="找不到装了 AiZynthFinder 的 Python"):
        engine.search(SearchRequest(target_smiles=ASPIRIN))
    # 明确指定 aizynth_bridge，却没有给 AIZYNTH_CONFIG：build_engine 自己就抛错（正式运行时服务会起不来），
    # 消息里提到要设 AIZYNTH_CONFIG，必要时还要设 AIZYNTH_PYTHON
    with pytest.raises(EngineError, match="AIZYNTH_PYTHON"):
        build_engine(make_settings(tmp_path, m3_engine="aizynth_bridge"))
    # .env 里写相对路径时按项目根目录算
    # 借项目里两个确实存在的文件来充当“Python”和“配置文件”：main.py 和 data/scoring.toml。
    # describe() 只看文件在不在，不看内容，所以 ready 是 True；config_path 已经变成了绝对路径
    relative = AiZynthEngine(make_settings(tmp_path, aizynth_python="main.py", aizynth_config="data/scoring.toml"), in_process=False)
    assert relative.describe()["ready"] and relative.config_path.endswith("scoring.toml") and Path(relative.config_path).is_absolute()

    # AiZynthFinder 和后端装在同一个环境里时不用填 AIZYNTH_PYTHON：另起进程用的就是后端自己的 Python
    # monkeypatch.setattr(对象, "属性名", 新值)：临时换掉模块里的一个函数，测试结束时自动换回来。
    # _aizynth_importable 本来是去查环境里装没装 aizynthfinder；这里让它固定回 True，结果就不取决于这台电脑装了什么。
    # sys.executable 是正在跑测试的这个 Python 解释器的路径
    monkeypatch.setattr(m03_retrosynthesis, "_aizynth_importable", lambda: True)
    engine = build_engine(make_settings(tmp_path, m3_engine="auto", aizynth_config=str(config)))
    assert engine.describe()["mode"] == "bridge" and engine.describe()["ready"] and engine.python_path == sys.executable
    # 环境里没有 aizynthfinder、也没指到别的 Python：退回演示路线
    monkeypatch.setattr(m03_retrosynthesis, "_aizynth_importable", lambda: False)
    assert build_engine(make_settings(tmp_path, m3_engine="auto", aizynth_config=str(config))).name == "demo"

    # 真的起一个子进程，让它像“环境里没装 aizynthfinder”那样失败：报错里要带上它自己的输出
    # 做法：写一个一运行就抛 ModuleNotFoundError 的小脚本，把 BRIDGE_SCRIPT（桥接脚本的路径）临时指到它。
    # engine 还是上面那个“用后端自己的 Python”的引擎，所以子进程起得来，只是马上以退出码 1 结束。
    # pytest.raises(…) as failed：把抛出来的异常接住，failed.value 就是它
    script = tmp_path / "missing.py"
    script.write_text("raise ModuleNotFoundError(\"No module named 'aizynthfinder'\")\n", encoding="utf-8")
    monkeypatch.setattr(m03_retrosynthesis, "BRIDGE_SCRIPT", script)
    with pytest.raises(EngineError, match="退出码 1") as failed:
        engine.search(SearchRequest(target_smiles=ASPIRIN))
    assert "No module named 'aizynthfinder'" in str(failed.value)
    # 最常见的失败：环境里 pyarrow 和 NumPy 版本冲突。认出来并指路
    # 子进程的输出里出现 “pyarrow requires NumPy” 时，报错后面会多一段怎么修的说明，里面有“版本冲突”这几个字
    script = tmp_path / "conflict.py"
    script.write_text("raise ImportError('pyarrow requires NumPy 2.0 or newer, found 1.26.4')\n", encoding="utf-8")
    monkeypatch.setattr(m03_retrosynthesis, "BRIDGE_SCRIPT", script)
    with pytest.raises(EngineError, match="版本冲突"):
        engine.search(SearchRequest(target_smiles=ASPIRIN))
    # 桥接脚本要能把 numpy 之类的值变成普通 JSON
    # 这个例子里：元组和集合变成列表，NaN 变成 None，字典的键一律变成字符串（3 变成 "3"）
    assert aizynth_bridge._json_safe({"a": (1, 2.5), "b": float("nan"), 3: {"x"}}) == {"a": [1, 2.5], "b": None, "3": ["x"]}


# ---------------------------------------------------------------- 队列与数据导入


def test_redis_queue_round_trips_jobs_in_order():
    """Redis 队列先进先出：先放进去的任务先被取出来，内容原样；队列空了以后取到 None。

    保证的行为：syntherisk/taskqueue.py 的 RedisQueue：enqueue（API 进程往里放）和 pop（Worker 进程往外取）。
    一个任务是 (任务类型, 参数字典)，在 Redis 里存成一段 JSON，放在一个列表里。
    怎么读：用 fakeredis.FakeRedis() 当 Redis：它是一个进程内的假 Redis，不用装、不用起服务。
    传了 client 以后，第一个参数的地址 "redis://unused" 就不起作用了。放两个任务，取三次。
    为什么重要：后台任务的两种类型（analysis.run 跑分析、inventory.import 导入大表格）都走这个队列。
    取出来的类型和参数要是和放进去的对不上，Worker 就会跑错任务，或者找不到那一次分析。
    """
    queue = RedisQueue("redis://unused", client=fakeredis.FakeRedis())
    queue.enqueue("analysis.run", {"analysis_id": "a1"})
    queue.enqueue("inventory.import", {"import_id": "i1"})
    # pop 返回 (任务类型, 参数字典)；timeout=1 是最多等 1 秒。先放的先出来
    assert queue.pop(timeout=1) == ("analysis.run", {"analysis_id": "a1"})
    assert queue.pop(timeout=1) == ("inventory.import", {"import_id": "i1"})
    # 队列已经空了：等到超时也没有任务，返回 None
    assert queue.pop(timeout=1) is None


def test_api_enqueues_to_redis_and_a_separate_worker_runs_the_analysis(client, account, ctx):
    """生产部署的形态：API 只把任务推进 Redis，Worker 进程取出来跑，进度通过数据库回到 API。

    保证的行为：
        队列是 Redis 时，POST /api/analyses（api/analyses.py 的 create_analysis）只把任务入队就返回：
            分析的状态是 queued，一个阶段都还没有开始
        syntherisk/worker.py 的 run_worker 从队列里取出任务，交给 taskqueue.run_job，再到 pipeline.run_analysis 跑完
        跑完以后，GET /api/analyses/{id} 看到的是 succeeded 和八个 done；
            GET /api/analyses/{id}/events（进度推送，SSE）能把 Worker 写下的进度事件发出来
    怎么读：
        准备  client 和 ctx 夹具默认用的是同步队列。这里把 ctx.queue 换成接着 fakeredis 的 RedisQueue，
              于是提交分析只是往假 Redis 里放了一个任务。
        调用  在测试自己的线程里直接调 run_worker（正式部署时它在另一个进程里一直循环）。它本来要跑到收到停止信号为止；
              这里交给它的“停止开关”是 StopWhenIdle：队列一空就算“该停了”，所以它处理完这一个任务就返回。
        断言  run_worker 返回 1（处理了一个任务）；再通过接口看分析的状态和进度事件。
    为什么重要：README 说明真的 Redis 没有在这台电脑上验证过；这个测试用假 Redis 把
    “API 入队、Worker 执行、进度回到 API”这条链路走了一遍。链路不通的话，生产环境里提交的分析会一直停在“排队中”。
    """
    queue = RedisQueue("redis://unused", client=fakeredis.FakeRedis())
    # 换掉上下文上的队列：之后接口里的 ctx.queue.enqueue(…) 就是往假 Redis 里放任务
    ctx.queue = queue
    queued = start_analysis(client, account)
    # 还没有人去执行任务，所以状态是排队中；current_stage 是 0，表示一个阶段都没开始
    assert queued["status"] == "queued" and queued["current_stage"] == 0

    class StopWhenIdle(threading.Event):
        """队列空了就让 Worker 停下来，测试才能结束。

        顶替的是 worker.main() 里那个真的 threading.Event（收到 Ctrl+C 或 SIGTERM 时才置位）。
        run_worker 每一轮循环开头都问一次 stop.is_set()，回 True 就退出循环。
        这里改写了 is_set：Redis 里存任务的那个列表（键名是 QUEUE_KEY）空了，就回 True。
        """

        def is_set(self):
            """“该不该停”：队列里一个任务都不剩时回 True。"""
            # llen 是 Redis 的命令：这个列表里还有几项。_redis 是 RedisQueue 里面的 Redis 客户端
            return queue._redis.llen(QUEUE_KEY) == 0

    # poll_seconds=1：每次最多等 1 秒任务。返回值是这一次运行一共执行了几个任务
    assert run_worker(ctx, queue, StopWhenIdle(), poll_seconds=1) == 1
    finished = client.get(f"/api/analyses/{queued['id']}", headers=account["headers"]).json()
    # ["done"] * 8 是八个 "done" 组成的列表：八个阶段全部完成
    assert finished["status"] == "succeeded" and [s["status"] for s in finished["stages"]] == ["done"] * 8
    # client.stream(…)：这个接口的响应是一段一段发回来的（Server-Sent Events），所以用流的方式读，
    # stream.iter_text() 逐段给出文字。分析已经结束，接口把存着的事件发完、再发一个 end，就关闭连接。
    # 这里只确认其中有 scored 这条事件：它是 Worker 在第 7 阶段评完分时写进数据库的
    with client.stream("GET", f"/api/analyses/{queued['id']}/events", headers=account["headers"]) as stream:
        assert "event: scored" in "".join(stream.iter_text())


def test_loading_precedents_changes_evidence_levels_in_the_next_analysis(client, account, ctx, tmp_path):
    """导入先例库（M5）：该拒绝的拒绝，统计数字对得上；导入以后，下一次分析的证据等级、产率、置信度和排名随之改变。

    保证的行为：
        syntherisk/loaders.py 的 load_precedents（命令行 load-precedents 背后的函数）：
            来源不允许使用、文件不存在、给的是压缩包、表头里没有反应那一列：各用一句中文说明，什么都不导入
            反应读不出来的行跳过并计数；带原子映射的反应算出反应中心
            用多个进程算，结果和单进程一样；replace=True 先删掉旧数据，不会导成两份
            带着 replace 递错了文件时先报错，原来导好的数据不丢
        loaders.summarize（命令行 status 里显示的数据量）
        m05_precedents.PrecedentIndex.refresh：重新导入以后版本号变了，内存里的索引会重新加载
        导入之后的分析（流水线第 4 阶段）：库里有同一个反应的步骤，证据等级是 E1、三颗星，产率用文献报道的值，
            说明里带出处编号；路线的置信度和排名也跟着变
    怎么读：
        准备  在临时目录里写一个 .rsmi 文件（Lowe 整理的 USPTO 文件的格式）：表头，加两行数据。
              一行是“水杨酸 + 乙酸酐 → 阿司匹林”（硫酸在试剂栏，产率 ~85%，1999 年，专利号 USTEST0001），
              另一行不是反应。
        前半  直接调 loaders 的函数：先试三种该报错的输入，再正常导入、用两个进程重新导入、递错文件。
        后半  提交阿司匹林的分析（演示引擎给三条路线），看“乙酸酐酰化”那一步的证据卡，和各条路线的置信度、名次。
    为什么重要：先例库是证据等级的全部依据（README：先例库里只要有数据，查不到先例的步骤就会被判成
    “仅模型预测”并且扣分）。导入时悄悄丢了数据、导成了两份，或者删了旧的却没导进新的，
    之后每一次分析的证据等级都会跟着错。
    """
    # Lowe 的 USPTO 文件格式（制表符分隔）。下面的反应和专利号是测试里现编的。
    # 六列依次是：反应 SMILES、专利号、段落号、年份、从专利正文里提取的产率、计算出来的产率。\t 是制表符。
    # 第一行数据的反应写成 “反应物>试剂>产物”：水杨酸 + 乙酸酐 > 硫酸 > 阿司匹林，带原子映射
    rsmi = tmp_path / "sample.rsmi"
    rsmi.write_text(
        "ReactionSmiles\tPatentNumber\tParagraphNum\tYear\tTextMinedYield\tCalculatedYield\n"
        "[OH:1][c:2]1[cH:3][cH:4][cH:5][cH:6][c:7]1[C:8](=[O:9])[OH:10].[CH3:11][C:12](=[O:13])O[C:12]([CH3:11])=[O:13]"
        ">OS(=O)(=O)O>[CH3:11][C:12](=[O:13])[O:1][c:2]1[cH:3][cH:4][cH:5][cH:6][c:7]1[C:8](=[O:9])[OH:10]\tUSTEST0001\t12\t1999\t~85%\t\n"
        "this is not a reaction\tUSTEST0002\t1\t2001\t\t\n",
        encoding="utf-8",
    )
    # 第三个参数是数据来源的编号。reaxys_scifinder 在 data/data_sources.toml 里登记的是 allowed = false
    # （学术许可，不能用于产品），所以被拒绝；报错的那句话是“…没有登记，或者登记为不允许使用…”。
    # progress 是进度回调，每导完一批调一次；这里给一个什么都不做的函数（lambda _m: None），不往屏幕上打印
    with pytest.raises(loaders.LoaderError, match="没有登记"):
        loaders.load_precedents(ctx, rsmi, "reaxys_scifinder", progress=lambda _m: None)
    # 路径写错、把压缩包直接递进来：说人话，不甩一屏报错
    with pytest.raises(loaders.LoaderError, match="找不到文件"):
        loaders.load_precedents(ctx, tmp_path / "路径" / "没有这个文件.rsmi", "uspto", progress=lambda _m: None)
    # 这个 .7z 文件其实也不存在：loaders 先看扩展名、再看文件在不在，所以报的是“是压缩包”
    with pytest.raises(loaders.LoaderError, match="是压缩包"):
        loaders.load_precedents(ctx, tmp_path / "1976_Sep2016_USPTOgrants_smiles.7z", "uspto", progress=lambda _m: None)
    stats = loaders.load_precedents(ctx, rsmi, "uspto", progress=lambda _m: None)
    # 这条反应带原子映射（两半乙酸酐还标着同一套映射号，专利数据里常见），所以算得出反应中心
    # read 2：读了两行数据；loaded 1：导进去那条反应；skipped 1：“this is not a reaction”那一行读不出来；
    # with_center 1：导进去的那一条算出了反应中心
    assert stats == {"read": 2, "loaded": 1, "skipped": 1, "duplicates": 0, "failed": 0, "with_center": 1}
    # summarize 按来源数先例的条数，另外数其中带反应中心的有几条
    assert loaders.summarize(ctx)["precedents"] == {"uspto": 1}
    assert loaders.summarize(ctx)["precedents_with_center"] == 1
    with ctx.session_factory() as session:
        # select(某一列) 配 session.scalar：取查到的第一行的这一列（库里只有一行）。
        # 这串文字和引擎给的同一步反应（MAPPED_ACETYLATION）算出来的一模一样
        assert session.scalar(select(models.ReactionPrecedent.center)) == "F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1"
    # 让内存里的先例索引按现在的库加载一遍，记下这时的版本号（每导入一次，库的版本号换一个随机值）
    with ctx.session_factory() as session:
        ctx.precedent_index.refresh(session)
    first_version = ctx.precedent_index.version
    # 全量数据要用多个进程算指纹：结果和单进程一样；--replace 先清掉旧的，不会导成两份
    parallel = loaders.load_precedents(ctx, rsmi, "uspto", replace=True, workers=2, progress=lambda _m: None)
    assert parallel == stats and loaders.summarize(ctx)["precedents"] == {"uspto": 1}
    # 重新导入同一个文件，行数没有变：靠版本号让（可能正在运行的）后端重新加载内存里的索引
    with ctx.session_factory() as session:
        ctx.precedent_index.refresh(session)
    # 三个条件：第一次的版本号不是空的；现在的版本号不是空的、也不是第一次那个；索引里仍然只有 1 条
    assert first_version and ctx.precedent_index.version not in (None, first_version) and ctx.precedent_index.count == 1
    # 带着 --replace 递错了文件（表头里没有反应那一列）：先报错，原来导好的数据一条不少
    wrong = tmp_path / "wrong.csv"
    wrong.write_text("name,price\naspirin,10\n", encoding="utf-8")
    with pytest.raises(loaders.LoaderError, match="反应 SMILES"):
        loaders.load_precedents(ctx, wrong, "uspto", replace=True, progress=lambda _m: None)
    assert loaders.summarize(ctx)["precedents"] == {"uspto": 1}

    # 导入完成。下面看它对分析的影响：演示引擎给阿司匹林三条路线，里面一共有三步不同的反应
    analysis = start_analysis(client, account, smiles=ASPIRIN)
    # stages[3] 是第 4 阶段“文献先例”，它的说明里数了各个证据等级各有几步；其中 1 步是“直接报道”（E1）
    assert "直接报道 1" in analysis["stages"][3]["message"]
    # r1 是演示数据里的第一条路线“乙酸酐酰化”，只有一步：水杨酸 + 乙酸酐 → 阿司匹林，正是刚导进去的那个反应
    card = client.get(f"/api/analyses/{analysis['id']}/routes/r1/steps/1", headers=account["headers"]).json()
    assert card["evidence"]["level"] == "E1" and card["evidence"]["stars"] == 3
    # 文件里写的 “~85%” 被读成了 85；E1 的产率标签写的是“文献报道”
    assert card["yield"]["label"] == "文献报道 85%（1 个先例）"
    # 先例列表里正好一条，就是导进去的那条专利。exact 是 True：反应物和产物都一样的“同一反应”
    (precedent,) = card["precedents"]
    assert precedent["document_id"] == "USTEST0001" and precedent["year"] == 1999 and precedent["exact"]
    # 证据卡上六个维度的分数，做成 {维度: 分数} 的字典
    dimensions = {d["key"]: d["score"] for d in card["dimensions"]}
    # 产率这一维直接用文献产率的中位数：85。
    # 文献依据这一维：E1 的分数段是 90–100（data/scoring.toml 的 [evidence.levels.E1]），
    # 有 1 条同一反应的先例时给 90 + (100 − 90) × 1/3 = 93.3，3 条及以上才到 100（m05_precedents 的 _grade）。
    # pytest.approx(93.3) 是“约等于”：比较小数时容许极小的误差
    assert dimensions["yield"] == 85 and dimensions["literature"] == pytest.approx(93.3)
    # 说明是按模板写的；引用先例的句子后面带着 [1]，也就是先例 P1 的引用标记
    assert "[1]" in card["explanation"]["text"]
    # 每一步都有文献的路线置信度高；从苯酚出发的那条有一步（羧基化）找不到先例，两步里占一步，置信度低
    result = client.get(f"/api/analyses/{analysis['id']}/result", headers=account["headers"]).json()
    # 结果页的路线卡片按路线名做成字典。下面用到的两个名字来自 data/demo_routes/aspirin.json。
    # “从苯酚出发”那条有两步：第 1 步是苯酚的羧基化，第 2 步就是上面那步乙酸酐酰化
    by_name = {route["name"]: route for route in result["routes"]}
    assert by_name["乙酸酐酰化"]["confidence_label"] == "高" and by_name["乙酸酐酰化"]["evidence_text"] == "每步都有文献"
    kolbe = by_name["从苯酚出发（Kolbe–Schmitt）"]
    # 两步里有一步是低置信步骤，占比 0.5 超过了 low_fraction_for_low（0.34），所以整条路线的置信度是“低”
    assert kolbe["confidence_label"] == "低" and kolbe["confidence_reason"] == "第 1 步无文献先例"
    assert by_name["乙酸酐酰化"]["rank"] < kolbe["rank"]  # 低置信度路线不排在高置信度路线前面


def test_a_database_created_before_a_column_was_added_gets_it_on_startup(tmp_path):
    """先例库的表是早先建的、还没有“反应中心”这一列：启动时补上，原有的数据不动。

    保证的行为：syntherisk/db.py 的 init_db 里调的 _add_missing_columns。建表用的 create_all 只建还不存在的表，
    表已经在了就不会发现模型里多了一列；所以启动时另外对照一张清单（_ADDED_COLUMNS），给旧表补上缺的列。
    怎么读：
        1. 用一份配置建上下文（这时会建库、建表），放一条先例，然后关掉。
        2. 绕过后端，用 Python 自带的 sqlite3 直接打开这个数据库文件，把 center 这一列删掉：
           这样就造出了一个“加入这一列之前建的”旧库。
        3. 用同一份配置再建一次上下文，相当于后端重新启动。查出来原来那条先例还在，center 是空的。
    这个测试不用 ctx 夹具：它要用同一个数据库文件先后建两次上下文，所以自己建、自己收尾。
    为什么重要：这台电脑上的先例库有一百七十多万行，全量导入一次要半小时左右（README）。
    代码里加了新列以后，旧库要是不能直接用，查先例时就会报“没有这一列”（db.py 里的说明）。
    """
    import sqlite3

    settings = make_settings(tmp_path)
    # 第一次“启动”：build_context 里建库、建表
    first = build_context(settings)
    with first.session_factory() as session:
        # 一条不带原子映射的先例（苯乙酮还原成 1-苯乙醇），所以它的 center 本来就是空的
        session.add(precedent_from_reaction("CC(=O)c1ccccc1>>CC(O)c1ccccc1", source="uspto", document_id="TEST-OLD"))
        session.commit()
    # 关掉后端这一边的数据库连接
    first.engine.dispose()
    # 数据库文件就是 make_settings 指定的 <tmp_path>/test.db。下面那句 SQL 是把 center 这一列从表里删掉
    connection = sqlite3.connect(tmp_path / "test.db")
    connection.execute("ALTER TABLE reaction_precedents DROP COLUMN center")
    connection.commit()
    connection.close()

    # 第二次“启动”：build_context 里的 init_db 发现 reaction_precedents 表缺 center 这一列，补上
    second = build_context(settings)
    with second.session_factory() as session:
        # scalars(…).one()：要求查出来正好一行。这个查询会读 center 列，列要是没补上，查询本身就会报错
        row = session.scalars(select(models.ReactionPrecedent)).one()
        assert row.document_id == "TEST-OLD" and row.center is None
    # 补上的列在原有的行上是空值，所以“带反应中心的先例”是 0 条
    assert loaders.summarize(second)["precedents_with_center"] == 0
    second.engine.dispose()


def test_loading_a_purchasable_catalog(client, account, ctx, tmp_path):
    """导入在售目录（M4）：目录里有的物质显示“可购买”；重复导入不会重复加；目录和库存里都没有的是“买不到”。

    保证的行为：
        syntherisk/loaders.py 的 load_catalog（命令行 load-catalog 背后的函数）：每行一个 InChIKey 的文本文件，
            取每行的前 14 位（key14）写进 catalog_compounds 表；已经在目录里的跳过
        GET /api/materials/availability（api/structures.py 的 material_availability，按钮 C4 原料标签）：
            按结构查库存和在售目录，返回状态、是在哪个目录里查到的、价格的显示文字
    怎么读：
        准备  在临时目录里写一个两行的清单文件。两个 InChIKey 分别是 D-葡萄糖和咖啡因的，这两个分子都不在内置试剂表里。
        调用  以 zinc 为来源把这个文件导入两次；然后通过接口查咖啡因和另一个哪里都没有的分子。
        断言  两次导入的统计数字；两个分子的可得性。
    为什么重要：原料买不买得到，决定路线会不会因为“原料买不到”被排除（m10_scoring.exclusion_reasons 里的 stock）。
    内置的在售目录只有一百来种常用试剂（README），真正用起来要靠导入外部的目录。
    """
    catalog = tmp_path / "stock.txt"
    # 第一行是 D-葡萄糖的 InChIKey，第二行是咖啡因的（下面用咖啡因的 SMILES 去查，对上的就是这一行）
    catalog.write_text("WQZGKKKJIJFFOK-GASJEMHNSA-N\nRYYVLZVUVIJVGH-UHFFFAOYSA-N\n", encoding="ascii")
    assert loaders.load_catalog(ctx, catalog, "zinc", progress=lambda _m: None) == {"read": 2, "loaded": 2, "existing": 0}
    # 上一句是第一次导入：read 读到 2 个，loaded 新加 2 个，existing 是 0（原来一个都没有）。
    # 同一个文件再导一遍：两个都已经在目录里，不再加
    assert loaders.load_catalog(ctx, catalog, "zinc", progress=lambda _m: None)["existing"] == 2
    # params 是加在地址后面的查询参数，这里相当于 ?smiles=…
    caffeine = {"smiles": "CN1C=NC2=C1C(=O)N(C(=O)N2C)C"}
    availability = client.get("/api/materials/availability", headers=account["headers"], params=caffeine).json()
    # catalog_source 是导入时给的来源编号 zinc
    assert availability["status"] == "purchasable" and availability["catalog_source"] == "zinc"
    # 价格现在没有数据，一律显示“待询价”；库存里没有它，所以 inventory 是 None
    assert availability["price_label"] == "待询价" and availability["inventory"] is None
    # 目录和库存里都没有的：买不到
    # 这个分子是演示数据里布洛芬格氏路线的“氯代物”中间体，内置试剂表和刚导入的目录里都没有它
    unknown = client.get("/api/materials/availability", headers=account["headers"], params={"smiles": "CC(C)Cc1ccc(cc1)C(C)Cl"}).json()
    assert unknown["status"] == "unavailable" and unknown["label"] == "买不到"
