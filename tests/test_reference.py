"""data/ 目录下的表格本身要自洽：改了表格跑一下这组测试，能挡住大部分手误。

这个文件测的是什么
    后端的规则、阈值、试剂表都不写在代码里，而是放在 data/ 目录下，由化学和数据同学维护，
    启动时由 syntherisk/reference.py 的 ReferenceData.load 读进内存：
        compounds.csv              内置常用试剂表：名称、CAS 号、结构、GHS 危害编码、溶剂评级
        controlled_substances.csv  各地管制清单
        scoring.toml               评分权重、阈值、证据等级的分数段
        condition_rules.toml       条件与复杂度规则（M7）
        vocabulary.toml            约束词表（M2）
        data_sources.toml          数据来源和许可登记表
        demo_routes/*.json         内置演示路线（M3 的演示引擎读）
    这里不测接口，直接读这些文件，检查两件事：每个文件自己写得对不对（CAS 号的校验位、SMILES 读不读得出来、
    取值在不在允许的范围里）；文件之间互相引用的东西存不存在（规则里点名的物质、规则对应的约束编号）。
    另外顺带测了 reference.py 里几个查表的函数（按名称、别名、CAS 号查；把拆开的离子拼回成盐）。
    有些错在服务启动时就会报出来（比如试剂表里的 SMILES 读不出来）；另一些不会报错，只是悄悄不起作用
    （比如规则里的 SMARTS 写错了，那条规则就永远命中不了），这类错要靠这组测试来发现。

夹具和辅助函数
    reference   本文件的夹具：把 data/ 目录完整读一遍得到的 ReferenceData，整个文件只读一次
    _rows()     把 data/ 下的一个 CSV 原样读成字典的列表（不经过 reference.py 的整理）
    ctx         来自 tests/conftest.py，只有一个测试用它（要一个数据库会话）
    tmp_path    pytest 自带的夹具：这个测试专用的临时文件夹
    本文件没有替身类。

本文件里有什么
    test_every_compound_has_a_valid_cas_number_structure_and_unique_key
        试剂表每一行：key 不重复、CAS 号合法、SMILES 读得出来、溶剂评级是规定的四档之一
    test_hazard_codes_are_known_ghs_statements
        试剂表里的 H 编码都是后端认识的 GHS 危险性说明
    test_a_question_mark_means_unknown_not_harmless
        h_codes 留空是“不属于危险品”，写 ? 是“不知道”，两者的处理不一样
    test_controlled_list_entries_are_well_formed
        管制清单每一行的 CAS 号、地区、严格程度、结构都合规；乙酸酐三地受管制，丙酮只是一般管理
    test_lookup_by_name_alias_and_cas
        试剂表可以按中文名、别名、CAS 号、结构查
    test_ions_from_patent_data_are_put_back_together_as_salts
        专利数据里拆成离子写的盐能拼回去
    test_vocabulary_and_rules_only_reference_things_that_exist
        词表和条件规则里的 SMARTS 写得对，引用的物质、约束编号、纯化方式都存在
    test_scoring_weights_sum_to_one_and_bands_are_ordered
        六个权重加起来是 1，路线分的两个系数加起来是 1，四个证据等级的分数段从低到高
    test_data_source_registry_blocks_unlicensed_sources
        登记为不允许、或者根本没登记的数据来源不能用
    test_demo_routes_are_chemically_consistent
        演示路线每一条都能完整地整理出来，每一步都有名字
"""

import csv
import json
from pathlib import Path

import pytest

from syntherisk.external import is_cas_number
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m03_retrosynthesis import DemoEngine, SearchRequest
from syntherisk.reference import ReferenceData
from syntherisk.routes import UNNAMED_CONVERSION, UNNAMED_COUPLING, build_skeleton

# 项目里 data/ 目录的位置。__file__ 是本文件的路径，.parent 是 tests/，再 .parent 是项目根目录。
# 测的就是仓库里正在用的这些文件，不是另做的测试数据
DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def reference() -> ReferenceData:
    """夹具：把 data/ 目录读成 ReferenceData，和后端启动时做的是同一件事（context.py 的 build_context）。

    @pytest.fixture 把这个函数登记成“夹具”：测试函数的参数里写 reference，pytest 就把它的返回值传进去。
    scope="module" 表示整个文件只读一次，各个测试共用同一份（读一遍要解析一百多个 SMILES，没必要每个测试都读）。
    共用的前提是谁都不改它；要换掉里面的试剂表时，测试用 dataclasses.replace 另做一份。
    表格有硬伤（缺文件、试剂表里的 SMILES 读不出来、规则引用了不存在的物质）时，这里就会抛异常，
    用到它的测试全部报错。
    """
    return ReferenceData.load(DATA)


def _rows(name: str) -> list[dict[str, str]]:
    """把 data/ 下的一个 CSV 文件读成 [{列名: 值}, …]，每行一个字典，值都是文件里原样的文字。

    和 reference 夹具的区别：这里不做任何整理和解析，看到的就是文件里写的东西，
    适合逐行检查“这一格写得对不对”。
    参数：name  文件名，比如 "compounds.csv"。
    """
    # utf-8-sig：文件开头有 BOM（Excel 另存的 CSV 常带）就去掉；newline="" 是 csv 模块要求的打开方式。
    # csv.DictReader 把第一行当表头，之后每一行变成 {列名: 值}
    with (DATA / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_every_compound_has_a_valid_cas_number_structure_and_unique_key(reference):
    """内置试剂表（data/compounds.csv）每一行都要合规：key 不重复、CAS 号合法、SMILES 读得出来、溶剂评级在范围内。

    保证的行为：这是对数据文件的检查，不是对某个接口的。各列被谁用：
      - key：condition_rules.toml、vocabulary.toml 用它指物质（reference.py 的 CompoundLibrary.by_key），
        重复的话后一行会把前一行盖掉；
      - cas：按 CAS 号查物质（A3 名称查询、E1 导入库存）。格式和校验位由 syntherisk/external.py 的
        is_cas_number 检查，抄错一位一般就通不过；
      - smiles：用 m01_structure.py 的 mol_record 解析，读不出来的话后端启动时就会报错停下
        （CompoundLibrary.load）；
      - solvent_class：只能留空或者是 CHEM21 的四档之一。写了别的值，M9 算绿色化学分时会出错
        （syntherisk/modules/m09_green.py 的 evaluate_green 里说明了这一点）。
    怎么读：逐行检查文件里的原文，最后确认读进内存的条数和文件的行数一样（没有哪一行被漏掉）。
    """
    rows = _rows("compounds.csv")
    # 集合会去掉重复的：去重以后的个数等于行数，说明 key 没有重复
    assert len({row["key"] for row in rows}) == len(rows)
    for row in rows:
        # assert 逗号后面的文字是失败时显示的说明，这里写明是哪一行出了问题
        assert is_cas_number(row["cas"]), f"{row['key']} 的 CAS 号不对：{row['cas']}"
        assert chem.mol_record(row["smiles"]) is not None, f"{row['key']} 的 SMILES 读不出来"
        # 溶剂评级：空（不是溶剂），或者 推荐 / 有问题 / 有害 / 高度有害 四档之一
        assert row["solvent_class"] in ("", "recommended", "problematic", "hazardous", "highly_hazardous")
    assert len(reference.compounds.compounds) == len(rows)


def test_hazard_codes_are_known_ghs_statements():
    """试剂表里的每个 GHS 危害编码（H 编码）都是后端认识的。

    保证的行为：compounds.csv 的 h_codes 一列用分号隔开若干个 H 编码（比如 H225;H319）。后端按
    syntherisk/modules/m06_safety.py 的 H_STATEMENTS 把编码换成中文说明和危害类别；带字母后缀的编码
    （H360FD）按前四位查。表里出现 H_STATEMENTS 没有的编码时，后端不会报错，只是把它原样显示、
    不归到任何危害类别（m06_safety.py 的 statement_for），也就不扣分、不参与“排除剧毒”这类约束。
    所以编码打错了要靠这个测试发现。
    怎么读：逐行拆开 h_codes，看每个编码的前四位在不在 H_STATEMENTS 里。
    H_STATEMENTS 是在函数里面导入的，效果和写在文件开头一样。
    """
    from syntherisk.modules.m06_safety import H_STATEMENTS

    for row in _rows("compounds.csv"):
        if row["h_codes"] == "?":  # 不知道：留给 PubChem 去查，不当作“不属于危险品”
            continue
        # split(";") 按分号拆开；留空的格子拆出来是一个空字符串，filter(None, …) 把空的去掉
        for code in filter(None, row["h_codes"].split(";")):
            assert code[:4] in H_STATEMENTS, f"{row['key']}：不认识的危险性说明 {code}"


def test_a_question_mark_means_unknown_not_harmless(reference, ctx, tmp_path):
    """表里 h_codes 留空是“不属于危险品”，写 ? 是“不知道”：后者要接着去查，查不到就标没有数据。

    保证的行为：
      - 读表时（syntherisk/reference.py 的 CompoundLibrary.load），h_codes 留空读成空列表，写 ? 读成 None；
      - 评估一个物质的危害时（syntherisk/modules/m06_safety.py 的 SafetyService.assess），表里有明确分类的
        （包括空列表）直接用，数据来源记 local；是 None 的接着去查 PubChem，查不到就记 none（没有危害数据）。
    怎么读：不动仓库里的试剂表，在临时文件夹里另写一张只有两行的表：水（h_codes 留空）和一个“某个新试剂”
    （h_codes 写 ?）。读出来以后，用它顶替 reference 里的试剂表，建一个不接 PubChem 的 SafetyService，
    分别评估这两个物质。
    为什么重要：README 写明查不到分类的物质标“没有危害数据”，不当作安全。把“不知道”读成“不属于危险品”的话，
    一个没查过的试剂会被当成无害的，安全分偏高，“排除剧毒”这类约束也拦不住它。
    """
    # dataclasses.replace(对象, 字段=新值)：复制一份 dataclass 对象，只把指定的字段换掉，原来的对象不变
    from dataclasses import replace

    from syntherisk.modules.m06_safety import SafetyService
    from syntherisk.reference import CompoundLibrary

    # 临时的试剂表：表头和 data/compounds.csv 一样，下面两行数据。
    # 第二行借用了 4-异丁基苯乙酮的 CAS 号和结构，名字是随便起的；要紧的只是它的 h_codes 一格写着 ?
    table = tmp_path / "compounds.csv"
    table.write_text(
        "key,name_zh,name_en,synonyms,cas,smiles,roles,purchasable,h_codes,solvent_class,reviewed\n"
        "water,水,water,,7732-18-5,O,solvent,1,,recommended,0\n"
        "mystery,某个新试剂,mystery reagent,,38861-78-8,CC(C)Cc1ccc(cc1)C(C)=O,building_block,1,?,,0\n",
        encoding="utf-8",
    )
    library = CompoundLibrary.load(table)
    # ? 读成 None（不知道）；留空读成空列表（不属于危险品）
    assert library.by_key["mystery"].h_codes is None and library.by_key["water"].h_codes == []
    with ctx.session_factory() as session:
        # 四个参数：数据库会话；参考数据（试剂表换成了上面那张临时的）；PubChem 客户端给 None，表示不联网去查；
        # 最后是要看哪些地区的管制清单
        service = SafetyService(session, replace(reference, compounds=library), None, ["CN"])
        unknown = service.assess(library.by_key["mystery"].smiles)
        assert (unknown["data_source"], unknown["name"]) == ("none", "某个新试剂")  # 测试里不联网，查不到就是“没有数据”
        # 水在表里是“留空”：直接用表里的结论，数据来源是 local
        assert service.assess("O")["data_source"] == "local"


def test_controlled_list_entries_are_well_formed(reference):
    """管制清单（data/controlled_substances.csv）每一行都要合规；并抽查两个物质读进来以后的样子。

    保证的行为：这张表每行是“一种物质在一个地区的一张清单上”。M6 查管制情况时按结构去对
    （syntherisk/modules/m06_safety.py 的 SafetyService._controlled），所以：
      - CAS 号要合法；地区只能是 CN、US、CA、TW；严格程度只能是 strict（管得严）或 basic（一般管理）；
      - 写了 SMILES 的要读得出来。没写或者读不出来的行在读表时会被跳过、不报错（reference.py 的
        ReferenceData.load），那一行就等于不存在。
    严格程度决定用户勾了“排除管制化学品”时排不排除路线：只排除 strict 的，basic 的只做提示。
    怎么读：先逐行检查原文；再用 reference.controlled（{物质的 key14: [清单条目, …]}）抽查乙酸酐和丙酮。
    """
    for row in _rows("controlled_substances.csv"):
        assert is_cas_number(row["cas"]), row
        assert row["jurisdiction"] in ("CN", "US", "CA", "TW")
        assert row["severity"] in ("strict", "basic")
        if row["smiles"]:
            assert chem.mol_record(row["smiles"]) is not None, f"{row['name_zh']} 的 SMILES 读不出来"
    # 乙酸酐在三地都受管制；丙酮只是一般管理
    # key14_of 把 SMILES 换成这个物质的编号（InChIKey 的前 14 位），清单就是按它建的索引。
    # 表里乙酸酐有三行：中国（第二类）、美国（List II）、加拿大（Class A）
    acetic_anhydride = reference.controlled[chem.key14_of("CC(=O)OC(C)=O")]
    assert {entry.jurisdiction for entry in acetic_anhydride} == {"CN", "US", "CA"}
    # 丙酮（CC(C)=O）也有三行，三行的严格程度都是 basic
    assert all(entry.severity == "basic" for entry in reference.controlled[chem.key14_of("CC(C)=O")])


def test_lookup_by_name_alias_and_cas(reference):
    """内置试剂表可以按中文名、别名、CAS 号、结构来查，查不到返回 None。

    保证的行为（syntherisk/reference.py 的 CompoundLibrary）：
      - find_by_name：中文名、英文名、synonyms 一列里的别名都能查到；比较前会去掉空格、连字符，不分大小写；
      - find_by_cas：按 CAS 号查；
      - find_by_smiles：按结构查，同一个分子的 SMILES 写法不同也认得出来。
    这些函数被 A3 名称查询、E1 导入库存、给分子取名等很多地方用。
    怎么读：每一行查一个，看返回的是表里的哪一行（.key 是那一行的 key）。
    """
    library = reference.compounds
    # 正式的中文名
    assert library.find_by_name("乙醚").key == "diethyl_ether"
    # 别名：“Mg turnings” 写在镁屑那一行的 synonyms 里
    assert library.find_by_name("Mg turnings").key == "magnesium"
    # 带连字符的缩写：2-甲基四氢呋喃的别名
    assert library.find_by_name("2-Me-THF").key == "2_methyltetrahydrofuran"
    assert library.find_by_cas("15687-27-1").key == "ibuprofen"
    # 布洛芬的另一种 SMILES 写法（表里写的是 CC(C)Cc1ccc(cc1)C(C)C(=O)O），按结构比是同一个分子
    assert library.find_by_smiles("OC(=O)C(C)c1ccc(CC(C)C)cc1").key == "ibuprofen"
    assert library.find_by_name("不存在的试剂") is None


def test_ions_from_patent_data_are_put_back_together_as_salts(reference):
    """专利数据把盐拆成一个个离子来写；借用先例的试剂之前，要把它们拼回成盐。

    保证的行为：syntherisk/reference.py 的 CompoundLibrary.combine_ions。输入是试剂的 SMILES 列表
    （一个片段一项），输出的顺序是：先是不带电荷的片段，然后是认出来的盐（换成试剂表里的写法），
    最后是凑不成盐的离子；重复的只留一个。
    谁在用：流水线第 4 阶段借用先例的试剂时（syntherisk/pipeline.py 的 _adopt_precedent_reagents）。
    不拼回去的话，试剂栏里是一堆离子，查不到名字，也查不到危害（combine_ions 的说明里写了这个原因）。
    怎么读：六个例子。SMILES 里方括号加正负号的是离子，比如 [Na+] 是钠离子、[Cl-] 是氯离子。
    期望值里“表里的写法”直接取 library.by_key[…].smiles，不在测试里另写一遍。
    """
    library = reference.compounds
    # 专利数据的写法：三氯化铝拆成四个离子，后面跟着溶剂
    # 一个 +3 的铝离子和三个氯离子合起来电中性，分子式和表里的三氯化铝一样；S=C=S 是二硫化碳，不带电荷，排在前面
    assert library.combine_ions(["[Cl-]", "[Al+3]", "[Cl-]", "[Cl-]", "S=C=S"]) == ["S=C=S", library.by_key["aluminium_chloride"].smiles]
    # 硼氢根和钠离子拼成硼氢化钠；CO 是甲醇
    assert library.combine_ions(["[BH4-]", "[Na+]", "CO"]) == ["CO", library.by_key["sodium_borohydride"].smiles]
    # 两种盐混在一起：各归各的
    # 一个钠离子配氢氧根是氢氧化钠，两个钾离子配碳酸根是碳酸钾。用集合比，不管先后
    mixed = library.combine_ions(["[K+]", "[Na+]", "[OH-]", "[K+]", "O=C([O-])[O-]"])
    assert set(mixed) == {library.by_key["sodium_hydroxide"].smiles, library.by_key["potassium_carbonate"].smiles}
    # 表里没有的盐也合成一个物质；凑不成电中性的单个离子原样留着
    # 碘化钾不在试剂表里：钾离子和碘离子合成一个用 “.” 连起来的物质。CCO 是乙醇
    assert library.combine_ions(["[K+]", "[I-]", "CCO"]) == ["CCO", "[I-].[K+]"]
    # 只有一个氢氧根，没有阳离子和它配
    assert library.combine_ions(["[OH-]", "CCO"]) == ["CCO", "[OH-]"]
    # 没有带电荷的片段：只去掉重复的（乙醇出现了两次）。O 是水
    assert library.combine_ions(["CCO", "O", "CCO"]) == ["CCO", "O"]


def test_vocabulary_and_rules_only_reference_things_that_exist(reference):
    """约束词表（data/vocabulary.toml）和条件规则（data/condition_rules.toml）里写的东西都要对得上。

    保证的行为：
      - 词表里每个试剂类别至少给出一种认法：子结构（smarts）或者点名的物质（substances）；
        SMARTS 要写得对；点名的物质要是 compounds.csv 里有的 key；
      - 每条条件规则的 SMARTS 要写得对；规则对应的“做不了的条件”（constraint）、纯化方式（purification）
        要是词表里有的编号；
      - 抽查：醋酸钯属于“钯催化剂”这一类，乙醇不属于（syntherisk/reference.py 的 reagent_class_matches）。
    为什么重要：这几种错都不会让后端报错，只会让规则悄悄失效。SMARTS 写错时，m01_structure.py 的
    matches_smarts 直接返回 False，那条规则或那个类别就永远命中不了；规则的 constraint 和词表对不上时，
    用户在 A10 勾的条件和这条规则就连不起来（syntherisk/constraints.py 里 ConditionKey 的说明）。
    怎么读：两个循环分别扫词表的试剂类别和全部条件规则，最后是两句抽查。SMARTS 是描述子结构的写法，
    比如 [Pd] 表示“含有钯原子”。
    """
    vocabulary = reference.vocabulary
    for key, spec in vocabulary["reagent_class"].items():
        # 两种认法至少要有一种；assert 逗号后面的 key 是失败时显示出来的类别编号
        assert spec.get("smarts") or spec.get("substances"), key
        for pattern in spec.get("smarts", []):
            # smarts_pattern 把 SMARTS 解析成查询对象，写错了返回 None
            assert chem.smarts_pattern(pattern) is not None, f"{key}：SMARTS 写错了 {pattern}"
        for substance in spec.get("substances", []):
            assert substance in reference.compounds.by_key, f"{key} 引用了不存在的物质 {substance}"
    for rule in reference.condition_rules:
        for pattern in rule.smarts:
            assert chem.smarts_pattern(pattern) is not None, f"规则 {rule.id}：SMARTS 写错了 {pattern}"
        if rule.constraint:
            assert rule.constraint in vocabulary["condition"], rule.id
        if rule.purification:
            assert rule.purification in vocabulary["purification"], rule.id
    # 醋酸钯（两个乙酸根和一个二价钯离子）含钯，匹配 [Pd]；乙醇（CCO）不含
    assert reference.reagent_class_matches("palladium", "CC(=O)[O-].CC(=O)[O-].[Pd+2]")
    assert not reference.reagent_class_matches("palladium", "CCO")


def test_scoring_weights_sum_to_one_and_bands_are_ordered(reference):
    """评分配置（data/scoring.toml）的几个基本关系：权重加起来是 1，两个系数加起来是 1，等级的分数段从低到高。

    保证的行为：这些数字是 M10 评分和 M5 定证据等级时直接拿来算的
    （syntherisk/modules/m10_scoring.py、syntherisk/modules/m05_precedents.py）：
      - [weights] 六个维度的权重加起来是 1；
      - [route] 的两个系数（各步平均分占多少、最弱一步占多少）加起来是 1，路线分才会落在 0–100 之间；
      - [evidence.levels] 里 E4、E3、E2、E1 的分数段（band）依次升高：证据越强，文献分所在的分数段越高。
    怎么读：三句断言各查一条。改 scoring.toml 里的权重时最容易碰到的就是第一条。
    """
    scoring = reference.scoring
    # 小数相加会有极小的误差（0.1 + 0.2 并不正好等于 0.3），所以不直接比相等，而是看差得够不够小
    assert abs(sum(scoring["weights"].values()) - 1.0) < 1e-9
    # pytest.approx(1.0) 是同样的意思：和 1.0 相差在很小的误差之内就算相等
    assert scoring["route"]["mean_coefficient"] + scoring["route"]["weakest_coefficient"] == pytest.approx(1.0)
    # 每个 band 是 [下限, 上限]。按 E4、E3、E2、E1 的顺序取出来，应当已经是从小到大排好的
    # （两个列表比大小时先比第一项，第一项相同再比第二项）。现在的值是 [0, 40]、[40, 70]、[70, 90]、[90, 100]
    bands = [scoring["evidence"]["levels"][level]["band"] for level in ("E4", "E3", "E2", "E1")]
    assert bands == sorted(bands)


def test_data_source_registry_blocks_unlicensed_sources(reference):
    """数据来源要先登记、并且登记为允许使用，才能导入；学术许可的来源和没登记的来源都不行。

    保证的行为：syntherisk/reference.py 的 source_allowed 查 data/data_sources.toml：来源登记过并且
    allowed = true 才返回 True。导入先例库和在售目录之前都要过这一关（syntherisk/loaders.py 的 _check_source），
    不允许的直接拒绝导入。
    怎么读：三个例子——允许的、登记了但不允许的、根本没登记的。
    """
    assert reference.source_allowed("uspto")
    assert not reference.source_allowed("reaxys_scifinder")  # 学术许可，禁止商用和机器学习
    assert not reference.source_allowed("某个没登记的来源")


def test_demo_routes_are_chemically_consistent(reference):
    """演示路线里每一步的反应物、产物都读得出来，而且每条路线的终点就是目标分子。

    保证的行为：data/demo_routes/ 下每个 JSON 文件是一个目标分子的几条演示路线（现在有布洛芬和阿司匹林），
    由演示引擎原样拿出来（syntherisk/modules/m03_retrosynthesis.py 的 DemoEngine），再由
    syntherisk/routes.py 的 build_skeleton 整理成后端自己的结构。build_skeleton 遇到下面几种路线会直接丢掉，
    不报错：有结构读不出来的、终点不是目标分子的、和前面某条用到的步骤完全相同的。
    这个测试要求一条都不丢，并且每一步都有正经的反应名。
    怎么读：对每个文件，按文件里写的目标分子搜一次，再整理；比较整理前后的路线条数，然后逐步看反应名。
    为什么重要：测试和没有接 AiZynthFinder 时的演示都靠这些文件。路线被丢掉时分析并不会失败，
    只是结果里少了一条路线（最多在提示里多一句话），不容易发现。
    """
    # 演示引擎启动时把这个文件夹下的 JSON 全部读进来
    engine = DemoEngine(DATA / "demo_routes")
    # sorted 让文件按名字的顺序处理，每次运行的先后一样
    for path in sorted((DATA / "demo_routes").glob("*.json")):
        target = json.loads(path.read_text(encoding="utf-8"))["target"]
        search = engine.search(SearchRequest(target_smiles=target["smiles"]))
        skeleton = build_skeleton(search, target["smiles"], reference)
        # 条数对不上时，把整理过程中的提示（skeleton["warnings"]，里面写着跳过了哪条、为什么）显示出来
        assert len(skeleton["routes"]) == len(search.routes), skeleton["warnings"]
        for reaction in skeleton["reactions"].values():
            # 这两个常量是认不出反应类型时用的通用名字：“官能团转化”和“片段连接”
            assert reaction["name"] not in (UNNAMED_CONVERSION, UNNAMED_COUPLING)  # 演示路线的每一步都要有正经名字
