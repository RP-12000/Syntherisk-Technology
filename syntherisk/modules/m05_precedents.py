"""M5 文献先例：给每一步找相同或最相似的已报道反应，定证据等级。

    E1 文献直接报道 ★★★   同一批反应物得到同一个产物
    E2 高相似先例   ★★☆   同类反应，底物相似度 ≥ 0.7
    E3 低相似先例   ★☆☆   同类反应，但底物差异较大
    E4 仅模型预测   ☆☆☆   找不到可参考的先例

“是不是同类反应”有两种判断办法：

    按反应中心（准）  这一步和先例都带原子映射时用。比的是到底改了哪些键（m01_structure.reaction_center）：
                      一模一样才算同类反应；成的键一样、掉下去的基团不一样（先例用酰氯，这一步用羧酸）算相关反应。
                      AiZynthFinder 给的每一步都带映射，USPTO 先例也带，所以正式使用时走的是这一种。
    按指纹（粗）      这一步没有映射（演示路线、手工给的路线）时用。
                      比的是原子类型净增净减了什么，分粗筛、细比两步，阈值在 data/scoring.toml。
                      指纹分不清“产物相近、成的键不同”的反应（酰胺的 N-芳基化和苯胺的酰化在它看来是一回事）。
                      所以这一步有映射、先例却没有的时候，按指纹找到的先例再像也只当旁证，不算同类反应。

“底物像不像”两种办法都用产物的结构指纹判断。
先例库是空的时候不评等级，也不给文献分，而不是把所有步骤都标成 E4。

---- 在整个系统里的位置 ----

对应 PPT 的模块 M5，是一次分析的第 4 阶段“文献先例”（pipeline.AnalysisRunner._precedents）：
对结果里的每一步调一次 PrecedentSearch.find()，把返回的字典存成 result["reactions"][步骤]["evidence"]，
其中的产率单独存成 result["reactions"][步骤]["yield"]。之后谁读它：
    流水线  等级是 E1 / E2 时，借用排第一的那条先例的试剂（_adopt_precedent_reagents）和条件、温度（M7）
    M10     literature_score 是“文献依据”这一维的分数；等级和相似度决定这一步的置信度；
            复杂度档位（A7）限制一条路线里 E4 步骤的个数；高级参数（A13）的“最低证据等级”也看它
    界面    R6 证据卡上的等级和星数；C1 文献列表、C2 相似先例
            （GET /api/analyses/{id}/routes/{route_id}/steps/{n}/precedents 读的就是这里存下的先例列表）

先例库本身（reaction_precedents 表）不是这个文件写进去的：python -m syntherisk.cli load-precedents
（loaders.load_precedents）读 USPTO 的文件，用这里的 precedent_values() 把每条反应算成一行再批量插入，
导完调 mark_library_changed() 换版本号。这个文件自己只写 library_versions 表。

依赖：m01_structure（指纹、反应编号、反应中心——化学计算都在那边）、models（ReactionPrecedent、
LibraryVersion 两张表）、reference（scoring.toml 的 [evidence] 一节、data_sources.toml 里的来源名称）。
不联网，不调大模型。

---- 先要知道的几个概念 ----

    同一反应      反应物和产物都一样（不看试剂，不分立体异构）。两边各算一个反应编号 reaction_key，相等就是。
    底物相似度    两个产物的结构指纹的 Tanimoto 相似度，0 到 1。结构指纹是 2048 位的 RDKit 路径指纹。
    Tanimoto      两个指纹里“都是 1 的位数”÷“至少一个是 1 的位数”。完全一样是 1，毫不相干是 0。
    反应中心      一步反应改了哪些键，写成一串文字，比如 F:C.acyl~N:1|L:C.acyl~Cl:1
                  （酰基碳和氮之间成了一根单键，酰基碳上掉了一个氯）。要有原子映射才算得出来。
    两把钥匙      chem.center_class(反应中心) 把它归并成 (同类钥匙, 核心钥匙)：
                  同类钥匙一样 -> 同类反应；只有核心钥匙一样（成的键一样，掉下去的基团不一样）-> 相关反应。
    反应类别指纹  不靠原子映射的粗办法：看从反应物到产物，哪些原子类型净增了、哪些净减了，
                  每种情况占一位，折叠成 1024 位（粗筛用）；细比时再数着“各增减了几个”来比。
    同类 / 相关   同类反应的先例才能把这一步定到 E2，或者 E3 的正常分数段，产率也只借同类的；
                  相关反应只当旁证，最多给 E3 分数段最下面的一小段。

---- 一步反应是怎么定级的（PrecedentSearch.find）----

    1. 先例库是空的：不评，等级和分数都是 None。
    2. 按反应编号查“同一反应”（_exact_rows）。查到 -> E1。
    3. 找相似先例：
         这一步有反应中心、库里也有带反应中心的先例 -> 按反应中心比（_class_rows）；
             另外把库里没带映射的那部分先例按指纹补查一遍，查到的只当旁证。
         否则 -> 整步按指纹比（_similar_rows）。反应物明显没给全时不找。
    4. 定级（_grade）：同类先例里最高的底物相似度 ≥ 0.7 -> E2；≥ 0.2 -> E3；
       只有相关反应 -> E3 的最低一段；什么都没有 -> E4（分数看逆合成模型给的概率）。
    5. 产率（_yield_info）：只取 E1 的同一反应、E2 的高相似同类先例报道的值。

---- 内存索引（PrecedentIndex）----

    第 3 步要拿这一步和库里的先例一条一条地比。USPTO 全量有 175 万条，所以把比较时要用的东西
    （两种指纹、反应中心的编号）一次读进内存，摆成 numpy 数组，每个进程一份。
    实测全量加载约 13 秒、占内存约 700 MB；之后按反应中心检索一步约 0.1 秒（整步按指纹约 0.9 秒）。
    先例库重新导入以后，下一次检索时会自动重新加载。

---- 本文件里有什么（按阅读顺序）----

    CLASS_BYTES、STRUCTURE_BYTES、DOC_TYPE_LABELS、_NUMBER、_US_PATENT、_US_PUBLICATION、_DOI   常量
    parse_yield              从 “85%”“57 to 60%” 这样的文字里取产率
    document_url             给先例拼原文链接（DOI 或 Google Patents）；拼不出能打开的链接时是 None
    google_patents_number    专利号 -> Google Patents 地址里的写法
    precedent_url            给存在分析结果里的先例重新拼链接（读旧分析时用）
    precedent_values         一条反应 SMILES -> 先例库一行的各列（导入数据时用）
    precedent_from_reaction  同上，直接给出数据库对象（测试用）
    PrecedentIndex           先例库的内存索引
        refresh              库变了就重新加载
    mark_library_changed     导入后换版本号，让各个进程重新加载索引
    PrecedentSearch          检索和定级
        db_empty             先例库是不是空的
        find                 入口：一步反应 -> 证据等级、文献分、先例列表、产率
        _exact_rows          同一反应
        _rank                相似先例的排序规则
        _class_rows          按反应中心找相似先例
        _similar_rows        按指纹找相似先例（粗筛 + 细比）
        _unaccounted_atoms   反应物是不是没给全
        _dedupe              同一篇文献里的同一个反应只留一条
        _grade               定等级和文献分
        _yield_info          汇总文献报道的产率
        _to_dict             一条先例 -> 证据卡里的一项
"""

from __future__ import annotations

import re
import statistics
import threading
from typing import Any
from urllib.parse import quote

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from syntherisk import i18n, models
from syntherisk.modules import m01_structure as chem
from syntherisk.reference import ReferenceData

# ---------------------------------------------------------------- 常量

# 两种指纹各占多少字节（位数除以 8）：反应类别指纹 1024 位 = 128 字节，结构指纹 2048 位 = 256 字节。
# 数据库里 fp_diff、fp_product 两列存的就是这么长的字节串；长度对不上的行在加载索引时会被跳过
CLASS_BYTES = chem.CLASS_FP_BITS // 8
STRUCTURE_BYTES = chem.STRUCTURE_FP_BITS // 8
# 文献类型 -> 显示文字（证据卡和报告里 “先例 1 · 专利 US…” 用）
# i18n.tr 返回的就是中文，同时登记了英文（请求带 ?lang=en 时换成英文，见 syntherisk/i18n.py）
DOC_TYPE_LABELS = {
    "patent": i18n.tr("专利", "Patent"),
    "journal": i18n.tr("期刊论文", "Journal article"),
    "dataset": i18n.tr("数据集", "Dataset"),
}
# 文献类型的先后：期刊论文优先于专利，数据集（比如 ORD 的高通量筛选数据）排在最后。
# 每种类型各自检索、各自最多列 max_precedents 条，证据卡上按这个先后分组列出（见 PrecedentSearch.find）。
# 不在这里的类型（以后新加的）排在它们后面
DOC_TYPE_ORDER = ("journal", "patent", "dataset")


def doc_type_rank(doc_type: str | None) -> int:
    """文献类型在 DOC_TYPE_ORDER 里排第几（0 最优先）；没写类型的按专利算，不认识的类型排在最后。"""
    doc_type = doc_type or "patent"
    return DOC_TYPE_ORDER.index(doc_type) if doc_type in DOC_TYPE_ORDER else len(DOC_TYPE_ORDER)


def _doc_type(row: models.ReactionPrecedent) -> str:
    """一条先例的文献类型；没写的按专利算（USPTO 导入的都是专利）。"""
    return row.doc_type or "patent"


# 没有可用的产率时显示的文字（库是空的、没有先例、先例没报产率都用它）
_NO_YIELD_LABEL = i18n.tr("无产率记录", "No yield on record")
# 一个整数或小数：\d+ 是一串数字，(?:\.\d+)? 是可有可无的小数部分。parse_yield 用它从文字里取数字
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# 美国专利号。USPTO 数据里的写法和 Google Patents 地址里的写法不一样：
#     数据里          US03930836   US06171520B2   USRE037029E1   USRE028728   USH0001943H1   US20010000038A1
#     Google Patents  US3930836    US6171520      USRE37029      USRE28728    USH1943        US20010000038
# 地址里的号码不能补 0，末尾的类型代码（A、B1、B2、E1、H1……）也不写：数据里的类型代码常常和 Google Patents
# 上的对不上（US06171520B2 在 Google Patents 上是 US6171520B1），代码一错就是 404；不写代码时 Google Patents
# 会自己跳到这个号码的那一篇。上面每一种写法都在 Google Patents 上实际打开过。
# 授权专利：^US 开头；(RE|PP|D|H|T)? 是再版、植物、外观设计、法定发明登记、防御性公开这几类的前缀，普通专利没有；
# 0* 是补位的 0；([1-9]\d{0,7}) 是去掉补位 0 以后的号码，最多 8 位；(?:[A-Z]\d?)? 是可有可无的类型代码
_US_PATENT = re.compile(r"^US(RE|PP|D|H|T)?0*([1-9]\d{0,7})(?:[A-Z]\d?)?$")
# 申请公开号：US 后面是 4 位年份和 7 位流水号，一共 11 位数字。这里的 0 不是补位，不能去掉；类型代码同样不写
_US_PUBLICATION = re.compile(r"^US(20\d{9})(?:[A-Z]\d?)?$")
# DOI：10. 开头，4 到 9 位数字的注册机构号，一个斜杠，后面是不含空白的任意字符
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")


# ---------------------------------------------------------------- 导入数据时用的小工具


def parse_yield(text: str | None) -> float | None:
    """从 “85%”“~78%”“57 to 60%” 这样的写法里取一个数；范围取中点。

    参数：
        text  产率的原文，比如 USPTO 数据里 TextMinedYield、CalculatedYield 两列的内容。可以是 None 或空字符串。
    返回：
        百分数（一位小数），比如 85.0、58.5。下面两种情况返回 None：
        文字里没有数字（空的、“quantitative”）；算出来不在 0 到 100 之间（“150%”；0 也不算）。
    算法：
        把文字里的数字都找出来，只看前两个：只有一个就用它，有两个就取平均（当作范围的两端）。
        它不检查这两个数是不是真的在表示一个范围。
    谁在用：
        loaders.py 的 _convert_row（导入先例时，先试文本里提取的产率，没有再试计算出来的产率）；
        tests/test_scoring.py。
    """
    # findall 找出所有数字；text 是 None 时当成空字符串
    numbers = [float(n) for n in _NUMBER.findall(text or "")]
    if not numbers:
        return None
    # numbers[:2] 是前两个数（只有一个时就是那一个），求平均
    value = sum(numbers[:2]) / len(numbers[:2])
    return round(value, 1) if 0 < value <= 100 else None


def document_url(doc_type: str | None, document_id: str | None, doi: str | None) -> str | None:
    """给一条先例拼一个能点开的原文链接；拼不出确定能打开的链接时返回 None。

    参数：
        doc_type     文献类型：patent / journal / dataset。
        document_id  文献号。专利就是专利号，比如 “US04981995”“US06171520B2”。
        doi          DOI，没有是 None。
    返回：
        有 DOI：https://doi.org/<DOI>。DOI 前面带着 https://doi.org/ 或 doi: 的先去掉；
                去掉以后不是 10. 开头的，当作没有 DOI。
        没有 DOI，是专利，专利号是认得的写法：Google Patents 上这篇专利的地址（见 google_patents_number）。
        其余情况返回 None，界面上就不显示链接：没有 DOI 的期刊论文和数据集；认不出来的文献号，
        比如导入时没有文献号、按 “来源-行号” 编出来的 uspto-17，或者美国以外的专利号。
    为什么认不出来就不给链接：
        以前认不出来的专利号原样接在 Google Patents 的地址后面，USPTO 先例里七成多的链接因此是 404。
        现在只给验证过能打开的写法拼链接，宁可不给，也不给打不开的。
    谁在用：
        本文件的 precedent_url() 和 PrecedentSearch._to_dict()；tests/test_scoring.py。
    """
    if doi:
        # 去掉 DOI 前面可能带着的地址或 “doi:”；flags=re.I 表示不分大小写
        text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)\s*", "", doi.strip(), flags=re.I)
        if _DOI.match(text):
            # quote 把在地址里有特殊含义的字符（# ? % 空格 < > 等）换成 %XX；斜杠、冒号、分号、括号保留原样
            return f"https://doi.org/{quote(text, safe='/:;()')}"
    if doc_type == "patent" and document_id:
        number = google_patents_number(document_id)
        if number:
            return f"https://patents.google.com/patent/{number}"
    return None


def google_patents_number(document_id: str) -> str | None:
    """把专利号换成 Google Patents 地址里的写法；不是认得的美国专利号时返回 None。

    参数：
        document_id  专利号，比如 USPTO 数据里的 “US06171520B2”。中间有空格、逗号、横线、斜杠的
                     写法（“US 6,171,520 B2”“US 2001/0000038 A1”）也认。
    返回：
        US03930836 -> US3930836；US06171520B2 -> US6171520；USRE037029E1 -> USRE37029；
        USH0001943H1 -> USH1943；US20010000038A1 -> US20010000038；uspto-17、EP1234567A1 -> None。
        规则见文件开头 _US_PATENT 上面的注释：去掉补位的 0 和末尾的类型代码。
    谁在用：
        本文件的 document_url()。
    """
    # 去掉空白（\s）、逗号、横线、斜杠，统一成大写
    number = re.sub(r"[\s,\-/]", "", document_id).upper()
    match = _US_PUBLICATION.match(number)
    if match:
        return f"US{match.group(1)}"
    match = _US_PATENT.match(number)
    if match:
        # group(1) 是前缀（RE、H……；普通专利没有，是 None），group(2) 是去掉补位 0 的号码
        return f"US{match.group(1) or ''}{match.group(2)}"
    return None


def precedent_url(precedent: dict[str, Any]) -> str | None:
    """存在分析结果里的一条先例现在应该给的原文链接：按它的文献类型、文献号、DOI 重新拼。

    分析结果里的每条先例都存着当时拼好的 url（见 _to_dict）。旧版本拼的链接有七成多打不开，
    而那些分析的结果还存在库里，所以读结果的地方都用这个函数重新拼，不用存着的 url。
    参数：
        precedent  _to_dict() 返回的那种字典；用其中的 doc_type、document_id、doi。
    返回：
        同 document_url()，拼不出能打开的链接时是 None。
    谁在用：
        cards.py 的 build_card()（证据卡、先例列表、报告里的先例）和 build_sources()（卡上的来源）。
    """
    return document_url(precedent.get("doc_type"), precedent.get("document_id"), precedent.get("doi"))


def precedent_values(
    reaction_smiles: str,
    *,
    source: str,
    document_id: str,
    doc_type: str = "patent",
    year: int | None = None,
    doi: str | None = None,
    title: str | None = None,
    yield_pct: float | None = None,
    conditions_text: str | None = None,
    temperature_c: float | None = None,
) -> dict[str, Any] | None:
    """把一条反应 SMILES 算成先例库一行的各列。读不出来、或者没有主产物时返回 None。

    只算不存，返回普通字典：批量导入时可以放到别的进程里算。

    参数（从 source 起都必须用关键字传）：
        reaction_smiles  “反应物>试剂>产物” 格式的反应 SMILES，可以带原子映射（[CH3:1] 这样的写法）。
        source           数据来源编号，要是 data/data_sources.toml 里登记过的（uspto、ord……）。
        document_id      文献号（专利号等）。
        doc_type         patent / journal / dataset，默认 patent。
        year             年份，可以是 None。
        doi、title       DOI 和题目，可以是 None。
        yield_pct        产率（百分数，0–100），可以是 None。
        conditions_text  条件的文字描述，可以是 None。
        temperature_c    温度（摄氏度），可以是 None。
    返回：
        一个字典，键和 models.ReactionPrecedent 的列一一对应（自增的 id 除外）：
            source、doc_type、document_id、year、doi、title、yield_pct、conditions_text、temperature_c
                             传进来什么就是什么
            reaction_smiles  整理过的 “反应物>试剂>产物”：去掉了原子映射，产物只留主产物
            reaction_key     反应编号（chem.reaction_key）：反应物和产物都一样就相同，用来认“同一反应”
            product_key      主产物的 key14（InChIKey 前 14 位）
            agents           试剂、溶剂、催化剂的 SMILES，用 “.” 连成一个字符串；没有是 None
            fp_diff          反应类别指纹，128 字节（bytes）
            fp_product       主产物的结构指纹，256 字节（bytes）
            center           反应中心的文字写法（见 m01_structure.reaction_center）；原始数据不带原子映射、
                             映射太不全、或者没有键发生变化时是 None
        返回 None 的情况：反应 SMILES 不是三段、反应物或产物解析不出来；算不出反应编号或指纹。
    副作用：
        没有，不碰数据库。
    谁在用：
        loaders.py 的 _convert_row（批量导入，可能在别的进程里运行）；本文件的 precedent_from_reaction()。
    """
    # 拆成反应物、试剂、产物三组规范 SMILES（已经去掉原子映射），外加趁映射还在时算出来的反应中心。
    # 专利数据里写在反应物一侧、却没有任何原子进入产物的东西（成盐的酸等），在这一步被挪到了试剂那一组
    parsed = chem.parse_mapped_reaction(reaction_smiles)
    if parsed is None:
        return None
    reactants, agents, products, center = parsed
    # 一条反应可能写了几个产物：取重原子（氢以外的原子）最多的那个当主产物
    product = max(products, key=lambda s: chem.mol_record(s).heavy_atoms)
    key = chem.reaction_key(reactants, product)
    # 两种指纹都是按位打包的 numpy uint8 数组；算不出来时是 None
    diff = chem.difference_bits(reactants, [product])
    product_fp = chem.structure_bits(product)
    if key is None or diff is None or product_fp is None:
        return None
    return {
        "source": source,
        "doc_type": doc_type,
        "document_id": document_id,
        "year": year,
        "doi": doi,
        "title": title,
        "reaction_smiles": chem.build_reaction_smiles(reactants, product, agents),
        "reaction_key": key,
        "product_key": chem.main_fragment_key14(product),
        "yield_pct": yield_pct,
        "agents": ".".join(agents) or None,
        "conditions_text": conditions_text,
        "temperature_c": temperature_c,
        # .tobytes() 把 numpy 数组变成原始字节串，存进数据库的二进制列；加载索引时用 np.frombuffer 还原
        "fp_diff": diff.tobytes(),
        "fp_product": product_fp.tobytes(),
        "center": center,
    }


def precedent_from_reaction(reaction_smiles: str, **fields: Any) -> models.ReactionPrecedent | None:
    """和 precedent_values 一样，但直接给出一个可以 session.add() 的 ReactionPrecedent 对象。

    参数：
        reaction_smiles  反应 SMILES。
        **fields         其余参数原样传给 precedent_values（source、document_id 必须给）。
    返回：
        还没有存进数据库的 ReactionPrecedent 对象；反应读不出来时返回 None。
    谁在用：
        现在只有 tests/ 里的测试用它往临时数据库里放几条先例。正式导入走 loaders.load_precedents，
        那里直接拿 precedent_values 的字典批量插入。
    """
    values = precedent_values(reaction_smiles, **fields)
    return models.ReactionPrecedent(**values) if values else None


# ---------------------------------------------------------------- 内存索引


class PrecedentIndex:
    """先例库的内存索引：每条先例的两种指纹和反应中心的类别。每个进程一份，行数变了才重新加载
    （先例库重新导入以后版本号变了，也会重新加载）。

    为什么要有它：
        找相似先例要拿这一步和库里的先例一条一条地比。USPTO 全量有 175 万条，逐条从数据库里读出来比太慢，
        所以把比较时要用的那几样东西一次读进内存，摆成 numpy 数组，之后每次比较都是整块的数组运算。
        数组里只有指纹和编号；专利号、产率这些内容不在里面，筛出要用的几十条以后再按 id 去数据库取。

    怎么看这些数组（设库里一共 N 条先例；所有数组的第 i 个位置说的都是同一条先例）：
        ids             长度 N，int64。第 i 条先例在 reaction_precedents 表里的 id，从小到大。
        diff            N 行 × 128 列，uint8。每一行是一条先例的反应类别指纹（1024 位，每 8 位装进一个字节）。
        product         N 行 × 256 列，uint8。每一行是一条先例主产物的结构指纹（2048 位）。
        diff_counts     长度 N，int32。diff 每一行里有多少位是 1。算 Tanimoto 相似度要用，提前数好。
        product_counts  长度 N，int32。product 每一行里有多少位是 1。
        class_ids       长度 N，int32。第 i 条先例的反应中心属于哪个“同类”编号；-1 表示这条先例没有反应中心。
        core_ids        长度 N，int32。第 i 条先例的反应中心的“核心”编号；-1 同上。
        doc_types       长度 N，int8。第 i 条先例的文献类型在 doc_type_names 里的位置（期刊论文、专利……），
                        检索时按它让每种文献类型各自挑候选（见 PrecedentSearch.find）。
    其他属性：
        doc_type_names  列表，doc_type_names[doc_types[i]] 是第 i 条先例的文献类型（journal / patent / dataset）。
        classes      {同类钥匙: 编号}。钥匙是一串文字，即 chem.center_class(反应中心) 返回的第一项。
        cores        {核心钥匙: 编号}。钥匙是 chem.center_class(反应中心) 返回的第二项。
        class_names  列表，class_names[编号] 是那个同类钥匙的文字（把 classes 反过来查）。
        with_center  有反应中心的先例有多少条。是 0 说明库是加入反应中心之前导入的，这时只能按指纹比。
        count        上次加载时数据库里的行数；-1 表示还没有加载过。
        version      上次加载时先例库的版本号（library_versions 表）；库里还没有版本记录时是 None。
        _lock        线程锁。两个分析同时发现要加载时，只让一个去加载，另一个等它加载完直接用。

    谁在用：
        context.build_context() 建一个，放在 AppContext.precedent_index 上，整个进程共用；
        本文件的 PrecedentSearch 每次构造时调 refresh()，检索时直接读上面这些数组；
        pipeline.py 用 with_center 判断要不要提醒“先例库该重新导入了”。
    """

    def __init__(self) -> None:
        """建一个空的、还没有加载的索引。不查数据库；真正读数据是在 refresh() 里。"""
        self._lock = threading.Lock()
        # -1 和 None：还没加载过。refresh() 拿它们和数据库里现在的行数、版本号比
        self.count = -1
        self.version: str | None = None
        # 下面都是长度为 0 的空数组，形状和数据类型同加载以后的一样，这样没加载时去读它们也不会出错
        self.ids = np.zeros(0, dtype=np.int64)
        self.diff = np.zeros((0, CLASS_BYTES), dtype=np.uint8)
        self.product = np.zeros((0, STRUCTURE_BYTES), dtype=np.uint8)
        self.diff_counts = np.zeros(0, dtype=np.int32)
        self.product_counts = np.zeros(0, dtype=np.int32)
        # 反应中心归类以后的编号（chem.center_class 的两把钥匙），-1 表示这条先例没有反应中心
        self.class_ids = np.zeros(0, dtype=np.int32)
        self.core_ids = np.zeros(0, dtype=np.int32)
        self.classes: dict[str, int] = {}
        self.cores: dict[str, int] = {}
        self.class_names: list[str] = []
        # 文献类型：每条先例一个小整数，doc_type_names 把它换回文字
        self.doc_types = np.zeros(0, dtype=np.int8)
        self.doc_type_names: list[str] = []
        self.with_center = 0

    def refresh(self, session: Session) -> None:
        """看一眼先例库变了没有；变了，或者还没加载过，就把索引重新读进内存。

        参数：
            session  数据库会话，只用来读。
        怎么判断“变了”：
            比较 (表里的行数, 版本号) 和上次加载时记下的是否一样。光看行数不够：重新导入同一个文件，
            行数不变但内容换过了，所以导入程序每次都会换一个版本号（mark_library_changed）。
            绕过导入程序直接改表、行数又没变的话，这里发现不了。
        副作用：
            每次调用查两次数据库（数行数、读版本号）。需要加载时，再把整张表的 id、两种指纹和反应中心
            读一遍，换掉本对象上的全部数组：USPTO 全量约 13 秒、700 MB，其间别的线程调 refresh 会等在锁上。
        容错：
            指纹长度不对的行直接跳过；反应中心的写法认不出来的行当作没有反应中心。都不报错。
        谁在用：
            PrecedentSearch.__init__()（每次分析的第 4 阶段开头调一次）；tests/test_integrations.py。
        """
        # session.scalar(查询) 取结果第一行的第一列。func.count() 是 SQL 的 count(*)。
        # 表是空的得到 0；还没有版本记录时 version 是 None
        count = session.scalar(select(func.count()).select_from(models.ReactionPrecedent)) or 0
        version = session.scalar(select(models.LibraryVersion.version).where(models.LibraryVersion.name == "precedents"))
        if (count, version) == (self.count, self.version):
            return
        with self._lock:
            if (count, version) == (self.count, self.version):  # 等锁的时候别的线程已经加载好了
                return
            # USPTO 全量约 180 万行、近 700 MB 指纹：直接往预先分好的数组里一块一块填，不先攒成列表
            ids = np.zeros(count, dtype=np.int64)
            diff = np.zeros((count, CLASS_BYTES), dtype=np.uint8)
            product = np.zeros((count, STRUCTURE_BYTES), dtype=np.uint8)
            # 两种编号先全部填成 -1（没有反应中心），下面读到有的再改
            class_ids = np.full(count, -1, dtype=np.int32)
            core_ids = np.full(count, -1, dtype=np.int32)
            doc_types = np.zeros(count, dtype=np.int8)
            # 文献类型 -> 编号，第一次见到时编下一个号。没写类型的按专利算
            type_codes: dict[str, int] = {}
            classes: dict[str, int] = {}
            cores: dict[str, int] = {}
            # 备忘：反应中心的原文 -> (同类编号, 核心编号)。同一种反应中心在库里会出现很多次，算过一次就记住。
            # None 和空字符串（没有反应中心）预先记成 (-1, -1)
            known: dict[str | None, tuple[int, int]] = {None: (-1, -1), "": (-1, -1)}

            def center_ids(center: str | None) -> tuple[int, int]:
                """一条先例的反应中心（库里存的原文）-> (同类编号, 核心编号)；认不出来是 (-1, -1)。

                第一次见到的钥匙当场分配一个新编号，记进外层的 classes / cores；算过的结果记在 known 里。
                """
                pair = known.get(center)
                if pair is None:
                    try:
                        # center_class 把库里存的原样写法归并成两把钥匙：whole 是同类钥匙，core 是核心钥匙。
                        # setdefault(钥匙, 现有个数)：钥匙第一次出现就拿到下一个编号（0、1、2……），再出现用原来的
                        whole, core = chem.center_class(center)
                        pair = (classes.setdefault(whole, len(classes)), cores.setdefault(core, len(cores)))
                    except ValueError:  # 库里存的是别的版本的写法：当作没有反应中心，重新导入以后才用得上
                        pair = (-1, -1)
                    known[center] = pair
                return pair

            # filled：数组里已经填了多少行
            filled = 0
            # 只取检索要用的五列，按 id 排好。.yield_per(20000) 让结果一次只取 20000 行，不把整张表一口气读进内存；
            # 下面的 rows.partitions() 就按这个大小一批一批地给，每一批是一个行的列表，
            # 行是 (id, fp_diff, fp_product, center, doc_type)
            rows = session.execute(
                select(
                    models.ReactionPrecedent.id,
                    models.ReactionPrecedent.fp_diff,
                    models.ReactionPrecedent.fp_product,
                    models.ReactionPrecedent.center,
                    models.ReactionPrecedent.doc_type,
                ).order_by(models.ReactionPrecedent.id)
            ).yield_per(20000)
            for chunk in rows.partitions():
                # 指纹的定义改过以后，旧数据的长度对不上：跳过，重新导入后才会被检索到
                good = [row for row in chunk if len(row[1]) == CLASS_BYTES and len(row[2]) == STRUCTURE_BYTES]
                good = good[: count - filled]  # 加载期间又有新数据导进来：这次先不管，下次行数变了会重新加载
                size = len(good)
                if not size:
                    continue
                ids[filled:filled + size] = [row[0] for row in good]
                # 把这一批的指纹字节串首尾相接，np.frombuffer 把它看成一长串 uint8，
                # 再 .reshape 成 “size 行 × 每行固定字节数”，填进大数组里对应的那一段
                diff[filled:filled + size] = np.frombuffer(b"".join(row[1] for row in good), dtype=np.uint8).reshape(size, CLASS_BYTES)
                product[filled:filled + size] = np.frombuffer(b"".join(row[2] for row in good), dtype=np.uint8).reshape(size, STRUCTURE_BYTES)
                # 每一行的反应中心换成两个编号：备忘里有就直接用（查到的是非空元组，or 后面的不会执行），没有才去算
                pairs = [known.get(row[3]) or center_ids(row[3]) for row in good]
                class_ids[filled:filled + size] = [pair[0] for pair in pairs]
                core_ids[filled:filled + size] = [pair[1] for pair in pairs]
                doc_types[filled:filled + size] = [type_codes.setdefault(row[4] or "patent", len(type_codes))
                                                   for row in good]
                filled += size
            # 有行被跳过时 filled 比 count 小：把数组截到实际填了的长度，再换到对象上
            self.ids, self.diff, self.product = ids[:filled], diff[:filled], product[:filled]
            self.class_ids, self.core_ids, self.classes, self.cores = class_ids[:filled], core_ids[:filled], classes, cores
            self.doc_types, self.doc_type_names = doc_types[:filled], list(type_codes)
            # 字典保持插入的先后，而编号正是按插入的先后给的，所以 list(classes) 的第 n 项就是编号 n 的钥匙
            self.class_names = list(classes)
            # (数组 >= 0) 得到一个布尔数组，.sum() 数其中有多少个 True
            self.with_center = int((self.class_ids >= 0).sum())
            # 每一行有多少位是 1；库是空的时给长度为 0 的数组
            self.diff_counts = chem.popcount_rows(self.diff) if filled else np.zeros(0, dtype=np.int32)
            self.product_counts = chem.popcount_rows(self.product) if filled else np.zeros(0, dtype=np.int32)
            # 行数和版本号放在最后才记：中途出错的话它们还是旧值，下次调用会再加载一遍
            self.count, self.version = count, version


def mark_library_changed(session: Session) -> None:
    """先例库的内容变了（导入、替换）以后调用：换一个版本号，各个后端进程下次检索时会重新加载索引。

    参数：
        session  数据库会话。
    副作用：
        把 library_versions 表里 name 是 precedents 的那一行换一个新的随机版本号，并更新时间；
        还没有这一行就新建一行。不提交事务，调用方要自己 commit。
    谁在用：
        loaders.load_precedents()，每次导完数据以后。
    """
    # 按主键取这一行；先例库第一次导入时还没有
    state = session.get(models.LibraryVersion, "precedents")
    if state is None:
        # 新建一行，version 和 updated_at 用表定义里的默认值（随机编号、当前时间）
        session.add(models.LibraryVersion(name="precedents"))
    else:
        # new_id() 是一串随机的十六进制字符：只要和原来不一样就行，没有先后大小的含义
        state.version, state.updated_at = models.new_id(), models.utcnow()


# ---------------------------------------------------------------- 检索和定级


class PrecedentSearch:
    """给一步反应找先例、定证据等级。一次分析建一个，然后每一步调一次 find()。

    属性：
        session    数据库会话：按反应编号查“同一反应”，以及按 id 取候选先例的完整内容。
        reference  参考数据：用 reference.data_sources 把来源编号换成来源名称。
        config     data/scoring.toml 里 [evidence] 这一节（各个阈值、分数段、最多列几条）。
        index      先例库的内存索引（PrecedentIndex），整个进程共用一份。

    find() 里的三路检索：
        同一反应    _exact_rows()    反应物、产物都一样（按 reaction_key 查数据库）          -> E1
        按反应中心  _class_rows()    这一步带原子映射、库里的先例也有反应中心时用            -> E2 / E3
        按指纹      _similar_rows()  这一步没有映射时用；也用来补查库里没带映射的那部分先例  -> E2 / E3，或只当旁证
        都没找到 -> E4。等级和分数怎么定见 _grade()。
    """

    def __init__(self, session: Session, reference: ReferenceData, index: PrecedentIndex):
        """记下要用的东西，并确保内存索引是最新的。

        参数：
            session    数据库会话。
            reference  参考数据（AppContext.reference）。
            index      内存索引（AppContext.precedent_index）。
        副作用：
            调 index.refresh(session)：索引还没加载、或者先例库变了，就在这里加载（USPTO 全量约 13 秒）。
            所以服务启动后第一次分析的第 4 阶段，开头会停一下。
        抛出：
            KeyError  data/scoring.toml 里没有 [evidence] 这一节。
        谁在用：
            pipeline.py 的 _precedents；tests/test_scoring.py。
        """
        self.session = session
        self.reference = reference
        self.config = reference.scoring["evidence"]
        self.index = index
        index.refresh(session)

    @property
    def db_empty(self) -> bool:
        """先例库是不是空的（一条先例都没有）。

        空的时候 find() 不评等级、不给文献分；流水线据此在结果里加一条提醒。
        @property 的意思是把它当属性用：写 search.db_empty，后面不加括号。
        """
        return self.index.count <= 0

    def find(
        self,
        reactants: list[str],
        product: str,
        *,
        model_probability: float | None = None,
        center: str | None = None,
    ) -> dict[str, Any]:
        """返回这一步的证据等级、文献分、先例列表和文献报道产率。

        center 是这一步的反应中心（chem.reaction_center / retro_reaction_center 的结果）；
        给了、并且库里的先例也有反应中心，就按反应中心判断是不是同类反应，否则按指纹。

        参数：
            reactants          这一步的反应物，SMILES 列表（不含试剂）。
            product            这一步的产物，SMILES。
            model_probability  逆合成模型给这一步的概率（0–1，AiZynthFinder 的 policy_probability）。
                               只在一条先例都找不到（E4）时用来多给一点分；没有就传 None。
            center             见上；逆合成引擎没给原子映射时是 None。
        返回（一个字典）：
            db_empty          先例库是不是空的
            level             "E1" / "E2" / "E3" / "E4"；先例库是空的时是 None，表示没有评估
            label             等级的名字：文献直接报道 / 高相似先例 / 低相似先例 / 仅模型预测
            stars             星数 3 / 2 / 1 / 0；没评估是 None
            literature_score  “文献依据”这一维的分数（0–100，一位小数）；没评估是 None
            precedents        先例列表，按文献类型分组：期刊论文在前、专利在后（DOC_TYPE_ORDER），每种类型各自最多
                              max_precedents 条（现在是 8），组内同一反应的排最前。每一项的键见 _to_dict()
            n_exact           库里“同一反应”的先例有几条（去重以后的数；可能比列出来的多）
            n_exact_by_type   上一项按文献类型分开数，如 {"journal": 1, "patent": 4}
            best_ref          借用试剂和条件文字时用的那条先例的编号（期刊优先，见 _best_ref）；没有先例是 None
            best_similarity   E1 时是 1.0；否则是同类反应先例里最高的底物相似度（三位小数）；没有同类先例是 None
            compared_by       这一步是怎么判断“同类反应”的："center" 按反应中心，"fingerprint" 按指纹；没评估是 None
            yield             文献报道的产率，见 _yield_info()。流水线把它从字典里拿出去，另存为 reaction["yield"]
            basis             一句话说明这个等级是怎么来的，证据卡上直接显示
        副作用：
            读数据库：按反应编号查一次同一反应，再按 id 取候选先例的完整内容（最多两次）。
            不写库，不改传进来的列表。
        抛出：
            ValueError  center 的写法不合 m01_structure.center_class 认的格式（引擎算出来的不会这样）。
        谁在用：
            pipeline.py 的 _precedents（每一步一次）；tests/test_scoring.py。
        """
        if self.db_empty:
            # 库是空的：如实说“没评估”。键和正常情况一样，只是等级、分数都是 None，产率是“无记录”
            return {
                "db_empty": True,
                "level": None,
                "label": i18n.tr("未评估（先例库还没有导入数据）", "Not assessed (the precedent library has no data yet)"),
                "stars": None,
                "literature_score": None,
                "precedents": [],
                "n_exact": 0,
                "best_similarity": None,
                "compared_by": None,
                "yield": {"source": "none", "label": _NO_YIELD_LABEL, "refs": []},
                "basis": i18n.tr("先例库还没有导入 USPTO / ORD 数据，文献依据这一项暂不计分",
                                 "No USPTO / ORD data has been imported into the precedent library yet, "
                                 "so literature evidence is not scored"),
            }

        # 证据卡里最多列几条先例
        limit = int(self.config.get("max_precedents", 8))
        # 第一路：同一反应。reaction_key 由反应物和产物算出来；有结构读不出来时是 None，这一路就跳过。
        # 专利数据不写“用了几份”：同一种原料用两份的反应（2X + Y → Z）常常只写一次 X，
        # 而逆合成引擎会把两份都列出来。所以再按“每种原料只算一次”算一个编号，两个都查
        keys = [key for key in dict.fromkeys([chem.reaction_key(reactants, product),
                                               chem.reaction_key(reactants, product, distinct=True)]) if key]
        exact_rows = self._exact_rows(keys) if keys else []
        # 产物比反应物多出一大块（反应物没给全）时，按指纹就比不准“反应类型”了：不找相似先例，免得硬凑出一个等级
        unaccounted = self._unaccounted_atoms(reactants, product)
        balanced = unaccounted <= int(self.config.get("max_unaccounted_atoms", 4))
        # 这一步给了反应中心，而且库里至少有一条先例带反应中心，才按反应中心比；否则按指纹比
        by_center = bool(center) and self.index.with_center > 0
        # 按指纹补查出来的、没带映射的先例的 id。它们没法确认和这一步是不是同一种转化
        unverified: set[int] = set()
        if by_center:
            # 第二路：按反应中心。similar 里每一项是 (先例行, 底物相似度, 反应一致程度, 是否同类反应)
            similar = self._class_rows(center, product, exclude_keys=keys, limit=limit)
            # np.nonzero(条件)[0] 给出满足条件的位置。这里是库里没有反应中心的那些先例在数组里的位置
            unmapped = np.nonzero(self.index.class_ids < 0)[0]
            if balanced and len(unmapped):
                # 库里没带映射的先例只能按指纹比。指纹分不清“产物相近、成的键不同”的反应，
                # 所以它们再像也只当旁证（相关反应），不拿来把这一步定成同类反应
                loose = self._similar_rows(reactants, product, exclude_keys=keys, limit=limit, pool=unmapped)
                # “row, *_” 的意思是只要元组的第一项（先例行），其余几项不管
                unverified = {row.id for row, *_ in loose}
                # 把第四项（是否同类反应）一律换成 False 再并进来，然后整体重新排序
                similar += [(row, substrate, match, False) for row, substrate, match, _ in loose]
                similar.sort(key=self._rank)
        else:
            # 第三路：整步按指纹比。反应物没给全时干脆不找
            similar = (self._similar_rows(reactants, product, exclude_keys=keys, limit=limit)
                       if balanced else [])

        # 拼先例列表：按文献类型分组，期刊论文在前、专利在后（DOC_TYPE_ORDER）。每种类型各自最多 limit 条：
        # 先是这种类型里同一反应的记录（两种相似度都记 1.0），再是相似先例。上面的检索也是每种类型各自挑候选的，
        # 所以专利再多也挤不掉期刊。库里只有专利时，结果和不分组完全一样
        present = {_doc_type(row) for row in exact_rows} | {_doc_type(row) for row, *_ in similar}
        precedents: list[dict[str, Any]] = []
        for doc_type in sorted(present, key=doc_type_rank):
            group: list[dict[str, Any]] = []
            for row in exact_rows:
                if _doc_type(row) == doc_type and len(group) < limit:
                    group.append(self._to_dict(row, similarity=1.0, class_similarity=1.0, exact=True,
                                               compared_by="exact"))
            for row, substrate_similarity, class_similarity, same_class in similar:
                if _doc_type(row) == doc_type and len(group) < limit:
                    group.append(self._to_dict(
                        row, similarity=substrate_similarity, class_similarity=class_similarity, exact=False,
                        same_class=same_class,
                        compared_by="center" if by_center and row.id not in unverified else "fingerprint",
                    ))
            precedents += group
        # 按列出来的顺序编号 P1、P2……说明文字、产率统计、借用的试剂都用这个编号指回先例
        for number, precedent in enumerate(precedents, 1):
            precedent["ref"] = f"P{number}"

        n_exact = len(exact_rows)
        # similar 已经排好序（同类在前，底物越像越靠前），所以取第一条就是相似度最高的
        same_class = [item for item in similar if item[3]]
        best = same_class[0][1] if same_class else None
        # 一条同类的都没有时，才看相关反应里最像的那条
        related = similar[0][1] if similar and not same_class else None
        # 找到的是不是全都是没带映射、没法确认的先例（只影响说明文字）
        only_unverified = bool(similar) and all(row.id in unverified for row, *_ in similar)
        level, score, basis = self._grade(n_exact, best, model_probability, related, only_unverified)
        # 每种文献类型里各有几条同一反应的记录（库里查到的全部，不只是列出来的）
        n_exact_by_type: dict[str, int] = {}
        for row in exact_rows:
            n_exact_by_type[_doc_type(row)] = n_exact_by_type.get(_doc_type(row), 0) + 1
        if not balanced and not by_center and not n_exact:
            # 按指纹比、反应物又没给全、也没有同一反应的记录：等级是 E4，但要把原因说清楚
            basis = i18n.tr("反应物没有给全（产物多出 {n} 个原子），没法按反应类型找相似先例",
                            "The reactants are incomplete (the product has {n} extra atoms), "
                            "so similar precedents cannot be searched by reaction type", n=unaccounted)
        # 等级的名字和星数在 scoring.toml 的 [evidence.levels.E1] 到 [evidence.levels.E4]
        spec = self.config["levels"][level]
        return {
            "db_empty": False,
            "level": level,
            "label": spec["label"],
            "stars": spec["stars"],
            "literature_score": round(score, 1),
            "precedents": precedents,
            "n_exact": n_exact,
            # 每种文献类型里各有几条同一反应的记录，如 {"journal": 1, "patent": 4}
            "n_exact_by_type": n_exact_by_type,
            # 借用试剂和条件文字时用的那条先例的编号（期刊优先，见 _best_ref）；没有先例是 None
            "best_ref": self._best_ref(precedents),
            "best_similarity": 1.0 if n_exact else (round(float(best), 3) if best is not None else None),
            "compared_by": "center" if by_center else "fingerprint",  # 这一步是按反应中心还是按指纹判断“同类反应”的
            # 同一反应的产率要用库里查到的全部记录来统计，不能只用列在卡上的那几条：
            # 卡上最多列 max_precedents 条，而且是按产率从高到低挑的，只统计它们会把产率报高
            "yield": self._yield_info(precedents, level, [row.yield_pct for row in exact_rows if row.yield_pct is not None]),
            "basis": basis,
        }

    # ------------------------------------------------------------ 检索

    def _exact_rows(self, keys: list[str]) -> list[models.ReactionPrecedent]:
        """库里和这一步是“同一反应”的先例：反应物和产物都一样（reaction_key 是 keys 之一）。

        参数：
            keys  这一步的反应编号（chem.reaction_key 的结果），一个或两个：照写的和“每种原料只算一次”的，
                  见 find()。编号不看试剂，也不分立体异构。
        返回：
            ReactionPrecedent 列表：期刊论文在前、专利在后（DOC_TYPE_ORDER）；同一种类型里有产率的排前面，
            产率高的在前，产率一样时年份新的在前；同一篇文献里重复记录的同一个反应只留一条。没有就是空列表。
        副作用：
            查一次数据库（reaction_key 列上有索引），最多取 200 行；期刊论文先取，不会因为专利太多被截掉。
        谁在用：
            本类的 find()。
        """
        # select(模型).where(...) 取整行，session.scalars 把每一行变成一个 ReactionPrecedent 对象。
        # .limit(200) 是上限：同一个反应的记录再多也只看 200 条
        rows = list(
            self.session.scalars(
                select(models.ReactionPrecedent)
                .where(models.ReactionPrecedent.reaction_key.in_(keys))
                # 期刊论文排在前面再截 200 条（doc_type != "journal" 对期刊是 False，排在 True 前面）
                .order_by(models.ReactionPrecedent.doc_type != "journal")
                .limit(200)
            )
        )
        # 排序的键是四项的元组，Python 从左到右依次比较：
        #   doc_type_rank         文献类型的先后：期刊论文 0，专利 1，数据集 2
        #   r.yield_pct is None   有产率的是 False，排在没有产率的（True）前面
        #   -(产率)               加负号让大的排前面；没有产率的按 0 算
        #   -(年份)               同理，新的排前面；没有年份的按 0 算
        rows.sort(key=lambda r: (doc_type_rank(r.doc_type), r.yield_pct is None, -(r.yield_pct or 0),
                                 -(r.year or 0)))
        return self._dedupe(rows)

    @staticmethod
    def _rank(item) -> tuple:
        """相似先例的排序：同类反应在前，然后底物越像越靠前。item 是 (行, 底物相似度, 反应一致程度, 是否同类反应)。

        返回一个元组，交给 list.sort 当排序的键（小的排前面）：
            not item[3]   同类反应是 False，排在相关反应（True）前面
            -item[1]      底物相似度高的在前
            -item[2]      底物一样像时，反应一致程度高的在前
            item[0].id    还分不出先后就按先例的 id，保证每次排出来的顺序一样
        谁在用：
            本类的 find()、_class_rows()、_similar_rows()。
        """
        return (not item[3], -item[1], -item[2], item[0].id)

    def _per_type(self, ranked: np.ndarray, positions: np.ndarray, take: int) -> np.ndarray:
        """从排好序的候选里，每种文献类型各留前 take 个。这就是“期刊、专利分开检索”：各自有自己的名额。

        参数：
            ranked     候选在 positions 里的下标，已经按优先的先后排好（底物最像的在前）。
            positions  候选在整个索引里的位置（用来查每条候选的文献类型 index.doc_types）。
            take       每种类型最多留几个。
        返回：
            留下的下标（numpy 数组），仍按 ranked 原来的先后。库里只有一种文献类型时，就是 ranked 的前 take 个。
        谁在用：
            _class_rows()、_similar_rows()。
        """
        if not len(ranked):
            return ranked
        codes = self.index.doc_types[positions[ranked]]
        keep = np.zeros(len(ranked), dtype=bool)
        for code in np.unique(codes):
            # 这种类型的候选在 ranked 里的位置（从前往后），只留前 take 个
            keep[np.nonzero(codes == code)[0][:take]] = True
        return ranked[keep]

    def _best_ref(self, precedents: list[dict[str, Any]]) -> str | None:
        """借用试剂和条件文字时用哪一条先例，返回它的编号（P1……）。期刊论文优先：

            1. 有同一反应的记录：用排在最前的那条。列表里期刊在前，所以有期刊就用期刊（同一类型里产率高的在前）；
            2. 没有同一反应：同类反应、底物相似度够得上 E2（high_similarity_threshold）的先例里，期刊优先；
            3. 都不够：用最相关的那条（同类反应优先，底物越像越前），和分组以前的做法一样。
        先例列表是空的返回 None。只有 E1、E2 的步骤会真的借用（见 pipeline.py 的 _adopt_precedent_reagents）。
        谁在用：find()。
        """
        if not precedents:
            return None
        exact = [p for p in precedents if p["exact"]]
        if exact:
            return exact[0]["ref"]
        high = float(self.config["high_similarity_threshold"])
        strong = [p for p in precedents if p["same_class"] and p["similarity"] >= high]
        if strong:
            return strong[0]["ref"]
        most_relevant = min(precedents, key=lambda p: (not p["same_class"], -p["similarity"], -p["class_similarity"]))
        return most_relevant["ref"]

    def _class_rows(self, center: str, product: str, *, exclude_keys, limit):
        """按反应中心找相似先例。返回 [(行, 底物相似度, 反应一致程度, 是否同类反应)]，同类的排在前面。

        反应中心（归并以后）和这一步一模一样的先例才算“同类反应”；成的键一样、掉下去的基团不一样的
        （先例用酰氯，这一步用羧酸；先例脱的是 Cbz，这一步脱 Boc）算“相关反应”，最多支持 E3 的下半段。
        两种都还要产物够像才列出来。“反应一致程度”是两边相同的变化占全部变化的比例。

        参数：
            center       这一步的反应中心（原样的文字，没有归并过）。
            product      这一步的产物 SMILES。
            exclude_keys  这一步自己的反应编号（一个或两个，见 find）：反应物、产物都一样的先例已经算作“同一反应”，
                          这里不再重复列。
            limit         证据卡每种文献类型最多列几条。这里会多挑几倍的候选（同类挑 limit × 6 条，相关挑 limit × 3 条，
                          期刊、专利各自这么多），因为后面去重、去掉同一反应以后会少一些。
        返回：
            见第一行。相似度和一致程度都在 0 到 1 之间；已经按 _rank 排好。一条都没有时返回空列表。
        步骤：
            1. 把这一步的反应中心归并成同类钥匙、核心钥匙（chem.center_class），换成索引里的编号。
            2. 在内存索引里挑候选：先挑同类编号相同的，再挑核心编号相同而同类编号不同的。
            3. 给候选算产物结构指纹的 Tanimoto 相似度，低于 low_similarity_threshold 的不要，留下最像的若干条。
            4. 按 id 去数据库取这些先例的完整内容，算“反应一致程度”，排序。
        副作用：
            查一次数据库（按 id 取行）。
        谁在用：
            本类的 find()，在这一步带反应中心、库里也有带反应中心的先例时。
        """
        index = self.index
        # 产物的结构指纹（256 字节的 uint8 数组）。产物读不出来就没法比“底物像不像”
        query_product = chem.structure_bits(product)
        if query_product is None:
            return []
        whole, core = chem.center_class(center)
        # 钥匙换成编号。库里没出现过的钥匙给 -2：它不等于任何真实的编号，也不等于代表“没有反应中心”的 -1，
        # 所以什么先例都匹配不上
        class_id, core_id = index.classes.get(whole, -2), index.cores.get(core, -2)
        low = float(self.config["low_similarity_threshold"])
        # 候选：先例 id -> (底物相似度, 这条先例的同类编号)
        picked: dict[int, tuple[float, int]] = {}
        # 分两轮挑：第一轮是同类反应（同类编号相同），第二轮是相关反应（核心编号相同、同类编号不同）。
        # mask 是和库一样长的布尔数组，& 是逐个位置的“并且”
        for same, mask in ((True, index.class_ids == class_id), (False, (index.core_ids == core_id) & (index.class_ids != class_id))):
            # 满足条件的先例在数组里的位置
            candidates = np.nonzero(mask)[0]
            if not len(candidates):
                continue
            # index.product[candidates] 用位置数组挑出这些行；一次算出这一步的产物和它们每一个的 Tanimoto 相似度
            substrate = chem.tanimoto_many(query_product, index.product[candidates], index.product_counts[candidates])
            # 相似度够格的候选（keep 里是它们在 candidates 里的下标）
            keep = np.nonzero(substrate >= low)[0]
            # np.argsort(-相似度) 给出从高到低的次序；kind="stable" 让一样高的保持原来的先后，每次结果相同。
            # 后面的切片只留前若干个。i 是在 candidates 里的下标，candidates[i] 才是在整个索引里的位置
            ranked = keep[np.argsort(-substrate[keep], kind="stable")]
            # 每种文献类型各留最像的若干条：期刊、专利分开挑，专利再多也挤不掉期刊
            for i in self._per_type(ranked, candidates, limit * (6 if same else 3)):
                picked[int(index.ids[candidates[i]])] = (float(substrate[i]), int(index.class_ids[candidates[i]]))
        if not picked:
            return []

        # 同类钥匙是用 “|” 连起来的一项一项变化；拆成集合，下面拿它和每条先例的比
        mine = set(whole.split("|"))
        # 按 id 把候选先例的整行取出来。.in_(列表) 就是 SQL 的 IN (...)，这里最多几十个 id
        rows = self.session.scalars(select(models.ReactionPrecedent).where(models.ReactionPrecedent.id.in_(list(picked))))
        found = []
        for row in self._dedupe([r for r in rows if r.reaction_key not in exclude_keys]):
            substrate, their_class = picked[row.id]
            theirs = set(index.class_names[their_class].split("|"))
            # 反应一致程度 = 两边共有的变化项数 ÷ 两边合起来的变化项数。同类反应两边完全一样，是 1；相关反应小于 1
            found.append((row, substrate, len(mine & theirs) / len(mine | theirs), their_class == class_id))
        found.sort(key=self._rank)
        return found

    def _similar_rows(self, reactants, product, *, exclude_keys, limit, pool=None):
        """按指纹找相似先例，分两步。返回值同 _class_rows。pool 给了的话，只在这些行里找。

        第一步粗筛：反应类别指纹（折叠成位）够像，产物也够像。快，但会放进来
        “产物碰巧一样、反应其实是另一回事”的先例（实测：把“水杨酸乙酰化”当成了“2-氟苯甲酸和乙酸成酯”的先例）。
        第二步对筛出来的几十条逐条细比：原子类型增减了几个，数着个数比。
        够一致的才算“同类反应”；只是沾边的算“相关反应”，最多支持 E3 的下半段；再差的丢掉。

        参数：
            reactants    这一步的反应物 SMILES 列表。
            product      这一步的产物 SMILES。
            exclude_keys  这一步自己的反应编号（一个或两个，见 find）；同一反应的先例不在这里重复列。
            limit         证据卡每种文献类型最多列几条。粗筛时类别最像的一档挑 limit × 6 条、次一档挑 limit × 3 条去细比，
                          期刊、专利各自这么多。
            pool         None：在整个库里找。给一个位置数组（numpy）：只在索引的这些位置上找——
                         find() 用它只查库里没带反应中心的那部分先例。
        返回：
            [(先例行, 底物相似度, 反应一致程度, 是否同类反应)]，已经按 _rank 排好。
            这里的“反应一致程度”是细比的结果（chem.delta_similarity，0–1）。
            这一步的结构读不出来、或者库是空的，返回空列表。
        用到的阈值（都在 data/scoring.toml 的 [evidence] 里，括号里是现在的值）：
            same_class_threshold      粗筛：类别指纹相似度到这个数（0.65）的，优先拿去细比
            related_class_threshold   粗筛：到这个数（0.50）的也少量拿去细比
            low_similarity_threshold  产物相似度低于它（0.20）的不要
            class_match_strict        细比：一致程度到这个数（0.65）才算同类反应
            class_match_related       细比：到这个数（0.45）算相关反应，再低就丢掉
        副作用：
            查一次数据库（按 id 取候选行）。粗筛要把这一步和范围内的每一条先例都比一遍，
            在整个库里找时是 M5 最费时间的计算。
        谁在用：
            本类的 find()。
        """
        # 这一步的三样东西：原子类型增减的计数（细比用）、折叠成位的类别指纹（粗筛用）、产物的结构指纹
        query_delta = chem.environment_delta(reactants, [product])
        query_diff = chem.difference_bits(reactants, [product])
        query_product = chem.structure_bits(product)
        if query_delta is None or query_diff is None or query_product is None or not len(self.index.ids):
            return []
        config = self.config
        index = self.index
        # slice(None) 相当于下标里的 “:”，也就是全部行；给了 pool 就只取那些位置上的行
        rows_in = slice(None) if pool is None else pool
        ids, products, product_counts = index.ids[rows_in], index.product[rows_in], index.product_counts[rows_in]
        same, loose = float(config["same_class_threshold"]), float(config.get("related_class_threshold", 0.5))
        # 第一步粗筛：这一步的类别指纹对范围内每一条先例的 Tanimoto 相似度，得到一个和范围一样长的数组
        bit_class = chem.tanimoto_many(query_diff, index.diff[rows_in], index.diff_counts[rows_in])
        # 候选：先例 id -> 底物相似度
        picked: dict[int, float] = {}
        # 分两档挑，每一项是 (下限, 上限, 最多挑几条)：
        #   类别相似度 ≥ same 的挑 limit × 6 条（上限写 2.0 只是表示“没有上限”，相似度最大是 1）；
        #   在 loose 和 same 之间的挑 limit × 3 条
        for lower, upper, take in ((same, 2.0, limit * 6), (loose, same, limit * 3)):
            candidates = np.nonzero((bit_class >= lower) & (bit_class < upper))[0]
            if not len(candidates):
                continue
            # 这一档里再看产物像不像：低于 low_similarity_threshold 的不要，从高到低留下前 take 条
            substrate = chem.tanimoto_many(query_product, products[candidates], product_counts[candidates])
            keep = np.nonzero(substrate >= float(config["low_similarity_threshold"]))[0]
            ranked = keep[np.argsort(-substrate[keep], kind="stable")]
            # candidates 是在 rows_in 范围里的位置；换成在整个索引里的位置，才查得到每条的文献类型
            positions = candidates if pool is None else pool[candidates]
            for i in self._per_type(ranked, positions, take):
                picked[int(ids[candidates[i]])] = float(substrate[i])
        if not picked:
            return []

        rows = self.session.scalars(select(models.ReactionPrecedent).where(models.ReactionPrecedent.id.in_(list(picked))))
        strict, related = float(config.get("class_match_strict", 0.65)), float(config.get("class_match_related", 0.45))
        found = []
        # 第二步细比：对筛出来的每一条，用它存着的反应 SMILES 现算一遍“各种原子类型增减了几个”，和这一步的数着个数比
        for row in self._dedupe([r for r in rows if r.reaction_key not in exclude_keys]):
            # 库里存的格式是 “反应物>试剂>产物”，反应物之间用 “.” 隔开
            their_reactants, _, their_product = row.reaction_smiles.split(">")
            their_delta = chem.environment_delta(their_reactants.split("."), [their_product])
            match = chem.delta_similarity(query_delta, their_delta) if their_delta is not None else 0.0
            if match >= related:
                # 第四项：一致程度达到 strict 才算同类反应，否则只是相关反应
                found.append((row, picked[row.id], match, match >= strict))
        found.sort(key=self._rank)
        return found

    @staticmethod
    def _unaccounted_atoms(reactants: list[str], product: str) -> int:
        """产物里有多少个重原子在反应物里找不到来源（氧化加上去的一两个氧不算问题，所以只是数个数）。

        参数：
            reactants  反应物 SMILES 列表。
            product    产物 SMILES。
        返回：
            产物的重原子数 − 各个反应物的重原子数之和（重原子指氢以外的原子）。
            正数表示产物多出来这么多个原子；负数很常见（反应物上掉下去了基团），不是问题。
            有结构读不出来时返回 0，当作没有问题。
        谁在用：
            本类的 find()：超过 max_unaccounted_atoms（现在是 4）就认为反应物没给全，不按指纹找相似先例。
        """
        # records[0] 是产物，后面的是各个反应物
        records = [chem.mol_record(smiles) for smiles in (product, *reactants)]
        if any(record is None for record in records):
            return 0
        return records[0].heavy_atoms - sum(record.heavy_atoms for record in records[1:])

    @staticmethod
    def _dedupe(rows: list[models.ReactionPrecedent]) -> list[models.ReactionPrecedent]:
        """去掉重复的先例：同一篇文献（document_id）里的同一个反应（reaction_key）只留排在最前面的一条。

        参数：
            rows  先例行的列表。顺序有意义：重复的几条里留下排在前面的那条。
        返回：
            去重以后的新列表，先后顺序不变。不改传进来的列表。
        谁在用：
            本类的 _exact_rows()、_class_rows()、_similar_rows()。
        """
        seen: set[tuple[str, str]] = set()
        unique = []
        for row in rows:
            identity = (row.document_id, row.reaction_key)
            if identity not in seen:
                seen.add(identity)
                unique.append(row)
        return unique

    # ------------------------------------------------------------ 定级

    def _grade(
        self,
        n_exact: int,
        best: float | None,
        model_probability: float | None,
        related: float | None = None,
        unverified: bool = False,
    ) -> tuple[str, float, str]:
        """根据找到了什么样的先例，定证据等级和文献分。

        best：同类反应先例里最高的底物相似度；related：没有同类先例时，相关反应里最高的相似度；
        unverified：这些相关的先例全都没带原子映射，没法确认是不是同一种转化。

        参数：
            n_exact            “同一反应”的先例有几条。
            best               见上；没有同类先例是 None。
            model_probability  逆合成模型给这一步的概率（0–1），没有是 None。只在 E4 时用。
            related            见上；有同类先例、或者什么都没找到时是 None。
            unverified         见上。只影响说明文字，不影响等级和分数。
        返回：
            (等级, 文献分, 说明文字)。文献分这里不取整，find() 再保留一位小数。
        规则（从上往下，先满足哪条就用哪条；分数段是 scoring.toml 里 [evidence.levels.*] 的 band，括号里是现在的值）：
            E1  有同一反应的先例。分数段 90–100：1 条 93.3，2 条 96.7，3 条及以上 100。
            E2  同类先例的底物相似度 ≥ high_similarity_threshold（0.7）。分数段 70–90：
                相似度从 0.7 到 1.0，分数从 70 到 90 按比例对应。
            E3  同类先例的底物相似度 ≥ low_similarity_threshold（0.2）。分数段 40–70：
                相似度从 0.2 到 0.7，分数从 40 到 70 按比例对应。
            E3  没有同类先例，只有相关反应。只给 E3 分数段最下面的一段：
                40 +（70 − 40）× related_score_share（0.3）× 相似度，也就是 40 到 49 之间。
            E4  什么都没找到。分数段 0–40：没有模型概率时给 no_precedent_score（15）；
                有的话是 15 +（40 − 15）× 概率，也就是 15 到 40 之间。
        谁在用：
            本类的 find()。
        """
        levels = self.config["levels"]
        high = float(self.config["high_similarity_threshold"])
        low = float(self.config["low_similarity_threshold"])
        if n_exact:
            # band 是这个等级的分数段 [下限, 上限]。先例越多越接近上限，3 条封顶
            lo, hi = levels["E1"]["band"]
            # 英文分单复数（1 precedent reports / 3 precedents report），中文是同一句
            return "E1", lo + (hi - lo) * min(1.0, n_exact / 3), i18n.tr(
                "{n} 个先例报道了同一个反应",
                "{n} precedent reports the same reaction" if n_exact == 1 else "{n} precedents report the same reaction",
                n=n_exact)
        if best is not None and best >= high:
            lo, hi = levels["E2"]["band"]
            # 相似度在 high 到 1 之间走了多远，分数就在分数段里走多远。max(1e-9, …) 防止阈值配成 1 时除以 0
            score = lo + (hi - lo) * (best - high) / max(1e-9, 1 - high)
            return "E2", score, i18n.tr("找到同类反应的先例，底物相似度最高 {best:.2f}",
                                        "Precedents of the same reaction type found; highest substrate similarity {best:.2f}",
                                        best=best)
        if best is not None and best >= low:
            lo, hi = levels["E3"]["band"]
            # 同样的算法：相似度在 low 到 high 之间的位置，对应到 E3 的分数段里
            score = lo + (hi - lo) * (best - low) / max(1e-9, high - low)
            return "E3", score, i18n.tr(
                "只找到底物差异较大的同类反应，相似度最高 {best:.2f}",
                "Only same-type reactions on rather different substrates were found; highest similarity {best:.2f}",
                best=best)
        if related is not None:
            # 产物相近、但不是同一种转化（比如先例用酰氯，这一步用羧酸）：只能算很弱的旁证，落在 E3 的最低一段
            lo, hi = levels["E3"]["band"]
            share = float(self.config.get("related_score_share", 0.3))
            basis = (
                i18n.tr("只找到没带原子映射的相近先例，没法确认是不是同一种转化，只当旁证",
                        "Only close precedents without atom mapping were found; they cannot be confirmed as "
                        "the same transformation and count only as supporting evidence")
                if unverified
                else i18n.tr("没有找到同类反应的先例，只有做法不同的相关反应",
                             "No precedents of the same reaction type were found; only related reactions done differently")
            )
            return "E3", lo + (hi - lo) * share * min(1.0, related), basis
        lo, hi = levels["E4"]["band"]
        base = float(self.config.get("no_precedent_score", 15))
        if model_probability is None:
            return "E4", base, i18n.tr("未找到先例", "No precedents found")
        # 概率先限制在 0 到 1 之间，再把 base 到分数段上限之间的那一截按概率给；min(hi, …) 保证不超出分数段
        score = min(hi, base + (hi - base) * max(0.0, min(1.0, model_probability)))
        return "E4", score, i18n.tr("未找到先例；只有模型依据（单步模型概率 {probability:.2f}）",
                                    "No precedents found; model evidence only (single-step model probability {probability:.2f})",
                                    probability=model_probability)

    def _yield_info(self, precedents: list[dict[str, Any]], level: str,
                    exact_yields: list[float] | None = None) -> dict[str, Any]:
        """只用同一反应（E1）或高相似先例（E2）报道的产率；更远的先例不拿来当这一步的产率。

        参数：
            precedents    find() 拼好的先例列表（最多 max_precedents 条，已经带 ref 编号）。
            level         这一步的证据等级。
            exact_yields  库里查到的全部“同一反应”记录的产率（没有产率的不算）。只在 E1 时用。
        返回（一个字典）：
            没有可用的产率时：{"source": "none", "label": "无产率记录", "refs": []}
            有可用的产率时：
                source  "literature"（E1，同一反应报道的）或 "similar_literature"（E2，高相似先例报道的）
                low     最低的产率（百分数）
                high    最高的产率
                median  中位数，一位小数。M10 拿它当“产率”这一维的分数
                n       用了几条先例
                label   显示文字，比如 “文献报道 85–92%（2 个先例）”
                refs    用到的先例里列在证据卡上的那几条的编号，比如 ["P1", "P2"]
        哪些先例的产率算数：
            E1      库里全部的同一反应记录（exact_yields）。同一反应的记录多于 max_precedents 条时，
                    卡上只列产率最高的几条，但区间、中位数和 n 是按全部记录算的，所以 n 可能比 refs 多。
            E2      列表里是同类反应、并且底物相似度不低于 high_similarity_threshold 的。
                    这一种统计的只是列在证据卡上的那几条先例，不是库里全部的。
            E3、E4  都不用。
        谁在用：
            本类的 find()。
        """
        if level == "E1":
            usable = [p for p in precedents if p["exact"]]
        elif level == "E2":
            high = float(self.config["high_similarity_threshold"])
            usable = [p for p in precedents if p["similarity"] >= high and p["same_class"]]
        else:
            usable = []
        # 其中有产率记录的才用得上
        reported = [p for p in usable if p["yield_pct"] is not None]
        if not reported:
            return {"source": "none", "label": _NO_YIELD_LABEL, "refs": []}
        # E1：用全部同一反应记录的产率；没传进来（或者是 E2）就用列在卡上的这几条
        values = list(exact_yields) if level == "E1" and exact_yields else [p["yield_pct"] for p in reported]
        low, high_value = min(values), max(values)
        # “:g” 这种格式会去掉多余的 0（85.0 写成 85）。最低和最高一样时只写一个数，否则写成范围
        span = f"{low:g}%" if low == high_value else f"{low:g}–{high_value:g}%"
        origin = (i18n.tr("文献报道", "Reported in the literature") if level == "E1"
                  else i18n.tr("相似先例报道", "Reported for similar precedents"))
        return {
            "source": "literature" if level == "E1" else "similar_literature",
            "low": low,
            "high": high_value,
            "median": round(statistics.median(values), 1),
            "n": len(values),
            # 整句经过 i18n.tr 才有英文（origin 那一段有自己的对照，会自动换上）；英文分单复数
            "label": i18n.tr("{origin} {span}（{n} 个先例）",
                             "{origin}: {span} ({n} precedent)" if len(values) == 1 else "{origin}: {span} ({n} precedents)",
                             origin=origin, span=span, n=len(values)),
            "refs": [p["ref"] for p in reported],
        }

    def _to_dict(self, row: models.ReactionPrecedent, *, similarity: float, class_similarity: float, exact: bool,
                 same_class: bool = True, compared_by: str = "fingerprint"):
        """把一条先例（数据库里的一行）变成证据卡里的一项。

        参数：
            row               先例行。
            similarity        底物相似度（产物结构指纹的 Tanimoto，0–1）；同一反应固定传 1.0。
            class_similarity  反应一致的程度（0–1，1 表示完全一致）；同一反应固定传 1.0。
            exact             是不是同一反应。
            same_class        是不是同类反应；False 表示只是相关反应。
            compared_by       这条先例是怎么认定的：exact / center / fingerprint。
        返回的键：
            same_class        见上
            compared_by       见上
            ref               在这一步里的编号（P1、P2……）。这里先留空，find() 排好顺序以后再填
            id                reaction_precedents 表里的 id
            source            数据来源编号（uspto、ord……）
            source_name       来源的名称，取自 data/data_sources.toml；没登记的来源就用编号本身
            doc_type          patent / journal / dataset
            doc_type_label    上一项的中文
            document_id       文献号（专利号等）
            year、doi、title  年份、DOI、题目，没有是 None
            url               原文链接；拼不出能打开的链接时是 None（见 document_url）
            similarity        底物相似度，三位小数
            class_similarity  反应一致程度，三位小数
            exact             是不是同一反应
            yield_pct         这条先例报道的产率（百分数），没有是 None
            reaction_smiles   先例的反应，“反应物>试剂>产物”
            agents            先例用的试剂、溶剂、催化剂，SMILES 列表。专利数据里的盐是拆成离子写的，这里原样给出
            conditions_text   条件的文字描述，没有是 None
            temperature_c     温度（摄氏度），没有是 None
        谁在用：
            本类的 find()。证据卡（C1、C2）、流水线借用试剂和条件、M11 写说明、M13 出报告，读的都是这些键。
        """
        # 来源登记表里这个来源的那一项（有 name、license 等）；没登记时是空字典
        source = self.reference.data_sources.get(row.source, {})
        return {
            "same_class": same_class,  # False：只是相关反应（产物相近，做法不同），不算同类反应的先例
            # 这条先例是怎么认定的：exact 同一反应；center 按反应中心；fingerprint 按指纹（粗，先例或这一步没有原子映射）
            "compared_by": compared_by,
            "ref": "",
            "id": row.id,
            "source": row.source,
            "source_name": source.get("name", row.source),
            "doc_type": row.doc_type,
            "doc_type_label": DOC_TYPE_LABELS.get(row.doc_type, row.doc_type),
            "document_id": row.document_id,
            "year": row.year,
            "doi": row.doi,
            "title": row.title,
            "url": document_url(row.doc_type, row.document_id, row.doi),
            "similarity": round(similarity, 3),
            "class_similarity": round(class_similarity, 3),
            "exact": exact,
            "yield_pct": row.yield_pct,
            "reaction_smiles": row.reaction_smiles,
            # 存的是用 “.” 连起来的一个字符串，这里拆成列表；空的片段不要
            "agents": [a for a in (row.agents or "").split(".") if a],
            "conditions_text": row.conditions_text,
            "temperature_c": row.temperature_c,
        }
