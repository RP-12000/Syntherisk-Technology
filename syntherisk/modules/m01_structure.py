"""M1 结构工具（RDKit）：解析和校验结构、画图、比较结构、算指纹。

界面上的用处：A1–A4 输入分子后的结构预览和校验（R1），C2 标出先例和本步的结构差异，
E4 按子结构搜库存。其他模块要处理分子时也都从这里走。

对应 PPT 的 M1（结构工具）。这个文件只做计算：不读写数据库，不发网络请求，不读写文件。
M1 里要联网的那一半（名称、CAS 号转结构，查 PubChem 和 OPSIN）在 m01_resolver.py。

依赖：RDKit（化学信息学库：解析分子、画图、算指纹、找子结构）和 numpy（指纹的位运算）。
别的文件几乎都用 `from syntherisk.modules import m01_structure as chem` 引入它，
所以在别处看到 chem.xxx，就是这个文件里的函数。

读这个文件要先知道的几个词：
    SMILES      用一行文字写一个分子，比如乙醇是 CCO。同一个分子有很多种写法（OCC 也是乙醇），
                所以进来的 SMILES 都先转成“规范 SMILES”（RDKit 给出的唯一写法）再用。
    InChIKey    分子的编号，27 个字符，形如 LFQSCWFLJHTTHZ-UHFFFAOYSA-N。同一个分子不管怎么写，编号都一样。
    key14       InChIKey 的前 14 个字符：只和原子怎么连有关，不分立体异构。
                后端里判断“是不是同一个物质”一律比 key14，Analysis.result 的 molecules 表也拿它当键。
    原子映射    反应 SMILES 里原子后面的编号，比如 [CH3:1]。反应物和产物里编号相同的是同一个原子，
                由此看得出每个原子从哪来、到哪去。USPTO 专利数据和 AiZynthFinder 的输出都带映射。
    反应中心    根据原子映射列出来的“这一步到底改了哪些键”，写成一串文字，比如
                F:C.acyl~N:1|L:C.acyl~Cl:1（见 reaction_center）。M5 用它判断先例和这一步是不是同一种反应。
    指纹        把一个分子（或者一步反应）变成固定长度的一串 0 和 1。两串里同为 1 的位越多，两者越相似
                （Tanimoto 相似度，见 tanimoto）。这里有两种指纹：
                结构指纹比“底物像不像”；反应类别指纹在没有原子映射时粗略地比“是不是同一类反应”。

本文件里有什么（按阅读顺序）：
    解析与校验
        StructureInfo        R1 结构预览和校验要显示的全部内容
        detect_format        猜一段文字是 SMILES、MOL 还是 SDF
        parse_mol            把文字解析成 RDKit 的分子，同时给出错误和警告
        describe_mol         算分子式、分子量、立体中心等，装进 StructureInfo
        validate_structure   上面两步合在一起；接口直接调它
    标识与缓存
        inchikey_of          分子的 InChIKey
        MolRecord、mol_record                    一个分子的基本信息（规范 SMILES、key14、分子式、分子量），带缓存
        to_mol、canonical_smiles、key14_of       最常用的三个小函数
        net_charge、is_salt_like、fragment_key14s  处理盐和离子
    画图
        mol_to_svg、mol_to_png   分子结构图
    指纹与相似度
        structure_bits       结构指纹
        difference_bits、environment_delta、delta_similarity   反应类别指纹和它的精确版本（不需要原子映射）
        popcount_rows、tanimoto_many、tanimoto、similarity     Tanimoto 相似度
    结构比较与搜索
        structure_diff       C2：两个结构的公共部分和各自多出来的原子
        parse_query、has_substructure、smarts_pattern、matches_smarts   子结构匹配
    反应
        parse_reaction_smiles、parse_mapped_reaction   把反应 SMILES 拆成反应物、试剂、产物
    反应中心（要原子映射）
        _atom_type、_outside_type、_alkyl_kind、_bond_label    给原子、基团、键分类
        _by_map、_settle_twin_oxygens、_outward_from_first     整理原子映射
        _center_from_mols    算反应中心的主体
        reaction_center、retro_reaction_center   对外的两个入口：正着写的反应、逆合成引擎的写法
        center_class         把反应中心归并成比较用的两把钥匙：(同类, 核心)
    反应的编号和图
        build_reaction_smiles、main_fragment_key14、reaction_key   拼反应 SMILES，算“同一反应”的编号
        reaction_rxn_block、reaction_to_png      报告里用的 RXN 文件和反应式图
"""

from __future__ import annotations

import hashlib
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from functools import lru_cache

import numpy as np
from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors, rdFingerprintGenerator, rdFMCS, rdMolDescriptors
from rdkit.Chem.Draw import rdMolDraw2D

# 给用户看的校验说明都写成 i18n.tr("中文", "English")：返回的就是中文，同时登记了英文（见 syntherisk/i18n.py）
from syntherisk import i18n

# 上面引入的 RDKit 各部分在这个文件里的用处：
#   Chem                    解析 SMILES / MOL，分子、原子、键这几种对象，子结构匹配，InChIKey
#   Descriptors             分子量
#   rdMolDescriptors        分子式
#   rdFingerprintGenerator  指纹生成器
#   rdFMCS                  找两个分子的最大公共子结构（structure_diff 用）
#   rdMolDraw2D             把分子、反应画成 SVG 或 PNG

# 关掉 RDKit 自己的日志输出：结构读不出来时它会往控制台打印一段英文报错。
# 读不出来的原因由 parse_mol 整理成中文放在返回值里，不靠这些打印。
RDLogger.DisableLog("rdApp.*")

# 输入的上限：SMILES 超过 2000 个字符，或者分子超过 250 个重原子（氢以外的原子），parse_mol 直接报错。
# 这两个数是这里定的，不是 RDKit 的限制。
MAX_SMILES_LENGTH = 2000
MAX_HEAVY_ATOMS = 250

# describe_mol() 给结构写的四句带数字的提示：(中文模板, 英文模板)。
# 这四句会跟着目标分子存进分析记录（analyses.target），以后别的请求（甚至重启以后）再取出来显示，
# 那时已经不在当初记对照的那个请求里了。所以不靠 i18n.tr 当场记，而是在导入本模块时把所有可能的数字
# 都登记成固定对照：个数不会超过重原子数的上限，净电荷的绝对值也一样。一共一千多条，只占一点内存
_MSG_STEREOCENTERS = ("{n} 个立体中心未指定，按外消旋处理", "Unspecified stereocenters: {n} (treated as racemic)")
_MSG_OPEN_BONDS = ("{n} 个双键的顺反构型未指定", "Double bonds with unspecified E/Z geometry: {n}")
_MSG_COMPONENTS = ("结构里有 {n} 个不相连的部分（盐或混合物）", "The structure has {n} disconnected parts (a salt or a mixture)")
_MSG_CHARGE = ("分子带净电荷 {charge:+d}", "The molecule carries a net charge of {charge:+d}")
for _n in range(1, MAX_HEAVY_ATOMS + 1):
    for _zh, _en in (_MSG_STEREOCENTERS, _MSG_OPEN_BONDS, _MSG_COMPONENTS):
        i18n.register(_zh.format(n=_n), _en.format(n=_n))
    for _charge in (_n, -_n):
        i18n.register(_MSG_CHARGE[0].format(charge=_charge), _MSG_CHARGE[1].format(charge=_charge))
# 两种指纹的长度（位数）。打包成字节后分别是 2048 / 8 = 256 个字节和 1024 / 8 = 128 个字节，
# 先例库 reaction_precedents 表的 fp_product、fp_diff 两列存的就是这些字节。
# 改这两个数等于改了指纹的定义：库里已有的指纹长度对不上，要重新导入先例库才检索得到
# （m05_precedents.PrecedentIndex.refresh 加载时会跳过长度不对的行）。
STRUCTURE_FP_BITS = 2048
CLASS_FP_BITS = 1024

# 结构指纹：RDKit 的路径指纹（Daylight 式）。“底物相似度 ≥ 0.7 算高度相似”这个经验阈值就是配这类指纹用的；
# 实测 1-(4-异丁基苯基)乙醇和它的同系物、换了烷基的类似物在 0.72–0.93，换成氯代或萘基的在 0.5 上下。
# 做法：把分子里所有不太长的成键片段（RDKit 的默认值：1 到 7 根键，直链的和带分叉的都算）各算一个哈希值，
# 每个片段把 2048 位里的 2 位置成 1。两个分子共有的片段越多，同为 1 的位就越多。
# 没有用更常见的 Morgan 指纹，原因见 README“几处和你预想的可能不一样的地方”一节：
# 用 Morgan 指纹算，很接近的同系物也只有 0.48–0.67，够不到 0.7 的阈值。
# 这是一个“生成器”对象，建一次反复用；真正算指纹在 structure_bits() 里。
_STRUCTURE_FP = rdFingerprintGenerator.GetRDKitFPGenerator(fpSize=STRUCTURE_FP_BITS)
# 反应类别指纹只看原子本身的变化（元素、成键数、氢原子数、电荷），不看邻居，也不看在不在环上：
# 这样芳基酮和环己酮的还原算同一类反应，底物像不像交给结构指纹去判断。
# 写法上：Morgan 指纹本来是“以每个原子为中心，往外看 radius 圈邻居”；这里 radius=0，一圈都不看，
# 只剩原子自己的那几项性质。includeRingMembership=False 是不把“在不在环上”算进去。
# 结果是每一种原子（比如“连着三个重原子、不带氢、不带电荷的碳”）对应一个整数编号，
# environment_delta() 数的就是每个编号从反应物到产物多了几个、少了几个。
# 注意这个分类很粗：键是单键还是双键不在其中，羰基碳和连着三个重原子的烯碳、芳环碳是同一个编号。
_ATOM_ENVIRONMENT = rdFingerprintGenerator.GetMorganGenerator(
    radius=0, atomInvariantsGenerator=rdFingerprintGenerator.GetMorganAtomInvGen(includeRingMembership=False)
)
# 查表用的数组：_POPCOUNT[i] 是整数 i（0–255，也就是一个字节）写成二进制时有几个 1，
# 例如 _POPCOUNT[7] 是 3（00000111）。指纹打包成字节以后，“有多少位是 1”就是把每个字节查这张表再加起来。
# numpy 允许拿整个数组当下标（_POPCOUNT[矩阵]），一次查完上百万个字节，不用在 Python 里逐个循环。
_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)


# ---------------------------------------------------------------- 解析与校验


@dataclass
class StructureInfo:
    """R1 结构预览和校验要显示的全部内容。

    一次校验的结果。由 validate_structure()（或 describe_mol()）生成；接口 POST /api/structures/validate
    和 /api/structures/upload 把它转成字典（另加一个 name 字段）直接返回给前端，“开始分析”时也用它检查目标分子。
    结构读不出来时 valid 是 False，只有 input_format、errors、warnings 有内容，其余都是默认值。
    三个文字列表的分工：errors 是读不出来的原因（有它就不能往下走）；warnings 是读得出来但值得提醒的事；
    messages 是关于立体化学的说明。三者都是可以直接显示给用户的中文句子。
    """

    # 结构读不读得出来、合不合化学规则。False 时不能拿去分析
    valid: bool
    # 实际按哪种格式读的：smiles、mol 或 sdf（传 auto 时由 detect_format 猜）
    input_format: str = "smiles"
    # 规范 SMILES：同一个分子不管用户怎么写，这里都是同一种写法
    smiles: str | None = None
    # 完整的 InChIKey（27 个字符），区分立体异构体
    inchikey: str | None = None
    # InChIKey 的前 14 个字符，后端认“同一个物质”用的键
    key14: str | None = None
    # 分子式，比如 C13H18O2
    formula: str | None = None
    # 分子量（g/mol，平均分子量），保留两位小数
    mol_weight: float | None = None
    # 重原子数：氢以外的原子有几个
    heavy_atoms: int = 0
    # 有几个互不相连的部分。盐（比如 NO.Cl 盐酸羟胺）和混合物大于 1
    components: int = 1
    # 四面体立体中心（手性碳这一类）一共几个
    stereocenters: int = 0
    # 其中没有写明构型的有几个；这些按外消旋处理
    unspecified_stereocenters: int = 0
    # 整个分子的净电荷，中性分子是 0
    formal_charge: int = 0
    # 立体化学的说明，比如“1 个立体中心未指定，按外消旋处理”
    messages: list[str] = field(default_factory=list)
    # 提醒：多组分、带净电荷、不含碳、文件里有多个结构只读了第一个
    warnings: list[str] = field(default_factory=list)
    # 读不出来的原因；valid 为 True 时是空的
    errors: list[str] = field(default_factory=list)
    # 结构图（SVG 文本，320 × 220）；调用时传了 with_svg=False 就是 None
    svg: str | None = None

    def to_dict(self) -> dict:
        """转成普通字典（键就是上面这些字段名），给接口当 JSON 返回。

        谁在用：api/structures.py 的 _with_name()。
        """
        # dataclasses.asdict 会把 dataclass 的每个字段（连同里面的列表）复制成普通的 dict / list
        return asdict(self)


def detect_format(text: str) -> str:
    """猜一段结构文本是哪种格式，返回 "sdf"、"mol" 或 "smiles"。

    判断办法只看几个标志性的字样：
        SDF  里面每个结构后面有一行 $$$$
        MOL  有结尾行 “M  END”（M 后面是两个空格），或者计数行里的版本标记 V2000 / V3000
        其余一律当成 SMILES
    这只是猜格式，不保证读得出来。谁在用：parse_mol()（fmt 传 "auto" 时）。
    """
    if "$$$$" in text:
        return "sdf"
    if "M  END" in text or "V2000" in text or "V3000" in text:
        return "mol"
    return "smiles"


def _problem_text(mol: Chem.Mol, problem) -> str:
    """把 RDKit 报的一个化学问题翻译成给用户看的中文句子。

    参数：
        mol      没做化学校验就读进来的分子（parse_mol 里的 raw），用来查出问题的是哪个原子
        problem  Chem.DetectChemistryProblems() 返回的一项，带有问题的类型和说明
    返回：
        一句中文。例如 CC(C)(C)(C)C（中间的碳连了五个碳）得到“第 2 个原子 C 的成键数超出了它的化合价”；
        c1cccc1（五个芳香碳排不出单双键交替）得到“芳香环写法有误，无法确定单双键的位置”；
        其他类型的问题直接带上 RDKit 的英文说明。
    谁在用：parse_mol()。
    """
    kind = problem.GetType()
    if kind == "AtomValenceException":
        # 价态超了：RDKit 的原子编号从 0 开始，给用户看的从 1 开始数，所以加 1
        atom = mol.GetAtomWithIdx(problem.GetAtomIdx())
        return i18n.tr("第 {n} 个原子 {symbol} 的成键数超出了它的化合价",
                       "Atom {n} ({symbol}) has more bonds than its valence allows",
                       n=atom.GetIdx() + 1, symbol=atom.GetSymbol())
    if kind in ("KekulizeException", "AtomKekulizeException"):
        # 凯库勒化失败：用小写字母写的芳香环，RDKit 没法给它排出一套合理的单键、双键
        return i18n.tr("芳香环写法有误，无法确定单双键的位置",
                       "The aromatic ring is written incorrectly; single and double bonds cannot be assigned")
    return i18n.tr("结构不合理：{message}", "Invalid structure: {message}", message=problem.Message())


def parse_mol(text: str, fmt: str = "auto") -> tuple[Chem.Mol | None, str, list[str], list[str]]:
    """把 SMILES、MOL 或 SDF 文本解析成分子。返回 (分子, 实际格式, 错误, 警告)。

    参数：
        text  结构文本：用户贴进来的 SMILES、结构编辑器导出的 MOL，或者上传文件的内容
        fmt   "smiles"、"mol"、"sdf"，或者 "auto"（默认，由 detect_format 猜）
    返回一个四项的元组：
        分子      RDKit 的 Chem.Mol 对象；读不出来、没过化学校验、是空的或者太大时是 None
        实际格式  "smiles"、"mol" 或 "sdf"（text 是空的时候固定是 "smiles"；fmt 传了别的值时原样返回）
        错误      中文句子的列表。分子不是 None 时一定是空列表
        警告      中文句子的列表。现在只有一种：SDF 里有多个结构，只读入了第 1 个
    不抛异常，所有问题都放在“错误”里。读不出来分两种情况，给的提示不一样：
        写法本身不对（括号不配对、环没有闭合）  RDKit 连原子和键都建不出来，只能笼统地提示检查写法
        写法对、化学上不合理（碳连了五根键）    建得出原子和键，再让 RDKit 指出具体哪里不对（见 _problem_text）
    谁在用：validate_structure()。
    """
    if not text or not text.strip():
        return None, "smiles", [i18n.tr("没有输入结构", "No structure was entered")], []
    fmt = detect_format(text) if fmt == "auto" else fmt
    warnings: list[str] = []

    if fmt == "smiles":
        # 只取空白隔开的第一段：SMILES 后面跟着的名字或者扩展信息不要
        smiles = text.strip().split()[0]
        if len(smiles) > MAX_SMILES_LENGTH:
            return None, fmt, [i18n.tr("SMILES 太长，超出了支持的范围", "The SMILES is too long to be supported")], warnings
        # 读两遍。第一遍是正常读法，RDKit 会顺带做化学校验（检查价态、芳香环等，RDKit 叫 sanitize）；
        # 读不出来时它返回 None，不抛异常。第二遍加 sanitize=False，只建原子和键、不做校验。
        # 两遍都是 None：写法本身不对。第二遍读得出来：写法没错，是化学上不合理，把 raw 留到下面查具体原因
        mol = Chem.MolFromSmiles(smiles)
        raw = None if mol is not None else Chem.MolFromSmiles(smiles, sanitize=False)
        if mol is None and raw is None:
            return None, fmt, [i18n.tr("无法识别这段 SMILES，请检查括号、环闭合数字和元素符号",
                                       "Could not read this SMILES; check the brackets, ring-closure digits and element symbols")], warnings
    elif fmt in ("mol", "sdf"):
        # SDF 文件是一个个 MOL 记录，每个记录后面有一行 $$$$。按它切开，去掉空白的段；只读第一个
        records = [r for r in text.split("$$$$") if r.strip()] if fmt == "sdf" else [text]
        if len(records) > 1:
            warnings.append(i18n.tr("文件里有 {n} 个结构，只读入了第 1 个",
                                    "The file contains {n} structures; only the first was read", n=len(records)))
        block = records[0]  # 第一行是标题行，可以是空行，不能去掉
        # 和 SMILES 一样读两遍：先带化学校验，读不出来再不带校验读一遍
        mol = Chem.MolFromMolBlock(block)
        raw = None if mol is not None else Chem.MolFromMolBlock(block, sanitize=False)
        if mol is None and raw is None:
            return None, fmt, [i18n.tr("无法读取这个 MOL / SDF 文件", "Could not read this MOL / SDF file")], warnings
    else:
        return None, fmt, [i18n.tr("不支持的结构格式：{fmt}", "Unsupported structure format: {fmt}", fmt=fmt)], warnings

    if mol is None:
        # 走到这里：不带校验读得出来（raw），带校验读不出来。让 RDKit 把问题逐条列出来，翻译成中文；
        # 它一条也没列的话（列表是空的，or 后面的那句就顶上）给一句笼统的说明
        problems = Chem.DetectChemistryProblems(raw)
        errors = [_problem_text(raw, p) for p in problems] or [
            i18n.tr("结构不合理，无法通过化学校验", "Invalid structure: it does not pass chemical validation")]
        return None, fmt, errors, warnings
    # 读出来了，再挡掉两种没法分析的：空结构、超过上限的大分子
    if mol.GetNumAtoms() == 0:
        return None, fmt, [i18n.tr("结构是空的", "The structure is empty")], warnings
    if mol.GetNumHeavyAtoms() > MAX_HEAVY_ATOMS:
        return None, fmt, [i18n.tr("分子太大（超过 {n} 个重原子），暂不支持",
                                   "The molecule is too large (more than {n} heavy atoms) and is not supported yet",
                                   n=MAX_HEAVY_ATOMS)], warnings
    return mol, fmt, [], warnings


def _stereo_counts(mol: Chem.Mol) -> tuple[int, int, int]:
    """返回 (四面体立体中心总数, 其中未指定的个数, 未指定构型的双键个数)。

    “未指定”是说输入里没有写明这个位置的构型（SMILES 里没有 @ / @@，或者双键两侧没有用斜杠标顺反）。
    例：布洛芬 CC(C)Cc1ccc(cc1)C(C)C(=O)O 有一个手性碳、没写构型，结果是 (1, 1, 0)；
        2-丁烯写成 CC=CC 是 (0, 0, 1)。
    谁在用：describe_mol()。
    """
    centers = unspecified = open_bonds = 0
    # FindPotentialStereo 列出分子里所有“可能有立体异构”的位置，不管输入里写没写构型。
    # 每一项的 type 说明是原子还是双键，specified 说明构型写明了没有
    for info in Chem.FindPotentialStereo(mol):
        is_open = info.specified == Chem.StereoSpecified.Unspecified
        if info.type == Chem.StereoType.Atom_Tetrahedral:
            centers += 1
            # 布尔值可以直接加到整数上：True 当 1，False 当 0
            unspecified += is_open
        elif info.type == Chem.StereoType.Bond_Double:
            open_bonds += is_open
    return centers, unspecified, open_bonds


def describe_mol(mol: Chem.Mol, fmt: str = "smiles", *, with_svg: bool = True) -> StructureInfo:
    """给一个已经读出来的分子算出 R1 要显示的各项内容。

    参数：
        mol       parse_mol() 读出来的分子（已经通过化学校验）
        fmt       它是按哪种格式读的，原样记进结果的 input_format
        with_svg  要不要顺便画结构图（320 × 220 的 SVG）。不需要图的调用方传 False
    返回：
        valid=True 的 StructureInfo。messages 和 warnings 在这里生成，它们都不算错误：
            立体中心或双键的构型没写明          → messages
            多组分（盐或混合物）、带净电荷、不含碳  → warnings
    谁在用：validate_structure()。
    """
    # 规范 SMILES：RDKit 按固定规则重新写一遍，同一个分子得到同一种写法
    smiles = Chem.MolToSmiles(mol)
    inchikey = inchikey_of(mol)
    centers, unspecified, open_bonds = _stereo_counts(mol)
    # GetMolFrags 把分子按“连不连在一起”分成几块（每块是一组原子编号），这里只要块数
    components = len(Chem.GetMolFrags(mol))
    # 所有原子的形式电荷加起来
    charge = Chem.GetFormalCharge(mol)

    info = StructureInfo(
        valid=True,
        input_format=fmt,
        smiles=smiles,
        inchikey=inchikey,
        key14=inchikey[:14],
        formula=rdMolDescriptors.CalcMolFormula(mol),
        mol_weight=round(Descriptors.MolWt(mol), 2),
        heavy_atoms=mol.GetNumHeavyAtoms(),
        components=components,
        stereocenters=centers,
        unspecified_stereocenters=unspecified,
        formal_charge=charge,
    )
    # 下面几条是提示，不影响 valid。立体化学的两条进 messages
    if unspecified:
        info.messages.append(i18n.tr(*_MSG_STEREOCENTERS, n=unspecified))
    if open_bonds:
        info.messages.append(i18n.tr(*_MSG_OPEN_BONDS, n=open_bonds))
    # 其余三条进 warnings
    if components > 1:
        info.warnings.append(i18n.tr(*_MSG_COMPONENTS, n=components))
    if charge != 0:
        # 格式 +d 让正数也带上加号：+1、-2
        info.warnings.append(i18n.tr(*_MSG_CHARGE, charge=charge))
    if not any(atom.GetSymbol() == "C" for atom in mol.GetAtoms()):
        info.warnings.append(i18n.tr("结构里没有碳原子，逆合成搜索可能找不到路线",
                                     "The structure contains no carbon atoms; the retrosynthesis search may find no routes"))
    if with_svg:
        info.svg = mol_to_svg(mol)
    return info


def validate_structure(text: str, fmt: str = "auto", *, with_svg: bool = True) -> StructureInfo:
    """校验一段结构文本，给出 R1 结构预览和校验要显示的全部内容（先 parse_mol，再 describe_mol）。

    参数：
        text      SMILES、MOL 或 SDF 文本
        fmt       "auto"（默认，自动判断）、"smiles"、"mol" 或 "sdf"
        with_svg  要不要带结构图
    返回：
        StructureInfo。读不出来时 valid=False，原因在 errors 里；读得出来时 valid=True。不抛异常。
    例：
        validate_structure("CC(C)Cc1ccc(cc1)C(C)C(=O)O")   布洛芬
            → formula "C13H18O2"，mol_weight 206.28，messages ["1 个立体中心未指定，按外消旋处理"]
        validate_structure("C1CC")    环没有闭合
            → valid False，errors ["无法识别这段 SMILES，请检查括号、环闭合数字和元素符号"]
        validate_structure("NO.Cl")   盐酸羟胺
            → valid True，components 2，warnings 里有“结构里有 2 个不相连的部分（盐或混合物）”
    谁在用：
        api/structures.py 的 validate_structure（A1、A2、A5）和 upload_structure（A4）；
        api/analyses.py 的 create_analysis（A16 开始分析时校验目标分子）；
        m01_resolver.NameResolver.resolve_row（E1、E3 校验库存行里填的 SMILES）。
    """
    mol, actual, errors, warnings = parse_mol(text, fmt)
    if mol is None:
        return StructureInfo(valid=False, input_format=actual, errors=errors, warnings=warnings)
    info = describe_mol(mol, actual, with_svg=with_svg)
    # 解析阶段的警告（SDF 里有多个结构）排在前面，describe_mol 生成的警告排在后面
    info.warnings = warnings + info.warnings
    return info


# ---------------------------------------------------------------- 标识与缓存


def inchikey_of(mol: Chem.Mol) -> str:
    """InChIKey；个别金属有机物生成不了 InChI，这时用规范 SMILES 的哈希顶替，格式保持一致。

    参数：mol  RDKit 分子。
    返回：27 个字符的字符串，分三段：14 个字符 - 10 个字符 - 1 个字符，
          例如乙醇是 LFQSCWFLJHTTHZ-UHFFFAOYSA-N。第一段只和原子怎么连有关，就是 key14。
          顶替用的编号以 X 开头，三段的长度和正常的一样，所以各处“取前 14 位当 key14”的代码不用区别对待。
    谁在用：describe_mol、mol_record、fragment_key14s、structure_diff、main_fragment_key14。
    """
    key = Chem.MolToInchiKey(mol)
    if key:
        return key
    # 生成不了：对规范 SMILES 取 SHA-1（一种哈希算法，同样的文字总是得到同样的 40 位十六进制数），
    # 转成大写，取前 13 位和接下来的 10 位，拼成同样的三段
    digest = hashlib.sha1(Chem.MolToSmiles(mol).encode("utf-8")).hexdigest().upper()
    return f"X{digest[:13]}-{digest[13:23]}-N"


@dataclass(frozen=True)
class MolRecord:
    """一个分子的基本信息。后端各处拿到一个 SMILES，要它的规范写法、key14、分子式、分子量时，用的就是它。

    由 mol_record() 生成并缓存。frozen=True 表示建好以后不能改（给字段赋值会报错），
    所以放在缓存里被很多地方共用也不会被改坏。
    """

    # 规范 SMILES
    smiles: str
    # 完整的 InChIKey（27 个字符）
    inchikey: str
    key14: str  # InChIKey 前 14 位：只看连接方式，不分立体异构
    # 分子式，比如 C2H6O
    formula: str
    # 分子量（g/mol），保留两位小数
    mol_weight: float
    # 重原子数（氢以外的原子）
    heavy_atoms: int


@lru_cache(maxsize=50000)
def _cached_mol(smiles: str) -> Chem.Mol | None:
    """把 SMILES 解析成分子，结果记在缓存里。空字符串或者读不出来时返回 None。

    lru_cache 是 Python 自带的缓存：同样的参数再调一次，直接把上次的结果拿出来，不再解析。
    最多记 50000 个不同的 SMILES，满了以后丢掉最久没用过的。
    注意：缓存里的是同一个分子对象，大家共用。拿到以后只能读，不能改；
    要改（比如算坐标、加属性）先复制一份：Chem.Mol(mol)。
    谁在用：to_mol()。
    """
    return Chem.MolFromSmiles(smiles) if smiles else None


def to_mol(smiles: str) -> Chem.Mol | None:
    """SMILES → RDKit 分子（带缓存）。空字符串、None、读不出来的 SMILES 都返回 None，不抛异常。

    先去掉首尾的空白再查缓存。返回的是缓存里共用的对象，不要改它（见 _cached_mol）。
    谁在用：本文件里的 mol_record、net_charge、is_salt_like、fragment_key14s、_as_mol、environment_delta、
            structure_diff、has_substructure、matches_smarts、main_fragment_key14；
            m07_conditions._count（数官能团）；m13_reports.export_structures_zip（导出 SDF，用之前先复制）。
    """
    return _cached_mol(smiles.strip()) if smiles else None


@lru_cache(maxsize=50000)
def mol_record(smiles: str) -> MolRecord | None:
    """SMILES → 这个分子的基本信息（MolRecord），带缓存。读不出来返回 None。

    例：mol_record("OCC") 得到 MolRecord(smiles="CCO", inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        key14="LFQSCWFLJHTTHZ", formula="C2H6O", mol_weight=46.07, heavy_atoms=3)。
    谁在用（主要的）：
        canonical_smiles、key14_of；
        routes._Builder.molecule、pipeline.ensure_molecule（往 Analysis.result 的 molecules 表里登记分子）；
        reference.CompoundLibrary（读内置试剂表 data/compounds.csv）；
        m05_precedents、m06_safety、m09_green、loaders；
        api/analyses.py、api/structures.py（检查前端传来的 SMILES 读不读得出来）。
    """
    mol = to_mol(smiles)
    if mol is None:
        return None
    inchikey = inchikey_of(mol)
    return MolRecord(
        smiles=Chem.MolToSmiles(mol),
        inchikey=inchikey,
        key14=inchikey[:14],
        formula=rdMolDescriptors.CalcMolFormula(mol),
        mol_weight=round(Descriptors.MolWt(mol), 2),
        heavy_atoms=mol.GetNumHeavyAtoms(),
    )


def canonical_smiles(smiles: str) -> str | None:
    """任意写法的 SMILES → 规范 SMILES。读不出来返回 None。例：canonical_smiles("OCC") 是 "CCO"。

    谁在用：constraints._substance_entry；m04_availability.InventoryService.upsert（库存里存的都是规范写法）；
            inventory_import.confirm_row、api/inventory.py 的 update_item（用户改结构时）；
            reference.CompoundLibrary.combine_ions；本文件的 _unmapped_smiles。
    """
    record = mol_record(smiles)
    return record.smiles if record else None


def key14_of(smiles: str) -> str | None:
    """SMILES → key14（InChIKey 的前 14 个字符）。读不出来返回 None。

    两个 SMILES 的 key14 相同，就当作同一个物质（不分立体异构、不分写法）。
    例：key14_of("OC(=O)C(C)c1ccc(CC(C)C)cc1") 和 key14_of("CC(C)Cc1ccc(cc1)C(C)C(=O)O") 相同，都是布洛芬。
    谁在用：constraints._substance_entry；m03_retrosynthesis.DemoEngine（认目标分子）；
            m04_availability（库存和在售目录都按它查）；m07_conditions；m10_scoring.exclusion_reasons；
            reference.CompoundLibrary.find_by_smiles、ReferenceData.load（管制清单按它建索引）。
    """
    record = mol_record(smiles)
    return record.key14 if record else None


# is_salt_like 认“金属”用的元素符号表。只有这 27 种；不在表里的金属（比如铱 Ir、金 Au）不按金属算
_METALS = {"Li", "Na", "K", "Rb", "Cs", "Mg", "Ca", "Ba", "Al", "Zn", "Cu", "Fe", "Co", "Ni", "Pd", "Pt", "Ru", "Rh",
           "Ag", "Sn", "Ti", "Cr", "Mn", "Os", "Ce", "Hg", "Pb"}


def net_charge(smiles: str) -> int:
    """一个物质（或者一个离子）的净电荷：所有原子的形式电荷加起来。读不出来的 SMILES 当 0。

    例：net_charge("[BH4-]") 是 -1，net_charge("[Al+3]") 是 3，net_charge("CCO") 是 0。
    谁在用：reference.CompoundLibrary.combine_ions（把专利数据里拆开写的离子拼回成盐）。
    """
    mol = to_mol(smiles)
    return Chem.GetFormalCharge(mol) if mol is not None else 0


def is_salt_like(smiles: str) -> bool:
    """含金属或带电荷的原子：这类物质在不同数据里可能写成离子，也可能写成共价的（[Al+3].[Cl-]… 和 Cl[Al](Cl)Cl）。

    返回 True / False；读不出来的 SMILES 返回 False。“金属”指 _METALS 里列的那些元素。
    例：[Al+3].[Cl-].[Cl-].[Cl-] 和 Cl[Al](Cl)Cl 都是 True，CCO 是 False。
    谁在用：reference.CompoundLibrary.__init__。内置试剂表里这类物质另外按分子式建一张索引
            （离子写法和共价写法的分子式相同），combine_ions 靠它认出一组离子是表里的哪个盐。
    """
    mol = to_mol(smiles)
    return mol is not None and any(a.GetFormalCharge() != 0 or a.GetSymbol() in _METALS for a in mol.GetAtoms())


def fragment_key14s(smiles: str) -> list[str]:
    """多组分物质（盐）里含碳片段各自的 key14，用来把“麻黄素盐酸盐”对上“麻黄素”。

    返回 key14 的列表。只有一个部分的物质、读不出来的 SMILES 返回空列表；不含碳的片段（氯离子、钠离子）不要。
    例：fragment_key14s("CC(=O)[O-].[Na+]") 是 ["QTBSBXVTEAMEQO"]（乙酸根；乙酸的 key14 也是它，
        因为去不去质子不影响前 14 位）；fragment_key14s("CCO") 是 []。
    谁在用：m06_safety.SafetyService._controlled。查管制清单时，除了拿整个物质的 key14 去对，
            还拿这些片段的去对：清单里登记的是游离的物质，手里的可能是它的盐。
    """
    mol = to_mol(smiles)
    if mol is None:
        return []
    # asMols=True：把互不相连的每一块各自做成一个分子对象（不加这个参数只给原子编号）
    frags = Chem.GetMolFrags(mol, asMols=True)
    if len(frags) < 2:
        return []
    keys = []
    for frag in frags:
        if any(atom.GetSymbol() == "C" for atom in frag.GetAtoms()):
            keys.append(inchikey_of(frag)[:14])
    return keys


# ---------------------------------------------------------------- 画图


def _as_mol(mol_or_smiles) -> Chem.Mol | None:
    """让一个参数既能传 SMILES 字符串、也能传已经解析好的分子：统一成分子。

    传字符串时走 to_mol（读不出来得到 None）；传别的东西（分子对象或 None）原样返回。
    谁在用：mol_to_svg、mol_to_png、structure_bits。
    """
    return to_mol(mol_or_smiles) if isinstance(mol_or_smiles, str) else mol_or_smiles


def mol_to_svg(mol_or_smiles, width: int = 320, height: int = 220, highlight_atoms=None) -> str | None:
    """把分子画成 SVG 图（矢量图，是一段 XML 文本，前端可以直接嵌进页面）。

    参数：
        mol_or_smiles    SMILES 字符串，或者 RDKit 分子
        width、height    图的宽和高（像素）
        highlight_atoms  要高亮的原子编号（从 0 开始）。structure_diff 用它标出两个结构不一样的部分
    返回：SVG 文本；结构读不出来时返回 None。
    谁在用：describe_mol（R1 预览）；structure_diff（C2）；m01_resolver.NameResolver.lookup（A3 候选结构的小图）；
            api/structures.py 的 depict_structure（GET /api/structures/depict）；api/views.py 的 tree_view（R5 路线树）；
            cards._molecule_view（R6 证据卡）；m04_availability.InventoryService.to_dict（R10 库存表）。
    """
    mol = _as_mol(mol_or_smiles)
    if mol is None:
        return None
    # 一张指定大小的 SVG 画布
    drawer = rdMolDraw2D.MolDraw2DSVG(width, height)
    # 不先涂底色，图的背景是透明的
    drawer.drawOptions().clearBackground = False
    # 给分子排好二维坐标并画上去；highlight_atoms 是 None 时换成空列表（没有要高亮的）
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol, highlightAtoms=list(highlight_atoms or []))
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


def mol_to_png(mol_or_smiles, width: int = 360, height: int = 240) -> bytes | None:
    """把分子画成 PNG 图（位图）。返回图片文件的字节；结构读不出来时返回 None。

    参数同 mol_to_svg（没有高亮）。PDF、Word 报告里用的是这种图。
    谁在用：m13_reports.build_blocks（报告开头的目标分子图）；reaction_to_png 画不出反应式时用它只画产物。
    """
    mol = _as_mol(mol_or_smiles)
    if mol is None:
        return None
    # 和 mol_to_svg 一样的画法，只是画布换成 Cairo 的位图画布，最后拿到的是 PNG 的字节
    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    rdMolDraw2D.PrepareAndDrawMolecule(drawer, mol)
    drawer.FinishDrawing()
    return drawer.GetDrawingText()


# ---------------------------------------------------------------- 指纹与相似度


def structure_bits(mol_or_smiles) -> np.ndarray | None:
    """结构指纹，打包成 STRUCTURE_FP_BITS / 8 个字节。

    参数：mol_or_smiles  SMILES 字符串，或者 RDKit 分子。
    返回：长度 256 的 numpy 数组，元素类型 uint8（一个元素是一个字节，一共 2048 位）；结构读不出来返回 None。
    两个分子的指纹越像（tanimoto 越接近 1），结构越像。M5 用它比“底物像不像”，比的是两步反应的产物。
    谁在用：similarity、structure_diff；
            m05_precedents.precedent_values（导入先例时算产物的指纹，存进 fp_product 列）；
            m05_precedents.PrecedentSearch._class_rows、_similar_rows（检索时算这一步产物的指纹）。
    """
    mol = _as_mol(mol_or_smiles)
    if mol is None:
        return None
    # GetFingerprintAsNumPy 给出 2048 个 0 / 1（一个数占一个位置）；np.packbits 每 8 个压成一个字节，
    # 体积是原来的八分之一。先例库有一百七十多万条，每条都要存一份，所以存压紧以后的
    return np.packbits(_STRUCTURE_FP.GetFingerprintAsNumPy(mol).astype(np.uint8))


def difference_bits(reactants: list[str], products: list[str]) -> np.ndarray | None:
    """反应类别指纹：从反应物到产物，哪些类型的原子少了、哪些多了。

    不变的原子互相抵消，剩下的只和“发生了什么变化”有关：同一类反应的指纹相同或相近，
    和底物上别的取代基无关。不需要原子映射。打包成 CLASS_FP_BITS / 8 个字节。

    参数：
        reactants  反应物的 SMILES 列表
        products   产物的 SMILES 列表（现在的调用方都只传主产物一个）
    返回：长度 128 的 uint8 数组（1024 位）；有读不出来的 SMILES 时返回 None。
    例：苯乙酮还原成 1-苯乙醇，CC(=O)c1ccccc1 → CC(O)c1ccccc1。
        甲基和苯环上的原子两边都有，抵消了。少了“羰基碳”“羰基氧”两种原子，多了“连羟基的 CH”“羟基氧”两种，
        所以这条指纹里正好有 4 位是 1。环己酮还原成环己醇得到的是同样的 4 位，两条指纹的 Tanimoto 是 1.0；
        苯甲醛还原成苄醇只有 2 位和它相同（两条合起来一共 6 位），是 0.33。
    这种比法的毛病（README、data/scoring.toml 里都有记录）：只数原子类型，不看哪个原子接到了哪里，
    分不清“产物相近、成的键不同”的反应。所以它只用在没有原子映射的步骤上；有映射的走反应中心。
    谁在用：m05_precedents.precedent_values（导入先例时算，存进 fp_diff 列）；
            m05_precedents.PrecedentSearch._similar_rows（用它在整个库里粗筛同类反应）。
    """
    delta = environment_delta(reactants, products)
    if delta is None:
        return None
    bits = np.zeros(CLASS_FP_BITS, dtype=np.uint8)
    for env, count in delta.items():
        # env 是原子类型的编号，一个很大的整数。每种类型占两位：env * 2 表示“净减”，env * 2 + 1 表示“净增”
        # （count > 0 是 True 时当 1 加上去）。再对 1024 取余数，把大编号折进 1024 位里。
        # 个数不记，只记是增还是减；不同的编号有可能折到同一位上
        bits[(env * 2 + (count > 0)) % CLASS_FP_BITS] = 1
    return np.packbits(bits)


def environment_delta(reactants: list[str], products: list[str]) -> dict[int, int] | None:
    """从反应物到产物，每种原子类型净增（正）或净减（负）了几个。不变的原子互相抵消。

    参数同 difference_bits。
    返回：字典 {原子类型的编号: 净变化的个数}，只留不为 0 的项；有读不出来的 SMILES 时返回 None。
          “原子类型的编号”由 _ATOM_ENVIRONMENT 给出：元素、连着几个原子、带几个氢、电荷，合成一个整数。
    例：苯乙酮还原 CC(=O)c1ccccc1 → CC(O)c1ccccc1 得到 4 项，意思是
        {连羟基的 CH: +1, 羟基氧: +1, 羰基碳: -1, 羰基氧: -1}（真正的键是整数编号，这里写成文字方便看）。
    谁在用：difference_bits；m05_precedents.PrecedentSearch._similar_rows（细比时给每条候选先例算一次）。
    """
    # Counter 是会自动从 0 开始计数的字典
    delta: Counter[int] = Counter()
    # 产物里的原子记正数，反应物里的记负数，都加到一起：两边都有的原子类型正负相抵
    for sign, group in ((1, products), (-1, reactants)):
        for smiles in group:
            mol = to_mol(smiles)
            if mol is None:
                return None
            # GetSparseCountFingerprint(mol).GetNonzeroElements() 给出 {原子类型编号: 这个分子里有几个}
            for env, count in _ATOM_ENVIRONMENT.GetSparseCountFingerprint(mol).GetNonzeroElements().items():
                delta[env] += sign * count
    return {env: count for env, count in delta.items() if count}


def delta_similarity(a: dict[int, int], b: dict[int, int]) -> float:
    """两个反应的原子类型变化有多一致（0–1）。和 difference_bits 比的是同一件事，但数个数、不折叠，更准，
    只是慢，所以只用在已经筛出来的少量候选先例上。

    参数：a、b  两个 environment_delta() 的结果。
    返回：0–1 的小数。1 表示增减的原子类型和个数完全一样；两边都是空的（什么都没变）也算 1。
    算法：
        分子  两边方向相同（都是增，或者都是减）的原子类型，取个数较小的那个，加起来
        分母  所有出现过的原子类型，取个数（绝对值）较大的那个，加起来
    例：a = {甲: +1, 乙: -1}，b = {甲: +2, 丙: -1}
        分子：只有甲方向相同，min(1, 2) = 1；分母：甲 2 + 乙 1 + 丙 1 = 4；结果 0.25。
    谁在用：m05_precedents.PrecedentSearch._similar_rows。结果达到多少算“同类反应”“相关反应”，
            由 data/scoring.toml 的 class_match_strict、class_match_related 定。
    """
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    # a.get(k, 0) * b.get(k, 0) > 0：两个数同号（都正或都负），也就是这种原子在两个反应里变化的方向相同
    shared = sum(min(abs(a.get(k, 0)), abs(b.get(k, 0))) for k in keys if a.get(k, 0) * b.get(k, 0) > 0)
    return shared / sum(max(abs(a.get(k, 0)), abs(b.get(k, 0))) for k in keys)


def popcount_rows(matrix: np.ndarray) -> np.ndarray:
    """指纹矩阵的每一行里有多少位是 1。

    参数：matrix  二维 uint8 数组，一行是一条打包好的指纹（N 行 × 每条指纹的字节数）。
    返回：长度 N 的 int32 数组。
    算 Tanimoto 要用每条指纹里 1 的个数。库里的指纹不变，所以加载索引时算一次存着，之后每次检索直接用。
    谁在用：m05_precedents.PrecedentIndex.refresh。
    """
    # _POPCOUNT[matrix]：把矩阵里每个字节换成“这个字节里有几个 1”（形状不变）；sum(axis=1) 再按行加起来
    return _POPCOUNT[matrix].sum(axis=1).astype(np.int32)


def tanimoto_many(query: np.ndarray, matrix: np.ndarray, matrix_counts: np.ndarray, chunk: int = 100_000) -> np.ndarray:
    """一个指纹对一整个指纹矩阵的 Tanimoto 相似度。分块算，百万级也不会吃光内存。

    参数：
        query          要查的那一条指纹（一维 uint8 数组，打包好的）
        matrix         库里的指纹，N 行，每行一条，每条的字节数和 query 相同
        matrix_counts  matrix 每一行里 1 的个数（popcount_rows 的结果），长度 N
        chunk          一次算多少行，默认十万
    返回：长度 N 的 float32 数组，第 i 个数是 query 和第 i 行的相似度（0–1）。
    Tanimoto 相似度 = 两条指纹同为 1 的位数 ÷ 至少有一条为 1 的位数：
        同为 1 的位数（交集）  两条指纹逐位做“与”，再数结果里有几个 1
        至少一条为 1（并集）    甲的 1 的个数 + 乙的 1 的个数 − 交集
    完全相同是 1，没有任何一位同为 1 是 0；两条都没有 1（并集是 0）时也记 0。
    谁在用：m05_precedents.PrecedentSearch._class_rows、_similar_rows。
    """
    out = np.zeros(matrix.shape[0], dtype=np.float32)
    query_count = int(_POPCOUNT[query].sum())
    for start in range(0, matrix.shape[0], chunk):
        block = matrix[start : start + chunk]
        # np.bitwise_and(block, query)：query 只有一行，numpy 会自动拿它和 block 的每一行逐字节做“与”（广播）。
        # 这个中间结果和 block 一样大，所以要分块：一次只多占 chunk 行的内存
        inter = _POPCOUNT[np.bitwise_and(block, query)].sum(axis=1).astype(np.int32)
        union = matrix_counts[start : start + chunk] + query_count - inter
        # np.where(条件, 甲, 乙)：条件成立的位置取甲，不成立的取乙。np.maximum(union, 1) 只是为了不除以 0
        out[start : start + chunk] = np.where(union > 0, inter / np.maximum(union, 1), 0.0)
    return out


def tanimoto(a: np.ndarray, b: np.ndarray) -> float:
    """两条打包好的指纹的 Tanimoto 相似度（0–1）。算法和 tanimoto_many 一样，只是一对一。

    两条都没有任何一位是 1 时返回 0.0。两条指纹要一样长（都是结构指纹，或者都是反应类别指纹）。
    谁在用：similarity、structure_diff；tests/test_structure.py 用它比两条反应类别指纹。
    """
    inter = int(_POPCOUNT[np.bitwise_and(a, b)].sum())
    union = int(_POPCOUNT[a].sum()) + int(_POPCOUNT[b].sum()) - inter
    return inter / union if union else 0.0


def similarity(smiles_a: str, smiles_b: str) -> float | None:
    """两个分子的结构相似度：结构指纹的 Tanimoto，保留三位小数。有一个读不出来就返回 None。

    例（都是和 1-(4-异丁基苯基)乙醇 CC(C)Cc1ccc(cc1)C(C)O 比；tests/test_structure.py 里测的也是这几个）：
        多一个碳的同系物 CC(C)Cc1ccc(cc1)C(CC)O   0.927
        氯代的类似物 CC(O)c1ccc(Cl)cc1            0.522
        环己醇 OC1CCCCC1                         0.047
        它自己                                    1.0
    谁在用：现在只有 tests/test_structure.py。后端代码里比结构时直接用 structure_bits 加 tanimoto / tanimoto_many。
    """
    a, b = structure_bits(smiles_a), structure_bits(smiles_b)
    if a is None or b is None:
        return None
    return round(tanimoto(a, b), 3)


# ---------------------------------------------------------------- 结构比较与搜索


def structure_diff(query_smiles: str, reference_smiles: str, *, with_svg: bool = True) -> dict | None:
    """C2：找出两个结构的最大公共部分，把各自多出来的原子标出来。

    参数：
        query_smiles      这一步的结构（证据卡里传的是这一步的产物）
        reference_smiles  拿来对比的结构（先例的产物）
        with_svg          要不要带两张把差异高亮出来的结构图
    返回：字典；有一个结构读不出来就返回 None。
        similarity                结构指纹的 Tanimoto 相似度，三位小数
        identical                 两个结构的 key14 是否相同（是同一个物质，不分立体异构）
        common_atoms              最大公共部分有几个原子
        query_only_atoms          query 里不在公共部分的原子编号（从 0 开始，按 query 自己的原子顺序）
        reference_only_atoms      reference 里不在公共部分的原子编号
        query_only_fragments      query 多出来的原子组成的片段，SMILES 列表（去重、排好序）
        reference_only_fragments  同上，reference 的
        query_svg、reference_svg  两张结构图，各自多出来的原子高亮；with_svg=True 才有这两个键
    例（tests/test_structure.py）：query 是 4-异丁基苯乙酮，reference 是苯乙酮
        → common_atoms 9（整个苯乙酮），reference_only_atoms []，
          query_only_atoms 是异丁基的 4 个碳 [0, 1, 2, 3]，query_only_fragments ["CC(C)C"]。
    谁在用：api/structures.py 的 diff_structures（POST /api/structures/diff）；
            api/analyses.py 的 get_step_precedents（C1、C2：给每条不是“同一反应”的先例标出结构差异）。
    """
    a, b = to_mol(query_smiles), to_mol(reference_smiles)
    if a is None or b is None:
        return None
    # 先按“找不到公共部分”把结果填好：两边的原子全都算各自独有的。找到了公共部分再改
    result = {
        "similarity": round(tanimoto(structure_bits(a), structure_bits(b)), 3),
        "identical": inchikey_of(a)[:14] == inchikey_of(b)[:14],
        "common_atoms": 0,
        "query_only_atoms": list(range(a.GetNumAtoms())),
        "reference_only_atoms": list(range(b.GetNumAtoms())),
        "query_only_fragments": [],
        "reference_only_fragments": [],
    }
    # 找最大公共子结构（MCS）：两个分子里都有的、最大的那一块。
    #   timeout=2              最多找 2 秒，到时间就用当时找到的最大的一块
    #   ringMatchesRingOnly    环上的原子只和环上的原子对应
    #   completeRingsOnly      一个环要么整个算进公共部分，要么整个不算，不取半个环
    mcs = rdFMCS.FindMCS([a, b], timeout=2, ringMatchesRingOnly=True, completeRingsOnly=True)
    # 结果是一段 SMARTS（子结构的查询写法）。转成查询对象，再分别到两个分子里找它对上了哪些原子
    pattern = Chem.MolFromSmarts(mcs.smartsString) if mcs.numAtoms else None
    if pattern is not None:
        # GetSubstructMatch 返回对上的那些原子的编号；没对上的就是“多出来的”
        in_a, in_b = set(a.GetSubstructMatch(pattern)), set(b.GetSubstructMatch(pattern))
        result["common_atoms"] = mcs.numAtoms
        result["query_only_atoms"] = [i for i in range(a.GetNumAtoms()) if i not in in_a]
        result["reference_only_atoms"] = [i for i in range(b.GetNumAtoms()) if i not in in_b]
    for mol, atoms, name in (
        (a, result["query_only_atoms"], "query_only_fragments"),
        (b, result["reference_only_atoms"], "reference_only_fragments"),
    ):
        # 有多出来的原子、又不是整个分子都多出来时，把这些原子写成 SMILES。
        # 互不相连的几块在 SMILES 里用 “.” 隔开，拆开以后去重、排序
        if atoms and len(atoms) < mol.GetNumAtoms():
            fragment = Chem.MolFragmentToSmiles(mol, atomsToUse=atoms)
            result[name] = sorted(set(fragment.split(".")))
    if with_svg:
        result["query_svg"] = mol_to_svg(a, highlight_atoms=result["query_only_atoms"])
        result["reference_svg"] = mol_to_svg(b, highlight_atoms=result["reference_only_atoms"])
    return result


def parse_query(query: str) -> Chem.Mol | None:
    """子结构查询：先按 SMILES 读（用户画出来的结构），读不了再按 SMARTS 读。

    参数：query  SMILES（比如苯环 c1ccccc1），或者 SMARTS（专门写子结构查询的语法，比如 [CX3]=O 表示羰基）。
    返回：RDKit 的查询分子，交给 has_substructure 用；空字符串、两种读法都读不出来时返回 None。
    谁在用：m04_availability.InventoryService.search（E4 按子结构搜库存）；
            m10_scoring.exclusion_reasons（A5 起始骨架：检查路线的原料里有没有这个骨架）。
    """
    query = (query or "").strip()
    if not query:
        return None
    # or：前一种读法得到 None 时才试后一种
    return Chem.MolFromSmiles(query) or Chem.MolFromSmarts(query)


def has_substructure(smiles: str, pattern: Chem.Mol) -> bool:
    """smiles 这个分子里有没有 pattern 这个子结构。

    参数：smiles  被检查的分子；pattern  parse_query() 得到的查询分子。
    返回 True / False；分子读不出来，或者 pattern 是 None 时返回 False。
    例：用 parse_query("c1ccccc1") 去查，水杨酸 O=C(O)c1ccccc1O 是 True，乙醚 CCOCC 是 False。
    谁在用：m04_availability.InventoryService.search；m10_scoring.exclusion_reasons。
    """
    mol = to_mol(smiles)
    return bool(mol is not None and pattern is not None and mol.HasSubstructMatch(pattern))


@lru_cache(maxsize=2000)
def smarts_pattern(pattern: str) -> Chem.Mol | None:
    """把一段 SMARTS 解析成查询分子，带缓存（最多 2000 段）。SMARTS 写错了返回 None。

    规则表和词表里的 SMARTS 是固定的那一批，每一步都要拿来匹配，解析一次以后直接从缓存里取。
    谁在用：matches_smarts；m07_conditions._count（数官能团）；
            tests/test_reference.py 用它检查 vocabulary.toml、condition_rules.toml 里的 SMARTS 有没有写错。
    """
    return Chem.MolFromSmarts(pattern)


def matches_smarts(smiles: str, pattern: str) -> bool:
    """smiles 这个分子是否匹配 pattern 这段 SMARTS。分子读不出来或者 SMARTS 写错时返回 False。

    例：matches_smarts("CC(=O)[O-].CC(=O)[O-].[Pd+2]", "[Pd]") 是 True（醋酸钯里有钯）。
    谁在用：reference.ReferenceData.reagent_class_matches（一个物质属不属于某一类试剂，比如钯催化剂）；
            m07_conditions.ConditionEvaluator._matches（条件规则里按结构匹配的那些）。
    """
    mol, query = to_mol(smiles), smarts_pattern(pattern)
    return bool(mol is not None and query is not None and mol.HasSubstructMatch(query))


# ---------------------------------------------------------------- 反应


def _unmapped_smiles(mol: Chem.Mol) -> tuple[str, bool]:
    """去掉原子映射后的规范 SMILES，以及它原来有没有带映射的原子。

    参数：mol  刚从反应 SMILES 里读出来的一个分子。注意：会直接改这个对象，把所有原子的映射号清成 0。
    返回：(规范 SMILES, 原来有没有带映射号的原子)。
    例：[CH3:1][C:2](=[O:3])[OH:4] → ("CC(=O)O", True)；O=S(=O)(O)O → ("O=S(=O)(O)O", False)。
    谁在用：parse_mapped_reaction()。
    """
    mapped = False
    for atom in mol.GetAtoms():
        # GetAtomMapNum() 是这个原子的映射号，没有时是 0
        if atom.GetAtomMapNum():
            mapped = True
            atom.SetAtomMapNum(0)
    # 重新读一遍，保证和没带映射的同一个分子写法完全一致
    # （canonical_smiles 读不回来时给的是 None，or 后面的那一份就顶上：直接用这个分子写出来的 SMILES）
    return canonical_smiles(Chem.MolToSmiles(mol)) or Chem.MolToSmiles(mol), mapped


def parse_reaction_smiles(reaction_smiles: str) -> tuple[list[str], list[str], list[str]] | None:
    """把 反应物>试剂>产物 拆成三组规范 SMILES，去掉原子映射和 CXSMILES 扩展。

    专利数据里的反应带原子映射：写在反应物一侧、却没有任何原子进入产物的东西
    （成盐的酸、抗衡离子）其实是试剂，这里把它们挪到试剂那一组。

    参数：reaction_smiles  “反应物>试剂>产物”，每组里的分子用 “.” 隔开；带不带原子映射都可以。
    返回：(反应物列表, 试剂列表, 产物列表)，都是去掉映射以后的规范 SMILES；读不出来返回 None。
          细节和 parse_mapped_reaction 完全一样，只是少了最后一项（反应中心）。
    例（tests/test_structure.py）：乙酸和甲醇在硫酸催化下成酯，硫酸写在了反应物一侧、没有映射号
        "[CH3:1][C:2](=[O:3])[OH:4].[CH3:5][OH:6].O=S(=O)(O)O>>[CH3:1][C:2](=[O:3])[O:6][CH3:5]"
        → (["CC(=O)O", "CO"], ["O=S(=O)(O)O"], ["COC(C)=O"])
    谁在用：现在只有测试（tests/test_structure.py、tests/test_scoring.py）；后端代码用的是 parse_mapped_reaction。
    """
    parsed = parse_mapped_reaction(reaction_smiles)
    return parsed[:3] if parsed else None


def parse_mapped_reaction(reaction_smiles: str) -> tuple[list[str], list[str], list[str], str | None] | None:
    """和 parse_reaction_smiles 一样，另外多返回一项：按原子映射算出来的反应中心（见 reaction_center）。
    没有映射、或者映射太不全时，这一项是 None。

    参数：reaction_smiles  “反应物>试剂>产物”。后面可以跟着用空格隔开的 CXSMILES 扩展（比如 “ |f:0.1|”），会被去掉。
    返回：(反应物, 试剂, 产物, 反应中心)。前三项是规范 SMILES 的列表，顺序和原文一致；第四项是字符串或 None。
    整个返回 None 的情况：不是正好两个 “>”；反应物或产物里有读不出来的分子；反应物或产物是空的。
    试剂里读不出来的分子直接跳过。
    几条规则：
        1. 每一组按 “.” 拆成一个个分子，所以写成离子的盐（[Na+].[OH-]）会变成两个“分子”。
           流水线借用先例的试剂时（pipeline.AnalysisRunner._adopt_precedent_reagents），
           再由 reference.CompoundLibrary.combine_ions 把它们拼回成盐。
        2. 反应带映射时，反应物一侧不带任何映射号的分子算试剂，排在原有试剂的前面。
           整条反应都没有映射时没法这样判断，反应物一侧的全都算反应物。
        3. 反应中心只对最大的那个产物（重原子最多）算。
    谁在用：m05_precedents.precedent_values（导入先例库时，把一条专利反应算成表里的一行）；
            parse_reaction_smiles、reaction_center。
    """
    # 取第一个空格之前的部分：空格后面是 CXSMILES 扩展
    core = reaction_smiles.strip().split(" ")[0]
    parts = core.split(">")
    if len(parts) != 3:
        return None
    # mols[0]、mols[1]、mols[2] 依次是反应物、试剂、产物三组分子
    mols: list[list[Chem.Mol]] = []
    for index, part in enumerate(parts):
        group = []
        for token in part.split("."):
            if not token:
                continue
            # 直接用 Chem.MolFromSmiles 新建分子，不走 to_mol 的缓存：下面会改这些分子上的映射号
            mol = Chem.MolFromSmiles(token)
            if mol is None:
                if index == 1:
                    continue  # 试剂读不出来就跳过，不影响这条反应
                return None
            group.append(mol)
        mols.append(group)
    if not mols[0] or not mols[2]:
        return None
    # 反应中心要趁映射还在的时候算；主产物取最大的那个，和先例库里存的产物一致
    main_product = max(mols[2], key=lambda mol: mol.GetNumHeavyAtoms())
    center = _center_from_mols(mols[0], main_product)

    # 三组分子各自变成一串 (规范 SMILES, 原来有没有映射)；这一步会把映射号清掉。
    # 写法：右边的圆括号是一个生成器，依次给出三个列表，左边的三个名字各接一个
    reactant_side, agent_side, product_side = ([_unmapped_smiles(mol) for mol in group] for group in mols)
    # 反应物一侧只要有一个分子带映射，就认为这条反应是带映射的
    has_mapping = any(mapped for _, mapped in reactant_side)
    # 带映射的反应：反应物只留带映射号的分子，不带的挪到试剂的最前面；不带映射的反应：全都留作反应物
    reactants = [s for s, mapped in reactant_side if mapped or not has_mapping]
    agents = [s for s, mapped in reactant_side if has_mapping and not mapped] + [s for s, _ in agent_side]
    products = [s for s, _ in product_side]
    if not reactants or not products:
        return None
    return reactants, agents, products, center


# ---------------------------------------------------------------- 反应中心（要原子映射）
#
# 这一段要回答的问题：两步反应是不是“同一种反应”。M5 靠这个决定一条先例能不能算这一步的依据。
#
# 办法：反应物和产物里映射号相同的原子是同一个原子。把两边的键逐根对照，就能列出这一步
#     F  哪两个原有的原子之间新成了键        B  哪两个原有的原子之间的键断了
#     C  哪根键的键级变了（双键变单键……）    L  哪个原子上掉下去一个基团
#     N  哪个原子上接上了一个新来的原子
# 每一处变化写成 “类别:两头的原子类型:键”，全部排好序用 | 连起来，这串文字就是“反应中心”。
#
# 完整的例子：苄胺和乙酰氯成酰胺（tests/test_structure.py 的“酰氯成酰胺”）。
# Ph 代表苯环，它的六个碳也带映射号（20–25），为了好读这里没有写出来。
#     反应物  [NH2:1][CH2:2]Ph  和  Cl[C:9](=[O:10])[CH3:11]
#     产物    [O:10]=[C:9]([CH3:11])[NH:1][CH2:2]Ph
#   · 1 号（氮）和 9 号（羰基碳）在反应物里不相连，在产物里连着一根单键          → F:C.acyl~N:1
#   · 9 号碳在反应物里连着一个没有映射号的氯（它没进产物），在产物里没有了       → L:C.acyl~Cl:1
#   · 其余的键（9=10、9–11、1–2、苯环上的键）两边一样，不记
#   结果：F:C.acyl~N:1|L:C.acyl~Cl:1，读作“酰基碳和氮之间成了一根单键，酰基碳上掉了一个氯”。
# 这串文字里没有苄基、甲基这些没参与反应的部分，所以换一个胺、换一个酰氯，得到的还是同一串。
#
# 各个函数的分工：
#     _atom_type            一个原子算哪一类（C.acyl、N、OH.ar……）
#     _outside_type         掉下去的或者新接上来的基团算哪一类（Cl、Boc、C.t……），里面用到 _alkyl_kind
#     _bond_label           一根键怎么写（1、2、3、ar）
#     _by_map               按映射号找原子
#     _settle_twin_oxygens  修正专利数据里常见的一种映射毛病：羧基的两个氧对反了
#     _outward_from_first   修正另一种：对称试剂的两半标着同一套映射号
#     _center_from_mols     对照两边的键，拼出上面那串文字
#     reaction_center、retro_reaction_center   对外的入口
#     center_class          比较之前再归并一次（氯、溴、碘算同一种离去基团……），得到 (同类, 核心) 两把钥匙
#
# 改了 _center_from_mols 这一段的算法（包括 _atom_type、_outside_type 的分类），先例库要重新导入，
# 因为库里存的是算好的文字；只改 center_class 的归并规则不用（见 README“USPTO 先例库”一节）。

# 羰基碳（带 =O 的碳）上如果还单键连着下面这些元素之一，就算“酰基碳”C.acyl：
# 羧酸、酯、酰胺、硫酯、酰卤、碳酸酯、氨基甲酸酯都是。没有的是醛、酮的羰基碳 C=O。见 _atom_type
_ACYL_PARTNERS = {"N", "O", "S", "F", "Cl", "Br", "I"}


def _atom_type(atom: Chem.Atom, ignore: int = -1) -> str:
    """反应中心里一个原子算哪一类。分得比元素细（酰基碳和烷基碳不是一回事），又不至于细到甲基、乙基都分开。

    ignore 是一个原子编号：判断时当作和它之间的那根键不存在。用在离去基团上——
    “掉下去的是什么”要看它自己还带着什么，不能把它原来连着的那个原子也算进去。

    参数：
        atom    RDKit 的原子对象
        ignore  同一个分子里另一个原子的编号；默认 -1，表示所有的键都算
    返回一个字符串。全部类别（同一种元素里从上往下判断，先符合哪个算哪个）：
        芳香原子  元素符号小写：c、n、o、s。苯环的碳是 c，吡啶的氮是 n
        碳   C.acyl  带 =O，同时单键连着 N、O、S 或卤素：羧酸、酯、酰胺、酰氯、碳酸酯、氨基甲酸酯的碳
             C=O     带 =O 的其他碳：醛、酮
             C=S     硫羰基的碳          C#N   腈的碳          C=N   亚胺的碳
             C#C     炔碳                C=C   烯碳            C     其余的碳（饱和碳）
        氮   N#      带三键的氮：腈的氮
             N=      带双键的氮：亚胺的氮、硝基的氮
             N.am    连着“带 =O 或 =S 的碳、硫、磷”的氮：酰胺、氨基甲酸酯、脲、磺酰胺
             N.ar    连着芳香原子的氮：苯胺
             N       其余的氮：烷基胺、氨
        氧   O=      带双键的氧：羰基氧、磺酰基上的氧
             O-      带负电荷、并且连着一个带正电荷的原子：硝基、N-氧化物上的那个氧
             OH      带氢，或者带负电荷（羧酸根、酚氧负离子和没去质子的算同一种）
             O       不带氢也不带电荷：醚、酯里的氧
             OH 和 O 后面再加一个后缀，说明它连着什么：
                 .sulf  连着带 =O 的硫：磺酸 OH.sulf、磺酸酯 O.sulf
                 .acyl  连着带 =O 的碳或磷：羧酸 OH.acyl、酯 O.acyl
                 .ar    连着芳香原子：酚 OH.ar、芳基醚 O.ar
                 不加   醇和水的 OH，二烷基醚的 O
        硫   S=O     带 =O 的硫：亚砜、砜、磺酰基
             SH      带氢或者带负电荷的硫：硫醇
             S       其余的硫：硫醚
        其他元素  直接用元素符号：F、Cl、Br、I、B、P、Si、Mg、Sn……
    例：乙酰氯 CC(=O)Cl 的四个原子依次是 C、C.acyl、O=、Cl；乙酸甲酯 CC(=O)OC 是 C、C.acyl、O=、O.acyl、C；
        乙酰苯胺 CC(=O)Nc1ccccc1 的氮既连着羰基又连着苯环，N.am 在前，所以是 N.am；
        乙酸苯酯 CC(=O)Oc1ccccc1 的酯氧同理是 O.acyl，不是 O.ar。
    谁在用：_outside_type、_alkyl_kind、_center_from_mols。
    """
    symbol = atom.GetSymbol()
    if atom.GetIsAromatic():
        return symbol.lower()
    # 这个原子的每一根键，记成 (键级, 键另一头的原子)。键级是小数：单键 1.0，双键 2.0，三键 3.0。
    # ignore 指的那个邻居不算在内
    links = [
        (bond.GetBondTypeAsDouble(), bond.GetOtherAtom(atom))
        for bond in atom.GetBonds()
        if bond.GetOtherAtomIdx(atom.GetIdx()) != ignore
    ]

    def bonded(order: float, element: str) -> bool:
        """这个原子有没有一根键级是 order 的键连着 element 这种元素。bonded(2, "O") 就是“带不带 =O”。"""
        return any(o == order and other.GetSymbol() == element for o, other in links)

    def beside(*centres: str) -> bool:  # 连着一个带 =O（或 =S）的碳、硫、磷
        """邻居里有没有这样的原子：元素是 centres 里的一种，并且它自己有一根双键连着氧或硫。"""
        for _, other in links:
            if other.GetSymbol() in centres and any(
                b.GetBondTypeAsDouble() == 2 and b.GetOtherAtom(other).GetSymbol() in ("O", "S") for b in other.GetBonds()
            ):
                return True
        return False

    def beside_aromatic() -> bool:
        """邻居里有没有芳香原子。"""
        return any(other.GetIsAromatic() for _, other in links)

    if symbol == "C":
        if bonded(2, "O"):
            # 酸、酯、酰胺、酰氯的碳和醛酮的碳分开：前者的反应是换掉那个杂原子，后者是加成或者脱氧
            return "C.acyl" if any(o == 1 and other.GetSymbol() in _ACYL_PARTNERS for o, other in links) else "C=O"
        # 其余带双键、三键的碳：按下面表里的先后挨个试，先符合哪个就是哪个
        for label, order, element in (("C=S", 2, "S"), ("C#N", 3, "N"), ("C=N", 2, "N"), ("C#C", 3, "C"), ("C=C", 2, "C")):
            if bonded(order, element):
                return label
        return "C"
    if symbol == "N":
        if any(o == 3 for o, _ in links):
            return "N#"
        if any(o == 2 for o, _ in links):
            return "N="
        # 连着写的条件表达式，从左往右读：酰胺那一类的氮 → N.am；否则连着芳环 → N.ar；都不是 → N
        return "N.am" if beside("C", "S", "P") else "N.ar" if beside_aromatic() else "N"
    if symbol == "O":
        if any(o == 2 for o, _ in links):
            return "O="
        negative = atom.GetFormalCharge() < 0
        if negative and any(other.GetFormalCharge() > 0 for _, other in links):
            return "O-"  # 硝基、N-氧化物上的氧，不是去了质子的羟基
        # GetTotalNumHs() 是这个氧上氢的个数：带氢或者带负电荷记 OH，否则记 O
        base = "OH" if negative or atom.GetTotalNumHs() else "O"  # 羧酸根、酚氧负离子和没去质子的算同一种
        # 后缀按 .sulf、.acyl、.ar 的先后取第一个符合的，都不符合就不加
        return base + (".sulf" if beside("S") else ".acyl" if beside("C", "P") else ".ar" if beside_aromatic() else "")
    if symbol == "S":
        if bonded(2, "O"):
            return "S=O"
        return "SH" if atom.GetFormalCharge() < 0 or atom.GetTotalNumHs() else "S"
    # 卤素、硼、磷、硅、金属等不再细分，直接用元素符号
    return symbol


def _outside_type(atom: Chem.Atom, toward: Chem.Atom) -> str:
    """掉下去的（或者新接上来的）基团算哪一类：当作它和 toward 之间的键不存在，再把常见的保护基分开。

    叔丁基和苄基、Boc 和 Cbz 的脱法完全不同，不能算同一类反应；甲基和乙基则不用分。

    参数：
        atom    只在一侧出现的那个基团里，直接连着反应中心的那个原子
        toward  它连着的那个“两边都有”的原子（反应中心上的原子）
    返回一个字符串：
        一般情况  就是 _atom_type(atom) 不算它和 toward 之间那根键的结果：Cl、Br、OH、O.acyl、B、Sn、C=O……
        饱和碳    再细分（见 _alkyl_kind）：C.t、C.het、C.bn、C.al、C
        带 =O、另一头又单键连着一个氧的碳（脱下来的是碳酸酯、氨基甲酸酯的那一半，或者羧基）：
            Boc    那个氧上是叔丁基型的碳        Cbz    那个氧上是苄基型的碳
            CO2H   那个氧上什么都没有（羧基）    CO2R   其他
    例：
        脱 Boc      CC(C)(C)OC(=O)[NH:1]…  氮上掉下去的羰基碳，另一头的氧连着叔丁基 → Boc
        乙酰胺水解  CC(=O)[NH:1]…          氮上掉下去的羰基碳，不算它和氮的键就只剩 =O 和甲基 → C=O
        酰氯成酰胺  Cl[C:9](=O)…           9 号碳上掉下去的氯 → Cl
    谁在用：_center_from_mols 里的 collect()。
    """
    kind = _atom_type(atom, ignore=toward.GetIdx())
    if kind == "C":
        return _alkyl_kind(atom, toward)
    if kind == "C.acyl":  # 另一头还连着杂原子的羰基：氧上是叔丁基的是 Boc，是苄基的是 Cbz
        mol = atom.GetOwningMol()
        # 找这个羰基碳上单键连着的氧（不是 toward，也不是 =O 的那个氧）
        for other in atom.GetNeighbors():
            if other.GetIdx() == toward.GetIdx() or other.GetSymbol() != "O":
                continue
            if mol.GetBondBetweenAtoms(atom.GetIdx(), other.GetIdx()).GetBondTypeAsDouble() != 1:
                continue
            # beyond：这个氧另一头连着的原子。没有的话就是 –C(=O)OH
            beyond = [far for far in other.GetNeighbors() if far.GetIdx() != atom.GetIdx()]
            if not beyond:
                return "CO2H"
            # 氧另一头是饱和碳时看它是哪种烷基；是别的原子（比如另一个羰基）时 tail 是空的，归到 CO2R
            tail = _alkyl_kind(beyond[0], other) if _atom_type(beyond[0], ignore=other.GetIdx()) == "C" else ""
            return "Boc" if tail == "C.t" else "Cbz" if tail == "C.bn" else "CO2R"
    # 其余情况（包括没找到单键氧的 C.acyl，比如另一头连的是氮或氯）：就用 _atom_type 的结果
    return kind


def _alkyl_kind(atom: Chem.Atom, toward: Chem.Atom) -> str:
    """一个饱和碳（烷基）是哪一种：叔丁基型、缩醛型、苄基型、烯丙基型，还是普通烷基。

    参数：atom  这个饱和碳；toward  它连着的那个原子（反应中心那一头，不算在它的取代基里）。
    只看 atom 另外连着的重原子（氢不算），按下面的先后，先符合哪个算哪个：
        C.t    另外正好连着三个原子，而且全是碳：叔丁基、三苯甲基
        C.het  另外连着氧、氮或硫：MOM、THP、SEM 这类缩醛型的保护基
        C.bn   另外连着芳香原子：苄基、对甲氧基苄基
        C.al   另外连着烯碳：烯丙基
        C      其余：甲基、乙基等普通烷基
    例（tests/test_structure.py）：酯水解时从氧上掉下去的烷基，甲酯、乙酯是 C，叔丁酯是 C.t，苄酯是 C.bn，
        所以这三种的反应中心分别是 L:O.acyl~C:1、L:O.acyl~C.t:1、L:O.acyl~C.bn:1，不算同类反应。
    谁在用：_outside_type()。
    """
    # rest：除了 toward 以外，这个碳还连着哪些原子
    rest = [other for other in atom.GetNeighbors() if other.GetIdx() != toward.GetIdx()]
    if len(rest) == 3 and all(other.GetSymbol() == "C" for other in rest):
        return "C.t"  # 叔丁基、三苯甲基
    if any(other.GetSymbol() in ("O", "N", "S") for other in rest):
        return "C.het"  # MOM、THP、SEM 这类缩醛型的
    if any(other.GetIsAromatic() for other in rest):
        return "C.bn"  # 苄基、对甲氧基苄基
    if any(_atom_type(other) == "C=C" for other in rest):
        return "C.al"  # 烯丙基
    return "C"


def _bond_label(bond: Chem.Bond) -> str:
    """一根键在反应中心里怎么写：芳香键写 ar，其余写键级——单键 "1"、双键 "2"、三键 "3"。

    格式 :g 会把 1.0 写成 1（去掉没用的小数点和 0）。
    谁在用：_center_from_mols 里的 collect()。
    """
    return "ar" if bond.GetIsAromatic() else f"{bond.GetBondTypeAsDouble():g}"


def _by_map(mols: list[Chem.Mol]) -> dict[int, tuple[int, Chem.Atom]]:
    """映射号 -> (第几个分子, 原子)。同一个号出现两次（专利数据里有）时取第一次出现的。

    参数：mols  一侧的全部分子：反应物一侧的列表，或者只装着主产物一个的列表。
    返回：字典 {映射号: (这个原子在 mols 的第几个分子里, 原子对象)}。没有映射号的原子（号是 0）不在里面。
    例：对 [NH2:1][CH2:2]c1ccccc1 和 Cl[C:9](=[O:10])[CH3:11] 这两个分子，
        结果是 {1: (0, 氮), 2: (0, 碳), 9: (1, 碳), 10: (1, 氧), 11: (1, 碳)}：氯和苯环没有映射号，不在里面。
    常见的用法是只拿它的键：`号 in _by_map([产物])` 就是“这个号的原子进没进产物”。
    谁在用：_center_from_mols()。
    """
    found: dict[int, tuple[int, Chem.Atom]] = {}
    for index, mol in enumerate(mols):
        for atom in mol.GetAtoms():
            if atom.GetAtomMapNum():
                # setdefault：这个号已经记过就不动，没记过才放进去，所以留下的是第一次出现的那个原子
                found.setdefault(atom.GetAtomMapNum(), (index, atom))
    return found


def _settle_twin_oxygens(mols: list[Chem.Mol], other_side: dict[int, tuple[int, Chem.Atom]]) -> None:
    """羧基、硝基、磺酰基上的两个氧是等价的，映射的时候谁对谁是软件随手定的。

    常见的后果：这一侧的双键氧被对到了另一侧的羟基氧上（或者干脆没对上），单键氧反而对到了另一侧的双键氧。
    照这样算，会凭空多出“一根键变单、一根键变双”，同一种反应就成了两种写法。这里把两个氧的映射号对调回来，
    统一成“留下来的是双键氧”：酯还原成醇，写成羰基氧变成了羟基、烷氧基整个掉下去，而不是反过来。

    参数：
        mols        要修正的这一侧的分子。会直接改它们原子上的映射号（对调两个氧的号）
        other_side  另一侧的 {映射号: (第几个分子, 原子)}，_by_map 的结果，只读不改
    没有返回值。_center_from_mols 调两次：先拿反应物对照产物修，再拿产物对照反应物修。
    做法：对这一侧每一个在另一侧也有的原子（下面叫“中心原子”，多半是羧基的碳），看它连着的氧：
        这一侧  double  双键连着、而且只连着它一个原子的氧（=O）
                single  单键连着的氧（羟基、烷氧基、氧负离子）
        另一侧  同一个中心原子上，各个映射号的氧是单键还是双键（代码里的 orders）
    遇到下面两种情况之一，就把一个 single 氧和一个 double 氧的映射号对调：
        情况 1  这一侧的单键氧在另一侧是双键，而这一侧的双键氧在另一侧不是双键（是单键，或者不在那里）。
                这是两个氧对反了。
        情况 2  这一侧的单键氧在另一侧仍是单键，这一侧的双键氧在另一侧根本没有，
                并且单键氧自己带着的东西（烷基）也不在另一侧。这时统一成“留下来的是双键氧”。
    例 1（情况 1，tests/test_structure.py 的“羧酸成酰胺（两个氧对反了）”）：
        反应物 [OH:10][C:9](=O)[CH3:11]，产物 [O:10]=[C:9]([CH3:11])N…
        10 号在反应物里是羟基氧，在产物里却是羰基氧。对调以后反应物成了 O[C:9](=[O:10])[CH3:11]：
        羰基氧是 10 号，一直没动；没有映射号的羟基掉下去。和正常标法的“羧酸成酰胺”得到同一个结果。
    例 2（情况 2，“酯还原成醇（留下的标成了烷氧基的氧）”）：
        反应物 [C:3](=O)[O:4]CC，产物 [CH2:3][OH:4]
        照原样读是“羰基氧没了，乙氧基的氧留下来成了羟基”；对调以后是“羰基氧（4 号）留下来变成羟基，
        乙氧基整个掉下去”，写出来是 C:C.acyl~O=:2>1|L:C.acyl~O:1。
    谁在用：_center_from_mols()。
    """
    for mol in mols:
        for centre in mol.GetAtoms():
            # mirror：这个原子在另一侧的同号原子，是 (第几个分子, 原子) 这样一对，mirror[1] 才是原子。
            # 没有映射号（号是 0），或者另一侧没有这个号，都查不到，跳过
            mirror = other_side.get(centre.GetAtomMapNum())
            if mirror is None:
                continue
            # 另一侧：同号原子连着的每个氧，记成 {氧的映射号: 键级}
            orders = {
                bond.GetOtherAtom(mirror[1]).GetAtomMapNum(): bond.GetBondTypeAsDouble()
                for bond in mirror[1].GetBonds()
                if bond.GetOtherAtom(mirror[1]).GetSymbol() == "O"
            }
            # 没有映射号的氧都挤在 0 这个键上，没法对应，去掉
            orders.pop(0, None)
            # 这一侧：把连着的氧分成 double、single 两堆
            double, single = [], []
            for bond in centre.GetBonds():
                oxygen = bond.GetOtherAtom(centre)
                if oxygen.GetSymbol() != "O":
                    continue
                # GetDegree() == 1：这个氧只连着 centre 一个原子
                if bond.GetBondTypeAsDouble() == 2 and oxygen.GetDegree() == 1:
                    double.append(oxygen)
                elif bond.GetBondTypeAsDouble() == 1:
                    single.append(oxygen)
            for low in single:
                # there：这个单键氧（按它的映射号）在另一侧是什么键级；None 是另一侧的同号原子上没有它
                there = orders.get(low.GetAtomMapNum())
                if there is None:
                    continue
                # 这个单键氧在另一侧还是单键时，只有它自己带着的东西都不在另一侧了（羟基，或者烷氧基的烷基掉了）才对调
                alone = all(
                    other.GetAtomMapNum() not in other_side for other in low.GetNeighbors() if other.GetIdx() != centre.GetIdx()
                )
                for high in double:
                    twin = orders.get(high.GetAtomMapNum())  # None：另一侧根本没有这个双键氧
                    # or 前面是文档里说的情况 1，后面是情况 2
                    if (there == 2 and twin != 2) or (there == 1 and twin is None and alone):
                        # 对调两个氧的映射号。这个双键氧用过了，从 double 里拿掉，然后去看下一个单键氧
                        number = high.GetAtomMapNum()
                        high.SetAtomMapNum(low.GetAtomMapNum())
                        low.SetAtomMapNum(number)
                        double.remove(high)
                        break


def _outward_from_first(mol: Chem.Mol, wanted) -> list[Chem.Atom]:
    """从第一个映射号在 wanted 里的原子出发，沿着键一圈一圈往外走，给出原子的先后次序。

    对称的试剂（Boc2O、乙酸酐）在专利数据里常常两半标着同一套映射号。按这个次序取“先遇到的那个”，
    取到的原子都连在一起、属于同一半；按书写顺序取的话会一半取一个，算出一堆并不存在的断键和成键。

    参数：
        mol     一个反应物分子
        wanted  产物里有的映射号。只用来做 in 判断，这里传进来的是 _by_map([产物]) 那个字典
    返回：mol 的全部原子，每个出现一次，离起点近的排在前面（广度优先的次序）。
          mol 里没有一个原子的号在 wanted 里时，从 0 号原子开始走；和起点不相连的部分排在最后。
    例（tests/test_structure.py 的“上 Boc（两半同号）”）：Boc2O 被写成
        [C:17](O[C:17](…)=[O:18])(…)=[O:18]，两个羰基碳都标着 17，两个羰基氧都标着 18，其余类推。
        从第一个 17 号碳往外走，一步就到的 18、19 号氧和再往外的叔丁基，都是它这一半的；
        另一半的 17 号碳要先经过中间那个没有映射号的氧，排在后面。
        于是“每个号取先遇到的那个原子”取到的全在同一半。
    谁在用：_center_from_mols()。
    """
    order: list[Chem.Atom] = []
    seen: set[int] = set()
    # 号在 wanted 里的原子的编号，按书写顺序
    starts = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomMapNum() in wanted]
    # 起点的候选：先是第一个这样的原子（starts[:1]，没有的话是空列表），后面跟着全部原子。
    # 跟着全部原子是为了把和起点不相连的部分也走到；已经走过的在下面跳过
    for start in starts[:1] + [atom.GetIdx() for atom in mol.GetAtoms()]:
        if start in seen:
            continue
        seen.add(start)
        # 广度优先：用队列，先进先出。deque 是两头都能快速进出的队列，popleft() 从左边取出一个
        queue = deque([start])
        while queue:
            atom = mol.GetAtomWithIdx(queue.popleft())
            order.append(atom)
            for neighbour in atom.GetNeighbors():
                if neighbour.GetIdx() not in seen:
                    seen.add(neighbour.GetIdx())
                    queue.append(neighbour.GetIdx())
    return order


def _center_from_mols(reactants: list[Chem.Mol], product: Chem.Mol) -> str | None:
    """按原子映射算一步反应的反应中心。reaction_center 和 retro_reaction_center 算的都是它。

    参数：
        reactants  反应物一侧的分子，原子上带着映射号。会被改动：_settle_twin_oxygens 可能对调氧的映射号
        product    主产物分子，同样带映射号，同样可能被改动
    返回：
        反应中心的文字，比如 "F:C.acyl~N:1|L:C.acyl~Cl:1"。写法见 reaction_center 的说明和本段开头的例子。
        返回 None 的两种情况：
            映射太不全：两边都有的原子不到 2 个，或者不到产物重原子数的一半
            对照下来什么变化都没有（比如成盐：胺加盐酸变成铵盐，键没有变）
    步骤：
        1. 修正两个等价氧的映射（_settle_twin_oxygens），反应物、产物各修一次。
        2. 找出“两边都有的原子”，记在 kept 里：映射号在产物里也有的反应物原子。
           同一个号出现多次时取先遇到的（按 _outward_from_first 的次序）；
           孤零零一个、身边没有别的 kept 原子的不算，它按“新接上来的”处理（原因见下面那段注释）。
        3. 映射太不全就放弃。
        4. 对反应物、产物各做一遍 collect()，把键分成两类：
               两头都是 kept 原子的键       inside   {(小号, 大号): 键的写法}
               只有一头是 kept 原子的键     outside  {(kept 原子的号, 另一头那个基团的类别, 键的写法): 有几根}
        5. 两边的 inside 对照：只在产物里有 → F；只在反应物里有 → B；两边都有但写法不同 → C。
           两边的 outside 对照：反应物里多出来的 → L；产物里多出来的 → N。
        6. 排序，同样的变化合并写成 “个数*变化”，用 | 连起来。
    原子的类别一律按它在反应物里的样子算（下面的 types）。所以酰氯和胺成酰胺写的是 C.acyl~N（胺的氮），
    不是产物里的 N.am。
    谁在用：parse_mapped_reaction（专利数据，写法是 反应物>试剂>产物）；
            retro_reaction_center（逆合成引擎，写法是 产物>>反应物）。
    """
    # 第 1 步。第二次调用时 _by_map(reactants) 是现算的，用的是反应物修正以后的映射号
    _settle_twin_oxygens(reactants, _by_map([product]))
    _settle_twin_oxygens([product], _by_map(reactants))
    # 产物里有哪些映射号（只用它的键）
    in_product = _by_map([product])

    # 第 2 步
    kept: dict[int, Chem.Atom] = {}  # 两边都有的原子：映射号 -> 它在反应物里的样子
    places: dict[tuple[int, int], int] = {}  # (第几个反应物, 原子编号) -> 映射号
    for index, mol in enumerate(reactants):
        # own：这个反应物里，号在产物里也有、并且前面的反应物还没占用这个号的原子；每个号只留先遇到的那个
        own: dict[int, Chem.Atom] = {}
        for atom in _outward_from_first(mol, in_product):
            number = atom.GetAtomMapNum()
            if number in in_product and number not in kept:
                own.setdefault(number, atom)
        # 身边没有别的“两边都有的原子”、孤零零一个进了产物的原子，按“新接上来的”算：
        # 试剂给出的一个原子（溴、氯化亚砜的氯、碘甲烷的碳、水的氧），或者从要掉下去的基团里单独对上产物的一个原子。
        # 这样不管数据里试剂的原子标没标映射、是从哪个试剂上来的，同一种转化得到的都是同一串文字
        # 例：NBS 溴代里的 [Br:7]N1C(=O)CCC1=O，只有溴进了产物，它的邻居（氮）不在 own 里，所以溴不进 kept；
        # 到了产物那一侧，这个溴就成了“连在 kept 原子上的、只在这一侧出现的原子”，记成 N:c~Br:1。
        # together：own 里这些原子的编号，用来判断“邻居是不是也在 own 里”
        together = {atom.GetIdx() for atom in own.values()}
        for number, atom in own.items():
            if any(neighbour.GetIdx() in together for neighbour in atom.GetNeighbors()):
                kept[number] = atom
                places[(index, atom.GetIdx())] = number
    # 第 3 步
    # 映射太不全（产物里一半以上的原子对不上来源）就不算了，免得算出一个似是而非的反应中心
    if len(kept) < 2 or len(kept) * 2 < product.GetNumHeavyAtoms():
        return None
    # 产物一侧的同一张表：(第几个分子, 原子编号) -> 映射号。产物只有一个分子，所以“第几个”都是 0
    product_places = {(0, atom.GetIdx()): number for number, (_, atom) in in_product.items() if number in kept}

    # 第 4 步
    def collect(mols: list[Chem.Mol], where: dict[tuple[int, int], int]):
        """把一侧的全部键分成 inside、outside 两类，返回 (inside, outside)。

        参数：mols  这一侧的分子；where  这一侧的 {(第几个分子, 原子编号): 映射号}，只含 kept 原子。
        两头都不是 kept 原子的键（比如试剂内部的键、离去基团内部的键）不管。
        """
        inside: dict[tuple[int, int], str] = {}  # 两头都是“两边都有的原子”的键
        outside: Counter[tuple[int, str, str]] = Counter()  # 一头连着只在这一侧出现的原子：(映射号, 那个原子的类别, 键级)
        for index, mol in enumerate(mols):
            for bond in mol.GetBonds():
                first, second = bond.GetBeginAtom(), bond.GetEndAtom()
                # a、b：这根键两头的原子的映射号；不是 kept 原子时是 None
                a, b = where.get((index, first.GetIdx())), where.get((index, second.GetIdx()))
                if a is not None and b is not None:
                    # 小号在前、大号在后，这样同一对原子在两侧是同一个键
                    inside[(min(a, b), max(a, b))] = _bond_label(bond)
                elif a is not None:
                    outside[(a, _outside_type(second, first), _bond_label(bond))] += 1
                elif b is not None:
                    outside[(b, _outside_type(first, second), _bond_label(bond))] += 1
        return inside, outside

    before, leaving = collect(reactants, places)
    after, arriving = collect([product], product_places)
    # 每个 kept 原子的类别，按它在反应物里的样子
    types = {number: _atom_type(atom) for number, atom in kept.items()}
    # 第 5 步。changes：{一处变化的文字: 有几处}
    changes: Counter[str] = Counter()
    # 两侧出现过的原子对都过一遍（| 是两个集合的并集）
    for pair in before.keys() | after.keys():
        old, new = before.get(pair), after.get(pair)
        if old == new:
            continue
        # 两头的原子类别先按字母顺序排好再用 ~ 连，免得 N~C.acyl 和 C.acyl~N 成了两种写法
        ends = "~".join(sorted(types[number] for number in pair))
        # 三选一：反应物里没有这根键 → F；产物里没有了 → B；两边都有、写法不同 → C，写成 “旧>新”
        changes[f"F:{ends}:{new}" if old is None else f"B:{ends}:{old}" if new is None else f"C:{ends}:{old}>{new}"] += 1
    # 同一个原子上“掉了一个某类基团、又多出一个一样的”，多半是那个基团只是没标映射，两边抵消
    # Counter 相减只留下结果是正数的项：leaving - arriving 就是“反应物一侧比产物一侧多出来的”
    for (number, other, order), count in (leaving - arriving).items():
        changes[f"L:{types[number]}~{other}:{order}"] += count
    for (number, other, order), count in (arriving - leaving).items():
        changes[f"N:{types[number]}~{other}:{order}"] += count
    # 第 6 步。一处变化都没有时 join 的结果是空字符串，or None 把它换成 None
    return "|".join(f"{count}*{item}" if count > 1 else item for item, count in sorted(changes.items())) or None


def reaction_center(mapped_reaction: str) -> str | None:
    """一步反应到底改了哪些键，写成一串可以直接比较的文字。输入是带原子映射的 “反应物>试剂>产物”。

    输出形如 “F:C.acyl~N:1|L:C.acyl~Cl:1”，每一项是一处变化（同样的变化有几处，前面写 “2*”）：
        F  两个原有的原子之间成了一根新键（这里是酰基碳–氮的单键）
        B  两个原有的原子之间断了一根键（开环、裂解）
        C  键级变了（C:C=O~O=:2>1 是羰基还原）
        L  掉下去一个基团（这里是酰基碳上的氯；L:N.am~Boc:1 是脱 Boc）
        N  接上了一个反应物里没有、或者由试剂给出的原子（N:c~Br:1 是芳环溴代）
    两步反应的这串文字一样，就是同一种转化，和分子上别的部分无关。
    没有映射、映射太不全、或者没有任何键发生变化时返回 None。

    参数：mapped_reaction  带原子映射的 “反应物>试剂>产物”（USPTO 专利数据的写法）。
    每一项的格式是 “类别:原子类型~原子类型:键”：
        原子类型  留在分子上的原子见 _atom_type；掉下去的、新接上来的基团见 _outside_type
        顺序      F、B、C 的两个原子类型按字母顺序排；L、N 是前一个留在分子上，后一个是掉下去的或者新来的
        键        1、2、3（单键、双键、三键）或 ar（芳香键）；C 类写成 “旧>新”
    更多例子（都在 tests/test_structure.py 里，名字是测试里用的名字）：
        酮还原                     C:C=O~O=:2>1                   羰基的双键变成单键，没有成键断键
        Suzuki（溴代物和硼酸）     F:c~c:1|L:c~B:1|L:c~Br:1       两个芳环碳成键，一边掉了硼、一边掉了溴
        甲酯水解                   L:O.acyl~C:1                   酯氧上掉了一个普通烷基
        酯还原成醇                 C:C.acyl~O=:2>1|L:C.acyl~O:1   羰基变成单键，烷氧基掉下去
        溴代（NBS）                N:c~Br:1                       芳环碳上接了一个新来的溴
        酚的甲基化（碘甲烷）       N:OH.ar~C:1                    甲基只有一个碳，也按“新接上来的”记
        上 Boc                     F:C.acyl~N:1|L:C.acyl~O.acyl:1
    这些文字存在两个地方：先例库 reaction_precedents 表的 center 列；Analysis.result 里每一步的 center 字段
    （证据卡把它原样给前端）。比较之前还要过一遍 center_class。
    谁在用：现在只有测试直接调它。导入先例库走 parse_mapped_reaction（算法相同），
            逆合成引擎给的步骤走 retro_reaction_center。
    """
    parsed = parse_mapped_reaction(mapped_reaction)
    return parsed[3] if parsed else None


# 下面四张表是 center_class 用的归并规则，是凭化学常识定的，可以改；改完重启服务就生效，不用重新导先例库。
# README“几处和你预想的可能不一样的地方”里有一张“算同一类 / 不算同一类”的对照表，说的就是这些规则。
#
# _ALIASES：比较时不区分“芳环上的”和“烷基上的”。酚的氧、芳基醚的氧并入 OH、O，苯胺的氮并入 N。
# （N.am 不在里面，酰胺的氮和胺的氮不算一类；OH.acyl 也不在里面，羧酸的羟基和醇的羟基不算一类。）
_ALIASES = {"O.ar": "O", "OH.ar": "OH", "N.ar": "N"}
# _LEAVING_ALIASES：只用在“掉下去的基团”上。在上面的基础上，氯、溴、碘和磺酸酯的氧（O.sulf，
# 甲磺酸酯、对甲苯磺酸酯、三氟甲磺酸酯都是）统一写成 X，算同一种离去基团。氟不在里面。
_LEAVING_ALIASES = {**_ALIASES, "Cl": "X", "Br": "X", "I": "X", "O.sulf": "X"}
# 同一件事的两种写法，统一成一种：下面两张表都是把“键”那种写法换成“值”那种
# _SAME_CHANGE 改写单独一处变化：键是归并以后的一项，值是它要换成的写法
_SAME_CHANGE = {"L:c~O:2": "L:c~OH:1"}  # 吡啶酮和羟基吡啶是同一个东西的两种画法
# _SAME_CLASS 改写整个反应中心：键是归并以后的整串文字，值是它要换成的那一串
_SAME_CLASS = {
    # 酸和醇成酯：有的数据把它标成“醇的 C–O 断了、酸的氧接上去”，有的标成“酸的羟基掉了、醇的氧接上去”
    "F:C~OH.acyl:1|L:C~OH:1": "F:C.acyl~OH:1|L:C.acyl~OH:1",
    # 甲基化。甲基只有一个碳，按上面的规矩记成“接上了一个碳”，不看甲基是谁给的
    # （专利数据里甲基碳常被对到碳酸钾、丙酮、溶剂上，看了反而乱）。下面这几种其实就是和卤代烷、硫酸酯的烷基化，
    # 和乙基化、苄基化归成一类。胺的 N-甲基化（可能是甲醛还原胺化）、羧酸成甲酯（可能是和甲醇酯化）做法不止一种，不归。
    "N:OH~C:1": "F:C~OH:1|L:C~X:1",
    "N:SH~C:1": "F:C~SH:1|L:C~X:1",
    "N:n~C:1": "F:C~n:1|L:C~X:1",
    "N:N.am~C:1": "F:C~N.am:1|L:C~X:1",
    "N:C~C:1": "F:C~C:1|L:C~X:1",
}


def _core_change(change: str) -> str:
    """核心里把“接上一个甲基”和“两个原有的原子之间成键”看成一回事：甲基加成和别的烷基加成是相关反应。

    参数：change  归并以后的一处变化，比如 "N:C=O~C:1"。
    返回：如果它是“接上了一个碳”（N 类，新来的是 C），改写成 F 类，并把两头按字母顺序排好；其他的原样返回。
    例：_core_change("N:C=O~C:1") 是 "F:C~C=O:1"；_core_change("N:c~Br:1") 还是 "N:c~Br:1"。
        于是甲基格氏试剂加成（反应中心里是 N:C=O~C:1）和乙基格氏试剂加成（F:C~C=O:1）的核心相同。
    谁在用：center_class()。
    """
    # 最多切两刀：类别、两头、键。两头再按 ~ 分成前后两个原子类型
    kind, ends, order = change.split(":", 2)
    first, second = ends.split("~")
    return f"F:{'~'.join(sorted((first, second)))}:{order}" if kind == "N" and second == "C" else change


@lru_cache(maxsize=4096)
def center_class(center: str) -> tuple[str, str]:
    """把反应中心归成比较用的两把钥匙：(同类, 核心)。

    同类：芳环上的和烷基上的羟基、氨基不分；离去的氯、溴、碘、磺酸酯算同一种离去基团。
          两步反应这把钥匙一样，才算同类反应。
    核心：只看成了哪些键、接上了什么、哪些键的键级变了，不看掉下去的是什么，也不数有几处。
          核心一样而“同类”不一样，是做法不同的相关反应（先例用酰氯，这一步用羧酸）。
          只有断键的反应（脱保护），核心是“从哪类原子上脱掉了东西”，不管脱掉的是哪种保护基。
    库里存的是没归并过的原样，所以这里的归并规则改了以后不用重新导数据。

    参数：center  reaction_center / retro_reaction_center 给出的文字（先例库 center 列里存的也是它）。
    返回：(同类, 核心) 两个字符串。M5 的用法：
        两步的“同类”相同                  → 同类反应，可以当这一步的先例
        “同类”不同、“核心”相同            → 相关反应，只当很弱的旁证
        两把钥匙都不同                    → 不相干，不列
    例（tests/test_structure.py）：
        酰氯成酰胺   F:C.acyl~N:1|L:C.acyl~Cl:1     → ("F:C.acyl~N:1|L:C.acyl~X:1", "F:C.acyl~N:1")
        酰溴和苯胺   F:C.acyl~N.ar:1|L:C.acyl~Br:1  → 同上。N.ar 并入 N，Br 并入 X，所以和上一个是同类反应
        羧酸成酰胺   F:C.acyl~N:1|L:C.acyl~OH:1     → ("F:C.acyl~N:1|L:C.acyl~OH:1", "F:C.acyl~N:1")
                     “同类”和酰氯的不一样，核心一样：相关反应
        脱 Boc       L:N.am~Boc:1                   → ("L:N.am~Boc:1", "L:N.am~*")
        脱 Cbz       L:N.am~Cbz:1                   → ("L:N.am~Cbz:1", "L:N.am~*")，和脱 Boc 是相关反应
        酚的甲基化   N:OH.ar~C:1                    → ("F:C~OH:1|L:C~X:1", "F:C~OH:1")，和碘乙烷的乙基化同类
    center 的写法不对（不是这个版本的格式）时抛 ValueError；
    m05_precedents.PrecedentIndex.refresh 接住它，把那条先例当作没有反应中心。
    带缓存（lru_cache，最多记 4096 种不同的 center）。
    谁在用：m05_precedents.PrecedentIndex.refresh（加载索引时给每条先例算两把钥匙）；
            m05_precedents.PrecedentSearch._class_rows（给这一步算两把钥匙，再到索引里找相同的）。
    """
    # items：{归并以后的一处变化: 有几处}
    items: Counter[str] = Counter()
    for piece in center.split("|"):
        # 一段前面可能带个数，比如 “2*F:c~c:1”。rpartition("*") 从右边找 *，切成 (前面, "*", 后面) 三份；
        # 没有 * 时前面两份是空字符串，个数在下面按 1 算
        count, _, item = piece.rpartition("*")
        kind, ends, order = item.split(":", 2)
        first, second = ends.split("~")
        # kind in "LN"：kind 是 L 或者 N（判断一个字符在不在这个字符串里）
        if kind in "LN":  # 有方向：前一个是留下来的原子，后一个是掉下去的（或者新接上来的）
            # 留下来的原子套 _ALIASES；后一个原子，L 套 _LEAVING_ALIASES，N 套 _ALIASES。表里没有的原样不动
            ends = f"{_ALIASES.get(first, first)}~{(_LEAVING_ALIASES if kind == 'L' else _ALIASES).get(second, second)}"
        else:
            # F、B、C 没有方向：两头各自归并以后，重新按字母顺序排
            ends = "~".join(sorted(_ALIASES.get(name, name) for name in (first, second)))
        change = f"{kind}:{ends}:{order}"
        items[_SAME_CHANGE.get(change, change)] += int(count or 1)
    # “同类”这把钥匙：归并后的各项排好序再拼起来，格式和 reaction_center 的一样
    whole = "|".join(f"{count}*{item}" if count > 1 else item for item, count in sorted(items.items()))
    if whole in _SAME_CLASS:
        # 这一整串是某种反应的另一种标法：换成表里对应的那一串，重新归并一遍
        return center_class(_SAME_CLASS[whole])
    # “核心”这把钥匙：只留成键（F）、键级变化（C）、新接上来的（N），不要个数，用集合去重
    made = sorted({_core_change(item) for item in items if item[0] in "FCN"})
    if not made:
        # 一处成键都没有，只有 L 或 B：脱保护、裂解这一类。
        # L 只留 ~ 前面的部分（从哪类原子上掉的）再接上 ~*，掉的是什么、键级都不要；B 原样留着
        made = sorted({item if item[0] == "B" else item.split("~")[0] + "~*" for item in items})
    return whole, "|".join(made)


def retro_reaction_center(mapped_retro_reaction: str) -> str | None:
    """同上，输入是逆合成引擎给的 “产物>>反应物”（AiZynthFinder 的 mapped_reaction_smiles）。

    “同上”指 reaction_center：算的是同一种反应中心，用的是同一个 _center_from_mols，
    差别只是这里的写法方向反着（产物在前），而且没有试剂那一栏。
    参数：mapped_retro_reaction  “产物>>反应物1.反应物2”，引擎给的每个原子都带映射号。
    返回：反应中心的文字；没有反应物、有读不出来的分子、映射太不全、没有任何变化时返回 None。
    例（tests/test_integrations.py：AiZynthFinder 4.4.1 给“水杨酸 + 乙酸酐 → 阿司匹林”这一步的真实输出）。
        引擎给的是一行 “阿司匹林>>乙酸酐.水杨酸”，三个分子分别是：
            阿司匹林  [CH3:1][C:2](=[O:3])[O:4][c:5]1[cH:6][cH:7][cH:8][cH:9][c:10]1[C:11](=[O:12])[OH:13]
            乙酸酐    [CH3:1][C:2](=[O:3])[O:14][C:15]([CH3:16])=[O:17]
            水杨酸    [OH:4][c:5]1[cH:6][cH:7][cH:8][cH:9][c:10]1[C:11](=[O:12])[OH:13]
        → "F:C.acyl~OH.ar:1|L:C.acyl~O.acyl:1"：酚羟基的氧（4 号）接到酰基碳（2 号）上，
          酰基碳上原来连着的乙酸根（14 号氧那一半，没进产物）掉下去。
    谁在用：routes._Builder._reaction。整理引擎返回的路线时给每一步算 center，
            存进 Analysis.result 的 reactions 表；之后 M5（PrecedentSearch.find）拿它去和先例比。
    """
    # 去掉空格后面的扩展，再按 >> 切成 (产物, ">>", 反应物) 三份
    product_side, _, reactant_side = mapped_retro_reaction.strip().split(" ")[0].partition(">>")
    # 和 parse_mapped_reaction 一样新建分子，不走缓存（后面会改映射号）
    reactants = [Chem.MolFromSmiles(token) for token in reactant_side.split(".") if token]
    product = Chem.MolFromSmiles(product_side) if product_side else None
    if not reactants or product is None or any(mol is None for mol in reactants):
        return None
    return _center_from_mols(reactants, product)


# ---------------------------------------------------------------- 反应的编号和图


def build_reaction_smiles(reactants: list[str], product: str, agents: list[str] | None = None) -> str:
    """把反应物、试剂、产物拼成一行反应 SMILES：“反应物1.反应物2>试剂1.试剂2>产物”。

    只是拼字符串，不检查结构。没有试剂（agents 是 None 或空列表）时中间那一栏是空的。
    例：build_reaction_smiles(["CC(=O)O", "CO"], "COC(C)=O", ["O=S(=O)(O)O"]) 是
        "CC(=O)O.CO>O=S(=O)(O)O>COC(C)=O"；不给试剂是 "CC(=O)O.CO>>COC(C)=O"。
    谁在用：routes._Builder._reaction（Analysis.result 里每一步的 reaction_smiles）；
            pipeline.AnalysisRunner._adopt_precedent_reagents（借用先例的试剂以后重写这一步的 reaction_smiles）；
            m05_precedents.precedent_values（先例库里存的 reaction_smiles）；本文件的 _reaction。
    """
    return f"{'.'.join(reactants)}>{'.'.join(agents or [])}>{product}"


@lru_cache(maxsize=50000)
def main_fragment_key14(smiles: str) -> str | None:
    """盐或多组分物质取最大的那个片段，这样“盐酸羟胺”和“羟胺”在比对反应时算同一个反应物。

    参数：smiles  一个物质，可以是用 “.” 连起来的多组分。
    返回：重原子最多的那个片段的 key14；只有一个部分时就是它自己的 key14；读不出来返回 None。带缓存。
    例：main_fragment_key14("NO.Cl")（盐酸羟胺）和 key14_of("NO")（羟胺）相同，都是 AVXURJPOCDRRFD；
        而 key14_of("NO.Cl") 是另一个值 WTDHULULXKLSOZ。
    谁在用：reaction_key；m05_precedents.precedent_values（先例库的 product_key 列）。
    """
    mol = to_mol(smiles)
    if mol is None:
        return None
    fragments = Chem.GetMolFrags(mol, asMols=True)
    if len(fragments) > 1:
        mol = max(fragments, key=lambda frag: frag.GetNumHeavyAtoms())
    return inchikey_of(mol)[:14]


def reaction_key(reactants: list[str], product: str, *, distinct: bool = False) -> str | None:
    """同一批反应物得到同一个产物，就认为是“同一反应”。不看试剂，也不分立体异构。

    参数：reactants  反应物的 SMILES 列表；product  产物的 SMILES。
          distinct   True 时同一种反应物只算一次：[X, X, Y] 和 [X, Y] 得到同一个编号。
                     专利数据不写化学计量，用两份 X 的反应（2X + Y → Z）在记录里常常只写一次 X；
                     M5 找“同一反应”时用这个写法再查一遍（m05_precedents.PrecedentSearch.find）。
                     默认 False：反应物写了几次就算几次，和先例库里存的编号一致。
    返回：40 个字符的十六进制字符串（SHA-1 哈希）；有读不出来的结构时返回 None。
    算法：每个反应物取最大片段的 key14，排好序用 “.” 连起来，后面接 “>>” 和产物的 key14，对这串文字取 SHA-1。
          排序使得反应物写的先后不影响结果；取最大片段使得带不带抗衡离子不影响结果；
          用 key14 使得立体异构体、同一个分子的不同写法不影响结果。
    例（tests/test_structure.py）：醛 + 盐酸羟胺（NO.Cl）→ 肟，和 羟胺（NO）+ 醛 → 肟，编号相同；
        少写一个反应物（只有醛 → 肟）编号就不同。
    谁在用：routes._Builder._reaction（Analysis.result 里每一步的 rxn_key；步骤编号 step_key 也是由它再算出来的）；
            m05_precedents.precedent_values（先例库的 reaction_key 列）；
            m05_precedents.PrecedentSearch.find（证据等级 E1“文献直接报道”就是按这个编号到库里查）。
    """
    keys = [main_fragment_key14(s) for s in reactants]
    product_key = main_fragment_key14(product)
    if product_key is None or any(k is None for k in keys):
        return None
    if distinct:
        # dict.fromkeys 去掉重复（下面反正要排序，先后无所谓）
        keys = list(dict.fromkeys(keys))
    text = ".".join(sorted(keys)) + ">>" + product_key
    return hashlib.sha1(text.encode("ascii")).hexdigest()


def _reaction(reactants: list[str], product: str):
    """把一步反应建成 RDKit 的反应对象，供画图和导出 RXN 文件用。只有反应物和产物，不带试剂。

    useSmiles=True 是让 RDKit 把这行文字当作具体的分子（反应 SMILES）来读；不加的话它会当成 SMARTS 模板。
    谁在用：reaction_rxn_block、reaction_to_png。
    """
    from rdkit.Chem import rdChemReactions

    return rdChemReactions.ReactionFromSmarts(build_reaction_smiles(reactants, product), useSmiles=True)


def reaction_rxn_block(reactants: list[str], product: str) -> str | None:
    """一步反应的 RXN 文件内容（MDL 的反应文件格式，文本，以 $RXN 开头）。只含反应物和产物。

    返回文本；_reaction 给出的是 None 时返回 None。
    谁在用：m13_reports.export_structures_zip（F2 选 sdf 格式时，zip 里每一步一个 step_N.rxn）。
    """
    from rdkit.Chem import rdChemReactions

    reaction = _reaction(reactants, product)
    return rdChemReactions.ReactionToRxnBlock(reaction) if reaction is not None else None


def reaction_to_png(reactants: list[str], product: str, width: int = 720, height: int = 220) -> bytes | None:
    """报告里的反应式图。画不出来时退回只画产物。

    参数：reactants、product  SMILES；width、height  图的宽和高（像素）。
    返回：PNG 图片的字节。画反应式时出了任何异常，就改为返回产物的结构图（mol_to_png）；
          产物也读不出来时返回 None。
    谁在用：m13_reports.build_blocks（报告里每一步的反应式）。
    """
    try:
        # 位图画布；DrawReaction 把“反应物 → 产物”画成一行
        drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
        drawer.DrawReaction(_reaction(reactants, product))
        drawer.FinishDrawing()
        return drawer.GetDrawingText()
    except Exception:  # noqa: BLE001 - 图只是辅助，画不出来不该让报告生成失败
        return mol_to_png(product, width, height)
