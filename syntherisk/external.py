"""外部免费接口的客户端：PubChem、OPSIN、Crossref / OpenAlex。

这些接口都有调用频率限制，所以统一做了限速、重试和超时。
接口不通时抛 ExternalServiceError，调用方应当降级（只用本地数据），不要让整个请求失败。

---- 在整个系统里的位置 ----

这个文件不单属于 PPT 里的哪一个模块，是 M1、M5、M6 共用的一层：后端发往外部网站的 HTTP 请求都从这里走
（大模型的调用除外，那在 syntherisk/llm/ 里）。三个客户端由 context.build_context() 各建一个，
放在 AppContext 上，整个进程共用：

    PubChemClient（ctx.pubchem）
        M1  名称、CAS 号转结构（m01_resolver.py）：A3 查候选结构、E1 导入库存时逐行识别、E3 手动添加库存、
            A14 一句话要求里点名的物质
        M6  查 GHS 危害分类、给没有名字的中间体取名（m06_safety.py，一次分析的第 5 阶段）
        命令行 check-hazards 另建一个放得很慢的客户端，把内置试剂表和 PubChem 对一遍
    OpsinClient（ctx.opsin）
        M1  内置试剂表和 PubChem 都没查到时，把英文系统命名解析成结构（m01_resolver.py）
    LiteratureClient（ctx.literature）
        C1 文献列表：先例只有 DOI、没有题目时，补上题目和作者（api/analyses.py 的 get_step_precedents）

开关和参数在 config.Settings 里（可以在 .env 里改）：ENABLE_PUBCHEM、PUBCHEM_TIMEOUT、ENABLE_OPSIN、
OPSIN_URL_TEMPLATE、ENABLE_LITERATURE_METADATA（默认关）、CONTACT_EMAIL。关掉的客户端在 AppContext 上是 None，
调用方都是先判断再用。测试不联网：给客户端传一个假的传输层（httpx.MockTransport）。

这个文件不读写数据库。PubChem 查到的危害分类由 m06_safety.py 存进 substance_hazards 表当缓存。

---- PubChem 的限流（实测得来，决定了这里的写法）----

    PubChem 的上限是每秒 5 次、每分钟 400 次，超了会按 IP 封一阵。实测 0.22 秒一次连发几百次就被封了；
    放慢到一秒半一次、连着查四百多次，总量太大，还是被封了。所以：
      · 正常分析用的客户端每秒最多 3 次（两次请求至少隔 0.34 秒）；
      · PubChem 在每个响应头里报告自己的负载，不是 Green 就自动放慢（_ThrottledClient._adapt）；
      · 收到 429（被限流）不重试，直接抛 ExternalServiceError，让调用方降级；
      · 查过的物质由调用方存进数据库，不再重复问；
      · 批量核对（check-hazards）另用一个三秒一次的客户端。

---- GHS 页面怎么读 ----

    PubChem 上一个物质的 “GHS Classification” 页面有好几套互不一致的分类。采用的规则是：
    欧盟统一分类，加上至少占全部企业申报 30%（scoring.toml 的 pubchem_min_share）的分类；
    这两个来源都没有时才退回其他来源（日本 NITE 等）；
    一个来源都没有是“没有数据”（返回 None），和“不属于危险品”（返回空列表）是两回事。
    每个来源的特点和踩过的坑写在 collect_ghs() 的说明里。

---- 本文件里有什么（按阅读顺序）----

    _H_CODE、_PERCENT、_REPORTS、_WITH_CODES、_MIXTURE_ENTRY、_CAS   解析用的正则表达式
    ExternalServiceError   外部接口不通时抛的异常
    is_cas_number          CAS 号的格式和校验位对不对
    _ThrottledClient       带限速、重试、超时的 HTTP 客户端，下面三个客户端共用
        request_json       发一个请求，返回 JSON
        _adapt             按 PubChem 报告的负载调整请求间隔
    collect_ghs            把 PubChem 的 GHS 页面按来源拆开
    _could_be_own_entry    统一分类里的一条是不是这个物质自己的（排除混合物条目）
    _mentions              条目名里有没有提到这个物质
    parse_ghs_codes        GHS 页面 -> 这个物质的 H 编码
    PubChemClient          PubChem：按名称或 InChIKey 查记录，取性质、CAS 号、GHS 分类
    OpsinClient            OPSIN：英文系统命名 -> SMILES
    LiteratureClient       Crossref / OpenAlex：按 DOI 查题目、作者、年份
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

# ---------------------------------------------------------------- 解析用的正则表达式

# H 编码：字母 H 加三位数字（\d{3}），后面可以带最多两个字母（H360FD、H361d）
_H_CODE = re.compile(r"H\d{3}[A-Za-z]{0,2}")
# 危险性说明里括号中的百分比，比如 “(97.6%)”“(> 99.9%)”。\( 和 \) 是字面上的括号，
# (?:[<>]\s*)? 是可有可无的大于号或小于号，(\d+(?:\.\d+)?) 把数字部分取出来
_PERCENT = re.compile(r"\((?:[<>]\s*)?(\d+(?:\.\d+)?)%\)")
# “… per 938 reports by companies from 27 notifications …”；旧页面写的是 “by 938 companies from …”
# 取出来的 (\d+) 是这一组一共有多少份申报
_REPORTS = re.compile(r"(\d+) (?:reports by )?compan(?:y|ies) from \d+ notifications?")
# “… provided by 327 of 938 reports by companies with hazard statement code(s)”：
# 两个 (\d+) 依次是“报了危害的申报有多少份”和“全部申报有多少份”
_WITH_CODES = re.compile(r"provided by (\d+) of (\d+) (?:reports by )?compan(?:y|ies) with hazard statement")
# 统一分类里“不是这个物质自己的条目”的标志词（混合物、反应产物、聚合物）。竖线是“或”，re.IGNORECASE 是不分大小写
_MIXTURE_ENTRY = re.compile(r"reaction mass|reaction product|mixture|product-by-process|obtained by|polymer", re.IGNORECASE)
# CAS 号的格式：2 到 7 位数字 - 2 位数字 - 1 位数字。^ 和 $ 表示整段文字从头到尾都要符合
_CAS = re.compile(r"^\d{2,7}-\d{2}-\d$")


class ExternalServiceError(RuntimeError):
    """外部接口（PubChem、OPSIN、Crossref、OpenAlex）连不上、超时、被限流，或者返回了读不懂的内容。

    这类故障不应该让整个请求失败：调用方接住它，降级成只用本地数据，并在结果里说明。
    接住它的地方：m01_resolver.py、m06_safety.py、api/analyses.py、cli.py。
    “查不到这个东西”不算故障，不抛这个异常（各个方法返回空列表或 None）。
    """

    pass


def is_cas_number(text: str) -> bool:
    """格式和校验位都对才算。

    参数：
        text  要检查的文字；None、空字符串、前后有空格都可以。
    返回：
        True：是一个合法的 CAS 号。False：格式不对，或者校验位对不上（多半是抄错了一位）。
    算法：
        CAS 号形如 15687-27-1：2 到 7 位数字、2 位数字、1 位校验位，用连字符隔开。
        校验位的算法：去掉连字符和最后一位，从右往左给每位数字依次乘上 1、2、3……，
        加起来除以 10 的余数应当等于校验位。
        比如 15687-27-1：7×1 + 2×2 + 7×3 + 8×4 + 6×5 + 5×6 + 1×7 = 131，余数是 1，和最后一位相同。
    谁在用：
        m01_resolver.py（判断输入的是不是 CAS 号）、inventory_import.py（把填在名称列里的 CAS 号挪到 CAS 列）、
        本文件的 PubChemClient.cas_number()；tests/test_reference.py 用它检查 data/ 下两张表里的 CAS 号。
    """
    text = (text or "").strip()
    if not _CAS.match(text):
        return False
    # body：去掉连字符和最后一位以后剩下的数字；check：最后一位，也就是校验位
    body, check = text.replace("-", "")[:-1], int(text[-1])
    # reversed(body) 从右往左走；enumerate 给出位置 i（从 0 起），所以乘的是 i + 1
    return sum(int(d) * (i + 1) for i, d in enumerate(reversed(body))) % 10 == check


# ---------------------------------------------------------------- 限速的 HTTP 客户端


class _ThrottledClient:
    """带限速、重试和超时的 HTTP 客户端。本文件里三个对外的客户端都建在它上面。

    限速：两次请求之间至少隔 _interval 秒。几个线程共用同一个客户端时靠一把锁排队，
          所以限的是整个进程发往这个网站的总速度，不是每个线程各自的速度。
    重试：一次调用最多试 3 次。只在网络出错、或者对方返回 500 / 502 / 503 / 504 时重试；被限流（429）不重试。

    属性：
        _http          httpx.Client，真正发请求的对象，带统一的超时和 User-Agent。
        _min_interval  正常情况下两次请求的最小间隔（秒），建客户端时定下来。
        _interval      现在实际用的间隔（秒）。PubChem 说自己负载高时被 _adapt() 调大，恢复以后调回 _min_interval。
        _lock          线程锁，保证同一时刻只有一个线程在“等间隔、记时间”。
        _last          上一次请求发出去的时刻（time.monotonic() 的读数，只用来算时间差）。
    """

    def __init__(self, timeout: float, min_interval: float, transport: httpx.BaseTransport | None = None,
                 follow_redirects: bool = False):
        """建一个 httpx 客户端，记下限速的间隔。不发请求。

        参数：
            timeout       一次请求最多等多少秒。超时按网络出错处理，会重试。
            min_interval  两次请求之间至少隔多少秒。0 表示不限速（测试里这样用）。
            transport     给 httpx 用的传输层。正常不传；测试里传 httpx.MockTransport，请求就不会真的发出去。
            follow_redirects  对方回 301 / 302 时跟着跳过去。只有 LiteratureClient 打开：Crossref 对改过号的 DOI
                          （比如 ChemRxiv 的旧 DOI）回 301；不跟的话按“别的状态码”算失败。
        谁在用：
            本文件的 PubChemClient、OpsinClient、LiteratureClient，各建一个。
        """
        # headers 里的 User-Agent 每次请求都会带上，让对方知道请求来自哪个程序
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"User-Agent": "syntherisk-backend/0.1"},
            follow_redirects=follow_redirects,
        )
        self._min_interval = min_interval
        self._interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def request_json(self, method: str, url: str, **kwargs) -> Any | None:
        """成功返回 JSON；查无此物返回 None；网络或服务故障抛 ExternalServiceError。

        参数：
            method    "GET" 或 "POST"。
            url       完整的地址。
            **kwargs  原样交给 httpx，比如 params=（地址后面的查询参数）、data=（POST 的表单内容）。
        返回：
            对方返回 200：解析好的 JSON（字典或列表）。
            对方返回 400 或 404：None，表示没有这个东西（名称查不到、记录不存在）。这不算出错。
        抛出：
            ExternalServiceError  下面任何一种情况：试了 3 次都是网络出错，或者对方返回 500 / 502 / 503 / 504；
                                  被限流（429，不重试）；别的状态码（不重试）；返回了 200 但内容不是 JSON。
        副作用：
            发网络请求。为了限速可能要睡一会儿（time.sleep），调用它的线程在这期间是卡住的。
            可能改 self._interval（见 _adapt）。
        谁在用：
            本文件的 PubChemClient、OpsinClient、LiteratureClient 的各个方法。
        """
        last_error = "未知错误"
        # 最多试 3 次
        for attempt in range(3):
            # 限速：拿到锁以后，看离上一次请求过了多久，不够间隔就睡到够为止，再记下这一次的时刻。
            # 睡的时候一直拿着锁，别的线程只能排在后面等，所以整个进程的请求是一个接一个、按间隔发出去的。
            # time.monotonic() 是只增不减的计时读数，不受系统改时间的影响
            with self._lock:
                wait = self._interval - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                response = self._http.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                # 连不上、超时这类网络层面的错误：记下原因，回到循环开头再试（重试之前照样要等够间隔）
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            # 每个响应都看一眼 PubChem 报告的负载。响应里没有这个头时取到的是空字符串，_adapt 什么都不做
            self._adapt(response.headers.get("X-Throttling-Control", ""))
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    # 状态码是 200，内容却不是 JSON
                    raise ExternalServiceError(f"{url} 返回的不是 JSON") from exc
            if response.status_code in (400, 404):
                # 查无此物：名称查不到、记录不存在、对方认不出这个输入
                return None
            last_error = f"HTTP {response.status_code}"
            if response.status_code == 429:
                # 被对方限流了。马上再试只会让限流持续得更久，所以不重试，让调用方降级
                last_error = "对方限流（HTTP 429），过一阵再试"
                break
            if response.status_code in (500, 502, 503, 504):
                # 对方服务器暂时出了问题：歇一下再试，三次分别歇 0.5、1、1.5 秒
                time.sleep(0.5 * (attempt + 1))
                continue
            # 其余的状态码重试也没有用，直接结束
            break
        raise ExternalServiceError(f"请求 {url} 失败：{last_error}")

    def _adapt(self, throttling: str) -> None:
        """PubChem 在每个响应头里报告自己的负载（Green / Yellow / Red / Black）：不是 Green 就放慢。

        参数：
            throttling  响应头 X-Throttling-Control 的内容，形如
                        "Request Count status: Yellow (60%), Request Time status: Green (9%),
                        Service status: Green (20%)"（实际是一行）。代码只是在这段文字里找 Red、Black、
                        Yellow 这几个词，所以三项里任何一项不是 Green 都会放慢。
                        没有这个响应头时传进来的是空字符串，这时什么都不做。
        效果（改的是 self._interval，之后的请求按新的间隔发）：
            出现 Red 或 Black  间隔调到 _min_interval 的 8 倍，并且不少于 2 秒
            出现 Yellow        间隔调到 _min_interval 的 3 倍，并且不少于 1 秒
            都是 Green         间隔恢复成 _min_interval
            按默认的 0.34 秒算，分别是 2.72 秒、1.02 秒、0.34 秒。
        谁在用：
            本类的 request_json()，每收到一个响应调一次。
        """
        if not throttling:
            return
        if "Red" in throttling or "Black" in throttling:
            self._interval = max(self._min_interval * 8, 2.0)
        elif "Yellow" in throttling:
            self._interval = max(self._min_interval * 3, 1.0)
        else:
            self._interval = self._min_interval

    def close(self) -> None:
        """关掉底层的 httpx 客户端，释放它占着的网络连接。

        谁在用：
            本文件三个客户端各自的 close()。
        """
        self._http.close()


# ---------------------------------------------------------------- 读 PubChem 的 GHS 页面


def collect_ghs(payload: Any) -> dict[str, Any]:
    """把 PubChem 的 GHS Classification 页面按数据来源拆开。

    同一个物质在这个页面上有好几套分类，互相并不一致：
      harmonized    欧盟统一分类（CLP 法规附件 VI）。最权威，但很多物质没有，而且往往只是“最低分类”。
                    同一个页面上还会挂着含有这个物质的混合物条目（“reaction mass of …”），那些不算它的。
      echa          ECHA 汇总的企业申报：{编码: 占全部申报的百分比}。页面上写的百分比只在“报了危害的公司”
                    里算，这里已经折算成占全部申报的比例（否则大多数公司都报“不属于危险品”的物质，
                    比如 DMSO，会被少数几家的申报带偏）。挂错的条目（申报数少的）不看，只取申报数最多的一组。
      echa_reports  上面那一组背后有多少份申报；页面上没有 ECHA 的汇总时是 None
      other         其他来源（日本 NITE、澳大利亚 HCIS 等）。NITE 的分类口径宽得多，
                    比如把乙醇列为致癌（指饮酒），不适合直接拿来筛实验室试剂
      found         这三类里有没有任何一类给出了结论。False 是“没有数据”，不是“不属于危险品”

    参数：
        payload  PubChemClient.ghs_page() 取回来的 JSON。None 或空字典也可以，当作空页面。
    返回：
        一个字典，五个键就是上面那五项：
            harmonized    排好序的 H 编码列表
            echa          字典 {编码: 百分比}；页面上没写百分比的编码，值是 None
            echa_reports  整数或 None
            other         排好序的 H 编码列表
            found         True / False
    页面的结构（只说用得到的部分）：
        Record.Reference  来源的列表。每个来源有 ReferenceNumber（编号）、SourceName（机构）、Name（条目名）。
        Record.Section    一层套一层的章节。章节里的 Information 列表，每一项用 ReferenceNumber 指回一个来源；
                          Name 是 “GHS Hazard Statements” 的那些项是危险性说明，
                          Name 是 “ECHA C&L Notifications Summary” 的那些项是企业申报的汇总文字。
        一条危险性说明的原文形如 “H225 (97.6%): Highly Flammable liquid and vapor [Danger Flammable liquids]”。
    怎么认来源（看 SourceName）：
        含有 “1272/2008”（CLP 法规的编号）     欧盟统一分类
        以 “European Chemicals Agency” 开头    ECHA 的企业申报
        其余                                   其他来源
    副作用：
        没有。只是计算，不联网。
    谁在用：
        本文件的 parse_ghs_codes()；m06_safety.compare_with_pubchem()；tests/test_integrations.py。
    """
    record = (payload or {}).get("Record", {})
    # 来源编号 -> 这个来源的信息（机构、条目名）
    references = {ref.get("ReferenceNumber"): ref for ref in record.get("Reference", [])}
    # 来源编号 -> {H 编码: 页面上写的百分比，没写是 None}
    statements: dict[Any, dict[str, float | None]] = {}
    # 来源编号 -> ECHA 申报汇总的整段文字
    summaries: dict[Any, str] = {}

    def walk(node: Any) -> None:
        """把页面的章节树从上到下走一遍，把危险性说明和 ECHA 汇总按来源编号收进外层的 statements、summaries。"""
        if isinstance(node, dict):
            # 这一层带 Information 列表就逐项看；没有，或者它不是列表，就当成空的
            for info in node.get("Information", []) if isinstance(node.get("Information"), list) else []:
                # 一项里可以有几行文字，都在 Value.StringWithMarkup 列表里每个元素的 String 里
                strings = [s.get("String", "").strip() for s in info.get("Value", {}).get("StringWithMarkup", [])]
                reference = info.get("ReferenceNumber")
                if info.get("Name") == "GHS Hazard Statements":
                    codes = statements.setdefault(reference, {})
                    for text in strings:
                        # 括号里的百分比（ECHA 的条目才带）；没有就是 None
                        percent = _PERCENT.search(text)
                        # 第一个冒号前面是编码部分。合并写法 “H300+H310+H330” 拆成三个编码，共用同一个百分比
                        for code in _H_CODE.findall(text.split(":")[0]):
                            codes[code] = float(percent.group(1)) if percent else None
                elif info.get("Name") == "ECHA C&L Notifications Summary":
                    summaries[reference] = " ".join(strings)
            # 不管这一层有没有 Information，都继续往下走：章节是一层套一层的
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(record.get("Section", []))
    # 统一分类的条目 [(条目名, {编码: None}), …]。一个页面上可能有好几条，其中有的是混合物的
    harmonized_entries: list[tuple[str, dict[str, float | None]]] = []
    other: set[str] = set()
    echa: dict[str, float | None] = {}
    echa_reports: int | None = None
    # 这个物质自己的名字（小写）：先放页面标题，下面再加上认出来的 ECHA 条目名
    own_names = {(record.get("RecordTitle") or "").strip().lower()}
    # dict.fromkeys(列表) 去重并保持先后顺序：每个来源编号只处理一次
    for number in dict.fromkeys([*statements, *summaries]):
        reference = references.get(number, {})
        source = reference.get("SourceName") or ""
        codes = statements.get(number, {})
        if "1272/2008" in source:
            harmonized_entries.append((reference.get("Name") or "", codes))
        elif source.startswith("European Chemicals Agency"):
            summary = summaries.get(number, "")
            # 这一组有多少份申报；汇总文字里找不到这句话就按 0 算
            reports = _REPORTS.search(summary)
            count = int(reports.group(1)) if reports else 0
            # 页面上可能有几组 ECHA 申报（有的是挂错的别的物质）：只留申报数最多的那一组
            if echa_reports is None or count > echa_reports:
                # “报了危害的申报”占全部申报的比例；汇总里没有这句话时按 1 算，也就是不折算
                with_codes = _WITH_CODES.search(summary)
                fraction = int(with_codes.group(1)) / int(with_codes.group(2)) if with_codes else 1.0
                # 页面上的百分比 × 这个比例 = 占全部申报的百分比（一位小数）；没写百分比的保持 None
                echa = {code: share if share is None else round(share * fraction, 1) for code, share in codes.items()}
                echa_reports = count
            if reports:  # 有申报汇总的条目才是这个物质自己的；它的名字用来认统一分类里哪一条是它
                # 条目名形如 “Propan-2-ol (EC: 200-661-7)”：只要 “ (EC” 前面的部分，转成小写
                own_names.add((reference.get("Name") or "").split(" (EC")[0].strip().lower())
        else:
            other.update(codes)
    own_names.discard("")
    # 统一分类里哪几条是这个物质自己的：先去掉明显是混合物的条目；剩下的里面，名字里提到这个物质的优先
    plausible = [(name, codes) for name, codes in harmonized_entries if _could_be_own_entry(name)]
    named = [(name, codes) for name, codes in plausible if _mentions(name, own_names)]
    # (named or plausible)：有提到名字的就只用它们，一条都没有才用全部“可能是它”的条目；把这些条目的编码并在一起
    harmonized = {code for _, codes in (named or plausible) for code in codes}
    return {
        "harmonized": sorted(harmonized),
        "echa": dict(sorted(echa.items())),
        "echa_reports": echa_reports,
        "other": sorted(other),
        "found": bool(harmonized or echa_reports is not None or other),
    }


def _could_be_own_entry(entry_name: str) -> bool:
    """PubChem 会把含有某个物质的混合物条目也挂到它的页面上：异丙醇的页面上有一条 “reaction mass of …”，
    氯化钠的页面上有一条偶氮染料（“product-by-process … polyazodyestuff …”，带着 H318）。这样的不算它自己的分类。

    参数：
        entry_name  统一分类里一个条目的名字（来源信息里的 Name）。
    返回：
        True：可能是这个物质自己的条目。
        False：名字里有 reaction mass、reaction product、mixture、product-by-process、obtained by、polymer
        这些字样（不分大小写），或者名字超过 120 个字符。
    谁在用：
        本文件的 collect_ghs()。
    """
    return not _MIXTURE_ENTRY.search(entry_name) and len(entry_name) <= 120


def _mentions(entry_name: str, own_names: set[str]) -> bool:
    """条目名里有没有提到这个物质的名字（按整个词算，不分大小写）。

    参数：
        entry_name  统一分类里一个条目的名字，比如 “propan-2-ol; isopropyl alcohol; isopropanol”。
        own_names   这个物质自己的名字的集合，已经是小写。
    返回：
        提到其中任何一个名字就是 True。
    “按整个词算”的意思：
        名字的前后不能紧挨着别的英文字母。所以 “ethanol” 不会在 “methanol” 里被认出来。
    谁在用：
        本文件的 collect_ghs()。
    """
    name = entry_name.lower()
    # re.escape 把名字里在正则里有特殊含义的字符（括号、加号等）变成普通字符；
    # (?<![a-z]) 要求名字前面不是字母，(?![a-z]) 要求名字后面不是字母
    return any(re.search(rf"(?<![a-z]){re.escape(own)}(?![a-z])", name) for own in own_names)


def parse_ghs_codes(payload: Any, min_percent: float = 30.0) -> list[str] | None:
    """PubChem 页面 → 这个物质的 H 编码。页面上没有任何分类数据时返回 None。

    取欧盟统一分类，加上 ECHA 企业申报里至少占全部申报 min_percent% 的编码
    （统一分类常常只是最低分类，也不一定包含易燃这类物理危害，所以两个都要）；
    这两个来源都没有时，才退回其他来源。

    参数：
        payload      PubChemClient.ghs_page() 取回来的 JSON。
        min_percent  企业申报的一条危险性说明至少要占全部申报的百分之几才采用，默认 30。
                     页面上没写百分比的编码一律采用。
    返回：
        排好序的 H 编码列表。可以是空列表：有数据，结论是不属于危险品。没有任何分类数据时是 None。
        同一种危害轻重两档可能同时出现在结果里，由调用方（m06_safety.strongest_per_hazard）去掉较轻的。
    谁在用：
        本文件的 PubChemClient.ghs_codes()；m06_safety.compare_with_pubchem()；tests/test_integrations.py。
    """
    ghs = collect_ghs(payload)
    if not ghs["found"]:
        return None
    codes = set(ghs["harmonized"])
    # 企业申报：share 是占全部申报的百分比；是 None（页面上没写比例）的照样采用
    codes.update(code for code, share in ghs["echa"].items() if share is None or share >= min_percent)
    # 统一分类和企业申报都没有的时候，才用其他来源（日本 NITE 等）
    if not ghs["harmonized"] and ghs["echa_reports"] is None:
        codes.update(ghs["other"])
    return sorted(codes)


# ---------------------------------------------------------------- 三个对外的客户端


class PubChemClient:
    """PubChem 的客户端：按名称、CAS 号或 InChIKey 查记录，取性质、CAS 号和 GHS 分类。

    PubChem 给每个化合物一个编号，叫 CID。这里用到它的两套接口：
        PUG   PUG REST：按名称、InChIKey、CID 查询，返回整齐的 JSON
        VIEW  PUG View：化合物页面上各个章节的内容。GHS 分类是从这里取的
    所有要联网的方法，在 PubChem 连不上、超时、被限流时都抛 ExternalServiceError；
    只是“查不到”不抛异常，返回空列表或 None。
    """

    # 两套接口的地址前缀
    PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"

    def __init__(self, timeout: float = 8.0, transport: httpx.BaseTransport | None = None, min_interval: float = 0.34):
        """建一个限速的客户端。不发请求。

        参数：
            timeout       一次请求最多等几秒。正常分析用配置里的 PUBCHEM_TIMEOUT（默认 8）。
            transport     测试用的假传输层；正常不传。
            min_interval  两次请求之间至少隔几秒。默认 0.34，也就是每秒最多 3 次；check-hazards 命令用 3.0。
        谁在用：
            context.build_context()（配置里 ENABLE_PUBCHEM 打开时）；cli.py 的 cmd_check_hazards；
            tests/test_integrations.py。
        """
        # PubChem 的上限是每秒 5 次、每分钟 400 次，超了会按 IP 封一阵（实测：0.22 秒一次连发几百次就被封了）。
        # 这里留了余量，每秒最多 3 次
        self._client = _ThrottledClient(timeout, min_interval, transport)

    def cids_by_name(self, name: str) -> list[int]:
        """名称或 CAS 号 -> CID。用 POST，名称里有斜杠、括号也没问题。

        参数：
            name  化合物的名称；CAS 号也当名称查。
        返回：
            CID 的列表（一个名称可能对应几条记录）；查不到是空列表。
        副作用：
            发一次网络请求。
        谁在用：
            m01_resolver.py（A3 查候选结构，E1、E3 识别库存里的名称和 CAS 号）；
            m06_safety.compare_with_pubchem()（按 CAS 号查）。
        """
        # 名称放在 POST 的表单里（data=），不拼进地址。接口返回 {"IdentifierList": {"CID": [...]}}
        data = self._client.request_json("POST", f"{self.PUG}/compound/name/cids/JSON", data={"name": name})
        # data 是 None（查不到）时用空字典顶上，一层层 .get 下去，最后得到空列表
        return list((data or {}).get("IdentifierList", {}).get("CID", []))

    def cids_by_inchikey(self, inchikey: str) -> list[int]:
        """InChIKey -> CID。按结构在 PubChem 上找记录。

        参数：
            inchikey  完整的 InChIKey（m01_structure.MolRecord.inchikey）。
        返回：
            CID 的列表（同一个结构偶尔对应几条记录）；PubChem 上没有这个结构时是空列表。
        副作用：
            发一次网络请求。
        谁在用：
            m06_safety.py 的 SafetyService._from_pubchem() 和 compare_with_pubchem()。
        """
        # quote 把不能直接出现在地址里的字符转成 %XX 的写法
        data = self._client.request_json("GET", f"{self.PUG}/compound/inchikey/{quote(inchikey)}/cids/JSON")
        return list((data or {}).get("IdentifierList", {}).get("CID", []))

    def properties(self, cids: list[int]) -> list[dict[str, Any]]:
        """一批 CID 的基本性质。

        参数：
            cids  CID 列表。空列表直接返回空列表，不发请求。
        返回：
            字典的列表，一条记录一个。键是 PubChem 的字段名：CID、Title（常用名）、MolecularFormula（分子式）、
            MolecularWeight（分子量）、InChIKey、InChI、IUPACName（系统命名）。查不到是空列表。
        副作用：
            发一次网络请求（所有 CID 写在同一个地址里，用逗号隔开）。
        谁在用：
            m01_resolver.py（候选结构的名称和结构）；m06_safety.SafetyService._from_pubchem()（取名字）。
        """
        if not cids:
            return []
        fields = "Title,MolecularFormula,MolecularWeight,InChIKey,InChI,IUPACName"
        ids = ",".join(str(c) for c in cids)
        # 接口返回 {"PropertyTable": {"Properties": [{...}, …]}}
        data = self._client.request_json("GET", f"{self.PUG}/compound/cid/{ids}/property/{fields}/JSON")
        return list((data or {}).get("PropertyTable", {}).get("Properties", []))

    def cas_number(self, cid: int) -> str | None:
        """一条记录的 CAS 号：在它的别名里找第一个格式和校验位都对的。

        参数：
            cid  PubChem 的记录编号。
        返回：
            CAS 号；别名里没有 CAS 号、或者没有这条记录时返回 None。
        副作用：
            发一次网络请求。
        谁在用：
            m01_resolver.py 的 _pubchem_candidates()（只给第一个候选查，省一次请求）。
        """
        # 接口返回 {"InformationList": {"Information": [{"Synonym": [别名, …]}]}}
        data = self._client.request_json("GET", f"{self.PUG}/compound/cid/{cid}/synonyms/JSON")
        for info in (data or {}).get("InformationList", {}).get("Information", []):
            for synonym in info.get("Synonym", []):
                if is_cas_number(synonym):
                    return synonym
        return None

    def ghs_page(self, cid: int) -> Any | None:
        """取一条记录的 “GHS Classification” 页面（原始 JSON，还没有解析）。

        参数：
            cid  PubChem 的记录编号。
        返回：
            PUG View 格式的字典，交给 collect_ghs() 或 parse_ghs_codes() 去读；
            这条记录没有 GHS 这一节、或者没有这条记录时返回 None。
        副作用：
            发一次网络请求。
        谁在用：
            本类的 ghs_codes()；m06_safety.compare_with_pubchem()。
        """
        # params 里的 heading 让 PubChem 只返回这一个章节，不用把整个页面取回来
        return self._client.request_json(
            "GET", f"{self.VIEW}/data/compound/{cid}/JSON", params={"heading": "GHS Classification"}
        )

    def ghs_codes(self, cid: int, min_percent: float = 30.0) -> list[str] | None:
        """这条记录的 H 编码；None 表示 PubChem 上没有它的分类数据（和“查到了，不属于危险品”不一样）。

        参数：
            cid          PubChem 的记录编号。
            min_percent  企业申报的分类至少占全部申报的百分之几才采用，默认 30（见 parse_ghs_codes）。
        返回：
            H 编码列表（可以是空的，表示不属于危险品）；没有分类数据时是 None。
        副作用：
            发一次网络请求（ghs_page）。
        谁在用：
            m06_safety.SafetyService._from_pubchem()。
        """
        data = self.ghs_page(cid)
        return parse_ghs_codes(data, min_percent) if data else None

    def close(self) -> None:
        """关掉底层的 HTTP 客户端。"""
        self._client.close()


class OpsinClient:
    """OPSIN：把英文系统命名转成结构。

    OPSIN 是一个免费的网络服务，解析的是英文的系统命名（IUPAC 名）。
    m01_resolver.py 只在名称里全是英文字母、数字和半角符号时才问它。
    接口地址在配置 OPSIN_URL_TEMPLATE 里。

    属性：
        _template  接口地址的模板，里面有一个 {name} 占位符。
        _client    限速的 HTTP 客户端，两次请求至少隔 0.2 秒。
    """

    def __init__(self, url_template: str, timeout: float = 8.0, transport: httpx.BaseTransport | None = None):
        """建一个限速的客户端。不发请求。

        参数：
            url_template  接口地址的模板，默认配置是 "https://www.ebi.ac.uk/opsin/ws/{name}.json"。
            timeout       一次请求最多等几秒。
            transport     测试用的假传输层；正常不传。
        谁在用：
            context.build_context()（配置里 ENABLE_OPSIN 打开时）。
        """
        self._template = url_template
        self._client = _ThrottledClient(timeout, 0.2, transport)

    def name_to_smiles(self, name: str) -> str | None:
        """英文系统命名 -> SMILES。

        参数：
            name  英文名称。
        返回：
            SMILES；OPSIN 解析不了这个名称时返回 None。
        抛出：
            ExternalServiceError  OPSIN 连不上、超时。
        副作用：
            发一次网络请求。
        谁在用：
            m01_resolver.py：A3 查候选结构、E1 导入库存时，内置试剂表和 PubChem 都没查到才问它。
        """
        # quote(name, safe="") 把名称里所有特殊字符（包括斜杠）都转成 %XX，再填进地址模板的 {name}
        data = self._client.request_json("GET", self._template.format(name=quote(name, safe="")))
        # 返回的 JSON 里有 smiles 这一项；没有或者是空的都当作没解析出来
        return (data or {}).get("smiles") or None

    def close(self) -> None:
        """关掉底层的 HTTP 客户端。"""
        self._client.close()


class LiteratureClient:
    """按 DOI 查文献题目、作者、年份：先问 Crossref，查不到再问 OpenAlex。

    属性：
        _email   联系邮箱（配置里的 CONTACT_EMAIL）。填了的话，每次请求都带上 mailto 参数，告诉对方是谁在调用。
        _client  限速的 HTTP 客户端，两次请求至少隔 0.2 秒。
    """

    def __init__(self, contact_email: str = "", timeout: float = 8.0, transport: httpx.BaseTransport | None = None):
        """建一个限速的客户端。不发请求。

        参数：
            contact_email  联系邮箱，可以是空字符串。
            timeout        一次请求最多等几秒。
            transport      测试用的假传输层；正常不传。
        谁在用：
            context.build_context()（配置里 ENABLE_LITERATURE_METADATA 打开时；默认是关的）。
        """
        self._email = contact_email
        # Crossref 对改过号的 DOI 回 301（2026-10-24 补 academic13k 的年份时，ChemRxiv 的旧 DOI 都是这样），跟着跳过去
        self._client = _ThrottledClient(timeout, 0.2, transport, follow_redirects=True)

    def by_doi(self, doi: str) -> dict[str, Any] | None:
        """按 DOI 查一篇文献的基本信息。

        参数：
            doi  DOI，比如 “10.1000/demo”，不带 https://doi.org/ 前缀。
        返回：
            查到时是一个字典：
                title    题目
                authors  作者姓名的列表
                year     年份
                venue    期刊名
                url      https://doi.org/<DOI>
            其中对方没给的项是 None。Crossref 和 OpenAlex 都没有这篇文献时返回 None。
        抛出：
            ExternalServiceError  接口连不上、超时。
        副作用：
            发一到两次网络请求。
        谁在用：
            api/analyses.py 的 get_step_precedents（C1 文献列表）：先例有 DOI、但库里没有题目的时候。
        """
        params = {"mailto": self._email} if self._email else None
        # 先问 Crossref。quote(doi, safe="/") 保留 DOI 里的斜杠，其余特殊字符转成 %XX。
        # Crossref 把文献信息放在返回值的 message 里
        data = self._client.request_json("GET", f"https://api.crossref.org/works/{quote(doi, safe='/')}", params=params)
        message = (data or {}).get("message")
        if message:
            # Crossref 的作者是 {"given": 名, "family": 姓} 的列表，拼成 “名 姓”；缺的那一半就不写
            authors = [
                " ".join(p for p in (a.get("given"), a.get("family")) if p) for a in message.get("author", [])
            ]
            # issued 里的 date-parts 形如 [[2019, 5, 14]]，取最前面的数当年份；没有这一项时得到 None
            year = (message.get("issued", {}).get("date-parts") or [[None]])[0][0]
            return {
                # title、container-title 都是列表，取第一项；是空的就得到 None
                "title": (message.get("title") or [None])[0],
                "authors": authors,
                "year": year,
                "venue": (message.get("container-title") or [None])[0],
                "url": f"https://doi.org/{doi}",
            }
        # Crossref 没有这篇：再问 OpenAlex。它的地址里要写完整的 https://doi.org/<DOI>
        data = self._client.request_json(
            "GET", f"https://api.openalex.org/works/https://doi.org/{quote(doi, safe='/')}", params=params
        )
        if data:
            return {
                "title": data.get("title"),
                "authors": [a.get("author", {}).get("display_name") for a in data.get("authorships", [])],
                "year": data.get("publication_year"),
                # primary_location 或它里面的 source 可能是 null，所以每一层都用 “or {}” 顶上
                "venue": ((data.get("primary_location") or {}).get("source") or {}).get("display_name"),
                "url": f"https://doi.org/{doi}",
            }
        return None

    def close(self) -> None:
        """关掉底层的 HTTP 客户端。"""
        self._client.close()
