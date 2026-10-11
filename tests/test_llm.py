"""大模型的三处用途（M2、M11、M12）和调用层。测试里不联网：用假的模型和假的 HTTP 传输。

这个文件测的是什么
    后端只有三处会调用大模型，这里各有一组测试，另有一组测两个厂商的调用层：
        M2  约束解析   syntherisk/modules/m02_constraints.py   一句话补充要求 → 约束标签（A14）
                       接口 POST /api/constraints/parse（syntherisk/api/structures.py 的 parse_requirement）
        M11 写说明     syntherisk/modules/m11_explain.py       根据证据卡写说明，每句附出处
                       用在分析的第 8 阶段，以及打开证据卡时补写（B10）
        M12 问答       syntherisk/modules/m12_qa.py            只根据证据卡回答（C5）
        调用层         syntherisk/llm/                         LLMService 和两个厂商适配类
                       （AnthropicProvider、OpenAICompatibleProvider）
        用量统计       GET /api/analyses/{id}/llm-usage（syntherisk/api/analyses.py 的 llm_usage）
    三处用途有同一条规矩，测试围绕它展开：大模型的输出不能直接信。
        - 没有配置大模型、调用失败、模型拒答时，各模块退回关键词规则或模板，请求不会因此失败；
        - 模型给的约束标签要在词表（data/vocabulary.toml）里，不在的如实告诉用户“暂不支持”；
        - 模型写的每句话都要带出处，出处必须是证据卡里有的，句子里的数字也必须是卡里有的，否则丢掉。

本文件自己的替身（都不联网）
    FakeProvider            假的厂商适配对象：按顺序吐出预先写好的回答，交给 LLMService 用
    _anthropic_response()   造一个长得像 Anthropic SDK 返回值的对象
    FakeAnthropicClient     假的 Anthropic SDK 客户端：记下收到的参数，返回上面那个对象
    httpx.MockTransport     httpx 自带的假传输层：请求不上网，交给测试里写的 handler 函数处理
                            （测 OpenAICompatibleProvider 时用）
共用的夹具和辅助函数
    ctx、client、account、start_analysis   来自 tests/conftest.py。测试环境没有配置大模型（LLM_PROVIDER=offline），
                                           所以 ctx.llm 本来是“不调大模型”的；要测大模型的路径时，
                                           测试自己用 LLMService(FakeProvider(...)) 建一个
    card                                   本文件的夹具：布洛芬格氏路线第 4 步（格氏羧基化）的证据卡

本文件里有什么
    FakeProvider
        假的厂商适配对象
    test_rule_parser_reproduces_the_example_from_the_deck
        没配大模型时，关键词规则能解析出 PPT 里那个例子的两个标签（一硬一软）
    test_rule_parser_handles_substances_steps_and_things_it_cannot_enforce
        关键词规则认得具体物质和步数上限；只是陈述的话、执行不了的要求归到“暂不支持”
    test_llm_tags_outside_the_vocabulary_are_reported_as_unsupported
        大模型给的标签不在词表里、物质认不出来时归到“暂不支持”；调用失败时退回关键词规则
    test_confirmed_requirement_tags_are_enforced_by_the_pipeline
        用户确认过的标签随分析提交以后真的被执行（用到钯催化剂的路线被排除）
    card
        夹具：一张真实跑出来的证据卡
    test_sentences_without_valid_sources_or_with_invented_numbers_are_dropped
        句子核对的规则：没有出处、出处不在卡里、数字不在卡里的句子都被丢掉
    test_llm_explanation_is_kept_only_where_it_cites_the_card
        大模型写的说明只留下通过核对的句子；一句都不剩或者模型拒答时退回模板
    test_route_explanation_says_so_when_no_score_could_be_computed
        路线没有算出分数时，推荐理由照实说，不出错
    test_question_answering_refuses_to_go_beyond_the_card
        问答只留有出处的回答；模型说答不了，或者出处是卡外的，就回答“没有这项依据”
    test_llm_calls_are_logged_for_cost_tracking
        每次大模型调用都记了用量，能按分析汇总；同一步的说明不重复调用
    _anthropic_response、FakeAnthropicClient
        Anthropic SDK 的替身
    test_anthropic_provider_requests_structured_output_and_reads_past_thinking_blocks
        Claude 的调用层：请求的写法、跳过思考块读结果、拒答和截断变成对应的错误
    test_openai_compatible_provider_speaks_chat_completions
        兼容 OpenAI 的调用层：请求的写法、两种 JSON 模式、各种出错的回复都变成 LLMError
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from syntherisk.cards import build_card, find_route
from syntherisk.llm import LLMError, LLMRefusal, LLMResponse, LLMService
from syntherisk.llm.anthropic_provider import AnthropicProvider
from syntherisk.llm.openai_compatible import OpenAICompatibleProvider
from syntherisk.modules.m02_constraints import ConstraintParser
from syntherisk.modules.m11_explain import Explainer, validate_sentences
from syntherisk.modules.m12_qa import NO_EVIDENCE, QuestionAnswerer
from tests.conftest import start_analysis


class FakeProvider:
    """按顺序吐出预先写好的回答，并记下每次收到的请求。

    顶替的是真的厂商适配对象（syntherisk/llm/ 下的 AnthropicProvider、OpenAICompatibleProvider）。
    它有同样的两个属性 name、model 和同样的方法 complete_json（syntherisk/llm/base.py 的 LLMProvider
    规定的样子），所以可以直接交给 LLMService(FakeProvider(...))，业务模块分不出它是假的。
    和真的不同：
      - 不联网。建的时候把“模型的回答”一个个传进来，每被调用一次就按顺序取出一个返回；
      - 传进来的某一项如果是异常对象（比如 LLMError("超时")），轮到它时就把它抛出来，用来模拟调用失败；
      - 每次调用收到的系统提示词、用户消息、Schema 的名字都记在 self.requests 里，测试可以回头看发了什么；
      - 用量是固定的：每次调用算 120 个输入 token、40 个输出 token。
    谁在用：本文件里测 M2、M11、M12 和用量统计的各个测试。
    """

    name = "fake"
    model = "fake-model"

    def __init__(self, *answers):
        """answers：预先写好的回答，按被调用的先后依次用掉。每一项是一个字典（模型返回的 JSON），或者一个异常对象。"""
        self.answers = list(answers)
        self.requests = []

    def complete_json(self, *, system, user, schema, schema_name):
        """和真的厂商适配对象同样的参数（都只能按名字传）。记下这次请求，然后给出下一个预先写好的回答。

        返回 LLMResponse（data 就是预先写好的那个字典）；轮到的是异常对象时把它抛出来。
        schema（要求模型遵守的 JSON 格式）收下但不用：回答是写死的。
        """
        self.requests.append({"system": system, "user": user, "schema_name": schema_name})
        # pop(0) 取出并去掉列表的第一项，所以回答是按传进来的先后用掉的
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(data=answer, provider=self.name, model=self.model, input_tokens=120, output_tokens=40)


# ---------------------------------------------------------------- M2 约束解析


def test_rule_parser_reproduces_the_example_from_the_deck(ctx):
    """没有配置大模型时，关键词规则能把 PPT 里的那句例子解析成两个标签：一个硬约束，一个软偏好。

    保证的行为：syntherisk/modules/m02_constraints.py 的 ConstraintParser.parse 在没有大模型时走
    _parse_with_rules：把句子按标点切成小句，每个小句里找表示软硬的措辞和词表（data/vocabulary.toml）里的
    同义词。“不要”是硬约束的措辞，“尽量”是软偏好的措辞。
    怎么读：LLMService(None) 就是“没有配置大模型”。解析“不要用钯催化剂，尽量不过柱色谱”，
    看返回的标签。每个标签比四项：类型、词表里的编号、软硬、显示的文字。
    """
    parser = ConstraintParser(ctx.reference, LLMService(None))
    parsed = parser.parse("不要用钯催化剂，尽量不过柱色谱")
    # generated_by 是 rules：这次是关键词规则解析的；整句话都解析出来了，没有“暂不支持”的部分
    assert parsed["generated_by"] == "rules" and parsed["unsupported"] == []
    # 第一个小句：“钯催化剂”是词表里试剂类别 palladium 的同义词，硬约束
    # 第二个小句：“柱色谱”是纯化方式 column_chromatography 的同义词，软偏好
    assert [(t["kind"], t["value"], t["hardness"], t["label"]) for t in parsed["tags"]] == [
        ("exclude_reagent_class", "palladium", "hard", "排除试剂：钯催化剂"),
        ("avoid_purification", "column_chromatography", "soft", "纯化偏好：避免柱色谱"),
    ]


def test_rule_parser_handles_substances_steps_and_things_it_cannot_enforce(ctx):
    """关键词规则还认得具体物质和步数上限；只是陈述的话、现在执行不了的要求，如实归到“暂不支持”。

    保证的行为（m02_constraints.py 的 _parse_with_rules、_make_tag）：
      - 点名一个具体物质（“氯仿”）：在内置试剂表（data/compounds.csv）里按名称或别名找到它，
        标签里带上它的结构，显示用表里的正式名称“三氯甲烷”；
      - “不超过 4 步”这类写法认成步数上限；
      - 小句里没有“不要”“尽量”这类措辞，就看不出用户是要排除还是在陈述，不生成标签；
      - 有措辞、但词表里没有对得上的内容，也不生成标签；这两种小句都原样放进 unsupported；
      - 空字符串直接返回空结果。
    怎么读：一句话里用分号隔开四个小句，各对应上面的一种情况。
    为什么重要：m02_constraints.py 的文件头写明了这样做的目的——不出现“界面上显示了约束、
    流水线却执行不了”的情况。
    """
    parser = ConstraintParser(ctx.reference, LLMService(None))
    parsed = parser.parse("不能用氯仿；不超过 4 步；我们有手套箱；希望便宜一点")
    # 把标签做成 {(类型, 值): 标签} 方便按类型取
    tags = {(t["kind"], t["value"]): t for t in parsed["tags"]}
    # “氯仿”是内置试剂表里三氯甲烷的别名；ClC(Cl)Cl 是它的 SMILES（一个碳上三个氯）
    assert tags[("exclude_substance", "三氯甲烷")]["smiles"] == "ClC(Cl)Cl"
    assert tags[("max_steps", "4")]["label"] == "步数 ≤ 4"
    # “我们有手套箱”只是陈述，不能变成“排除手套箱”；“便宜一点”现在没有价格数据，执行不了
    assert parsed["unsupported"] == ["我们有手套箱", "希望便宜一点"]
    assert parser.parse("") == {"tags": [], "unsupported": [], "generated_by": "rules", "note": ""}


def test_llm_tags_outside_the_vocabulary_are_reported_as_unsupported(ctx):
    """大模型给出的标签要再检查一遍：不在词表里的类别、认不出结构的物质归到“暂不支持”；调用失败退回关键词规则。

    保证的行为（m02_constraints.py）：
      - 配了大模型时，parse 把词表拼进系统提示词（_system_prompt），拿到回答以后逐个标签检查（_validate、
        _make_tag）：试剂类别、条件的编号必须在 data/vocabulary.toml 里，具体物质必须能找到结构；
        通不过的标签不要，把它对应的那段原话加进 unsupported；标签上显示的文字由后端自己生成；
      - 大模型调用抛出 LLMError 时不让请求失败，改用关键词规则解析，并在 note 里写明原因。
    怎么读：FakeProvider 预先写好一个回答，里面有四个标签和一条模型自己放进 unsupported 的原话。
    四个标签里两个合格、两个不合格。解析的那句话写成“……”，因为假模型不看输入。
    第二段换一个一调用就抛 LLMError("超时") 的 FakeProvider，看是不是退回了规则。
    """
    provider = FakeProvider({
        "tags": [
            # 合格：tin 是词表里的试剂类别“有机锡试剂”
            {"kind": "exclude_reagent_class", "value": "tin", "hardness": "hard", "source_text": "不用锡试剂"},
            # 不合格：词表的试剂类别里没有 plutonium（钚）
            {"kind": "exclude_reagent_class", "value": "plutonium", "hardness": "hard", "source_text": "不用钚"},
            # 不合格：内置试剂表里没有这个名字，这里也没有给名称解析器，找不到结构
            {"kind": "exclude_substance", "value": "独角兽粉", "hardness": "hard", "source_text": "不用独角兽粉"},
            # 合格：cryogenic 是词表里的条件“低温”，软偏好
            {"kind": "exclude_condition", "value": "cryogenic", "hardness": "soft", "source_text": "最好不要低温"},
        ],
        "unsupported": ["要好闻"],
    })
    parsed = ConstraintParser(ctx.reference, LLMService(provider)).parse("……")
    # 这次是大模型解析的；model 是厂商适配对象在回答里报的型号（FakeProvider 固定是 fake-model）
    assert parsed["generated_by"] == "llm" and parsed["model"] == "fake-model"
    # 只留下两个合格的标签。文字是后端按词表生成的：硬约束的试剂类别是“排除试剂：…”，软偏好的条件是“尽量避免：…”
    assert [t["label"] for t in parsed["tags"]] == ["排除试剂：有机锡试剂", "尽量避免：低温"]
    # 先是模型自己说不支持的，后面是两个没通过检查的标签对应的原话（source_text）
    assert parsed["unsupported"] == ["要好闻", "不用钚", "不用独角兽粉"]
    assert "tin（有机锡试剂）" in provider.requests[0]["system"]  # 词表写进了提示词

    # 调用失败的情况：FakeProvider 的回答是一个异常对象，被调用时抛出来
    failing = ConstraintParser(ctx.reference, LLMService(FakeProvider(LLMError("超时"))))
    fallback = failing.parse("不要用钯催化剂")
    # 退回关键词规则；note 里带着失败的原因；规则照样解析出了一个标签（钯催化剂）
    assert fallback["generated_by"] == "rules" and "超时" in fallback["note"] and len(fallback["tags"]) == 1


def test_confirmed_requirement_tags_are_enforced_by_the_pipeline(client, account):
    """用户确认过的一句话要求随分析一起提交以后，真的会被执行：用到钯催化剂的路线被排除。

    保证的行为：
      - A14 的流程是先解析（POST /api/constraints/parse，syntherisk/api/structures.py 的 parse_requirement），
        用户确认以后，前端把标签放进开始分析时的 constraints.requirements；
      - 流水线第 1 阶段把这些标签并进实际执行的约束（syntherisk/constraints.py 的 merge_constraints），
        进度里写明其中几条来自一句话要求；
      - 硬约束“排除试剂类别”由 syntherisk/modules/m10_scoring.py 的 exclusion_reasons 执行：路线的哪一步
        用到这一类试剂，就排除这条路线并写明是哪一步、哪个试剂。某个物质属不属于某一类，
        看 data/vocabulary.toml 里这个类别的定义（钯催化剂：结构里含钯）。
    怎么读：提交布洛芬，约束里带一个“排除钯催化剂”的硬约束标签。三条演示路线里只有 BHC 的第 3 步
    （钯催化羰基化）用到钯催化剂。然后单独调一次解析接口，看没配大模型时返回什么。
    """
    # 一个确认过的标签。标签里只需要 kind、value、hardness；显示的文字后端会按词表重新生成
    palladium = {"kind": "exclude_reagent_class", "value": "palladium", "hardness": "hard"}
    analysis = start_analysis(client, account, constraints={"requirement_text": "不要用钯催化剂", "requirements": [palladium]})
    assert "其中 1 条来自一句话要求" in analysis["stages"][0]["message"]
    result = client.get(f"/api/analyses/{analysis['id']}/result", headers=account["headers"]).json()
    # 被排除的正好一条（左边的写法要求右边的列表只有一个元素）
    (bhc,) = result["excluded_routes"]
    # 试剂的名字取自内置试剂表（data/compounds.csv 里的 pdcl2_pph3_2）
    assert bhc["exclusion_reasons"][0]["label"] == "第 3 步用到钯催化剂：双(三苯基膦)二氯化钯"

    # 解析接口：测试环境没有配置大模型，用的是关键词规则，note 里如实说明。“尽量”是软偏好的措辞
    parsed = client.post("/api/constraints/parse", headers=account["headers"], json={"text": "尽量不过柱色谱"}).json()
    assert parsed["tags"][0]["hardness"] == "soft" and parsed["note"] == "未配置大模型，用关键词规则解析"


# ---------------------------------------------------------------- M11 / M12


@pytest.fixture
def card(client, account, ctx):
    """夹具：一张真实跑出来的证据卡，给下面测 M11、M12 的测试当输入。

    @pytest.fixture 把这个函数登记成“夹具”：测试函数的参数里写 card，pytest 就先运行它，把返回值传进去。
    做法：不带约束提交一次布洛芬的分析（同步队列，返回时已经跑完），从数据库里读出这次分析存下的结果，
    用 syntherisk/cards.py 的 build_card 拼出格氏路线（r1）第 4 步“格氏羧基化”的证据卡。
    这张卡的来源编号里有 ROUTE、RULE:anhydrous_organometallic、GHS、STOCK、CALC、SCORE、DEMO，
    没有任何先例（P1、P2……），因为测试的先例库是空的。
    """
    analysis = start_analysis(client, account)
    with ctx.session_factory() as session:
        from syntherisk import models

        # result 列是加密存的，通过模型读出来时已经自动解密成字典
        result = session.get(models.Analysis, analysis["id"]).result
    return build_card(result, find_route(result, "r1"), 4, ctx.reference)


def test_sentences_without_valid_sources_or_with_invented_numbers_are_dropped():
    """核对大模型写的句子：没有出处、出处不在卡里、数字不在卡里的句子都丢掉。

    保证的行为：syntherisk/modules/m11_explain.py 的 validate_sentences（M11 和 M12 都用它）。
    一句话要同时满足三条才留下：有出处；每个出处都在允许的范围里；句子里的每个数字都能在发给模型的事实里
    找到（允许四舍五入，相差不到 0.51 就算）。同一个出处写了两遍会去重。输入不是列表时当作一句都没有。
    怎么读：这个测试不建数据库、不调模型，直接拿五句话去核对。允许的出处是 SCORE 和 P1 两个；
    事实里出现的数字是 78.4、60、75、3。
    为什么重要：m11_explain.py 把这一步称作说明里不出现编造内容的最后一道检查。
    """
    facts = {"score": 78.4, "yield": "文献报道 60–75%（3 个先例）"}
    # 返回 (留下的句子, 丢掉的句数)
    kept, dropped = validate_sentences(
        [
            # 留下：78 和事实里的 78.4 只差 0.4，算四舍五入
            {"text": "综合得分 78 分", "sources": ["SCORE"]},
            # 留下：60 和 75 事实里都有；出处 P1 写了两遍，留下以后只剩一个
            {"text": "报道产率 60–75%", "sources": ["P1", "P1"]},
            {"text": "产率可以达到 95%", "sources": ["P1"]},  # 卡里没有 95 这个数
            {"text": "这一步很成熟", "sources": []},  # 没有出处
            {"text": "参考了经典教材", "sources": ["教材"]},  # 出处不在卡里
        ],
        {"SCORE", "P1"},
        facts,
    )
    assert [s["text"] for s in kept] == ["综合得分 78 分", "报道产率 60–75%"] and dropped == 3
    assert kept[1]["sources"] == ["P1"]
    # 模型返回的不是句子列表（这里是一个字符串）：当作一句都没有，也没有丢掉的
    assert validate_sentences("不是列表", {"SCORE"}, facts) == ([], 0)


def test_llm_explanation_is_kept_only_where_it_cites_the_card(card):
    """M11 写说明：大模型写的句子只留下通过核对的；一句都没通过，或者模型拒答，就退回模板。

    保证的行为（syntherisk/modules/m11_explain.py 的 Explainer.explain_step）：
      - 发给大模型的是证据卡的精简版（syntherisk/cards.py 的 card_facts）：只有事实和来源编号，转成 JSON 文本；
      - 回来的句子逐句核对（validate_sentences），没通过的丢掉，丢了几句记在 dropped_sentences 里；
      - 留下的句子拼成一段文字，每句后面是方括号里的出处标记（cards.py 的 render_sentences）；
      - 一句都不剩，或者调用抛出 LLMError（模型拒答 LLMRefusal 是它的一种），改用模板生成，
        generated_by 是 template，note 里写明原因。
    怎么读：card 夹具给出格氏羧基化那一步的证据卡。FakeProvider 预先写好三句话：前两句的出处是卡里有的
    ROUTE 和 RULE:anhydrous_organometallic；第三句引用的 P7 卡里没有（这张卡没有任何先例），
    句子里的 99 卡里也没有。然后回头看发给模型的用户消息。最后一段试两种要退回模板的情况。
    """
    provider = FakeProvider({
        "sentences": [
            {"text": "这一步用格氏试剂和二氧化碳反应得到布洛芬", "sources": ["ROUTE"]},
            {"text": "操作上需要无水无氧条件", "sources": ["RULE:anhydrous_organometallic"]},
            {"text": "文献报道产率 99%", "sources": ["P7"]},
        ]
    })
    explanation = Explainer(LLMService(provider)).explain_step(card)
    # 大模型写的，丢掉了一句（第三句）
    assert explanation["generated_by"] == "llm" and explanation["dropped_sentences"] == 1
    # 留下的两句拼在一起。ROUTE 显示成 [路线]；RULE:anhydrous_organometallic 显示成 [规则 · 气氛]
    # （“气氛”是这条规则在 data/condition_rules.toml 里的 category）
    assert explanation["text"] == "这一步用格氏试剂和二氧化碳反应得到布洛芬[路线]。操作上需要无水无氧条件[规则 · 气氛]。"
    # 发给模型的用户消息是一段 JSON，解析回来看里面有什么
    sent = json.loads(provider.requests[0]["user"])
    # step 是卡片的标题；allowed_sources 列出模型可以引用的全部来源编号
    assert sent["step"] == "第 4 步 · 格氏羧基化" and "RULE:anhydrous_organometallic" in sent["allowed_sources"]
    # 结构图不发给模型
    assert "svg" not in provider.requests[0]["user"]

    # 一句都没通过核对，或者模型拒答：退回模板，不把没有出处的话给用户
    # 第一个假模型只写了一句没有出处的话；第二个一调用就抛 LLMRefusal（模型拒答）
    for provider in (FakeProvider({"sentences": [{"text": "相信我", "sources": []}]}), FakeProvider(LLMRefusal("拒答"))):
        fallback = Explainer(LLMService(provider)).explain_step(card)
        # 模板生成的说明有内容，note 里说明了为什么没用大模型的结果
        assert fallback["generated_by"] == "template" and fallback["sentences"] and fallback["note"]


def test_route_explanation_says_so_when_no_score_could_be_computed(client, account, ctx):
    """整条路线的推荐理由：正常时写出综合得分；路线没有算出分数时照实说，不能出错。

    保证的行为：syntherisk/modules/m11_explain.py 的 Explainer.explain_route 按模板生成时，第一句里写路线分；
    路线分是 None（每一步的六个维度都没有数据）时，改写成“各项都没有数据，没有算出综合得分”，
    而不是拿 None 去套数字格式而抛异常。
    怎么读：跑一次布洛芬的分析，从数据库里取出格氏路线。先按原样生成推荐理由，里面有它的分数；
    再把这条路线的 score 手工改成 None（改的是复制出来的字典，不影响存着的结果），重新生成。
    LLMService(None) 表示没有配置大模型，所以两次都是模板生成的。
    """
    analysis = start_analysis(client, account)
    with ctx.session_factory() as session:
        from syntherisk import models

        result = session.get(models.Analysis, analysis["id"]).result
    # dict(…) 复制一份路线字典，下面改 score 改的是这份副本
    route = dict(find_route(result, "r1"))
    explainer = Explainer(LLMService(None))
    # :g 去掉分数末尾多余的 0，和模板里的写法一样
    assert f"综合得分 {route['score']:g}" in explainer.explain_route(route)["text"]

    # 每一步的六个维度都没有数据时路线分是 None，模板要照实说，不能在这里出错
    route["score"] = None
    explanation = explainer.explain_route(route)
    assert explanation["generated_by"] == "template" and "没有算出综合得分" in explanation["text"]


def test_question_answering_refuses_to_go_beyond_the_card(card):
    """M12 问答：有出处的回答原样给出；模型说答不了，或者写的话出处不在卡里，就回答“证据卡里没有这项依据”。

    保证的行为（syntherisk/modules/m12_qa.py 的 QuestionAnswerer.answer）：
      - 把证据卡的精简版和问题一起发给大模型，模型返回“能不能答”（answerable）和若干句话；
      - 句子照样用 validate_sentences 核对；模型说能答、并且至少有一句通过核对，才算答上了；
      - 否则 answerable 是 false，answer 是固定的那句 NO_EVIDENCE（“证据卡里没有这项依据。”）。
    怎么读：用 card 夹具的证据卡。先让假模型给一个出处有效的回答；再试两种答不了的情况：
    模型自己说答不了；模型说能答，但出处写的是“我的知识”，不是卡里的来源。
    """
    grounded = FakeProvider({"answerable": True, "sentences": [{"text": "需要无水无氧操作", "sources": ["RULE:anhydrous_organometallic"]}]})
    answer = QuestionAnswerer(LLMService(grounded)).answer(card, "操作上要注意什么？")
    # 句子通过了核对，后面接上出处标记 [规则 · 气氛]
    assert answer["answerable"] and answer["answer"] == "需要无水无氧操作[规则 · 气氛]。"
    # 用户的问题原样放在发给模型的 JSON 里（键是 question，证据卡在 card 这个键下）
    assert json.loads(grounded.requests[0]["user"])["question"] == "操作上要注意什么？"

    for data in (
        # 模型自己判断卡里没有回答这个问题的依据
        {"answerable": False, "sentences": []},
        # 模型说能答，但这句话的出处不是卡里的来源，核对时被丢掉，等于一句都没有
        {"answerable": True, "sentences": [{"text": "机理是亲核加成", "sources": ["我的知识"]}]},
    ):
        answer = QuestionAnswerer(LLMService(FakeProvider(data))).answer(card, "机理是什么？")
        assert not answer["answerable"] and answer["answer"] == NO_EVIDENCE


def test_llm_calls_are_logged_for_cost_tracking(client, account, ctx):
    """每次大模型调用都记一行用量，可以按分析汇总出来；同一步的说明只调一次模型。

    保证的行为：
      - syntherisk/llm/base.py 的 LLMService 每调用一次大模型，就往 llm_calls 表记一行
        （哪个模块、哪个型号、输入输出各多少 token）；
      - GET /api/analyses/{id}/llm-usage（syntherisk/api/analyses.py 的 llm_usage）按模块和型号汇总；
      - 分析的第 8 阶段（syntherisk/pipeline.py 的 _explain）给前 3 条路线各写一段推荐理由，
        再给每一步写一段说明；同一步出现在几条路线里、序号和分数都一样时复用，不再调模型。
    怎么读：把上下文里的 ctx.llm 换成接着 FakeProvider 的 LLMService，并把数据库会话工厂交给它
    （有了会话工厂它才会记用量）。假模型准备了 40 个一样的回答，比实际要用的多。
    然后不带约束跑一次布洛芬的分析，看第 8 阶段的进度和用量接口。
    """
    # [回答] * 40 得到 40 项的列表，前面的 * 把它拆成 40 个参数。每个回答是一句出处为 SCORE 的话，
    # SCORE 在路线和步骤的允许出处里都有，所以每次都能通过核对
    ctx.llm = LLMService(FakeProvider(*[{"sentences": [{"text": "路线可行", "sources": ["SCORE"]}]}] * 40), ctx.session_factory)
    analysis = start_analysis(client, account)
    usage = client.get(f"/api/analyses/{analysis['id']}/llm-usage", headers=account["headers"]).json()
    # 三条路线一共 16 段说明（3 段推荐理由 + 13 个步骤）；
    # 三氯化铝催化的傅-克酰基化在两条路线里是同一步，只调一次模型，所以是 15 次调用
    # 13 个步骤 = 格氏路线 4 步 + Boots 6 步 + BHC 3 步
    assert "给前 3 条路线写了 16 段说明" in analysis["stages"][7]["message"]
    assert usage["calls"] == 15
    # 都是 M11 调的。token 数是 FakeProvider 每次固定报的 120 和 40 乘以 15 次：1800 和 600
    assert usage["by_module"] == [{"module": "M11", "model": "fake-model", "calls": 15, "input_tokens": 1800, "output_tokens": 600}]


# ---------------------------------------------------------------- 厂商适配


def _anthropic_response(stop_reason="end_turn", text='{"ok": true}'):
    """造一个长得像 Anthropic SDK 返回值的对象，交给下面的 FakeAnthropicClient 返回。

    参数：
        stop_reason  模型为什么停下：end_turn 是正常结束；测试里还会传 refusal（拒答）和 max_tokens（被截断）。
        text         模型写的文本，应当是一段 JSON。
    返回的对象有 AnthropicProvider 会读的那几个属性：stop_reason、model、usage（输入输出的 token 数）、
    content（内容块的列表）。SimpleNamespace(a=1) 得到一个可以用 .a 取值的简单对象，这里拿它冒充 SDK 的对象。
    """
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-haiku-5-5",
        usage=SimpleNamespace(input_tokens=321, output_tokens=45),
        # 开了思考时，文本前面会有 thinking 块
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
    )


class FakeAnthropicClient:
    """假的 Anthropic SDK 客户端：不联网，记下每次调用收到的参数，返回预先给它的那个响应。

    顶替的是 anthropic.Anthropic(...) 建出来的真客户端。AnthropicProvider 调的是
    client.messages.create(…)，所以这里做一个带 create 的 messages 属性就够了。
    AnthropicProvider 的构造函数留了 client 这个参数，测试把假客户端从那里传进去。
    谁在用：下面测 AnthropicProvider 的那个测试。
    """

    def __init__(self, response):
        """response：每次调用都返回的那个响应对象（_anthropic_response 造的）。"""
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._response = response

    def _create(self, **kwargs):
        """冒充 messages.create：把这次调用的全部参数（一个字典）记进 self.calls，返回固定的响应。"""
        self.calls.append(kwargs)
        return self._response


def test_anthropic_provider_requests_structured_output_and_reads_past_thinking_blocks():
    """Claude 的调用层：请求按结构化输出的写法发，结果跳过思考块读出来；拒答和被截断变成对应的错误。

    保证的行为（syntherisk/llm/anthropic_provider.py 的 AnthropicProvider.complete_json）：
      - 请求里用 output_config.format 要求输出符合给定的 JSON Schema，用 output_config.effort 控制思考深度；
        系统提示词带缓存标记；用户消息作为唯一的一条消息；不带 temperature 这类采样参数，也不带 thinking；
      - 读结果时按类型找文本块（回复前面可能有思考块，不能直接取第一个），解析成字典；
        token 数取自回复里的 usage；
      - stop_reason 是 refusal 时抛 LLMRefusal，是 max_tokens 时抛 LLMError（输出被截断）。
    怎么读：这个测试不需要数据库，也不联网。建一个 FakeAnthropicClient 传给 AnthropicProvider，
    调一次 complete_json，然后看返回值，再看假客户端记下的那一次调用的参数。最后两段换成“拒答”“被截断”的响应。
    为什么重要：业务模块只接 LLMError（见 syntherisk/llm/base.py）。拒答和截断不转成这两种错误的话，
    业务模块就不会退回规则或模板。
    """
    client = FakeAnthropicClient(_anthropic_response())
    provider = AnthropicProvider(model=AnthropicProvider.DEFAULT_MODEL, client=client)
    # 一个很小的 JSON Schema：返回的对象必须有一个布尔型的 ok，不许有别的键
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    response = provider.complete_json(system="系统提示", user="问题", schema=schema, schema_name="demo")
    # data 是文本块里那段 JSON 解析出来的；token 数是假响应的 usage 里写的 321 和 45
    assert response.data == {"ok": True} and (response.input_tokens, response.output_tokens) == (321, 45)

    # 假客户端正好被调用了一次；call 是那一次的参数
    (call,) = client.calls
    # 型号是 AnthropicProvider.DEFAULT_MODEL
    assert call["model"] == "claude-haiku-5-5"
    # effort 没有另外指定，是默认的 low；format 里原样带着上面的 schema
    assert call["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    # 系统提示词写成内容块，带缓存标记
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call["messages"] == [{"role": "user", "content": "问题"}]
    # 这个型号不接受采样参数，也不用手动的思考预算
    # set(call) 是这次调用用到的参数名；& 求两个集合的交集，交集是空的才对
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(call)

    # pytest.raises(异常类型)：with 里面的代码必须抛出这种异常，否则测试失败。模型拒答 → LLMRefusal
    with pytest.raises(LLMRefusal):
        AnthropicProvider(model="claude-haiku-5-5", client=FakeAnthropicClient(_anthropic_response("refusal"))).complete_json(
            system="s", user="u", schema=schema, schema_name="demo")
    # 输出被截断 → LLMError，而且错误说明里要有“截断”两个字（match 按正则表达式在说明里查找）
    with pytest.raises(LLMError, match="截断"):
        AnthropicProvider(model="claude-haiku-5-5", client=FakeAnthropicClient(_anthropic_response("max_tokens"))).complete_json(
            system="s", user="u", schema=schema, schema_name="demo")


def test_openai_compatible_provider_speaks_chat_completions():
    """兼容 OpenAI 的调用层：按 Chat Completions 的格式发请求、读回复；各种出错的回复都变成 LLMError。

    保证的行为（syntherisk/llm/openai_compatible.py 的 OpenAICompatibleProvider.complete_json）：
      - 接口地址是 base_url 后面接 /chat/completions，base_url 末尾带不带斜杠都一样；密钥放在请求头里；
      - json_schema 模式：把 Schema 放进请求的 response_format，由厂商按它约束输出；
        json_object 模式：只要求输出 JSON，Schema 的原文写进系统提示词；
      - 回复里的 content 解析成字典，token 数取自 usage 的 prompt_tokens、completion_tokens；
      - 状态码不是 200、回复的结构不对、content 不是合法的 JSON，都抛 LLMError。
    怎么读：这个测试不联网。httpx.MockTransport(handler) 是 httpx 自带的假传输层：客户端发出的请求不上网，
    直接交给 handler 函数，由它返回一个 httpx.Response。handler 把收到的请求记进 seen，
    测试回头检查请求的地址和请求体。依次试：正常的一次、json_object 模式、密钥不对、四种奇怪的回复。
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """冒充厂商的服务器：记下请求；请求头里的密钥不对就回 401，否则回一个正常的回复。

        正常回复的结构就是 Chat Completions 的格式：choices[0].message.content 是模型写的文本（一段 JSON），
        usage 里是用了多少 token，model 是实际作答的型号。
        """
        seen.append(request)
        if request.headers["authorization"] != "Bearer test-key":
            return httpx.Response(401, json={"error": "bad key"})
        return httpx.Response(200, json={
            "model": "qwen-flash",
            "choices": [{"message": {"role": "assistant", "content": '{"tags": [], "unsupported": []}'}}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 12},
        })

    schema = {"type": "object"}
    # base_url 末尾故意多带了一个斜杠；transport 参数是 OpenAICompatibleProvider 专门留给测试换传输层用的
    provider = OpenAICompatibleProvider(base_url="https://llm.example.com/v1/", api_key="test-key", model="qwen-flash",
                                        transport=httpx.MockTransport(handler))
    response = provider.complete_json(system="系统", user="用户", schema=schema, schema_name="tags")
    # data 是 content 里那段 JSON 解析出来的；输入 token 数是 usage 里的 prompt_tokens
    assert response.data == {"tags": [], "unsupported": []} and response.input_tokens == 200
    # seen[0] 是第一次请求。先看地址（没有因为多带的斜杠变成两个斜杠），再看请求体
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://llm.example.com/v1/chat/completions"
    # 默认是 json_schema 模式：带上 Schema 的名字和内容，strict 表示要严格照 Schema 输出
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "tags", "schema": schema, "strict": True}}

    # json_object 模式：厂商只保证输出是 JSON，所以 Schema 写进了系统提示词（messages[0] 是系统消息）
    plain = OpenAICompatibleProvider(base_url="https://llm.example.com/v1", api_key="test-key", model="m",
                                     json_mode="json_object", transport=httpx.MockTransport(handler))
    plain.complete_json(system="系统", user="用户", schema=schema, schema_name="tags")
    body = json.loads(seen[1].content)
    assert body["response_format"] == {"type": "json_object"} and "JSON Schema" in body["messages"][0]["content"]

    # 密钥不对：handler 回 401，调用层抛 LLMError，说明里带着状态码
    wrong_key = OpenAICompatibleProvider(base_url="https://llm.example.com/v1", api_key="nope", model="m",
                                         transport=httpx.MockTransport(handler))
    with pytest.raises(LLMError, match="401"):
        wrong_key.complete_json(system="s", user="u", schema=schema, schema_name="tags")

    # 各家“兼容”接口出错时回的东西五花八门：都要变成 LLMError，业务模块才会退回规则或模板，而不是整个请求报 500
    # 四种回复都是状态码 200、内容不对，每一项是 (回复的 JSON, 期望在错误说明里看到的字样)：
    #   choices 是 null；整个回复是一个列表；message 是 null —— 结构不对；
    #   content 不是一段文本，而是被拆成了一个列表 —— 当作不是合法的 JSON
    for payload, message in (
        ({"choices": None}, "格式不对"),
        ([], "格式不对"),
        ({"choices": [{"message": None}]}, "格式不对"),
        ({"choices": [{"message": {"content": [{"type": "text", "text": "{}"}]}}]}, "不是合法 JSON"),
    ):
        # 这里的 handler 是一个 lambda：不管收到什么请求，都回这一轮的 payload。
        # 写成 p=payload（默认参数）是为了在定义 lambda 的这一刻就把当前的 payload 记住
        odd = OpenAICompatibleProvider(base_url="https://llm.example.com/v1", api_key="k", model="m",
                                       transport=httpx.MockTransport(lambda _r, p=payload: httpx.Response(200, json=p)))
        with pytest.raises(LLMError, match=message):
            odd.complete_json(system="s", user="u", schema=schema, schema_name="tags")
