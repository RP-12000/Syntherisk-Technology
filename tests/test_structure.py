"""M1 结构工具。

---- 测的是什么 ----

syntherisk/modules/m01_structure.py 里的函数（M1 里只做计算、不联网的那一半），外加两个建在它上面的小函数：
m07_conditions.name_reaction（按官能团变化给反应起名）和 m09_green.atom_economy（原子经济性）。

这些函数都是“给一段文字，算出一个结果”：不查数据库、不联网、不经过接口。所以这个文件里的测试不用任何夹具，
也没有替身，从 tests/conftest.py 只借了两个 SMILES 常量（IBUPROFEN、ASPIRIN）。每个测试都是直接调函数，再核对返回值。
文件里都把 m01_structure 叫作 chem（后端各处也是这个叫法），看到 chem.xxx 就是那个文件里的函数。

和界面、和别的模块的关系（接口只是把这里的函数包了一层；接口本身在 tests/test_app_service.py 里测）：
    A1 A2 A5  画结构、粘贴 SMILES、起始骨架   POST /api/structures/validate   chem.validate_structure
    A4        上传 MOL / SDF 文件             POST /api/structures/upload     chem.validate_structure
    C2        相似先例的结构差异              POST /api/structures/diff       chem.structure_diff
    M5 文献先例定证据等级（E1–E4）时怎么认“同一反应”“同类反应”“底物像不像”：
              chem.reaction_key、chem.reaction_center 和 chem.center_class、chem.difference_bits、chem.similarity
    M3 整理路线树时给没有名字的步骤起名：name_reaction；M9 绿色化学：atom_economy
    注意：chem.similarity、chem.reaction_center、chem.parse_reaction_smiles 这三个现在只有测试直接调
    （见它们各自说明里的“谁在用”）。后端用的是同一套算法的另外几个入口：比结构用 structure_bits 加
    tanimoto / tanimoto_many；拆专利反应、算它的反应中心用 parse_mapped_reaction；引擎给的步骤用 retro_reaction_center。

---- 读这个文件要先知道的几个词（更完整的说明在 m01_structure.py 的文件头）----

    SMILES      用一行文字写一个分子，比如乙醇是 CCO。小写的 c 是苯环这类芳香环上的碳；括号里是支链；
                “.” 两边是互不相连的两部分（盐）；@ / @@ 标手性碳的构型
    InChIKey    分子的编号，27 个字符。key14 是它的前 14 个字符，后端判断“是不是同一个物质”比的就是它
    原子映射    反应 SMILES 里原子后面的编号，比如 [CH3:1]。反应物和产物里编号相同的是同一个原子
    反应中心    按原子映射列出来的“这一步改了哪些键”，是一串文字，比如 F:C.acyl~N:1|L:C.acyl~Cl:1
    指纹        把分子或反应变成一串 0 和 1；Tanimoto 相似度（0–1）比两串有多像，1 是完全相同

---- 本文件里有什么（每个测试保证的行为）----

    解析与校验
        test_valid_smiles_reports_formula_weight_and_open_stereocenter
            合法的 SMILES 给出分子式、分子量、结构图；没写构型的手性碳得到一句提示
        test_invalid_structures_explain_what_is_wrong
            读不出来的结构不会让程序出错，而是 valid=False，并用中文说明哪里不对
        test_molblock_and_sdf_are_detected_automatically
            MOL、SDF 文本不用指明格式也认得出来；SDF 里有多个结构时只读第 1 个，并给一条提醒
        test_salts_and_inorganics_get_warnings_not_errors
            盐、不含碳的物质是合法的结构，只给提醒，不当成错误
    同一个物质、两个结构的比较
        test_same_molecule_written_differently_has_one_identity
            同一个分子换一种写法 key14 不变；立体异构体 key14 相同、完整的 InChIKey 不同
        test_targets_are_searched_in_their_standard_tautomer_with_stereo_kept
            交给逆合成引擎之前换成标准互变异构体写法，构型保留；换了会改变 key14 的写法不换
        test_structure_diff_marks_the_atoms_that_differ
            两个结构能找出公共部分，并标出各自多出来的原子
    反应：同一反应、同类反应（不靠原子映射的办法）
        test_reaction_key_ignores_counter_ions_reagents_and_atom_maps
            “同一反应”的编号不受反应物先后、抗衡离子的影响；带映射的反应 SMILES 能拆成反应物、试剂、产物
        test_reaction_class_fingerprint_groups_reactions_by_type_not_by_substrate
            反应类别指纹：同一类反应换了底物仍然完全相同，不同类的反应相似度低
    反应中心（按原子映射）
        test_reaction_center_lists_the_bonds_that_changed
            反应中心如实列出成键、断键、键级变化；没有映射或没有变化时是 None
        test_the_same_transformation_gets_the_same_class_whatever_the_substrate_or_halide
            同一种转化换底物、换卤素还是同类；换一种做法只算“相关反应”；成的键不同就不相干
        test_protecting_groups_that_come_off_differently_are_different_classes
            脱法不同的保护基（Boc / Cbz / 乙酰基，甲酯 / 叔丁酯 / 苄酯）各算一类，彼此是相关反应
        test_reaction_center_does_not_depend_on_how_the_data_happened_to_be_mapped
            同一个反应，数据里原子映射的标法不同（氧对反了、对称试剂两半同号、试剂标没标映射），结果一样
        test_methylation_is_the_same_class_as_other_alkylations_whoever_supplied_the_methyl
            甲基化和别的烷基化归成一类，不管数据里把甲基碳对到了哪个试剂上
    相似度、反应命名、原子经济性
        test_structure_similarity_separates_close_analogs_from_distant_ones
            结构相似度：很接近的同系物高于 0.7，换了取代基的落在中间，不相干的接近 0
        test_reaction_naming_and_atom_economy
            按官能团变化认得出常见的反应类型；原子经济性按分子量算，反应物不全时不给数
"""

from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m07_conditions import name_reaction
from syntherisk.modules.m09_green import atom_economy
from tests.conftest import ASPIRIN, IBUPROFEN

# 乙醇（CH3–CH2–OH）的 MOL 文件内容（V2000 格式），结构编辑器导出的就是这种文本。选它是因为它很小。
# 从上到下：
#   前 3 行      标题行、软件信息行、备注行。这里是空行、“  test”、空行；第一行可以是空的，但不能少
#   计数行       “  3  2 … V2000”：3 个原子、2 根键，最后是版本标记
#   3 行原子     每行是 x、y、z 坐标和元素符号：C、C、O（氢不用写）
#   2 行键       “  1  2  1” 是第 1、第 2 个原子之间一根单键；“  2  3  1” 同理
#   M  END       结束行
# chem.detect_format 看到 “M  END” 或 “V2000” 就认它是 MOL；下面的测试里在它后面拼上 “$$$$” 就成了 SDF。
ETHANOL_MOLBLOCK = """
  test

  3  2  0  0  0  0  0  0  0  0999 V2000
    0.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    1.5000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0
    2.2500    1.2990    0.0000 O   0  0  0  0  0  0  0  0  0  0  0  0
  1  2  1  0
  2  3  1  0
M  END
"""


def test_valid_smiles_reports_formula_weight_and_open_stereocenter():
    """合法的 SMILES：校验通过，并给出 R1“结构预览和校验”要显示的各项内容。

    保证的行为：chem.validate_structure（syntherisk/modules/m01_structure.py）对一个读得出来的结构，
    给出分子式、分子量、没写构型的立体中心的个数、关于立体化学的提示，以及一张结构图。
    界面上 A1 画结构、A2 粘贴 SMILES 之后看到的预览，就是接口 POST /api/structures/validate 把这个结果转成的 JSON。
    怎么读：直接拿布洛芬的 SMILES 调函数（不经过接口，没有夹具），逐项核对返回的 StructureInfo。
    为什么重要：这些内容原样显示在结构预览里，用户靠分子式和分子量确认自己输对了结构。
    布洛芬有一个手性碳，而这个 SMILES 没写它的构型：后端按外消旋处理，messages 里的那句话就是告诉用户这一点的，
    并提醒要单一构型的话补上构型（不写构型时，搜索认不出库存和在售清单里带构型的原料）。
    """
    info = chem.validate_structure(IBUPROFEN)
    assert info.valid
    # 布洛芬是 C13H18O2。照着 SMILES 数碳：异丁基 4 个、苯环 6 个、CH 和它上面的甲基 2 个、羧基 1 个，共 13 个
    assert info.formula == "C13H18O2"
    # 平均分子量（g/mol），保留两位小数
    assert info.mol_weight == 206.28
    # 连着羧基的那个碳上有四个不同的基团（氢、甲基、羧基、芳环），是手性碳；
    # SMILES 里没有用 @ / @@ 标它的构型，所以算“未指定”
    assert info.unspecified_stereocenters == 1
    assert info.messages == ["1 个立体中心未指定，按外消旋处理；要的是某一个立体异构体的话请补上构型，不然搜索认不出带构型的原料"]
    # 默认会带一张 320 × 220 的结构图（SVG 是一段 XML 文本）。RDKit 画出来的以 XML 声明 <?xml 开头；
    # 这里两种情况都接受：以 <?xml 开头，或者文本里有 <svg 标签
    assert info.svg.startswith("<?xml") or "<svg" in info.svg


def test_invalid_structures_explain_what_is_wrong():
    """读不出来的结构：不抛异常，valid 是 False，errors 里用中文说明原因。

    保证的行为：chem.validate_structure（里面是 parse_mol）把三种“不行”的输入区分开，各给各的说明：
        什么都没输入               “没有输入结构”
        写法本身不对（环没有闭合）   校验不通过
        写法对、化学上不合理         指出是第几个原子的成键数超了
    对应接口 POST /api/structures/validate（A1、A2）；POST /api/analyses 开始分析时也用它校验目标分子，
    不合格返回 422，响应里的 errors 就是这里的这些句子（docs/API.md）。
    怎么读：三个输入各调一次函数，看 valid 和 errors。
    为什么重要：errors 里的句子是直接显示给用户的。说不清哪里错了，用户只能自己去猜。
    """
    # 空字符串：解析的第一步就挡住了，错误列表里只有这一句
    assert chem.validate_structure("").errors == ["没有输入结构"]
    # C1CC：数字 1 表示“从这里开一个环”，后面却没有第二个 1 把环闭上，RDKit 读不出来
    assert not chem.validate_structure("C1CC").valid
    # CC(C)(C)(C)C：第 2 个碳连了五个碳。SMILES 的写法没有错，但碳最多成四根键，过不了化学校验
    pentavalent = chem.validate_structure("CC(C)(C)(C)C")
    assert not pentavalent.valid
    # 完整的句子是“第 2 个原子 C 的成键数超出了它的化合价”（m01_structure._problem_text 拼的），这里只找关键词
    assert "化合价" in pentavalent.errors[0]


def test_molblock_and_sdf_are_detected_automatically():
    """MOL 和 SDF 文本不用指明格式也认得出来；SDF 里有多个结构时只读第 1 个，并给一条提醒。

    保证的行为：chem.validate_structure 的格式参数默认是 "auto"，由 chem.detect_format 猜：
    有 “$$$$” 的是 SDF，有 “M  END” 或 V2000 / V3000 的是 MOL，其余当 SMILES。
    对应按钮 A1（结构编辑器导出的 MOL）和 A4 上传文件（POST /api/structures/upload，MOL 或 SDF）。
    怎么读：先把文件开头那段乙醇的 MOL 文本直接交给校验函数；再把它拼两遍、每遍后面加一行 “$$$$”，
    造出一个含两个结构的 SDF，再校验一次。
    为什么重要：SDF 文件可以装好几个结构，而一次分析只有一个目标分子。只读第 1 个可以，但必须说出来，
    不然用户不知道其余的没有被读进去。
    """
    info = chem.validate_structure(ETHANOL_MOLBLOCK)
    # 认成了 MOL；读出来的结构写成规范 SMILES 是 CCO（乙醇）
    assert info.valid and info.input_format == "mol" and info.smiles == "CCO"
    # SDF 就是一个接一个的 MOL 记录，每个记录后面跟一行 $$$$
    two_records = ETHANOL_MOLBLOCK + "$$$$\n" + ETHANOL_MOLBLOCK + "$$$$\n"
    info = chem.validate_structure(two_records)
    assert info.valid and info.input_format == "sdf"
    # 完整的提醒是“文件里有 2 个结构，只读入了第 1 个”。它放在 warnings（提醒）里，不在 errors（错误）里
    assert "只读入了第 1 个" in info.warnings[0]


def test_salts_and_inorganics_get_warnings_not_errors():
    """盐和不含碳的物质是合法的结构：照样通过校验，只在 warnings 里提醒。

    保证的行为：chem.validate_structure（里面是 describe_mol）对“有几个互不相连的部分”“没有碳原子”
    这两种情况只加提醒，valid 仍然是 True。
    怎么读：两个输入各校验一次。
        NO.Cl   盐酸羟胺。SMILES 里的 “.” 表示两边不相连：羟胺 NH2OH 和氯化氢 HCl 是两个部分
        [Mg]    金属镁，一个碳都没有
    为什么重要：实验室的试剂里有不少本身就是盐或无机物，内置试剂表（data/compounds.csv）里的盐酸羟胺、
    镁屑就是这两个结构。手动添加库存时给的 SMILES 也走这个校验；如果把它们判成错误，
    这类试剂按 SMILES 添加时会被接口拒绝（api/inventory.py 的 add_item 返回 422）。
    """
    salt = chem.validate_structure("NO.Cl")
    # components 是互不相连的部分的个数。排第一的提醒是“结构里有 2 个不相连的部分（盐或混合物）”
    assert salt.valid and salt.components == 2 and "不相连" in salt.warnings[0]
    # 完整的提醒是“结构里没有碳原子，逆合成搜索可能找不到路线”。先把几条提醒连成一句再找，就不用管它排第几
    assert "没有碳原子" in " ".join(chem.validate_structure("[Mg]").warnings)


def test_same_molecule_written_differently_has_one_identity():
    """同一个分子不管怎么写，key14 都一样；立体异构体共用 key14，完整的 InChIKey 才分得开。

    保证的行为：chem.key14_of 和 chem.mol_record 给出的“身份”只取决于分子本身，不取决于 SMILES 的写法。
    key14 是 InChIKey 的前 14 个字符，只和原子怎么连有关。后端里凡是要判断“是不是同一个物质”的地方比的都是它：
    库存匹配、在售目录、内置试剂表、管制清单、分析结果里的分子表（见 m01_structure.py 文件头）。
    怎么读：第一句拿布洛芬的两种写法比 key14；后两句拿丙氨酸的一对对映体比 key14 和完整的 InChIKey。
    为什么重要：用户画的、粘贴的、PubChem 给的、逆合成引擎给的 SMILES，写法各不相同。
    key14 要是跟着写法变，库存和在售目录里明明有的物质就会对不上。
    """
    # 两个 SMILES 都是布洛芬：一个从羧基写起，一个从异丁基写起（conftest.py 里的 IBUPROFEN）
    assert chem.key14_of("OC(=O)C(C)c1ccc(CC(C)C)cc1") == chem.key14_of(IBUPROFEN)
    # 立体异构体共用 key14（只看连接方式），完整的 InChIKey 不同
    # 例子是丙氨酸 CH3–CH(NH2)–COOH 的一对对映体：@ 和 @@ 是手性碳的两种构型，原子怎么连完全一样。
    # 注意变量名和实际构型是反的：按 RDKit 的判定，第一个（@）是 S 构型，第二个（@@）是 R 构型。
    # 断言对两者是对称的，结论不受影响
    r, s = chem.mol_record("C[C@H](N)C(=O)O"), chem.mol_record("C[C@@H](N)C(=O)O")
    assert r.key14 == s.key14 and r.inchikey != s.inchikey


def test_targets_are_searched_in_their_standard_tautomer_with_stereo_kept():
    """交给逆合成引擎的目标分子换成标准的互变异构体写法，立体构型一个不丢；换了会改变 key14 的写法不换。

    保证的行为：chem.standard_tautomer（流水线第 2 阶段 pipeline.AnalysisRunner._search 用它）。
    怎么读：
        亚胺醇写法的青霉素 G（不带构型、带三个手性中心的构型各一次）换成酰胺写法，带构型的那个三个构型都还在；
        2-羟基吡啶换成 2-吡啶酮；S-布洛芬本来就是标准写法，原样返回、构型还在；
        丙-1-烯-2-醇（丙酮的烯醇式）原样返回：InChI 把它和丙酮当成两个分子，换了 key14 就变了；读不出来的原样返回。
    为什么重要：反应模板是从专利反应里抽的，几乎都写成酰胺这类常见写法。实测 2026-10-22，青霉素 G 写成亚胺醇时
    搜不到“6-APA + 苯乙酰氯”这一步；而 RDKit 默认的规范化会抹掉手性碳的构型，带构型才买得到的 6-APA 又对不上库存。
    """
    iminol = "CC1(C)SC2C(N=C(O)Cc3ccccc3)C(=O)N2C1C(=O)O"
    assert chem.standard_tautomer(iminol) == "CC1(C)SC2C(NC(=O)Cc3ccccc3)C(=O)N2C1C(=O)O"
    with_stereo = "CC1(C)S[C@@H]2[C@H](N=C(O)Cc3ccccc3)C(=O)N2[C@H]1C(=O)O"
    amide = chem.standard_tautomer(with_stereo)
    assert amide == "CC1(C)S[C@@H]2[C@H](NC(=O)Cc3ccccc3)C(=O)N2[C@H]1C(=O)O"
    # 换写法不改变“是哪个分子”：key14 一样，完整的 InChIKey（带构型）也一样
    assert chem.mol_record(amide).inchikey == chem.mol_record(with_stereo).inchikey
    assert chem.standard_tautomer("Oc1ccccn1") == "O=c1cccc[nH]1"
    s_ibuprofen = "CC(C)Cc1ccc([C@H](C)C(=O)O)cc1"
    assert chem.standard_tautomer(s_ibuprofen) == s_ibuprofen
    assert chem.standard_tautomer("C=C(C)O") == "C=C(C)O"
    assert chem.standard_tautomer("这不是结构") == "这不是结构"


def test_structure_diff_marks_the_atoms_that_differ():
    """比较两个结构：找出最大的公共部分，并标出各自多出来的原子。

    保证的行为：chem.structure_diff 返回相似度、公共部分有几个原子，以及 query、reference 各自独有的原子编号。
    对应按钮 C2“相似先例”：证据卡把这一步的产物和先例的产物放在一起，把不一样的地方高亮出来
    （POST /api/structures/diff；先例列表接口 GET …/steps/{n}/precedents 给每条相似先例附带的也是它）。
    怎么读：query 是 4-异丁基苯乙酮，reference 是苯乙酮：前者比后者多一个异丁基，别的都一样。
    传 with_svg=False 是不要那两张高亮图，只看数字。最后拿布洛芬和它自己比。
    为什么重要：界面上高亮的就是 query_only_atoms、reference_only_atoms 这两组原子。标错了，
    用户看到的“先例和这一步差在哪”就是错的。
    """
    diff = chem.structure_diff("CC(C)Cc1ccc(cc1)C(C)=O", "CC(=O)c1ccccc1", with_svg=False)
    # 相似度是结构指纹的 Tanimoto。两个分子不一样，但有共同的骨架，所以严格落在 0 和 1 之间（实际是 0.647）
    assert 0 < diff["similarity"] < 1
    # 苯乙酮 C8H8O 有 9 个重原子（8 个碳、1 个氧；氢不算），整个都在公共部分里
    assert diff["common_atoms"] == 9  # 苯乙酮骨架
    # 所以 reference（苯乙酮）没有独有的原子
    assert diff["reference_only_atoms"] == []
    # query 里不在公共部分的是异丁基的 4 个碳（按 query 自己的原子顺序，编号是 0–3）
    assert len(diff["query_only_atoms"]) == 4  # 多出来的异丁基
    # identical 看的是两边的 key14 是否相同
    assert chem.structure_diff(IBUPROFEN, IBUPROFEN, with_svg=False)["identical"]


def test_reaction_key_ignores_counter_ions_reagents_and_atom_maps():
    """“同一反应”的编号只看反应物和产物是什么；带原子映射的反应 SMILES 能拆成反应物、试剂、产物。

    保证的行为（都在 m01_structure.py）：
        chem.reaction_key           同一批反应物得到同一个产物，编号就相同：反应物写的先后、带不带抗衡离子
                                    （盐酸羟胺和羟胺）都不影响；少了一个反应物，编号就不同
        chem.parse_reaction_smiles  把专利数据的 “反应物>试剂>产物” 拆成三组规范 SMILES，去掉原子映射；
                                    写在反应物一侧、却没有任何原子进入产物的东西，挪到试剂那一组
    用在哪：M5 的证据等级 E1“文献直接报道”，就是拿这一步的 reaction_key 到先例库里找编号相同的
    （m05_precedents.PrecedentSearch._exact_rows）。先例库的每一行在导入时用同一套办法拆反应、算编号。
    怎么读：前半段用“醛和羟胺成肟”（演示数据里布洛芬 Boots 路线的一步）；后半段用一条手写的、带映射的酯化反应。
    为什么重要：同一个反应在不同的数据里写法不同：有的写盐酸羟胺，有的写羟胺；专利数据还会把催化剂写在反应物一侧。
    编号要是把这些差别算进去，明明有文献直接报道的步骤就会查不到，证据等级被定低。
    """
    # 醛中间体和它的肟（C=O 变成 C=N–OH）
    aldehyde, oxime = "CC(C)Cc1ccc(cc1)C(C)C=O", "CC(C)Cc1ccc(cc1)C(C)C=NO"
    # NO.Cl 是盐酸羟胺，NO 是羟胺：算编号时盐只取最大的那个片段，反应物的先后也不计，所以两边相同
    assert chem.reaction_key([aldehyde, "NO.Cl"], oxime) == chem.reaction_key(["NO", aldehyde], oxime)
    # 少写一个反应物（只有醛）就是另一个编号了
    assert chem.reaction_key([aldehyde, "NO"], oxime) != chem.reaction_key([aldehyde], oxime)

    # 乙酸和甲醇成乙酸甲酯。带编号的原子在产物里都找得到；硫酸 O=S(=O)(O)O 写在反应物一侧，但一个编号都没有
    mapped = "[CH3:1][C:2](=[O:3])[OH:4].[CH3:5][OH:6].O=S(=O)(O)O>>[CH3:1][C:2](=[O:3])[O:6][CH3:5]"
    reactants, agents, products = chem.parse_reaction_smiles(mapped)
    # 三组都是去掉映射号以后的规范 SMILES。先排序再比，免得依赖反应物的先后
    assert sorted(reactants) == ["CC(=O)O", "CO"]
    assert agents == ["O=S(=O)(O)O"]  # 没有原子进入产物，算试剂
    assert products == ["COC(C)=O"]
    # 不是 “反应物>试剂>产物” 的格式（里面没有两个 >）：返回 None，不抛异常
    assert chem.parse_reaction_smiles("not a reaction") is None


def test_reaction_class_fingerprint_groups_reactions_by_type_not_by_substrate():
    """反应类别指纹：同一类反应换了底物，指纹完全相同；相关但不同类的反应，相似度明显低。

    保证的行为：chem.difference_bits（不需要原子映射的那种粗办法）只记“从反应物到产物，哪些类型的原子少了、
    哪些多了”，没变的部分互相抵消。chem.tanimoto 比两条指纹有多像（0–1）。
    用在哪：M5 给没有原子映射的步骤（比如内置演示路线）找“同类反应”的先例时，先用它在整个先例库里粗筛
    （m05_precedents.PrecedentSearch._similar_rows）。带映射的步骤不用它，用下面测的反应中心。
    怎么读：以“苯乙酮还原成 1-苯乙醇”为基准。先比两个同样是酮还原、只是底物不同的反应，应当是 1.0；
    再比四个沾边、但不是酮还原的反应，应当都低于 0.6。
    为什么重要：这把尺子太松，不相干的专利会被当成这一步的先例；太紧，换个取代基就认不出先例。
    两种错都会让证据等级定错。
    """
    def reaction(reactants, product):
        """一步反应的反应类别指纹。只是为了少写几个字：difference_bits 的第二个参数要的是产物的列表。"""
        return chem.difference_bits(reactants, [product])

    # 基准：苯乙酮 CC(=O)c1ccccc1 还原成 1-苯乙醇 CC(O)c1ccccc1。
    # 少了“羰基碳、羰基氧”，多了“连羟基的 CH、羟基氧”，这条指纹里正好有 4 位是 1
    aryl_ketone = reaction(["CC(=O)c1ccccc1"], "CC(O)c1ccccc1")
    # 换底物（换取代基、换成环酮）还是同一类反应
    # 第一个是 4'-甲基苯丙酮的还原，第二个是环己酮还原成环己醇：增减的原子类型和基准一模一样，所以是 1.0
    assert chem.tanimoto(aryl_ketone, reaction(["CCC(=O)c1ccc(C)cc1"], "CCC(O)c1ccc(C)cc1")) == 1.0
    assert chem.tanimoto(aryl_ketone, reaction(["O=C1CCCCC1"], "OC1CCCCC1")) == 1.0
    # 相关但不同类的反应，明显低于粗筛“同类反应”的门槛
    # （data/scoring.toml 的 same_class_threshold，现在是 0.65；这里写死的 0.6 是它最初的取值，比现在的更严）。
    # 四个反应实际算出来依次是 0.33、0.33、0.5、0.2，两条线都够不着
    for reactants, product in (
        (["O=Cc1ccccc1"], "OCc1ccccc1"),  # 醛还原
        (["CC(O)c1ccccc1"], "CC(=O)c1ccccc1"),  # 反过来：醇氧化
        (["CC(=O)c1ccccc1", "CN"], "CNC(C)c1ccccc1"),  # 还原胺化
        (["CC(=O)O", "CO"], "COC(C)=O"),  # 酯化
    ):
        assert chem.tanimoto(aryl_ketone, reaction(reactants, product)) < 0.6


# ---------------------------------------------------------------- 反应中心（按原子映射）
# 下面的反应都是手工标的映射，写法照着 USPTO 专利数据和 AiZynthFinder 输出里实际见到的样子。
#
# 怎么读这些反应 SMILES：
#   格式是 “反应物>试剂>产物”，每一组里的分子用 “.” 隔开。[CH3:1] 这样带冒号编号的是原子映射：
#   反应物和产物里编号相同的是同一个原子。这些例子里，反应物一侧没有编号的原子（比如 Cl[C:9]… 里的 Cl）
#   都没有进入产物，它们就是这一步掉下去的部分。
# 怎么读算出来的反应中心（完整的说明在 m01_structure.py 的 reaction_center 和 _atom_type）：
#   每一处变化写成 “类别:两头的原子类型:键”，几处变化用 | 连起来；同样的变化有两处时前面写 “2*”。
#   类别      F 两个原有的原子之间成了一根新键      B 原有的一根键断了      C 键级变了（键写成 旧>新）
#             L 某个原子上掉下去一个基团            N 某个原子上接上了一个新来的原子
#   原子类型  C.acyl 酰基碳（羧酸、酯、酰胺、酰氯里带 =O 的那个碳）   C=O 醛、酮的羰基碳   c 芳环碳   C 饱和碳
#             N 胺的氮   N.ar 苯胺的氮   N.am 酰胺的氮   OH 羟基氧   O 醚、酯里的氧   O= 双键氧
#             后缀 .ar 是“连着芳环”，.acyl 是“连着羰基”，.sulf 是“连着磺酰基”；卤素、硼、锡、镁直接写元素符号
#   键        1 单键，2 双键
#   例：F:C.acyl~N:1|L:C.acyl~Cl:1 读作“酰基碳和氮之间成了一根单键，酰基碳上掉了一个氯”。
# 两步反应是不是同一种，不直接比这串文字，而是先用 chem.center_class 归并成两把钥匙再比：
#   同类（下面的 _class）  芳环上的和烷基上的羟基、氨基不分；掉下去的氯、溴、碘、磺酸酯统一写成 X。
#                          两步的这把钥匙相同，才算“同类反应”
#   核心（下面的 _core）   只看成了哪些键、接上了什么、键级怎么变，不看掉下去的是什么。
#                          同类钥匙不同、只有核心相同的是“相关反应”

# 下面几个片段是拼反应用的积木（Python 的 f"...{PH}" 会把花括号里的变量原样嵌进字符串）。
# PH：带映射号（20–25）的苯基，20 号碳是和别的部分相连的那个
PH = "[c:20]1[cH:21][cH:22][cH:23][cH:24][cH:25]1"
# BENZYLAMINE：苄胺 PhCH2NH2，氮是 1 号，亚甲基碳是 2 号。下面成酰胺、上保护基、脱保护基的反应都拿它当底物
BENZYLAMINE = "[NH2:1][CH2:2]" + PH
# ANISOLE：苯甲醚（甲氧基苯）。BROMOANISOLE：它在甲氧基对位（6 号碳）接上一个 7 号原子溴以后的产物
ANISOLE, BROMOANISOLE = "[CH3:1][O:2][c:3]1[cH:4][cH:5][cH:6][cH:8][cH:9]1", "[CH3:1][O:2][c:3]1[cH:4][cH:5][c:6]([Br:7])[cH:8][cH:9]1"
# ARYL：对甲氧基苯基（1 号碳是连接点）。PHENYL：苯基（9 号碳是连接点）。
# BIARYL：两者连起来的 4-甲氧基联苯，是下面三个偶联反应共同的产物
ARYL = "[c:1]1[cH:2][cH:3][c:4]([O:5][CH3:6])[cH:7][cH:8]1"
BIARYL = "[CH3:6][O:5][c:4]1[cH:3][cH:2][c:1](-[c:9]2[cH:10][cH:11][cH:12][cH:13][cH:14]2)[cH:8][cH:7]1"
PHENYL = "[c:9]1[cH:10][cH:11][cH:12][cH:13][cH:14]1"
# CENTERS：名字 → 一条手写的、带映射的反应。下面的测试都按名字来取（见 _center、_class、_core）。
# 字典里每一组前面的注释列出：名字、它是什么反应，“→” 后面是它算出来的反应中心。
CENTERS = {
    # ---- 成酰胺。前四条成的都是“酰基碳–氮”键，差别在掉下去的是什么；第五条成的是另一根键
    #   酰氯成酰胺          苄胺 + 乙酰氯 → N-苄基乙酰胺                      → F:C.acyl~N:1|L:C.acyl~Cl:1
    #   酰溴和苯胺成酰胺    苯胺 + 丙酰溴 → N-苯基丙酰胺（底物、卤素都换了）  → F:C.acyl~N.ar:1|L:C.acyl~Br:1
    #   羧酸成酰胺          苄胺 + 乙酸 → 同一个酰胺，掉下去的是羟基          → F:C.acyl~N:1|L:C.acyl~OH:1
    #   羧酸成酰胺（两个氧对反了）   同上，只是 10 号标在了乙酸的羟基氧上，到了产物里却是羰基氧
    #   酰胺 N-芳基化       乙酰胺 + 对溴苯酚 → 对乙酰氨基酚：成的是“酰胺氮–芳环碳”键  → F:N.am~c:1|L:c~Br:1
    "酰氯成酰胺": f"{BENZYLAMINE}.Cl[C:9](=[O:10])[CH3:11]>>[O:10]=[C:9]([CH3:11])[NH:1][CH2:2]{PH}",
    "酰溴和苯胺成酰胺": "[NH2:1][c:3]1[cH:4][cH:5][cH:6][cH:7][cH:8]1.Br[C:9](=[O:10])[CH2:11][CH3:12]"
                ">>[O:10]=[C:9]([CH2:11][CH3:12])[NH:1][c:3]1[cH:4][cH:5][cH:6][cH:7][cH:8]1",
    "羧酸成酰胺": f"{BENZYLAMINE}.O[C:9](=[O:10])[CH3:11]>>[O:10]=[C:9]([CH3:11])[NH:1][CH2:2]{PH}",
    "羧酸成酰胺（两个氧对反了）": f"{BENZYLAMINE}.[OH:10][C:9](=O)[CH3:11]>>[O:10]=[C:9]([CH3:11])[NH:1][CH2:2]{PH}",
    "酰胺 N-芳基化": "[CH3:1][C:2](=[O:3])[NH2:4].Br[c:5]1[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1"
                ">>[CH3:1][C:2](=[O:3])[NH:4][c:5]1[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1",
    # ---- 酮还原：苯乙酮 → 1-苯乙醇，硼氢化钠写在试剂栏。没有成键断键，只是 C=O 变成了 C–O  → C:C=O~O=:2>1
    "酮还原": f"[CH3:1][C:2](=[O:3]){PH}>[BH4-].[Na+]>[CH3:1][CH:2]([OH:3]){PH}",
    # ---- 三种芳基–芳基偶联：产物都是 4-甲氧基联苯，成的都是两个芳环碳之间的键
    #   Suzuki（溴代物和硼酸）    对溴苯甲醚 + 苯硼酸                              → F:c~c:1|L:c~B:1|L:c~Br:1
    #   Suzuki（三氟甲磺酸酯和硼酸频哪醇酯）   离去基团和硼试剂的形式都换了        → F:c~c:1|L:c~B:1|L:c~O.sulf:1
    #   Stille                    对溴苯甲醚 + 三丁基苯基锡：掉下去的是锡，不是硼  → F:c~c:1|L:c~Br:1|L:c~Sn:1
    "Suzuki（溴代物和硼酸）": f"Br{ARYL}.OB(O){PHENYL}>>{BIARYL}",
    "Suzuki（三氟甲磺酸酯和硼酸频哪醇酯）": f"FC(F)(F)S(=O)(=O)O{ARYL}.CC1(C)OB(OC1(C)C){PHENYL}>>{BIARYL}",
    "Stille": f"Br{ARYL}.CCCC[Sn](CCCC)(CCCC){PHENYL}>>{BIARYL}",
    # ---- 酯变回羧酸：都是酯氧上掉了一个烷基，差别在掉的是哪种烷基。底物是 2-苯基丙酸的酯
    #   甲酯水解        掉甲基（普通烷基，写成 C）   → L:O.acyl~C:1
    #   乙酯水解（两个氧对反了）   掉乙基；产物里 4 号、11 号两个氧的位置和反应物里是对调的  → L:O.acyl~C:1
    #   叔丁酯脱除      掉叔丁基（C.t）              → L:O.acyl~C.t:1
    #   苄酯氢解        掉苄基（C.bn）               → L:O.acyl~C.bn:1
    "甲酯水解": f"[CH3:1][CH:2]([C:3](=[O:4])[O:11]C){PH}>>[CH3:1][CH:2]([C:3](=[O:4])[OH:11]){PH}",
    "乙酯水解（两个氧对反了）": f"[CH3:1][CH:2]([C:3](=[O:4])[O:11]CC){PH}>>[CH3:1][CH:2]([C:3](=[O:11])[OH:4]){PH}",
    "叔丁酯脱除": f"[CH3:1][CH:2]([C:3](=[O:4])[O:11]C(C)(C)C){PH}>>[CH3:1][CH:2]([C:3](=[O:4])[OH:11]){PH}",
    "苄酯氢解": f"[CH3:1][CH:2]([C:3](=[O:4])[O:11]Cc1ccccc1){PH}>>[CH3:1][CH:2]([C:3](=[O:4])[OH:11]){PH}",
    # ---- 还原
    #   酯还原成醇      乙酯 → 伯醇：羰基的双键变单键，乙氧基整个掉下去  → C:C.acyl~O=:2>1|L:C.acyl~O:1
    #   酯还原成醇（留下的标成了烷氧基的氧）
    #                   同一个反应，只是 4 号标在了乙氧基的氧上：照原样读是“羰基氧没了，烷氧基的氧留下来”
    #   酰胺还原成胺    N-甲基酰胺 → 胺：氮留下，羰基氧整个掉了          → L:C.acyl~O:2
    "酯还原成醇": f"[CH3:1][CH:2]([C:3](=[O:4])OCC){PH}>>[CH3:1][CH:2]([CH2:3][OH:4]){PH}",
    "酯还原成醇（留下的标成了烷氧基的氧）": f"[CH3:1][CH:2]([C:3](=O)[O:4]CC){PH}>>[CH3:1][CH:2]([CH2:3][OH:4]){PH}",
    "酰胺还原成胺": f"[CH3:1][CH:2]([C:3](=O)[NH:4][CH3:5]){PH}>>[CH3:1][CH:2]([CH2:3][NH:4][CH3:5]){PH}",
    # ---- 氮上脱保护：产物都是苄胺，掉下去的保护基不同
    #   脱 Boc        掉叔丁氧羰基  → L:N.am~Boc:1
    #   脱 Cbz        掉苄氧羰基    → L:N.am~Cbz:1
    #   乙酰胺水解    掉乙酰基      → L:N.am~C=O:1
    "脱 Boc": f"CC(C)(C)OC(=O)[NH:1][CH2:2]{PH}>>{BENZYLAMINE}",
    "脱 Cbz": f"c1ccccc1COC(=O)[NH:1][CH2:2]{PH}>>{BENZYLAMINE}",
    "乙酰胺水解": f"CC(=O)[NH:1][CH2:2]{PH}>>{BENZYLAMINE}",
    # ---- 上 Boc：苄胺 + Boc2O（二碳酸二叔丁酯）→ N-Boc 苄胺。氮接到羰基碳上，另一半碳酸酯掉下去。
    #   两种标法都应当得到 F:C.acyl~N:1|L:C.acyl~O.acyl:1。第二条是正常标法：只有进入产物的那一半带映射号
    # Boc2O 的两半标着同一套映射号：专利数据里对称试剂常这样
    "上 Boc（两半同号）": f"{BENZYLAMINE}.[C:17](O[C:17]([O:19][C:30]([CH3:33])([CH3:32])[CH3:31])=[O:18])"
                   f"([O:19][C:30]([CH3:33])([CH3:32])[CH3:31])=[O:18]>>[C:30]([O:19][C:17]([NH:1][CH2:2]{PH})=[O:18])([CH3:33])([CH3:32])[CH3:31]",
    "上 Boc": f"{BENZYLAMINE}.CC(C)(C)OC(=O)O[C:17]([O:19][C:30]([CH3:33])([CH3:32])[CH3:31])=[O:18]"
            f">>[C:30]([O:19][C:17]([NH:1][CH2:2]{PH})=[O:18])([CH3:33])([CH3:32])[CH3:31]",
    # ---- 成酯的三种写法，产物都是苯甲酸乙酯
    #   酸和醇成酯（酸的羟基掉了）   乙醇的氧（3 号）进了产物，苯甲酸那个没有编号的羟基掉下去
    #                                → F:C.acyl~OH:1|L:C.acyl~OH:1
    #   酸和醇成酯（醇的羟基掉了）   反过来标：苯甲酸的羟基氧（3 号）进了产物，乙醇的羟基掉下去
    #                                → F:C~OH.acyl:1|L:C~OH:1
    #   酸和碘乙烷成酯               苯甲酸 + 碘乙烷：羧酸的氧真的去取代了碘  → F:C~OH.acyl:1|L:C~I:1
    "酸和醇成酯（酸的羟基掉了）": f"[CH3:1][CH2:2][OH:3].O[C:4](=[O:5]){PH}>>[CH3:1][CH2:2][O:3][C:4](=[O:5]){PH}",
    "酸和醇成酯（醇的羟基掉了）": f"[CH3:1][CH2:2]O.[OH:3][C:4](=[O:5]){PH}>>[CH3:1][CH2:2][O:3][C:4](=[O:5]){PH}",
    "酸和碘乙烷成酯": f"[CH3:1][CH2:2]I.[OH:3][C:4](=[O:5]){PH}>>[CH3:1][CH2:2][O:3][C:4](=[O:5]){PH}",
    # ---- 芳环卤代：苯甲醚的对位接上一个卤素原子。三种溴代的写法都应当得到 N:c~Br:1，氯代是 N:c~Cl:1
    #   依次是：溴素 Br2（其中一个溴标了 7 号）；NBS（N-溴代丁二酰亚胺）给出溴；
    #           溴素写在试剂栏、产物上的溴没有编号；氯气 Cl2
    "溴代（溴素，溴标了映射）": f"{ANISOLE}.[Br:7]Br>>{BROMOANISOLE}",
    "溴代（NBS，溴标了映射）": f"{ANISOLE}.[Br:7]N1C(=O)CCC1=O>>{BROMOANISOLE}",
    "溴代（溴没标映射，溴素写在试剂里）": f"{ANISOLE}>BrBr>[CH3:1][O:2][c:3]1[cH:4][cH:5][c:6](Br)[cH:8][cH:9]1",
    "氯代": f"{ANISOLE}.[Cl:7]Cl>>[CH3:1][O:2][c:3]1[cH:4][cH:5][c:6]([Cl:7])[cH:8][cH:9]1",
    # ---- 烷基化
    #   酚的甲基化（碘甲烷）   苯酚 + 碘甲烷 → 苯甲醚，甲基碳是 7 号                 → N:OH.ar~C:1
    #   酚的甲基化（甲基碳对到了碳酸根上）   同一个反应的错标写法（见下面那行注释）  → N:OH.ar~C:1
    #   酚的乙基化（碘乙烷）   苯酚 + 碘乙烷 → 苯乙醚。乙基的两个碳都进了产物，记成“成键 + 掉碘”
    #                          → F:C~OH.ar:1|L:C~I:1
    #   胺的甲基化             苄胺 + 碘甲烷 → N-甲基苄胺                            → N:N~C:1
    "酚的甲基化（碘甲烷）": f"[CH3:7]I.[OH:1]{PH}>>[CH3:7][O:1]{PH}",
    # 专利数据里常见的错标：碘甲烷写在试剂里，甲基碳被对到了碳酸根上
    "酚的甲基化（甲基碳对到了碳酸根上）": f"[OH:1]{PH}.O=[C:7]([O-])[O-]>CI.[K+].[K+]>[CH3:7][O:1]{PH}",
    "酚的乙基化（碘乙烷）": f"[CH3:8][CH2:7]I.[OH:1]{PH}>>[CH3:8][CH2:7][O:1]{PH}",
    "胺的甲基化": f"[CH3:7]I.{BENZYLAMINE}>>[CH3:7][NH:1][CH2:2]{PH}",
    # ---- 格氏试剂对苯甲醛的加成：羰基的双键变单键，羰基碳上多一根 C–C 键
    #   醛和甲基格氏试剂   甲基溴化镁：新来的只有一个碳      → C:C=O~O=:2>1|N:C=O~C:1
    #   醛和乙基格氏试剂   乙基溴化镁：记成“成键 + 掉镁”     → C:C=O~O=:2>1|F:C~C=O:1|L:C~Mg:1
    "醛和甲基格氏试剂": f"[CH3:7][Mg]Br.[O:1]=[CH:2]{PH}>>[CH3:7][CH:2]([OH:1]){PH}",
    "醛和乙基格氏试剂": f"[CH3:8][CH2:7][Mg]Br.[O:1]=[CH:2]{PH}>>[CH3:8][CH2:7][CH:2]([OH:1]){PH}",
    # α-酮酸氧化脱羧成酸。引擎的写法：新羟基的氧算是从掉下去的羧基上来的；专利数据的写法：算是氢氧根给的
    # 两种写法都应当得到 L:C=O~CO2H:1|N:C=O~OH:1：羰基碳上掉了一个羧基，又接上一个新来的羟基
    "酮酸脱羧（氧来自掉下去的羧基）": f"[CH3:1][CH:2]([C:3](=[O:4])C(=O)[OH:5]){PH}>>[CH3:1][CH:2]([C:3](=[O:4])[OH:5]){PH}",
    "酮酸脱羧（氧来自氢氧根）": f"[CH3:1][CH:2]([C:3](=[O:4])C(=O)O){PH}.[OH-:5]>OO>[CH3:1][CH:2]([C:3](=[O:4])[OH:5]){PH}",
}


def _center(name: str) -> str:
    """按名字取 CENTERS 里的一条反应，算它的反应中心：chem.reaction_center 给出的原样文字。"""
    return chem.reaction_center(CENTERS[name])


def _class(name: str) -> str:
    """这条反应的“同类”钥匙：chem.center_class 返回的两项里的第一项。

    两步反应的这把钥匙相同，M5 才把其中一步当作另一步的同类先例。
    """
    return chem.center_class(_center(name))[0]


def _core(name: str) -> str:
    """这条反应的“核心”钥匙：chem.center_class 返回的两项里的第二项。

    同类钥匙不同、核心钥匙相同的两步，M5 只当作“相关反应”（做法不同的旁证）。
    """
    return chem.center_class(_center(name))[1]


def test_reaction_center_lists_the_bonds_that_changed():
    """反应中心如实写出这一步改了哪些键；算不出来的时候给 None，不硬凑。

    保证的行为（m01_structure.py）：
        chem.reaction_center         输入是专利数据的写法 “反应物>试剂>产物”
        chem.retro_reaction_center   输入是逆合成引擎的写法 “产物>>反应物”（AiZynthFinder 的 mapped_reaction_smiles）
        两个入口算的是同一种反应中心：同一个反应正着写、反着写，结果应当一样。
        没有原子映射，或者对照下来没有任何键发生变化（比如成盐）时返回 None。
    用在哪：导入先例库时每条专利反应算一次，存进 reaction_precedents 表的 center 列；
    引擎给的每一步在整理路线树时算一次（routes._Builder._reaction），证据卡的 center 字段原样给前端。
    怎么读：前四句各取 CENTERS 里的一条反应，核对算出来的文字；中间一段现写一条引擎写法的反应；
    最后三句是算不出来的情况。
    为什么重要：M5 靠这串文字判断先例和这一步是不是同一种反应。写错一处，要么把不同的反应当成先例，
    要么认不出真正的先例。
    """
    assert _center("酰氯成酰胺") == "F:C.acyl~N:1|L:C.acyl~Cl:1"  # 成了酰基碳–氮键，酰基碳上的氯掉了
    assert _center("酮还原") == "C:C=O~O=:2>1"  # 没有成键断键，羰基的双键变成了单键
    # 氨基甲酸酯的氮（按酰胺氮 N.am 算）上掉下去一个 Boc，别的键都没有变
    assert _center("脱 Boc") == "L:N.am~Boc:1"
    assert _center("溴代（NBS，溴标了映射）") == "N:c~Br:1"  # 试剂给出的那一个溴原子，按“新接上来的”算
    # 逆合成引擎给的是“产物>>反应物”，原子全都带映射；算出来的和正着写的一样
    # 这里写的是“对氨基苯酚 + 乙酸酐 → 对乙酰氨基酚”：氮（4 号）接到酰基碳（2 号）上，
    # 乙酸酐的另一半（12–15 号）没有进产物，从酰基碳上掉下去。
    # 原子类型按它在反应物里的样子取，所以写的是苯胺的氮 N.ar，不是产物里的酰胺氮
    retro = ("[CH3:1][C:2](=[O:3])[NH:4][c:5]1[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1>>"
             "[CH3:1][C:2](=[O:3])[O:12][C:13]([CH3:14])=[O:15].[NH2:4][c:5]1[cH:6][cH:7][c:8]([OH:9])[cH:10][cH:11]1")
    assert chem.retro_reaction_center(retro) == "F:C.acyl~N.ar:1|L:C.acyl~O.acyl:1"
    # 没有映射，或者什么键都没变（成盐），算不出来
    # 第一句：乙酸 + 甲醇成酯，一个映射号都没有。第二句：苄胺加盐酸成铵盐，氮只是多了一个氢和一个正电荷。
    # 第三句：根本不是反应 SMILES，parse_mapped_reaction（reaction_center 里面调的就是它）直接返回 None
    assert chem.reaction_center("CC(=O)O.CO>>COC(C)=O") is None
    assert chem.reaction_center(f"{BENZYLAMINE}.Cl>>[NH3+:1][CH2:2]{PH}") is None
    assert chem.parse_mapped_reaction("not a reaction") is None


def test_the_same_transformation_gets_the_same_class_whatever_the_substrate_or_halide():
    """同一种转化，换底物、换卤素仍然是“同类反应”；换一种做法只算“相关反应”；成的键不同就不相干。

    保证的行为：chem.center_class（m01_structure.py）把反应中心归并成 (同类, 核心) 两把钥匙。
    M5（m05_precedents.PrecedentSearch._class_rows）照这两把钥匙把先例分成三等：
        同类钥匙相同          同类反应：可以把这一步定到 E2“高相似先例”，产率和试剂也借它的
        同类不同、核心相同    相关反应：只当很弱的旁证，最多支持 E3 的最低一段
        两把都不同            不相干，不列出来
    归并的规则对应 README“几处和你预想的可能不一样的地方”里那张“算同一类 / 不算同一类”的表。
    怎么读：每一句断言拿 CENTERS 里的两三条反应，比它们的 _class 或 _core。
    写法上 a == b == c 是连着的两个比较（a == b 并且 b == c），a == b != c 同理。
    为什么重要：规则太严，用苄基溴的先例就支持不了用苄基氯的步骤；太松，就会出现 README 记的那种错判：
    把“乙酰胺 + 对溴苯酚 → 对乙酰氨基酚”（酰胺的 N-芳基化）当成苯胺酰化的同类。
    """
    # 苄胺 + 乙酰氯、苯胺 + 丙酰溴：苯胺的氮 N.ar 并入 N，氯和溴都写成 X，所以同类钥匙一样
    assert _class("酰氯成酰胺") == _class("酰溴和苯胺成酰胺") == "F:C.acyl~N:1|L:C.acyl~X:1"
    # 两种 Suzuki 偶联：溴和三氟甲磺酸酯（O.sulf）都算离去基团 X；硼酸和硼酸频哪醇酯掉下去的都是硼
    assert _class("Suzuki（溴代物和硼酸）") == _class("Suzuki（三氟甲磺酸酯和硼酸频哪醇酯）")
    # 换一种做法（先例用酰氯，这一步用羧酸；Suzuki 换成 Stille）：不是同类，但成的键一样，算相关反应
    # 酰氯掉的是 X，羧酸掉的是 OH，同类钥匙不同；核心里都只剩“酰基碳–氮成键”这一项。
    # Stille 掉的是锡，Suzuki 掉的是硼；核心都是 F:c~c:1（两个芳环碳成键）
    assert _class("酰氯成酰胺") != _class("羧酸成酰胺") and _core("酰氯成酰胺") == _core("羧酸成酰胺") == "F:C.acyl~N:1"
    assert _class("Stille") != _class("Suzuki（溴代物和硼酸）") and _core("Stille") == _core("Suzuki（溴代物和硼酸）")
    # 产物都是酰胺，成的键却不是同一根：既不同类也不相关
    # N-芳基化成的是“酰胺氮–芳环碳”键，羧酸成酰胺成的是“酰基碳–氮”键
    assert _core("酰胺 N-芳基化") == "F:N.am~c:1" != _core("羧酸成酰胺")
    # 接上去的是溴还是氯不归并：溴代和氯代不是同类
    assert _class("溴代（溴素，溴标了映射）") != _class("氯代")


def test_protecting_groups_that_come_off_differently_are_different_classes():
    """脱法不同的保护基各算一类：脱 Boc 的先例不能当脱 Cbz 的依据；它们之间只是“相关反应”。

    保证的行为：算反应中心时，掉下去的基团分得比“是什么原子”更细（m01_structure._outside_type、_alkyl_kind）：
        氮上掉下去的羰基     Boc（叔丁氧羰基）、Cbz（苄氧羰基）、普通的酰基 C=O 各是各的
        酯氧上掉下去的烷基   普通烷基 C（甲基、乙基不分）、叔丁基 C.t、苄基 C.bn 各是各的
    而 chem.center_class 给的“核心”只记“从哪一类原子上脱掉了东西”，所以同一类原子上的脱保护互为相关反应。
    怎么读：前两句核对六条反应各自的同类钥匙；中间两句核对它们的核心两两相同；最后一句是两种还原。
    为什么重要：这几种保护基的脱法完全不同（README 的表里把它们列在“不算同一类”一栏）。
    同类先例的产率和试剂会被借给这一步；借错了先例，证据等级、产率、试剂就都不对。
    """
    assert _class("脱 Boc") == "L:N.am~Boc:1" and _class("脱 Cbz") == "L:N.am~Cbz:1" and _class("乙酰胺水解") == "L:N.am~C=O:1"
    assert _class("甲酯水解") == "L:O.acyl~C:1" and _class("叔丁酯脱除") == "L:O.acyl~C.t:1" and _class("苄酯氢解") == "L:O.acyl~C.bn:1"
    # 都是从同一类原子上脱保护：相关反应
    # L:N.am~* 读作“酰胺氮上掉了东西”；* 表示掉的是什么不管
    assert _core("脱 Boc") == _core("脱 Cbz") == _core("乙酰胺水解") == "L:N.am~*"
    # 三种酯的核心都是 L:O.acyl~*（酯氧上掉了东西）
    assert _core("甲酯水解") == _core("叔丁酯脱除") == _core("苄酯氢解")
    # 酯还原成醇（羰基变单键，烷氧基掉下去）和酰胺还原成胺（氮留下，羰基氧整个掉了）不是同类。
    # L:C.acyl~O:2 读作“酰基碳上掉了一个双键连着的氧”
    assert _class("酯还原成醇") != _class("酰胺还原成胺") == "L:C.acyl~O:2"


def test_reaction_center_does_not_depend_on_how_the_data_happened_to_be_mapped():
    """同一个反应，原始数据里原子映射的标法不一样，算出来的反应中心（或同类钥匙）必须一样。

    保证的行为：m01_structure._center_from_mols 在对照两边的键之前先整理映射，抹平几种常见的标法差异：
        _settle_twin_oxygens    羧基的两个氧谁对谁，是标映射的软件随手定的：统一成“留下来的是双键氧”
        _outward_from_first     对称试剂（Boc2O）两半标着同一套号：只取连在一起的那一半
        孤零零一个进了产物的原子   不管它标没标映射、来自哪个试剂，一律按“新接上来的”算
    另外 chem.center_class 里的 _SAME_CLASS 表把“酸和醇成酯”的两种标法并成一种。
    怎么读：每一句断言里，名字后面括号注明了标法差异的那条反应，和正常标法的那条反应应当相等。
    每一组上面原有的那行注释，说的是这一组针对哪一种差异。
    为什么重要：先例库是一百七十多万条专利反应，上面这些标法在专利数据里很常见（见各函数的说明）。
    同一种反应要是因为标法不同被写成两串不同的文字，库里明明有的先例就认不出来。
    """
    # 羧基的两个氧谁对谁
    # 第一句比的是反应中心的原样文字；第二句比同类钥匙（甲酯、乙酯掉下去的都是普通烷基 C）
    assert _center("羧酸成酰胺（两个氧对反了）") == _center("羧酸成酰胺")
    assert _class("乙酯水解（两个氧对反了）") == _class("甲酯水解")
    # 两种标法统一读成“羰基氧留下来变成羟基（双键变单键），烷氧基整个掉下去”
    assert _center("酯还原成醇（留下的标成了烷氧基的氧）") == _center("酯还原成醇") == "C:C.acyl~O=:2>1|L:C.acyl~O:1"
    # 对称试剂的两半标着同一套号
    # 两条都读成“胺的氮接到酰基碳上，酰基碳上掉下去另一半碳酸酯（O.acyl）”
    assert _center("上 Boc（两半同号）") == _center("上 Boc") == "F:C.acyl~N:1|L:C.acyl~O.acyl:1"
    # 试剂给出的原子标没标映射、用的是哪种试剂
    # 三种写法都是 N:c~Br:1：芳环碳上接了一个新来的溴
    assert _center("溴代（溴素，溴标了映射）") == _center("溴代（NBS，溴标了映射）") == _center("溴代（溴没标映射，溴素写在试剂里）")
    # 酸和醇成酯，断的算是哪一边的 C–O
    # 这两种标法归成同一类；酸和碘乙烷成酯是真的断了 C–I，不并进来，所以和它们不同类
    assert _class("酸和醇成酯（醇的羟基掉了）") == _class("酸和醇成酯（酸的羟基掉了）") != _class("酸和碘乙烷成酯")
    # 孤零零一个对上产物的原子，不管它原来在哪个试剂上、还是在要掉下去的基团里
    # L:C=O~CO2H:1|N:C=O~OH:1 读作“羰基碳上掉了一个羧基，又接上一个新来的羟基”
    assert _center("酮酸脱羧（氧来自掉下去的羧基）") == _center("酮酸脱羧（氧来自氢氧根）") == "L:C=O~CO2H:1|N:C=O~OH:1"


def test_methylation_is_the_same_class_as_other_alkylations_whoever_supplied_the_methyl():
    """甲基化和别的烷基化是同一类反应，不管数据里把甲基碳对到了哪个试剂上；胺的 N-甲基化单独一类。

    保证的行为（m01_structure.py）：
        甲基只有一个碳，算反应中心时按“新接上来的一个原子”记（N:OH.ar~C:1），不看甲基是谁给的；
        chem.center_class 的 _SAME_CLASS 表再把酚、醇、硫醇、酰胺氮、芳环氮上的甲基化改写成
        “和卤代烷的烷基化”（F:C~OH:1|L:C~X:1），于是和乙基化、苄基化是同类；
        胺的 N-甲基化不改写（它也可能是甲醛还原胺化做的），单独一类；
        _core_change 在“核心”里把“接上一个碳”看成“成了一根键”，所以甲基、乙基格氏试剂的加成是相关反应。
    怎么读：四组断言，依次对应上面四条。
    为什么重要：m01_structure.py 里记着，专利数据常把甲基碳对到碳酸钾、溶剂上。
    要是按“甲基是谁给的”来分类，同一种 O-甲基化会被拆成好几类，各类的先例互相用不上。
    """
    # 两种标法（碘甲烷带着 7 号碳；7 号碳被错标到碳酸根上）得到同一个反应中心
    assert _center("酚的甲基化（碘甲烷）") == _center("酚的甲基化（甲基碳对到了碳酸根上）") == "N:OH.ar~C:1"
    # 归并以后，和碘乙烷的乙基化是同一把同类钥匙：酚氧和碳成键，碳上掉了一个卤素（X）
    assert _class("酚的甲基化（碘甲烷）") == _class("酚的乙基化（碘乙烷）") == "F:C~OH:1|L:C~X:1"
    # 胺的 N-甲基化也可能是甲醛还原胺化做的，单独一类
    assert _class("胺的甲基化") == "N:N~C:1"
    # 甲基格氏试剂加成和乙基格氏试剂加成：相关反应（成的键一样）
    # 核心里有两项：羰基的双键变单键（C:C=O~O=:2>1），羰基碳上成了一根 C–C 键（F:C~C=O:1）
    assert _class("醛和甲基格氏试剂") != _class("醛和乙基格氏试剂")
    assert _core("醛和甲基格氏试剂") == _core("醛和乙基格氏试剂") == "C:C=O~O=:2>1|F:C~C=O:1"


def test_structure_similarity_separates_close_analogs_from_distant_ones():
    """结构相似度（底物像不像）：很接近的同系物在 0.7 以上，换了取代基的落在中间，不相干的接近 0。

    保证的行为：chem.similarity，也就是两个分子的结构指纹（chem.structure_bits，RDKit 的路径指纹）的 Tanimoto 相似度。
    用在哪：M5 定证据等级时，“底物像不像”比的是这一步的产物和先例的产物。data/scoring.toml 的 [evidence] 里：
        同类反应的先例里，相似度 ≥ high_similarity_threshold（0.7）   E2 高相似先例
        ≥ low_similarity_threshold（0.2）                            E3 低相似先例
        再低                                                         不当作可参考的先例
    这个测试里的 0.7 和 0.2 两条线对着的就是这两个阈值。
    怎么读：基准分子是 1-(4-异丁基苯基)乙醇（演示数据里布洛芬格氏路线的“醇中间体”），拿它和五个分子比。
    实际算出来依次是 0.927、0.852、0.522、0.047、1.0。
    为什么重要：README 说明了为什么没有用更常见的 Morgan 指纹：用那种指纹，很接近的同系物也只有 0.48–0.67，
    0.7 这条线基本够不着。以后换指纹或者改指纹的参数，这几档要是变了，证据等级会跟着整体变。
    """
    alcohol = "CC(C)Cc1ccc(cc1)C(C)O"
    assert chem.similarity(alcohol, "CC(C)Cc1ccc(cc1)C(CC)O") > 0.9  # 多一个碳的同系物
    assert chem.similarity(alcohol, "CCCCc1ccc(cc1)C(C)O") > 0.7  # 异丁基换成正丁基
    assert 0.2 < chem.similarity(alcohol, "CC(O)c1ccc(Cl)cc1") < 0.7  # 换成氯代：相似但算不上高度相似
    assert chem.similarity(alcohol, "OC1CCCCC1") < 0.2  # 环己醇：不相干
    assert chem.similarity(alcohol, alcohol) == 1.0


def test_reaction_naming_and_atom_economy():
    """按官能团的增减给反应起名；按分子量算原子经济性。

    保证的行为：
        m07_conditions.name_reaction   数一数各种官能团在反应物和产物里各有几个，按“少了什么、多了什么”
                                       套一串规则，第一条成立的就是名字；一条都不成立时返回 None
        m09_green.atom_economy         产物分子量 ÷ 反应物分子量之和（百分数，一位小数）；
                                       算出来超过 100.5% 说明反应物没给全，返回 None
    用在哪：AiZynthFinder 只给反应物和产物，引擎没给出可用的反应类别时，步骤的标题靠 name_reaction 猜
    （routes._Builder._reaction）；它也认不出来的那一步叫“片段连接”或“官能团转化”。
    atom_economy 是 M9“绿色化学”这一维的一半，另一半是溶剂评级。
    怎么读：每一句给一组反应物和产物，核对名字。前四句是内置演示路线里的步骤；
    中间那几句的例子取自真实的搜索结果（见那里原有的注释）；最后两句是原子经济性。
    为什么重要：名字不只是显示用。M7 会在名字里找关键词来判断操作条件（data/condition_rules.toml 的
    class_keywords：名字里有“格氏”算无水无氧操作，有“加氢”算加压气体反应），所以名字起错了会连带影响操作分；
    用户勾了对应的“做不了的条件”时，还会让整条路线被排除。
    """
    # 异丁基苯 + 乙酸酐 → 4-异丁基苯乙酮（布洛芬三条演示路线共同的第 1 步）：产物多出芳基酮，反应物里有酸酐
    assert name_reaction(["CC(C)Cc1ccccc1", "CC(=O)OC(C)=O"], "CC(C)Cc1ccc(cc1)C(C)=O") == "傅-克酰基化"
    # 水杨酸 + 乙酸酐 → 阿司匹林：产物多出一个酯，酚羟基少了一个
    assert name_reaction(["OC(=O)c1ccccc1O", "CC(=O)OC(C)=O"], ASPIRIN) == "酯化（O-酰化）"
    # 氯代物 + 二氧化碳 → 布洛芬：二氧化碳是反应物，产物多出羧基。反应物里没有酚，所以不叫 Kolbe–Schmitt 羧基化
    assert name_reaction(["CC(C)Cc1ccc(cc1)C(C)Cl", "O=C=O"], IBUPROFEN) == "格氏羧基化"
    # 4-异丁基苯乙酮 → 对应的醇：酮少了，醇多了
    assert name_reaction(["CC(C)Cc1ccc(cc1)C(C)=O"], "CC(C)Cc1ccc(cc1)C(C)O") == "羰基还原"
    # 丁烷 → 戊烷：没有任何官能团增减，一条规则都不成立
    assert name_reaction(["CCCC"], "CCCCC") is None
    # 接上 AiZynthFinder 以后补的几类，例子取自它给布洛芬、对乙酰氨基酚搜出来的步骤
    # 草酰氯 + 羧酸 → 酰氯：羧酸少了，产物里有酰氯
    assert name_reaction(["O=C(Cl)C(Cl)=O", "CC(C)=Cc1ccc(C(C)C(=O)O)cc1"], "CC(C)=Cc1ccc(C(C)C(=O)Cl)cc1") == "酰氯化"
    # 只少了一个碳碳双键，别的官能团都没动
    assert name_reaction(["CC(C)=Cc1ccc(C(C)C(=O)Cl)cc1"], "CC(C)Cc1ccc(C(C)C(=O)Cl)cc1") == "烯烃加氢"
    # 芳基酮没了，又没有变成醇、烯烃、胺、肟或环氧：当作 C=O 被还原成了 CH2
    assert name_reaction(["CC(C)C(=O)c1ccc(C(C)CCl)cc1"], "CC(C)Cc1ccc(C(C)CCl)cc1") == "羰基还原成亚甲基"
    # 对甲氧基乙酰苯胺 → 对乙酰氨基酚：芳基甲基醚少了，酚多了（脱掉了甲基）
    assert name_reaction(["COc1ccc(NC(C)=O)cc1"], "CC(=O)Nc1ccc(O)cc1") == "脱保护成酚"
    # 碘甲烷 + 酯 → 酯的 α 位多一个甲基：前面的规则都不成立，只是卤代烷少了，笼统地叫“烷基化”
    assert name_reaction(["CI", "CCOC(=O)Cc1ccc(C=C(C)C)cc1"], "CCOC(=O)C(C)c1ccc(C=C(C)C)cc1") == "烷基化"
    # 伯醇 → 布洛芬：醇少了，羧酸多了
    assert name_reaction(["CC(C)Cc1ccc(C(C)CO)cc1"], IBUPROFEN) == "氧化成羧酸"

    # 水杨酸 138.12 + 乙酸酐 102.09 -> 阿司匹林 180.16
    # 180.16 ÷ (138.12 + 102.09) = 180.16 ÷ 240.21 = 75.0%：投进去的原子按质量算有四分之三留在了产物里
    assert atom_economy(["OC(=O)c1ccccc1O", "CC(=O)OC(C)=O"], ASPIRIN) == 75.0
    # 甲烷 16.04 → 丁烷 58.12：58.12 ÷ 16.04 ≈ 362%。超过了 100.5% 的容许范围，返回 None，而不是给一个离谱的数
    assert atom_economy(["C"], "CCCC") is None  # 反应物不全，算出来超过 100%
