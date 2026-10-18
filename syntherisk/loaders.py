"""把公开数据导入数据库：先例库（M5）和在售目录（M4）。

数据来源必须先在 data/data_sources.toml 里登记并且允许使用，否则拒绝导入。
这两类数据都很大（USPTO 约 177 万条反应，ZINC 约 1700 万个化合物），自己下载，不进代码仓库。

在整个流程里的位置
    这个文件不在“一次分析”的八个阶段里，是给管理员在命令行里用的，界面上没有对应的按钮：

        python -m syntherisk.cli load-precedents --source uspto --file …    → load_precedents()
        python -m syntherisk.cli load-catalog    --source zinc  --file …    → load_catalog()
        python -m syntherisk.cli status                                     → summarize()

    导进去的数据在分析时被读取：
        reaction_precedents 表（先例库）   流水线第 4 阶段，M5 给每一步找文献先例、定证据等级
        catalog_compounds 表（在售目录）   流水线第 3 阶段，M4 判断原料能不能买到

数据从哪来、到哪去
    先例库    Lowe 整理的 USPTO 专利反应（.rsmi 文件，制表符分隔）、列名对得上的 CSV，
              或者每行 “DOI|反应 SMILES” 的期刊反应文件（van der Lingen 整理的学术文献反应，来源编号 academic13k）
              → 每一行算成先例库的一行（modules/m05_precedents.py 的 precedent_values：
                规范化、反应编号、两种指纹、反应中心）
              → 分批写进 reaction_precedents 表
              → 最后换一个版本号（library_versions 表），正在运行的后端下次检索时会重新加载内存里的索引
    在售目录  每行一个 InChIKey 的文本文件，或者带 inchi_key / smiles 列的 CSV
              → 取 InChIKey 的前 14 位 → 目录里还没有的写进 catalog_compounds 表

依赖
    context.py 的 AppContext（数据库会话、数据来源登记表）、models.py（上面几张表）、
    modules/m05_precedents.py（把一条反应算成一行的各列）、modules/m01_structure.py（解析 SMILES）。
    不联网。

本文件里有什么
    Progress           进度回调的类型
    _COLUMN_ALIASES    先例文件的表头别名表
    LoaderError        导入做不了时抛的错误（来源没登记、文件不对）
    _check_source      确认数据来源登记过并且允许使用
    _check_file        确认文件存在、不是压缩包
    _is_doi_line       这一行是不是 “DOI|反应 SMILES” 的写法
    _rows              逐行读先例文件
    _number            文字转小数
    _convert_row       一行原始数据 → 先例库一行的各列（可以放到别的进程里算）
    load_precedents    导入反应先例
    _catalog_keys      逐行读在售目录文件
    load_catalog       导入在售目录
    summarize          统计库里现在有多少数据
"""

from __future__ import annotations

import csv
import itertools
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import delete, insert, select

from syntherisk import models
from syntherisk.context import AppContext
from syntherisk.modules import m01_structure as chem
from syntherisk.modules.m05_precedents import mark_library_changed, parse_yield, precedent_values

# 进度回调：导入过程中每有一条进度就调用一次，参数是一行文字。默认直接 print；
# 命令行传的是 cli._print（Windows 命令行窗口显示不了的字符会被替换掉，不会因为打印而报错），
# 测试里传一个什么都不做的函数
Progress = Callable[[str], None]

# 先例文件的表头别名：左边是后端用的字段名，右边是文件里可以出现的列名（比较时不分大小写）。
# 一个字段有几个列名都对得上时，取右边排在前面的那个。
# Lowe 的 USPTO 文件的表头是 ReactionSmiles、PatentNumber、ParagraphNum、Year、TextMinedYield、
# CalculatedYield，对应这里的 reaction_smiles、document_id、year、yield、calculated_yield（ParagraphNum 不用）
_COLUMN_ALIASES = {
    # 反应 SMILES，“反应物>试剂>产物”。唯一必须有的一列
    "reaction_smiles": ("reaction_smiles", "reactionsmiles", "rxn_smiles", "reaction", "smiles"),
    # 文献号（专利号等）
    "document_id": ("document_id", "patentnumber", "patent_number", "patent", "document", "id"),
    # 年份
    "year": ("year",),
    # 产率，文字形式（“85%”“~78%”“57 to 60%”都认）。USPTO 文件里是 TextMinedYield：从专利正文里提取的产率
    "yield": ("yield", "yield_pct", "textminedyield"),
    # 另一列产率（USPTO 文件里的 CalculatedYield）。只在上一列取不出数时才用
    "calculated_yield": ("calculatedyield", "calculated_yield"),
    # DOI。有 DOI 而没写文献类型的行，按期刊论文算
    "doi": ("doi",),
    # 文献题目
    "title": ("title",),
    # 条件说明（文字）
    "conditions": ("conditions", "conditions_text", "procedure"),
    # 反应温度（摄氏度）
    "temperature_c": ("temperature_c", "temperature"),
    # 文献类型：patent / journal / dataset
    "doc_type": ("doc_type", "type"),
}


class LoaderError(RuntimeError):
    """导入做不了，而且原因是运行命令的人自己能解决的：数据来源没登记、文件路径不对、
    给的是压缩包、表头不对。

    报错信息是写给人看的中文。cli.py 的 main 接住它，只打印这句话就退出，不甩一长串调用栈。
    """

    pass


def _check_source(ctx: AppContext, source: str) -> None:
    """确认数据来源登记过、并且登记为允许使用；否则抛 LoaderError，什么都不导入。

    参数：
        ctx     运行时上下文。登记表（data/data_sources.toml）启动时已经读进 ctx.reference.data_sources。
        source  来源编号，比如 "uspto"、"ord"、"zinc"，就是登记表里 [source.xxx] 的 xxx。
    抛出：
        LoaderError  表里没有这个编号，或者它的 allowed 不是 true（比如学校的 Reaxys、SciFinder 账号）。
    谁在用：
        本文件的 load_precedents、load_catalog。
    """
    if not ctx.reference.source_allowed(source):
        raise LoaderError(
            f"数据来源 {source!r} 没有登记，或者登记为不允许使用。先在 data/data_sources.toml 里登记，确认许可后再导入。"
        )


def _check_file(path: Path) -> None:
    """在真正开始之前，把最常见的两种文件问题用人话说清楚。

    参数：
        path  命令行 --file 给的路径。
    抛出：
        LoaderError  扩展名是 .7z / .zip / .gz / .rar（把没解压的压缩包直接递进来了），
                     或者这个路径上没有文件。先看扩展名、再看文件在不在，
                     所以一个不存在的压缩包路径报的是“是压缩包”。
    谁在用：
        本文件的 load_precedents、load_catalog。
    """
    # path.suffix 是最后一个扩展名（带点），比如 ".7z"；转成小写再比
    if path.suffix.lower() in (".7z", ".zip", ".gz", ".rar"):
        raise LoaderError(f"{path.name} 是压缩包。先解压，再把 --file 指向解压出来的文件。")
    if not path.is_file():
        raise LoaderError(
            f"找不到文件：{path}\n"
            "--file 后面要写这个文件在电脑上的实际位置，比如 data\\external\\uspto\\1976_Sep2016_USPTOgrants_smiles.rsmi"
        )


# ---------------------------------------------------------------- 先例库（M5）


def _is_doi_line(line: str) -> bool:
    """这一行是不是 “DOI|反应 SMILES” 的写法：竖线左边以 “10.” 开头（DOI 都这样开头），右边正好有两个 “>”。

    参数：
        line  文件里的一行文字。
    返回：
        True / False。
    谁在用：
        本文件的 _rows：只看文件的第一行，决定整个文件按哪种格式读。
    """
    # partition 在第一个 “|” 处切成三段：左边、“|” 本身（没有竖线时是空字符串）、右边
    doi, bar, reaction = line.strip().partition("|")
    return bool(bar) and doi.strip().startswith("10.") and reaction.count(">") == 2


def _rows(path: Path) -> Iterator[dict[str, str]]:
    """读 Lowe 的 USPTO .rsmi 文件（制表符分隔）、普通 CSV，或者每行 “DOI|反应 SMILES” 的期刊反应文件。

    这是一个生成器：一行一行地读、一行一行地交出去，不把整个文件读进内存（USPTO 全量约 180 万行）。
    也因为是生成器，文件是在第一次向它要数据时才打开、才检查表头的，不是在调用 _rows(path) 的那一刻。

    参数：
        path  文件路径。必须是 UTF-8 编码。.rsmi 和 CSV 的第一行是表头，列名不分大小写。
              “DOI|反应 SMILES” 的文件没有表头，第一行就是数据（van der Lingen 整理的学术文献反应就是这样，
              data/data_sources.toml 里的 academic13k）；第一行符合 _is_doi_line 的，整个文件按这种格式读。
    产出：
        每个数据行一个字典 {字段名: 单元格文字}。字段名是 _COLUMN_ALIASES 左边的那些，
        只包含表头里找得到的列；单元格去掉了首尾空白，这一行不够长时缺的格子是空字符串。
        “DOI|反应 SMILES” 的文件每一行给 {"doi": …, "reaction_smiles": …}，空行跳过。
    抛出：
        LoaderError         既不是 “DOI|反应 SMILES” 的文件，表头里也没有反应 SMILES 那一列。
        UnicodeDecodeError  文件不是 UTF-8 编码。
    谁在用：
        本文件的 load_precedents。
    """
    # utf-8-sig：文件开头有 BOM（有些 Windows 软件会加）就自动去掉；newline="" 是 csv 模块要求的打开方式
    with path.open(encoding="utf-8-sig", newline="") as handle:
        first = handle.readline()
        # 回到文件开头：下面不论按哪种格式，都从第一行读起
        handle.seek(0)
        if _is_doi_line(first):
            for line in handle:
                # 在第一个 “|” 处切开：左边是 DOI，右边是反应 SMILES（SMILES 里不会出现 “|”）
                doi, _, reaction = line.strip().partition("|")
                if doi or reaction:
                    yield {"doi": doi.strip(), "reaction_smiles": reaction.strip()}
            return
        # 看第一行猜分隔符：有制表符、而且不比逗号少，就按制表符分；否则按逗号分
        delimiter = "\t" if first.count("\t") >= first.count(",") and "\t" in first else ","
        reader = csv.reader(handle, delimiter=delimiter)
        header = [cell.strip().lower() for cell in next(reader)]
        # mapping：字段名 → 它在第几列。每个字段按别名的先后找，找到第一个就不再找
        mapping: dict[str, int] = {}
        for field, aliases in _COLUMN_ALIASES.items():
            for alias in aliases:
                if alias in header and field not in mapping:
                    mapping[field] = header.index(alias)
        if "reaction_smiles" not in mapping:
            raise LoaderError("文件第一行要有表头，并且至少有一列反应 SMILES（ReactionSmiles 或 reaction_smiles）；"
                              "或者每行写成 “DOI|反应 SMILES”")
        for cells in reader:
            yield {field: (cells[index].strip() if index < len(cells) else "") for field, index in mapping.items()}


def _number(text: str) -> float | None:
    """把一格文字转成小数；是空的、不是数字、或者传进来的是 None，都返回 None，不报错。

    谁在用：
        本文件的 _convert_row（年份和温度两列）。
    """
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _convert_row(job: tuple[int, dict[str, str], str]) -> dict[str, Any] | None | str:
    """一行原始数据 → 先例库一行的各列。算指纹最费时间，所以这一步可以分给多个进程做。

    读不出来的返回 None；处理时出了意外的返回一句说明（不让一行坏数据中断一次要跑半小时的导入）。

    参数：
        job  一个三元组 (行号, 这一行的字典, 数据来源编号)。行号从 1 开始，不算表头。
             三样东西打包成一个参数、函数又写在模块最外层，是因为它要交给进程池：
             pool.map 每次只传一个参数，而且函数和参数都要能序列化（pickle）以后送到子进程里去。
    返回（三种）：
        dict  先例库一行的各列，可以直接插进 reaction_precedents 表。键见 m05_precedents.precedent_values：
              source、doc_type、document_id、year、doi、title、reaction_smiles、reaction_key、product_key、
              yield_pct、agents、conditions_text、temperature_c、fp_diff、fp_product、center。
        None  这一行的反应读不出来（不是合法的反应 SMILES、没有产物……），调用方记成“跳过”。
        str   处理时抛了没料到的异常：返回 “第 N 行：错误类型: 错误信息”，调用方记成“失败”。
    副作用：
        没有。只算不存，不碰数据库。
    谁在用：
        本文件的 load_precedents（直接调用，或者通过进程池）。
    """
    number, row, source = job
    year = _number(row.get("year", ""))
    try:
        return precedent_values(
            row["reaction_smiles"],
            source=source,
            # 文献号：没有这一列就用 DOI，再没有就编一个 “来源-行号”，保证每条先例都有文献号
            document_id=row.get("document_id") or row.get("doi") or f"{source}-{number}",
            # 文献类型：文件里写了就用文件里的；没写的话，有 DOI 算期刊论文，否则算专利
            doc_type=row.get("doc_type") or ("journal" if row.get("doi") else "patent"),
            year=int(year) if year else None,
            doi=row.get("doi") or None,
            title=row.get("title") or None,
            # 产率先看文字提取的那一列，取不出数再看另一列。parse_yield 只接受大于 0、不超过 100 的数
            yield_pct=parse_yield(row.get("yield")) or parse_yield(row.get("calculated_yield")),
            conditions_text=row.get("conditions") or None,
            temperature_c=_number(row.get("temperature_c", "")),
        )
    except Exception as error:  # noqa: BLE001
        # 故意接住所有异常（行尾的 noqa 是告诉代码检查工具“这里是有意的”）：坏的只是这一行，记下来继续
        return f"第 {number} 行：{type(error).__name__}: {error}"


def load_precedents(
    ctx: AppContext,
    path: Path,
    source: str,
    *,
    limit: int | None = None,
    replace: bool = False,
    workers: int = 1,
    batch_size: int = 20000,
    progress: Progress = print,
) -> dict[str, int]:
    """导入反应先例。replace=True 时先删掉这个来源原有的数据，避免同一个文件导两遍。

    workers > 1 时用多个进程算指纹（USPTO 全量约 180 万条，单进程要一个多小时）；写库始终在当前进程里按原顺序做。

    参数：
        ctx         运行时上下文（数据库会话工厂、数据来源登记表）。
        path        要导入的文件：Lowe 的 USPTO .rsmi、表头对得上 _COLUMN_ALIASES 的 CSV，
                    或者每行 “DOI|反应 SMILES” 的期刊反应文件（见 _rows）。
        source      数据来源编号（data/data_sources.toml 里登记的，比如 "uspto"、"ord"），
                    写进每一行的 source 列。
        limit       只读前多少行数据，试跑用；None 表示全部。对应命令行的 --limit。
        replace     True 时先删掉 reaction_precedents 表里这个来源的全部旧数据。对应命令行的 --replace。
        workers     用几个进程算；1 表示就在当前进程里算。对应命令行的 --workers，不写时是 CPU 核数的一半。
        batch_size  每批多少行：算完一批、写一次库、提交一次、报一次进度。默认两万。
        progress    进度回调，每批调用一次；另外前 5 个处理失败的行各报一次。
    返回：
        统计数字的字典：
            read         读了多少行数据
            loaded       写进库里多少条
            skipped      反应读不出来、跳过的行数
            duplicates   和这次导入里前面某一行重复（文献号和反应编号都相同）、没有再写的行数
            failed       处理时出错的行数
            with_center  写进去的里面有多少条算出了反应中心（原始数据带原子映射才算得出）
        read = loaded + skipped + duplicates + failed。
    副作用：
        写数据库：replace 时删旧数据；分批插入 reaction_precedents；最后更新 library_versions 表里
        先例库的版本号。每批提交一次，所以中途停下的话，已经提交的那些批留在库里。
        workers > 1 时会另起进程。
    抛出：
        LoaderError  来源没登记或不允许使用；文件不存在或是压缩包；表头里没有反应 SMILES。
                     这几种错都在删除旧数据之前报出来，所以带着 replace 递错了文件，库里原有的数据不会丢。
    注意：
        查重只在这一次导入的范围内做（在内存里记着见过的“文献号 + 反应编号”），不和库里已有的数据比。
        同一个文件不带 replace 再导一遍，库里就会有两份。
    谁在用：
        cli.py 的 cmd_load_precedents；tests/test_integrations.py。
    """
    _check_source(ctx, source)
    _check_file(path)
    stats = {"read": 0, "loaded": 0, "skipped": 0, "duplicates": 0, "failed": 0, "with_center": 0}
    # 这次导入里已经见过的 (文献号, 反应编号)：同一篇文献里重复出现的同一个反应只留第一条
    seen: set[tuple[str, str]] = set()
    # 一个惰性的生成器，每项是 (行号, 行, 来源)。islice(x, limit) 只取前 limit 项，limit 是 None 时取全部。
    # 到这里还没有真的去读文件，下面要一批数据时才读
    jobs = ((number, row, source) for number, row in enumerate(itertools.islice(_rows(path), limit), 1))
    # 进程池：workers 个子进程，各算各的。只用一个进程时不建
    pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        with ctx.session_factory() as session:
            # 先取第一批（最多 batch_size 行）：到这一步才真正打开文件、检查表头。
            # 文件不对的话在这里就报错，旧数据还一行没动——所以取数要放在删除的前面
            batch = list(itertools.islice(jobs, batch_size))
            if replace:
                # delete(表).where(条件)：一条 SQL 删掉这个来源的全部旧数据，马上提交
                session.execute(delete(models.ReactionPrecedent).where(models.ReactionPrecedent.source == source))
                session.commit()
            # 一批一批处理；循环末尾取下一批，取不到东西（文件读完了）循环就结束
            while batch:
                # pool.map 把这一批分给各个子进程，结果仍按原来的顺序交回；chunksize=250 是每次交给
                # 一个子进程 250 行，省得一行一行来回传。没有进程池时用内置的 map，在当前进程里一行行算
                converted = pool.map(_convert_row, batch, chunksize=250) if pool else map(_convert_row, batch)
                # fresh：这一批里要写进库的行
                fresh = []
                for values in converted:
                    stats["read"] += 1
                    if values is None:
                        stats["skipped"] += 1
                        continue
                    if isinstance(values, str):
                        stats["failed"] += 1
                        # 出错的行只报前 5 个，免得一个坏文件刷满屏幕；总数在最后的统计里
                        if stats["failed"] <= 5:
                            progress(f"跳过一行处理不了的数据（{values}）")
                        continue
                    identity = (values["document_id"], values["reaction_key"])
                    if identity in seen:
                        stats["duplicates"] += 1
                        continue
                    seen.add(identity)
                    fresh.append(values)
                    # 布尔值可以直接加：True 当 1，False 当 0
                    stats["with_center"] += values["center"] is not None
                if fresh:
                    # insert(表) 配一个字典列表：一次把这一批全部插进去，比一行一行加快得多
                    session.execute(insert(models.ReactionPrecedent), fresh)
                    session.commit()
                    stats["loaded"] += len(fresh)
                # 格式里的 “:,” 是给数字加千位分隔符：1750279 显示成 1,750,279
                progress(f"已读 {stats['read']:,} 行，导入 {stats['loaded']:,} 条")
                batch = list(itertools.islice(jobs, batch_size))
            # 换一个版本号：正在运行的后端进程下次检索时会发现，并重新加载内存里的索引
            mark_library_changed(session)
            session.commit()
    finally:
        # 不管成功还是出错，都把子进程收掉
        if pool:
            pool.shutdown()
    return stats


# ---------------------------------------------------------------- 在售目录（M4）


def _catalog_keys(path: Path) -> Iterator[tuple[str, str | None]]:
    """每行一个 InChIKey 的文本文件，或者带 inchi_key / smiles 列的 CSV。

    生成器，一行一行读。两种文件靠第一行来分：第一行里没有逗号和制表符、也不是
    inchi_key / inchikey / smiles 这样的表头，就当成“每行一个 InChIKey”的纯文本；否则当成带表头的表格。

    参数：
        path  在售目录文件，UTF-8 编码。
    产出：
        (key14, smiles) 二元组。key14 是 InChIKey 的前 14 位；没有 SMILES 时第二项是 None。
        - 纯文本：每行取前 14 个字符，第二项是 None。不到 14 个字符的行（比如空行）跳过。
          这里不检查这一行是不是真的 InChIKey。
        - 表格：列名不分大小写。有 inchi_key（或 inchikey）列并且够 14 位，就用它的前 14 位，
          smiles 列有值就带上；否则有 SMILES 的话，用 RDKit 从 SMILES 算出 key14 和规范 SMILES；
          两样都没有、或者 SMILES 读不出来的行跳过。
    谁在用：
        本文件的 load_catalog。
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        # 先读第一行判断是哪种文件，再回到开头正式读
        first = handle.readline().strip()
        handle.seek(0)
        if "," not in first and "\t" not in first and first.lower() not in ("inchi_key", "inchikey", "smiles"):
            # 纯文本：一行一个 InChIKey
            for line in handle:
                key = line.strip()
                if len(key) >= 14:
                    yield key[:14], None
            return
        # 带表头的表格。DictReader 把每一行读成 {列名: 值}
        reader = csv.DictReader(handle, delimiter="\t" if "\t" in first else ",")
        for row in reader:
            # 列名统一成小写、去掉首尾空白；值是 None（这一行比表头短）的当成空字符串
            lowered = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            key = lowered.get("inchi_key") or lowered.get("inchikey")
            smiles = lowered.get("smiles") or None
            if key and len(key) >= 14:
                yield key[:14], smiles
            elif smiles:
                record = chem.mol_record(smiles)
                if record:
                    yield record.key14, record.smiles


def load_catalog(
    ctx: AppContext, path: Path, source: str, *, batch_size: int = 5000, progress: Progress = print
) -> dict[str, int]:
    """导入在售化合物目录。已经在目录里的化合物跳过。

    参数：
        ctx         运行时上下文。
        path        目录文件，格式见 _catalog_keys。
        source      数据来源编号（data/data_sources.toml 里登记的，比如 "zinc"），写进每一行的 source 列。
        batch_size  攒够多少个不同的化合物就写一次库、提交一次、报一次进度。默认 5000。
        progress    进度回调，每批调用一次。
    返回：
        统计数字的字典：
            read      从文件里读到多少个 key（被跳过的行不算）
            loaded    新加进目录多少个
            existing  目录里本来就有、没有再加的个数（不管原来是哪个来源加的）
        同一批里重复出现的 key 只算一次，所以 loaded + existing 可能小于 read。
    副作用：
        往 catalog_compounds 表里加行，每批提交一次。已有的行不改（来源、SMILES 都保持原样），不删任何数据。
        新加的行只填 key14、smiles、source 三列，name 是空的。
    抛出：
        LoaderError  来源没登记或不允许使用；文件不存在或是压缩包。
    谁在用：
        cli.py 的 cmd_load_catalog；tests/test_integrations.py。
    """
    _check_source(ctx, source)
    _check_file(path)
    stats = {"read": 0, "loaded": 0, "existing": 0}

    def flush(session, batch: dict[str, str | None]) -> None:
        """把攒下的一批 {key14: smiles} 写进目录：查出库里已经有的，只加没有的，然后提交、累计统计、报进度。"""
        # session.scalars(...)：查询只取一列时，直接给出这一列的值，而不是一行一行的元组。
        # key14.in_(...) 相当于 SQL 的 WHERE key14 IN (...)；list(batch) 是字典的全部键
        existing = set(
            session.scalars(select(models.CatalogCompound.key14).where(models.CatalogCompound.key14.in_(list(batch))))
        )
        fresh = [
            models.CatalogCompound(key14=key, smiles=smiles, source=source)
            for key, smiles in batch.items()
            if key not in existing
        ]
        session.add_all(fresh)
        session.commit()
        stats["loaded"] += len(fresh)
        stats["existing"] += len(batch) - len(fresh)
        progress(f"已读 {stats['read']:,} 行，新增 {stats['loaded']:,} 个")

    with ctx.session_factory() as session:
        # 用字典攒一批：键是 key14，天然去重
        batch: dict[str, str | None] = {}
        for key, smiles in _catalog_keys(path):
            stats["read"] += 1
            # setdefault：这个 key 这一批里已经有了就不动，保留先读到的那个 SMILES
            batch.setdefault(key, smiles)
            if len(batch) >= batch_size:
                flush(session, batch)
                batch = {}
        # 最后不满一批的零头
        if batch:
            flush(session, batch)
    return stats


# ---------------------------------------------------------------- 统计


def summarize(ctx: AppContext) -> dict[str, Any]:
    """现在库里有多少数据。

    返回：
        {
          "precedents":             {来源编号: 先例条数}，比如 {"uspto": 1750279}；先例库是空的时是 {},
          "precedents_with_center": 先例里带反应中心的条数（不分来源）,
          "catalog":                {来源编号: 在售目录里的化合物个数}；启动时从内置试剂表写进去的那些，
                                    来源是 "seed",
        }
    副作用：
        只读数据库（三条 COUNT 查询）。
    谁在用：
        cli.py 的 cmd_status（python -m syntherisk.cli status 输出里的 data 部分）；
        tests/test_integrations.py。
    """
    # func 是 SQL 函数的入口，func.count() 就是 COUNT(*)。只有这个函数用，所以在函数里导入
    from sqlalchemy import func

    with ctx.session_factory() as session:
        # SELECT source, COUNT(*) … GROUP BY source；.all() 得到 [(来源, 条数), ...]，dict() 转成字典
        precedents = dict(
            session.execute(
                select(models.ReactionPrecedent.source, func.count()).group_by(models.ReactionPrecedent.source)
            ).all()
        )
        # 其中带反应中心的有多少：是 0 的话说明是旧版本导入的，重新导入以后才会按反应中心比较同类反应
        # session.scalar(...)：只要结果的第一行第一列，这里就是一个数。center.is_not(None) 即 SQL 的 IS NOT NULL
        with_center = session.scalar(
            select(func.count()).select_from(models.ReactionPrecedent).where(models.ReactionPrecedent.center.is_not(None))
        )
        catalog = dict(
            session.execute(
                select(models.CatalogCompound.source, func.count()).group_by(models.CatalogCompound.source)
            ).all()
        )
    return {"precedents": precedents, "precedents_with_center": with_center or 0, "catalog": catalog}
