"""参考数据：data/ 目录下由化学和数据同学维护的表格，启动时读进内存。

    compounds.csv              常用试剂：名称、CAS、结构、危害分类初稿、溶剂评级、能否买到
    controlled_substances.csv  各地管制清单初稿
    scoring.toml               评分权重和阈值
    condition_rules.toml       条件与复杂度规则
    vocabulary.toml            约束词表
    data_sources.toml          数据来源和许可登记表

---- 在整个系统里的位置 ----

这个文件不单属于 PPT 里的哪一个模块，是各个模块共用的“查表”层：规则、阈值、试剂表都从这里拿，不写死在代码里。
context.build_context() 在每个进程（API、Worker）启动时调一次 ReferenceData.load()，结果放在
AppContext.reference 上，整个进程共用，之后只读不改。所以：
    · 改了 data/ 下的表格，要重启服务才生效；
    · 表格有错（试剂表里的 SMILES 读不出来、规则引用了不存在的物质）时，服务在启动时就报错停下，不会带着错的数据跑。
tests/test_reference.py 专门检查这些表格自己是否自洽，改完表格可以跑一下。

不读写数据库，不联网。只依赖 m01_structure（解析 SMILES、算 key14、匹配子结构）。

---- 每个文件读成什么 ----

ReferenceData 有六个属性，各对应一个文件：

  compounds  <-  compounds.csv
      读成 CompoundLibrary：Compound 对象的列表，加上按编号、结构、CAS 号、名称查找的几个字典。
      每行一种常用试剂，各列是：
        key            唯一的英文编号，比如 acetic_anhydride。condition_rules.toml、vocabulary.toml 用它指物质
        name_zh        中文名
        name_en        英文名
        synonyms       别名、缩写，用 | 隔开
        cas            CAS 号
        smiles         结构
        roles          用途，用 | 隔开。表里出现过的值：solvent、reagent、base、acid、catalyst、building_block、
                       gas、product、ubiquitous。代码只读其中两个：ubiquitous（随手可得，M4 不把它算作要准备的
                       原料，现在只有水）和 solvent（M9 只给标了它的物质算溶剂评级）
        purchasable    1 表示能买到：启动时写进在售目录（db.init_db），也交给逆合成引擎当“可以停下来的原料”
        h_codes        GHS 危险性说明的编码，用 ; 隔开。留空 = 不属于危险品；写 ? = 不知道，M6 会去查 PubChem
        solvent_class  溶剂评级：recommended / problematic / hazardous / highly_hazardous；不是溶剂就留空
        reviewed       1 表示危害分类有人复核过；0 表示还没有，报告里会提示
      谁在用：M1 按名称查结构、M2 认物质名、M4、M6、M7、M9，以及流水线和接口给分子取名。

  controlled  <-  controlled_substances.csv
      读成字典 {key14: [ControlledEntry, …]}。
      每行是“一种物质在一个地区的一张清单上”，一种物质在三个地区受管制就有三行。各列是：
        name_zh、name_en  名称
        cas               CAS 号
        smiles            结构。没写结构、或者结构读不出来的行会被跳过
        jurisdiction      地区：CN / US / CA / TW（读进来统一转成大写）
        list_name         清单的名字，比如 “易制毒化学品”“DEA Listed Chemicals”
        category          清单里的类别，比如 “第一类”“List I”“Class A”
        severity          strict 管得严 / basic 一般管理
        reviewed          1 表示对照官方清单复核过
      谁在用：M6（m06_safety.SafetyService._controlled）。

  scoring  <-  scoring.toml
      整个文件原样读成嵌套的字典，比如 scoring["evidence"]["levels"]["E1"]["band"] 是 [90, 100]。
      顶层的键，和读它的地方：
        version                              配置的版本号，记进每次分析的结果
        weights、dimension_labels、dimension_short_labels、route、confidence、soft_preference、weakest_link
                                             M10 评分与排序（m10_scoring.py），证据卡（cards.py）
        evidence                             M5 证据等级的阈值和分数段（m05_precedents.py）
        complexity                           复杂度三档的步数上限（constraints.merge_constraints）
        availability                         可得性一维的分数（m10_scoring.py）
        safety                               pubchem_min_share（M6）和 penalties 各类危害的扣分（M10）
        operation                            操作分的满分和分档线（m07_conditions.py）
        green                                绿色化学的权重和溶剂分数（m09_green.py）
        scale                                三档规模各对应多少毫克（api/reports.py）
      GET /api/scoring/config 和 GET /api/meta/options 把其中一部分给前端。

  condition_rules  <-  condition_rules.toml
      文件里每个 [[rule]] 读成一个 ConditionRule 对象，合成一个列表，顺序和文件里一样。
      规则有哪些键见 ConditionRule。谁在用：M7（m07_conditions.ConditionEvaluator）。

  vocabulary  <-  vocabulary.toml
      原样读成两层的字典：组 -> 条目编号 -> 条目。
        reagent_class  试剂类别。条目有 label（显示文字）、synonyms（用户可能的说法），
                       以及 smarts（子结构）和 substances（compounds.csv 的 key）两者之一或都有
        condition      做不了的条件（A10）。条目有 label、short（短写法）、synonyms
        purification   纯化方式。条目有 label、synonyms
        safety         安全要求（A11）。条目有 label、short、synonyms
        hardness       不是条目，而是两张词表 {hard: [...], soft: [...]}：一句话里出现哪些措辞算硬约束、哪些算软偏好
      谁在用：M2（m02_constraints.py）、constraints.merge_constraints、M10、GET /api/meta/options。

  data_sources  <-  data_sources.toml
      只取文件里 [source.*] 这一层，读成字典 {来源编号: {name, used_for, license, commercial_use, allowed, notes}}
      （notes 可以没有）。
      谁在用：导入数据前检查许可（loaders.py）、M5 显示先例的来源名称、GET /api/meta/data-sources。

data/ 目录下还有两样东西不是这里读的：demo_routes/（内置演示路线，m03_retrosynthesis 读）
和 external/（AiZynthFinder 的模型、USPTO 的原始文件）。

---- 本文件里有什么（按阅读顺序）----

    _NAME_NOISE、normalize_name   比较名称之前的统一处理
    _read_csv、_read_toml         读文件的两个小函数
    Compound                      compounds.csv 的一行
    CompoundLibrary               整张试剂表和它的几个索引
        load                      从 CSV 读
        find_by_smiles / find_by_name / find_by_cas   精确查找
        search                    名称或 CAS 号的模糊查找（A3）
        name_for                  结构 -> 显示名
        combine_ions              把拆开写的离子拼回成盐
    ControlledEntry               管制清单的一行
    ConditionRule                 一条条件规则
    ReferenceData                 六份参考数据放在一起
        load                      读 data/ 目录
        reagent_class_matches     一个物质属不属于某个试剂类别
        label_for                 词表里一个条目的显示文字
        source_allowed            一个数据来源允不允许用
"""

from __future__ import annotations

import csv
import itertools
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from syntherisk.modules import m01_structure as chem

# 名称里不影响含义的字符：空白（\s）、连字符、下划线、间隔号（· •）、逗号（半角和全角）、顿号、
# 圆括号（半角和全角）、方括号。最后的 + 表示连着的一串一起处理
_NAME_NOISE = re.compile(r"[\s\-_·•,，、()（）\[\]]+")


def normalize_name(text: str) -> str:
    """把名称变成便于比较的形式：去掉首尾空格，转成小写，再去掉空格、连字符、括号这类字符。

    这样 “2-Me-THF” 和 “2 me thf” 比较时是相同的（都变成 “2methf”）。
    CAS 号经过它以后连字符也没有了（“69-72-7” 变成 “69727”），所以结果只能用来比较，不能当 CAS 号用。

    参数：
        text  名称；None 当作空字符串。
    返回：
        处理后的字符串，可能是空字符串。
    谁在用：
        本文件的 CompoundLibrary（建名称索引、按名称查）；m01_resolver.py、m02_constraints.py、
        m04_availability.py（搜库存）、inventory_import.py（认表头）。
    """
    # _NAME_NOISE.sub("", 文字)：把匹配到的字符都替换成空字符串，也就是删掉
    return _NAME_NOISE.sub("", (text or "").strip().lower())


def _read_csv(path: Path) -> list[dict[str, str]]:
    """读一个 CSV 文件，返回每一行的字典。第一行是表头。

    参数：
        path  文件路径。
    返回：
        列表，每一行是 {列名: 值}。值都是去掉了首尾空格的字符串，空的单元格是空字符串。
    细节：
        用 utf-8-sig 编码打开：Excel 另存的 CSV 开头常带一个看不见的 BOM 标记，这个编码会把它去掉。
        newline="" 是 csv 模块要求的写法，换行交给它自己处理。
    抛出：
        FileNotFoundError  文件不存在。
    谁在用：
        CompoundLibrary.load()、ReferenceData.load()。
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        # csv.DictReader 把每一行变成 {列名: 值}；某一行比表头短时，缺的值是 None，这里换成空字符串
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def _read_toml(path: Path) -> dict[str, Any]:
    """读一个 TOML 文件，返回字典。

    TOML 里的 [表] 变成嵌套的字典，[[表]] 变成字典的列表，值是字符串、数字、布尔值或列表。
    tomllib 是 Python 自带的模块，要求用二进制方式（"rb"）打开文件。

    抛出：
        FileNotFoundError        文件不存在。
        tomllib.TOMLDecodeError  文件写得不合 TOML 语法。
    谁在用：
        ReferenceData.load()。
    """
    with path.open("rb") as handle:
        return tomllib.load(handle)


# ---------------------------------------------------------------- 内置常用试剂表


@dataclass
class Compound:
    """内置常用试剂表（data/compounds.csv）的一行。

    @dataclass 让 Python 按下面列出的字段自动生成构造函数，不用手写 __init__。
    """

    key: str  # 表里的唯一编号（英文，下划线连接），比如 acetic_anhydride
    name_zh: str  # 中文名，可以是空字符串
    name_en: str  # 英文名，可以是空字符串
    synonyms: list[str]  # 别名和缩写
    cas: str  # CAS 号
    smiles: str  # 规范 SMILES（读表时由 RDKit 重新写过，不一定和表里的写法一样）
    key14: str  # InChIKey 前 14 位：按结构找这个物质时用的键，不分立体异构
    inchikey: str  # 完整的 InChIKey
    roles: set[str]  # 用途的集合，比如 {"solvent", "ubiquitous"}；可取的值见文件头
    purchasable: bool  # 能不能买到
    h_codes: list[str] | None  # 空 = 不属于危险品；None（表里写 “?”）= 不知道，要去查
    solvent_class: str | None  # 溶剂评级 recommended / problematic / hazardous / highly_hazardous；不是溶剂是 None
    reviewed: bool  # 危害分类有没有人复核过

    @property
    def display_name(self) -> str:
        """显示用的名字：有中文名用中文名，没有就用英文名，都没有就用 key。

        @property 的意思是把它当属性用：写 compound.display_name，后面不加括号。
        谁在用：m01_resolver.py、m02_constraints.py、m06_safety.py、m09_green.py、db.py、cli.py、api/analyses.py。
        """
        return self.name_zh or self.name_en or self.key

    @property
    def all_names(self) -> list[str]:
        """这个物质的所有叫法：中文名、英文名、各个别名，去掉空的。

        谁在用：CompoundLibrary.__init__() 建名称索引。
        """
        return [n for n in (self.name_zh, self.name_en, *self.synonyms) if n]


class CompoundLibrary:
    """整张内置试剂表，加上几个查找用的字典。建好以后只读。

    属性：
        compounds          Compound 的列表，顺序和 CSV 里一样。
        by_key             {key: Compound}
        by_key14           {key14: Compound}，按结构查。表里两行结构相同时留靠前的那一行。
        by_cas             {CAS 号: Compound}
        by_name            {处理过的名称: Compound}。中文名、英文名、每个别名各占一项，
                           名称都先经过 normalize_name。两个物质有同样的叫法时留表里靠前的那个。
        _salts_by_formula  {分子式: Compound}，只收盐和金属化合物。combine_ions() 用它把离子认回成盐。
    """

    def __init__(self, compounds: list[Compound]):
        """建上面那几个查找用的字典。

        参数：
            compounds  读好的 Compound 列表。
        谁在用：
            本类的 load()。
        """
        self.compounds = compounds
        self.by_key = {c.key: c for c in compounds}
        self.by_key14: dict[str, Compound] = {}
        self.by_cas: dict[str, Compound] = {}
        self.by_name: dict[str, Compound] = {}
        self._salts_by_formula: dict[str, Compound] = {}  # 盐和金属化合物，按分子式找：离子写法和共价写法的分子式一样
        for compound in compounds:
            # is_salt_like：含金属或者带电荷的原子。mol_record(...).formula 是分子式，比如 AlCl3
            if chem.is_salt_like(compound.smiles):
                self._salts_by_formula.setdefault(chem.mol_record(compound.smiles).formula, compound)
            # setdefault：这个键还没有才登记，所以键重复时留下的是表里靠前的那一行
            self.by_key14.setdefault(compound.key14, compound)
            if compound.cas:
                self.by_cas.setdefault(compound.cas, compound)
            for name in compound.all_names:
                self.by_name.setdefault(normalize_name(name), compound)

    @classmethod
    def load(cls, path: Path) -> "CompoundLibrary":
        """从 compounds.csv 读出整张试剂表。

        参数：
            path  CSV 文件的路径。各列的含义见文件头。
        返回：
            建好索引的 CompoundLibrary。
        抛出：
            ValueError  某一行的 SMILES 读不出来。报错里写明是哪一行（它的 key）。
            KeyError    表头里缺了要用的列。
        副作用：
            读一个文件；每一行的 SMILES 都用 RDKit 解析一遍。
        谁在用：
            ReferenceData.load()；tests/test_reference.py。
        @classmethod 的意思是它挂在类上，不用先有对象：写 CompoundLibrary.load(路径) 就得到一个对象。
        """
        compounds = []
        for row in _read_csv(path):
            # 解析结构，同时得到规范 SMILES、InChIKey 和 key14
            record = chem.mol_record(row["smiles"])
            if record is None:
                raise ValueError(f"{path.name}：{row['key']} 的 SMILES 读不出来：{row['smiles']}")
            compounds.append(
                Compound(
                    key=row["key"],
                    name_zh=row["name_zh"],
                    name_en=row["name_en"],
                    # 别名用 | 隔开：拆成列表，去掉空的
                    synonyms=[s.strip() for s in row["synonyms"].split("|") if s.strip()],
                    cas=row["cas"],
                    smiles=record.smiles,
                    key14=record.key14,
                    inchikey=record.inchikey,
                    roles={r for r in row["roles"].split("|") if r},
                    # 表里写 1 才算能买到
                    purchasable=row["purchasable"] == "1",
                    # “?” 读成 None（不知道）；其余按 ; 拆开，留空的话得到空列表（不属于危险品）
                    h_codes=None if row["h_codes"] == "?" else [h for h in row["h_codes"].split(";") if h],
                    solvent_class=row["solvent_class"] or None,
                    reviewed=row["reviewed"] == "1",
                )
            )
        return cls(compounds)

    def find_by_smiles(self, smiles: str) -> Compound | None:
        """按结构查。

        参数：
            smiles  任意写法的 SMILES。
        返回：
            表里的 Compound；结构读不出来、或者表里没有这个结构时返回 None。
            按 key14 比，所以不分立体异构。
        谁在用：
            本文件的 name_for()、ReferenceData.reagent_class_matches()；m01_resolver.py、m09_green.py、
            api/analyses.py 的 mark_owned。
        """
        key = chem.key14_of(smiles)
        return self.by_key14.get(key) if key else None

    def find_by_name(self, text: str) -> Compound | None:
        """按名称查，要完全一致（比较前两边都经过 normalize_name，所以不分大小写，空格、连字符也不计）。

        参数：
            text  中文名、英文名或者别名。
        返回：
            Compound；表里没有这个叫法时返回 None。
        谁在用：
            m01_resolver.py（导入库存时认名称）、m02_constraints.py（一句话要求里点名的物质）。
        """
        return self.by_name.get(normalize_name(text))

    def find_by_cas(self, cas: str) -> Compound | None:
        """按 CAS 号查，要完全一致（只去掉首尾空格）。

        参数：
            cas  CAS 号；None 当作空字符串。
        返回：
            Compound；表里没有这个 CAS 号时返回 None。
        谁在用：
            本类的 search()；m01_resolver.py（导入库存时认 CAS 号）。
        """
        return self.by_cas.get((cas or "").strip())

    def search(self, text: str, limit: int = 8) -> list[Compound]:
        """名称或 CAS 的模糊查找：完全一致的排前面，其次是包含关系。

        参数：
            text   用户输入的名称或 CAS 号。
            limit  最多返回几个。
        返回：
            Compound 的列表：CAS 号或名称完全一致的那个排第一，后面是名称里包含搜索词的。没有就是空列表。
            搜索词处理后不足 3 个字符时，只返回完全一致的那个。
        谁在用：
            m01_resolver.NameResolver.lookup()（A3 按名称或 CAS 号查候选结构时，先查这张表）。
        """
        needle = normalize_name(text)
        if not needle:
            return []
        # 先找完全一致的：当 CAS 号查（用原文），查不到再当名称查（用处理过的）
        exact = self.find_by_cas(text) or self.by_name.get(needle)
        hits = [exact] if exact else []
        if len(needle) < 3:
            return hits  # 一两个字符只接受完全一致，否则会带出一大堆不相干的结果
        # 再找名称里包含搜索词的。by_name 的顺序就是表里的顺序；同一个物质的几个叫法都对上时只收一次
        for name, compound in self.by_name.items():
            if needle in name and compound not in hits:
                hits.append(compound)
                if len(hits) >= limit:
                    break
        return hits

    def name_for(self, smiles: str) -> str | None:
        """结构 -> 显示名。表里没有这个结构（或者结构读不出来）时返回 None。

        谁在用：
            给分子、原料、约束标签取名的地方：pipeline.py、routes.py、constraints.py、m10_scoring.py、
            api/analyses.py、api/structures.py。
        """
        compound = self.find_by_smiles(smiles)
        return compound.display_name if compound else None

    def combine_ions(self, fragments: list[str]) -> list[str]:
        """把拆开写的离子拼回成盐。

        专利数据里的试剂是一个个片段，盐被拆成了离子（三氯化铝写成 [Al+3]、[Cl-]、[Cl-]、[Cl-]）。
        不拼回去的话，试剂栏里是一堆离子，查不到名字，也查不到危害。
        做法：在带电荷的片段里找电中性的组合，分子式和表里某个盐对得上，就换成表里的写法；
        剩下的离子如果合起来电中性，就合成一个表里没有的盐；还剩单个的离子原样留着。

        参数：
            fragments  SMILES 的列表（先例里的试剂，一个片段一项）。
        返回：
            新的 SMILES 列表：先是不带电荷的片段（保持原来的先后），然后是认出来的盐，
            最后是合成的“表里没有的盐”或者凑不成对的离子；重复的只留一个。不改传进来的列表。
            片段里没有带电荷的，或者带电荷的超过 12 个，就只去重，不拼。
        谁在用：
            pipeline.py 的 _adopt_precedent_reagents（借用先例的试剂之前）；tests/test_reference.py。
        """
        # 带电荷的片段在列表里的位置（net_charge 是整个片段的净电荷）
        charged = [index for index, fragment in enumerate(fragments) if chem.net_charge(fragment) != 0]
        if not charged or len(charged) > 12:
            # dict.fromkeys(列表) 去掉重复的并保持先后顺序
            return list(dict.fromkeys(fragments))
        # result：已经定下来的；pool：还没有归属的离子
        result = [fragment for index, fragment in enumerate(fragments) if index not in charged]
        pool = [fragments[index] for index in charged]

        def take_known_salt() -> bool:
            """在 pool 里找一组电荷加起来是 0、分子式又和表里某个盐相同的离子。

            找到：把表里那个盐的 SMILES 加进 result，把这几个离子从 pool 里拿掉，返回 True。
            找不到：返回 False。
            """
            # 从离子多的组合试起（最多 6 个一组），一直试到 2 个一组
            for size in range(min(len(pool), 6), 1, -1):
                # itertools.combinations(序列, n) 给出从中取 n 个的所有取法；这里取的是离子在 pool 里的位置
                for chosen in itertools.combinations(range(len(pool)), size):
                    ions = [pool[i] for i in chosen]
                    if sum(chem.net_charge(ion) for ion in ions) != 0:
                        continue
                    # 把这几个离子用 “.” 连成一个多组分的 SMILES，算它的分子式，到表里去对
                    record = chem.mol_record(".".join(ions))
                    compound = self._salts_by_formula.get(record.formula) if record else None
                    if compound is not None:
                        result.append(compound.smiles)
                        # 从后往前删：先删位置靠后的，前面的位置才不会错开
                        for i in sorted(chosen, reverse=True):
                            del pool[i]
                        return True
            return False

        # 一次认出一个盐，认到认不出来为止
        while len(pool) > 1 and take_known_salt():
            pass
        if pool and sum(chem.net_charge(ion) for ion in pool) == 0:
            # 剩下的离子合起来是电中性的：当成一个表里没有的盐（规范 SMILES 写不出来时就直接用 “.” 连）
            result.append(chem.canonical_smiles(".".join(pool)) or ".".join(pool))
        else:
            # 凑不成电中性：各自原样留着
            result.extend(pool)
        return list(dict.fromkeys(result))


# ---------------------------------------------------------------- 管制清单、条件规则


@dataclass
class ControlledEntry:
    """管制清单（data/controlled_substances.csv）的一行：一种物质在一个地区的一张清单上。"""

    name: str  # 显示名：有中文名用中文名，否则用英文名
    cas: str  # CAS 号
    jurisdiction: str  # 地区代码，大写：CN / US / CA / TW
    list_name: str  # 清单的名字，比如 “易制毒化学品”
    category: str  # 清单里的类别，比如 “第一类”“List I”
    # 管得多严。strict：用户勾了“排除易制毒 / 管制化学品”时，用到它的路线会被排除；basic：只提示，评分时少量扣分
    severity: str  # strict / basic
    reviewed: bool  # 有没有对照官方清单复核过

    def to_dict(self) -> dict[str, Any]:
        """变成放进危害评估结果、返回给前端的字典。

        返回：
            {name, jurisdiction, list, category, severity, reviewed}。
            注意清单名的键叫 list（不是 list_name）；CAS 号不在里面。
        谁在用：
            m06_safety.SafetyService._controlled()。
        """
        return {
            "name": self.name,
            "jurisdiction": self.jurisdiction,
            "list": self.list_name,
            "category": self.category,
            "severity": self.severity,
            "reviewed": self.reviewed,
        }


@dataclass
class ConditionRule:
    """一条条件与复杂度规则：data/condition_rules.toml 里的一个 [[rule]]。字段名和 TOML 里的键一一对应。

    前六个字段每条规则都要写；后面的可以不写。
    substances、smarts、class_keywords、text_keywords、两个温度是“命中条件”，它们之间是“或”的关系：
    任何一项对上，这条规则就命中。规则由 M7（m07_conditions.ConditionEvaluator）执行。
    """

    id: str  # 规则编号，比如 pressurized_gas。证据卡里的出处写成 “RULE:编号”
    label: str  # 标签文字，比如 “加压气体反应”
    short: str  # 短标签，比如 “需加压气体”，显示在 “操作 中等 · ” 的后面
    category: str  # 归类：设备 / 气氛 / 温度 / 安全设施 / 纯化
    penalty: float  # 命中以后从操作分（满分 100）里扣多少分
    reason: str  # 为什么算复杂，一句话，显示在证据卡上
    # 对应 A10 “做不了的条件”里的哪一项（vocabulary.toml 的 condition 组的编号）。
    # 用户勾了那一项，命中这条规则的路线就被排除；None 表示这条规则只扣分
    constraint: str | None = None
    # 对应哪一种纯化方式（vocabulary.toml 的 purification 组的编号）。
    # 用户要求不用这种纯化方式时排除路线，只是希望避免时扣分
    purification: str | None = None
    # 下面四个列表的 field(default_factory=list) 表示：没写时每个对象各自得到一个新的空列表
    # 这一步用到其中任何一个物质就命中。写的是 compounds.csv 里的 key
    substances: list[str] = field(default_factory=list)
    # 这一步的某个物质含有其中任何一个子结构（SMARTS）就命中
    smarts: list[str] = field(default_factory=list)
    # 反应名称里出现其中任何一个词就命中，不分大小写
    class_keywords: list[str] = field(default_factory=list)
    # 先例的条件文字里出现其中任何一个词就命中，不分大小写
    text_keywords: list[str] = field(default_factory=list)
    max_temperature: float | None = None  # 先例记录的温度不高于它（摄氏度）就命中
    min_temperature: float | None = None  # 先例记录的温度不低于它（摄氏度）就命中


# ---------------------------------------------------------------- 六份参考数据放在一起


@dataclass
class ReferenceData:
    """data/ 目录下的六份参考数据。启动时读一次，放在 AppContext.reference 上，之后只读。

    每个属性从哪个文件来、是什么形状，见文件头“每个文件读成什么”。
    """

    compounds: CompoundLibrary  # compounds.csv：内置常用试剂表
    controlled: dict[str, list[ControlledEntry]]  # controlled_substances.csv：{key14: [清单条目, …]}
    scoring: dict[str, Any]  # scoring.toml：整个文件原样读成的嵌套字典
    condition_rules: list[ConditionRule]  # condition_rules.toml：规则的列表，顺序同文件
    vocabulary: dict[str, Any]  # vocabulary.toml：{组: {条目编号: 条目}}
    data_sources: dict[str, Any]  # data_sources.toml：{来源编号: {name, license, allowed, …}}

    @classmethod
    def load(cls, data_dir: Path) -> "ReferenceData":
        """把 data/ 目录下的六个文件读进内存。

        参数：
            data_dir  参考数据所在的目录（配置里的 DATA_DIR，默认是项目根目录下的 data/）。
        返回：
            ReferenceData。
        抛出（都发生在启动的时候，服务会起不来）：
            FileNotFoundError  少了某个文件。
            ValueError         compounds.csv 里有 SMILES 读不出来；或者某条规则引用了 compounds.csv 里没有的物质。
            TypeError          condition_rules.toml 里某条规则缺了必写的键，或者写了 ConditionRule 没有的键。
            KeyError           CSV 的表头里缺了要用的列。
        副作用：
            读六个文件；试剂表和管制清单里的 SMILES 都要解析一遍。
        谁在用：
            context.build_context()（每个进程启动时一次）；tests/test_reference.py。
        """
        compounds = CompoundLibrary.load(data_dir / "compounds.csv")

        # 管制清单：按物质的 key14 归到一起。同一种物质可以有好几行（不同的地区、不同的清单）
        controlled: dict[str, list[ControlledEntry]] = {}
        for row in _read_csv(data_dir / "controlled_substances.csv"):
            key = chem.key14_of(row["smiles"]) if row["smiles"] else None
            if key is None:
                # 没写结构，或者结构读不出来：这一行没法按结构匹配，跳过，不报错
                continue
            # setdefault(key, [])：这个物质第一次出现时先放一个空列表，然后往里加
            controlled.setdefault(key, []).append(
                ControlledEntry(
                    name=row["name_zh"] or row["name_en"],
                    cas=row["cas"],
                    # 地区代码统一成大写，和配置里的 CONTROLLED_JURISDICTIONS 才对得上
                    jurisdiction=row["jurisdiction"].upper(),
                    list_name=row["list_name"],
                    category=row["category"],
                    severity=row["severity"],
                    reviewed=row["reviewed"] == "1",
                )
            )

        rules = []
        # TOML 里每个 [[rule]] 是一个字典，合起来是 "rule" 这个键下面的一个列表
        for raw in _read_toml(data_dir / "condition_rules.toml").get("rule", []):
            # ConditionRule(**raw)：把字典的键值当成关键字参数传给构造函数，所以 TOML 里的键必须和字段名一致
            rule = ConditionRule(**raw)
            # 规则里点名的物质必须是 compounds.csv 里有的 key；写错了在启动时就报出来
            unknown = [s for s in rule.substances if s not in compounds.by_key]
            if unknown:
                raise ValueError(f"condition_rules.toml：规则 {rule.id} 引用了不存在的物质 {unknown}")
            rules.append(rule)

        return cls(
            compounds=compounds,
            controlled=controlled,
            scoring=_read_toml(data_dir / "scoring.toml"),
            condition_rules=rules,
            vocabulary=_read_toml(data_dir / "vocabulary.toml"),
            # 来源登记表只取 [source.*] 这一层，得到 {来源编号: {…}}
            data_sources=_read_toml(data_dir / "data_sources.toml").get("source", {}),
        )

    # ------------------------------------------------------------ 词表

    def reagent_class_matches(self, class_key: str, smiles: str) -> bool:
        """一个物质属不属于词表里的某个试剂类别（比如“钯催化剂”“卤代溶剂”）。

        参数：
            class_key  试剂类别的编号，也就是 vocabulary.toml 里 [reagent_class.编号] 的那个编号，比如 palladium。
            smiles     物质的 SMILES。
        返回：
            True / False。词表里没有这个类别时是 False。
        判断办法（满足一条就算）：
            这个物质是类别里点名的物质之一（substances，写的是 compounds.csv 的 key）；
            这个物质含有类别里列的某个子结构（smarts，比如 [Pd] 表示含钯）。
        谁在用：
            m10_scoring.py 的 exclusion_reasons()（用户排除了这类试剂）和 _soft_hits()（用户希望少用这类试剂）；
            tests/test_reference.py。
        """
        spec = self.vocabulary.get("reagent_class", {}).get(class_key)
        if not spec:
            return False
        # 先看是不是点名的物质：按结构在试剂表里找到它，再看它的 key 在不在名单里
        compound = self.compounds.find_by_smiles(smiles)
        if compound and compound.key in spec.get("substances", []):
            return True
        # 再看子结构：只要有一个 SMARTS 匹配上就算
        return any(chem.matches_smarts(smiles, pattern) for pattern in spec.get("smarts", []))

    def label_for(self, group: str, key: str, *, short: bool = False) -> str:
        """词表里一个条目的显示文字。

        参数：
            group  词表的组：reagent_class / condition / purification / safety。
            key    条目的编号，比如 cryogenic。
            short  True 时优先用短写法（条目里的 short，比如 “低温”），没有短写法就用 label。必须用关键字传。
        返回：
            显示文字；词表里没有这个条目时返回 key 本身。
        谁在用：
            constraints.merge_constraints()（约束标签上的文字）、m02_constraints.py、m10_scoring.py。
        """
        spec = self.vocabulary.get(group, {}).get(key, {})
        # 依次试：短写法（只在 short=True 时）、label、编号本身，用第一个不为空的
        return (spec.get("short") if short else None) or spec.get("label") or key

    def source_allowed(self, source_id: str) -> bool:
        """一个数据来源允不允许用：要在 data_sources.toml 里登记过，并且 allowed = true。

        参数：
            source_id  来源编号，比如 uspto、zinc。
        返回：
            True / False。没登记的来源是 False。
        谁在用：
            loaders.py 的 _check_source()：导入先例库、在售目录之前先问它，不允许就拒绝导入；
            tests/test_reference.py。
        """
        return bool(self.data_sources.get(source_id, {}).get("allowed"))
