"""M1 的另一半：把名称或 CAS 号变成结构。

顺序是：内置常用试剂表 → PubChem → OPSIN。
A3（名称或 CAS 号查候选结构）和 E1（导入库存时逐行识别）都用它。

对应 PPT 的 M1（结构工具）里要查外部数据的那一部分；只做计算的那一半在 m01_structure.py。

三个数据来源：
    内置试剂表  data/compounds.csv，启动时读进内存（reference.CompoundLibrary）。中文名、英文名、别名、CAS 号都能查
    PubChem     美国国立卫生研究院的公开化合物数据库，联网查（external.PubChemClient）。它会限流，见 README
    OPSIN       把英文系统命名（IUPAC 名）直接解析成结构的服务，联网查（external.OpsinClient）。只认英文
后两个可以在配置里关掉（ENABLE_PUBCHEM、ENABLE_OPSIN），关掉时对应的客户端是 None，这里就不查。
外部接口连不上时不会让请求失败：lookup 在返回的提示里说明，resolve_row 把原因记进结果的 notes。

两个入口：
    NameResolver.lookup       A3：用户输入名称或 CAS 号，给出几个候选结构让用户点选
    NameResolver.resolve_row  E1 / E3：库存表里的一行（名称、CAS 号、SMILES 可能只填了一部分），
                              判断它是哪个物质，结果分成 已匹配 / 需确认 / 未识别 三种

谁调用它：
    context.AppContext.resolver   每次用到时新建一个 NameResolver（它只是把三个数据来源包在一起，建起来不费事）
    api/structures.py 的 lookup_structure        GET /api/structures/lookup（A3）
    api/inventory.py 的 add_item                 POST /api/inventory/items（E3 手动添加）
    inventory_import.process_import              E1 导入表格，逐行调 resolve_row
    m02_constraints.ConstraintParser._make_tag   把“不要用某某物质”里的名称变成结构
它依赖：m01_structure（解析结构、画图）、reference（内置试剂表）、external（PubChem、OPSIN 的客户端）。
这个文件自己不读写数据库，也不写文件；发不发网络请求看上面两个客户端有没有给。

本文件里有什么：
    _GRADE_WORDS、_MIXTURE_HINTS、_ASCII_NAME   认规格说明、混合物、纯英文名称用的规则
    Candidate      A3 的一个候选结构
    Resolution     E1 一行的识别结果
    NameResolver   lookup（A3）和 resolve_row（E1），以及它们用到的几个内部方法
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from rdkit import Chem

from syntherisk.external import ExternalServiceError, OpsinClient, PubChemClient, is_cas_number
from syntherisk.modules import m01_structure as chem
from syntherisk.reference import Compound, ReferenceData, normalize_name

# 库存表的“名称”一栏里常夹着规格说明，比如“乙醇 分析纯 99.7%”。这个正则认出规格的部分，
# resolve_row 把它们删掉以后再拿剩下的名字去查。用 | 隔开的各段：
#   \d+(\.\d+)?\s*%        百分比含量：99%、99.5 %（\d+ 是一串数字，(\.\d+)? 是可有可无的小数部分，\s* 是可有可无的空白）
#   ≥ 和 >=                “≥ 99%” 里的符号
#   分析纯 … 试剂级        中文的纯度等级
#   无水、anhydrous、reagent grade
#   \b(AR|CP|GR|HPLC)\b    单独成词的等级缩写。\b 是单词的边界，所以 CARBON 里面的 AR 不算
# re.IGNORECASE：不分大小写。
_GRADE_WORDS = re.compile(
    r"(\d+(\.\d+)?\s*%|≥|>=|分析纯|化学纯|优级纯|色谱纯|试剂级|无水|anhydrous|reagent grade|\b(AR|CP|GR|HPLC)\b)",
    re.IGNORECASE,
)
# 名称（转成小写以后）里出现这些字样，就当它是混合物或溶液，比如 “Pd/C 10%”“1 M HCl in dioxane”。
# 只影响给用户的说明文字：这样的行按主要成分匹配上以后，会注明“混合物或溶液，没有单一结构”
_MIXTURE_HINTS = ("/", "%", "溶液", "缓冲", "solution", "buffer", " in ")
# 整个字符串只由可见的 ASCII 字符组成（空格到 ~，十六进制 20–7E），也就是不含中文。
# OPSIN 只认英文命名，名称通不过这个检查就不发给它。^ 和 $ 表示从头到尾都要符合
_ASCII_NAME = re.compile(r"^[\x20-\x7e]+$")


@dataclass
class Candidate:
    """A3 的一个候选结构：按名称或 CAS 号查到的一个物质。

    NameResolver.lookup() 返回它的列表；接口 GET /api/structures/lookup 把每个转成字典给前端，
    用户点选其中一个，它的 smiles 就成了目标分子。
    """

    # 显示用的名称。内置试剂表的是中文名（没有中文名时用英文名）；PubChem 的是它的标题；OPSIN 的就是用户输入的名称
    name: str
    # 规范 SMILES
    smiles: str
    # InChIKey 的前 14 个字符。lookup 用它去重：几个来源查到同一个物质时只留先查到的
    key14: str
    # 分子式
    formula: str
    # 分子量（g/mol）
    mol_weight: float
    # 来源，直接显示给用户：“内置试剂表”“PubChem” 或 “OPSIN”
    source: str
    # 英文名。内置试剂表和 PubChem 的候选有，OPSIN 的没有
    name_en: str | None = None
    # CAS 号。查不到时是 None；PubChem 的候选只有第一个会去查
    cas: str | None = None
    # PubChem 的化合物编号（CID），只有 PubChem 的候选有
    cid: int | None = None
    # 结构小图（160 × 110 的 SVG 文本）；lookup 传 with_svg=False 时是 None
    svg: str | None = None

    def to_dict(self) -> dict:
        """转成普通字典（键就是上面的字段名），给接口当 JSON 返回。谁在用：api/structures.py 的 lookup_structure。"""
        return asdict(self)


@dataclass
class Resolution:
    """E1 导入库存时一行的识别结果。

    NameResolver.resolve_row() 的返回值。三种状态和导入时的处理（inventory_import.process_import）：
        matched             认准了，这一行直接进库存
        needs_confirmation  有一个猜测（smiles 有值），原因写在 reason 里，等用户在 E2 确认、改结构或者忽略
        unrecognized        没认出来，smiles 是 None
    E3 手动添加（api/inventory.py 的 add_item）也用它，但不看状态：认出了结构就带上，认不出也照样记进库存。
    """

    status: str  # matched / needs_confirmation / unrecognized
    # 物质名称：表里填了就用表里的，没填就用认出来的物质的名字（按 SMILES 认的、又不在内置试剂表里时用分子式）
    name: str = ""
    # CAS 号：这一行自己填的，或者内置试剂表里登记的（哪种情况用哪个，见 resolve_row 里的注释）；都没有是空字符串
    cas: str = ""
    # 认出来的结构（SMILES）；没认出来是 None
    smiles: str | None = None
    # 需要确认或者没认出来的原因，给用户看的一句话（notes 里的各条用分号连起来）
    reason: str = ""
    # 靠什么认出来的：“SMILES”“CAS · 内置试剂表”“CAS · PubChem”“名称 · 内置试剂表”“名称 · PubChem”“名称 · OPSIN”；
    # 没认出来是空字符串
    source: str = ""
    # 识别过程中记下的各条说明，比如“CAS 号的校验位不对”“名称是缩写、别名或商品名”
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转成普通字典。谁在用：inventory_import.process_import（存进导入行的 proposal）；api/inventory.py 的 add_item。"""
        return asdict(self)


class NameResolver:
    """名称 / CAS 号 → 结构。把内置试剂表、PubChem、OPSIN 三个来源按固定的先后包在一起。

    对象本身没有状态，只是拿着三个数据来源；由 context.AppContext.resolver 每次现建。
    """

    def __init__(self, reference: ReferenceData, pubchem: PubChemClient | None = None, opsin: OpsinClient | None = None):
        """参数：
            reference  参考数据，这里只用它的内置试剂表 reference.compounds
            pubchem    PubChem 客户端；None 表示不查 PubChem（配置里关了，或者测试里不联网）
            opsin      OPSIN 客户端；None 表示不查 OPSIN
        """
        self.reference = reference
        self.pubchem = pubchem
        self.opsin = opsin

    # ------------------------------------------------------------ A3

    def lookup(self, query: str, limit: int = 5, *, with_svg: bool = True) -> tuple[list[Candidate], list[str]]:
        """返回 (候选结构, 提示)。提示用来告诉用户哪个外部接口暂时用不了。

        A3：按名称或 CAS 号查候选结构。
        参数：
            query     用户输入的名称（中文、英文、别名都行）或 CAS 号
            limit     最多给几个候选，默认 5
            with_svg  要不要给每个候选画一张结构小图（160 × 110）
        返回一个两项的元组：
            候选结构  Candidate 的列表，按查到的先后排：内置试剂表的在前，PubChem 的在后；同一个物质只出现一次
            提示      中文句子的列表；外部接口都正常时是空的
        查的先后：
            1. 内置试剂表（不联网）。名称完全一致的排最前，然后是名称里包含这段文字的
            2. PubChem：候选还不到 limit 个时才查。所以内置表里已经查到一两个时，也还会问一次 PubChem
            3. OPSIN：前两步一个都没查到、输入是纯英文、又不是 CAS 号时才查
        查不到不是错误，返回的就是空列表。query 是空的直接返回 ([], [])。外部接口出故障也不会抛异常，只多一条提示。
        副作用：第 2 步向 PubChem 发 1–3 个网络请求，第 3 步向 OPSIN 发 1 个。不写数据库。
        谁在用：api/structures.py 的 lookup_structure（A3）；
                m02_constraints.ConstraintParser._make_tag（传 limit=1，把物质名称变成结构）。
        """
        query = (query or "").strip()
        warnings: list[str] = []
        candidates: list[Candidate] = []
        # 已经收下的物质的 key14，用来去重
        seen: set[str] = set()

        def add(candidate: Candidate | None) -> None:
            """收下一个候选：不是 None、没出现过、名额没满才收；需要的话顺手画小图。"""
            if candidate and candidate.key14 not in seen and len(candidates) < limit:
                seen.add(candidate.key14)
                if with_svg:
                    candidate.svg = chem.mol_to_svg(candidate.smiles, 160, 110)
                candidates.append(candidate)

        if not query:
            return [], warnings
        # 第 1 步：内置试剂表
        for compound in self.reference.compounds.search(query, limit):
            add(self._from_compound(compound))

        # 第 2 步：PubChem。连不上（或者被限流）时客户端抛 ExternalServiceError，这里接住，换成一句提示
        if self.pubchem and len(candidates) < limit:
            try:
                for candidate in self._pubchem_candidates(query, limit):
                    add(candidate)
            except ExternalServiceError:
                warnings.append("PubChem 暂时连不上，这次没有查它，结果可能不全")

        # 第 3 步：OPSIN。它是按命名规则把名字“翻译”成结构，所以查到的候选名称就是用户输入的那段文字
        if self.opsin and not candidates and _ASCII_NAME.match(query) and not is_cas_number(query):
            try:
                add(self._from_smiles(self.opsin.name_to_smiles(query), name=query, source="OPSIN"))
            except ExternalServiceError:
                warnings.append("OPSIN 暂时连不上")
        return candidates, warnings

    def _from_compound(self, compound: Compound) -> Candidate:
        """内置试剂表里的一行（reference.Compound）→ 候选结构。来源标成“内置试剂表”。

        谁在用：lookup()。
        """
        # 表里的 SMILES 在启动时已经检查过读得出来，所以这里的 record 不会是 None
        record = chem.mol_record(compound.smiles)
        return Candidate(
            name=compound.display_name,
            name_en=compound.name_en or None,
            cas=compound.cas or None,
            smiles=record.smiles,
            key14=record.key14,
            formula=record.formula,
            mol_weight=record.mol_weight,
            source="内置试剂表",
        )

    def _from_smiles(self, smiles: str | None, *, name: str, source: str, **extra) -> Candidate | None:
        """用一个 SMILES 建候选结构（PubChem、OPSIN 查到的结果都走这里）。

        参数：
            smiles  外部接口给的结构；可以是 None（没查到）
            name    显示用的名称
            source  来源：“PubChem” 或 “OPSIN”
            extra   Candidate 的其他字段，原样传过去：name_en、cas、cid
        返回：Candidate；smiles 是 None 或者读不出来时返回 None。
        谁在用：lookup、_pubchem_candidates、resolve_row、_pubchem_resolve。
        """
        record = chem.mol_record(smiles) if smiles else None
        if record is None:
            return None
        return Candidate(
            name=name, smiles=record.smiles, key14=record.key14, formula=record.formula,
            mol_weight=record.mol_weight, source=source, **extra,
        )

    def _pubchem_candidates(self, query: str, limit: int) -> list[Candidate]:
        """到 PubChem 按名称或 CAS 号查，返回最多 limit 个候选结构（A3 用）。

        发的请求：按名称查编号（CID）一次；批量取这些编号的属性一次；
        输入不是 CAS 号时，再给第一个候选查一次 CAS 号。一个都没查到时只有第一次。
        连不上或者被限流时抛 ExternalServiceError，由 lookup() 接住。只有 self.pubchem 不是 None 时才会被调用。
        谁在用：lookup()。
        """
        # 名称 → PubChem 的化合物编号列表，只要前 limit 个
        cids = self.pubchem.cids_by_name(query)[:limit]
        out = []
        for index, props in enumerate(self.pubchem.properties(cids)):
            # 这里向 PubChem 要的属性里，结构是 InChI（另一种写结构的文字格式）：读成分子，下面再写成 SMILES
            mol = Chem.MolFromInchi(props.get("InChI", "")) if props.get("InChI") else None
            if mol is None:
                continue
            cid = props.get("CID")
            # 只给第一个候选查 CAS 号，省一次请求
            # 用户输入的本来就是 CAS 号时直接用它；否则只有第一个候选（index == 0）去查，其余的 CAS 留空
            cas = query if is_cas_number(query) else (self.pubchem.cas_number(cid) if index == 0 else None)
            # 名称：PubChem 的标题；没有标题用 IUPAC 名；再没有就用用户输入的那段文字
            title = props.get("Title") or props.get("IUPACName") or query
            candidate = self._from_smiles(
                Chem.MolToSmiles(mol), name=title, source="PubChem", name_en=title, cas=cas, cid=cid
            )
            if candidate:
                out.append(candidate)
        return out

    # ------------------------------------------------------------ E1

    def resolve_row(self, *, name: str = "", cas: str = "", smiles: str = "") -> Resolution:
        """E1 / E3：认出库存表里的一行是哪个物质。

        参数（都只能按名字传，三个里至少该有一个不是空的）：
            name    名称一栏的内容，可以带规格说明（“乙醇 分析纯”）、可以是别名或缩写
            cas     CAS 号一栏的内容
            smiles  SMILES 一栏的内容
        返回：Resolution（字段含义见它的定义）。外部接口的故障不会抛出来，而是记在 notes 里。
        判断的先后（越靠前越可信，认出来就不往下走）：
            1. SMILES 读得出来                          → matched，来源 “SMILES”
            2. CAS 号校验位正确：
               内置试剂表里有                            → matched（前面有疑点时 needs_confirmation）
               PubChem 查到                              → 只对应一个结构、前面没有疑点时 matched，否则 needs_confirmation
            3. 名称：
               内置试剂表里的正式中文名或英文名          → matched（前面有疑点时 needs_confirmation）
               内置试剂表里的别名、缩写、商品名          → needs_confirmation
               去掉规格说明、或者取 “/” 前面一段才对上   → needs_confirmation
               PubChem 查到                              → 只有一个结果、标题和名称一致、前面没有疑点时 matched，
                                                           否则 needs_confirmation
               OPSIN 解析出来（只试纯英文的名称）        → needs_confirmation
            4. 都不行                                    → unrecognized
        “前面的疑点”指 notes 里已经记下的问题：SMILES 读不出来、CAS 号的校验位不对、PubChem 或 OPSIN 连不上。
        只要有疑点，后面就算认出来了也只给 needs_confirmation，让人看一眼。
        例（tests/test_inventory.py 里那张表）：
            乙酸酐 + 108-24-7             → matched（CAS · 内置试剂表）
            只有 SMILES                    → matched（SMILES）
            Mg turnings                    → needs_confirmation：“名称是缩写、别名或商品名”
            Pd/C 10%                       → needs_confirmation：“混合物或溶液，没有单一结构，按主要成分匹配”
            乙醚 + 一个写错一位的 CAS 号   → needs_confirmation：“CAS 号的校验位不对”
            自制中间体 B-7                 → unrecognized
        副作用：第 2、3 步在内置试剂表查不到时会向 PubChem、OPSIN 发网络请求。不写数据库
        （把结果存进库存是调用方的事）。
        谁在用：inventory_import.process_import（E1，逐行）；api/inventory.py 的 add_item（E3）。
        """
        # 三栏都去掉首尾空白，None 当成空字符串
        name, cas, smiles = (name or "").strip(), (cas or "").strip(), (smiles or "").strip()
        # notes：一路上记下的疑点。它不是空的，后面认出来的结果就要人确认
        notes: list[str] = []
        library = self.reference.compounds

        # ---- 第 1 步：SMILES
        if smiles:
            info = chem.validate_structure(smiles, with_svg=False)
            if info.valid:
                # 顺便看看内置试剂表里有没有这个结构：有的话名称、CAS 号可以从表里补
                known = library.find_by_smiles(info.smiles)
                return Resolution(
                    "matched",
                    name=name or (known.display_name if known else info.formula),
                    cas=cas or (known.cas if known else ""),
                    smiles=info.smiles,
                    source="SMILES",
                )
            # 填了 SMILES 但读不出来：记一条疑点，接着用 CAS 号和名称去认
            notes.append("SMILES 读不出来")

        # ---- 第 2 步：CAS 号
        if cas:
            # is_cas_number 检查格式和最后一位校验位（external.py）。不对的话记一条疑点，这个 CAS 号不用，接着看名称
            if not is_cas_number(cas):
                notes.append("CAS 号的校验位不对")
            else:
                compound = library.find_by_cas(cas)
                if compound:
                    return self._resolved(compound, name, notes, source="CAS · 内置试剂表")
                # 内置表里没有，问 PubChem。found 是 (候选列表, 是不是只对应一个结果)；没查到或连不上是 None
                found = self._pubchem_resolve(cas, notes)
                if found:
                    candidates, unique = found
                    best = candidates[0]
                    # 名称：这一行填了就用填的，没填用 PubChem 上的标题；CAS 号用这一行填的
                    if unique and not notes:
                        return Resolution("matched", name=name or best.name, cas=cas, smiles=best.smiles, source="CAS · PubChem")
                    reason = "；".join(notes) or "这个 CAS 号在 PubChem 里对应多个结构"
                    return Resolution("needs_confirmation", name=name or best.name, cas=cas, smiles=best.smiles,
                                      reason=reason, source="CAS · PubChem", notes=notes)

        # ---- 第 3 步：名称。走到这里说明 SMILES、CAS 号都没能认出来（或者没填）
        if name:
            # 3a. 原样去内置试剂表里查。find_by_name 不分大小写，也不管空格、连字符、括号（reference.normalize_name）
            compound = library.find_by_name(name)
            if compound:
                # 对上的是表里的正式中文名或英文名，还是别名一栏里的名字？后一种要人确认
                primary = normalize_name(name) in {normalize_name(compound.name_zh), normalize_name(compound.name_en)}
                if not primary:
                    notes.append("名称是缩写、别名或商品名")
                return self._resolved(compound, name, notes, source="名称 · 内置试剂表")

            # 3b. 删掉规格说明再查一次；strip 去掉删完以后两头剩下的空格、逗号、分号。
            #     例：“乙醇 分析纯 99.7%” → “乙醇”；“Pd/C 10%” → “Pd/C”（表里钯碳的别名）。
            #     还查不到、而名称里有 “/” 时，只拿 “/” 前面的那一段再查一次
            cleaned = _GRADE_WORDS.sub("", name).strip(" ,，;；")
            compound = library.find_by_name(cleaned) if cleaned and cleaned != name else None
            if compound is None and "/" in cleaned:
                compound = library.find_by_name(cleaned.split("/")[0])
            if compound:
                # 这样对上的都要人确认。说明文字分两种：名称看着像混合物或溶液的，和单纯带规格的
                mixture = any(hint in name.lower() for hint in _MIXTURE_HINTS)
                notes.append("混合物或溶液，没有单一结构，按主要成分匹配" if mixture else "去掉规格说明后才匹配上")
                return self._resolved(compound, name, notes, source="名称 · 内置试剂表")

            # 3c. PubChem，用原始的名称去查（不是删过规格的）
            found = self._pubchem_resolve(name, notes)
            if found:
                candidates, unique = found
                best = candidates[0]
                # PubChem 上第一个结果的标题和这一行的名称是不是同一个词（不分大小写、不管空格和连字符）
                exact_title = normalize_name(best.name) == normalize_name(name)
                if unique and exact_title and not notes:
                    return Resolution("matched", name=name, cas=best.cas or cas, smiles=best.smiles, source="名称 · PubChem")
                notes.append("按名称从 PubChem 查到，请确认是不是这个结构")
                return Resolution("needs_confirmation", name=name, cas=best.cas or cas, smiles=best.smiles,
                                  reason="；".join(notes), source="名称 · PubChem", notes=notes)

            # 3d. OPSIN：只试纯英文的名称。它是按命名规则推出结构，所以推出来了也一律要人确认
            if self.opsin and _ASCII_NAME.match(name):
                try:
                    candidate = self._from_smiles(self.opsin.name_to_smiles(name), name=name, source="OPSIN")
                except ExternalServiceError:
                    candidate = None
                    notes.append("OPSIN 暂时连不上")
                if candidate:
                    notes.append("由 OPSIN 按系统命名解析，请确认")
                    return Resolution("needs_confirmation", name=name, cas=cas, smiles=candidate.smiles,
                                      reason="；".join(notes), source="名称 · OPSIN", notes=notes)

        # ---- 第 4 步：没认出来。原因是一路记下的疑点；一条疑点都没有时用后面那句笼统的话
        reason = "；".join(notes) or "名称、CAS 号、SMILES 都没能识别"
        return Resolution("unrecognized", name=name, cas=cas, reason=reason, notes=notes)

    def _resolved(self, compound: Compound, name: str, notes: list[str], *, source: str) -> Resolution:
        """按内置试剂表里的一个物质给出识别结果。

        参数：
            compound  表里对上的那个物质
            name      这一行自己填的名称；空的话用表里的显示名
            notes     到这时为止记下的疑点；有疑点就是 needs_confirmation，没有就是 matched
            source    靠什么对上的：“CAS · 内置试剂表” 或 “名称 · 内置试剂表”
        返回的 Resolution 里，CAS 号和结构都用表里登记的（不用这一行自己填的 CAS 号）。
        谁在用：resolve_row()。
        """
        status = "needs_confirmation" if notes else "matched"
        return Resolution(status, name=name or compound.display_name, cas=compound.cas, smiles=compound.smiles,
                          reason="；".join(notes), source=source, notes=notes)

    def _pubchem_resolve(self, query: str, notes: list[str]) -> tuple[list[Candidate], bool] | None:
        """到 PubChem 按名称或 CAS 号查（E1 用），并说明结果唯一不唯一。

        参数：
            query  名称或 CAS 号
            notes  resolve_row 的疑点列表。PubChem 连不上时会往里面加一条“PubChem 暂时连不上”（直接改这个列表）
        返回：(候选列表, 是否唯一)，或者 None。
            候选列表  PubChem 给的前 3 个结果里读得出结构的那些，Candidate 的列表（没有 CAS 号，没有小图）
            是否唯一  PubChem 对这个名称只给了一个编号（CID）时为 True
            None      没有配置 PubChem、没查到、查到的都读不出结构，或者连不上
        PubChem 的故障（ExternalServiceError）在这里接住，不往外抛。
        副作用：发 1–2 个网络请求（查编号；查到了再取属性）。
        谁在用：resolve_row()（CAS 号和名称各可能调一次）。
        """
        if not self.pubchem:
            return None
        try:
            cids = self.pubchem.cids_by_name(query)
            if not cids:
                return None
            candidates = []
            # 只看前 3 个编号。PubChem 给的结构是 InChI，读成分子再写成 SMILES（同 _pubchem_candidates）
            for props in self.pubchem.properties(cids[:3]):
                mol = Chem.MolFromInchi(props.get("InChI", "")) if props.get("InChI") else None
                # “mol and …”：mol 是 None 时整个式子就是 None，不去建候选
                candidate = mol and self._from_smiles(
                    Chem.MolToSmiles(mol), name=props.get("Title") or query, source="PubChem", cid=props.get("CID")
                )
                if candidate:
                    candidates.append(candidate)
            return (candidates, len(cids) == 1) if candidates else None
        except ExternalServiceError:
            notes.append("PubChem 暂时连不上")
            return None
