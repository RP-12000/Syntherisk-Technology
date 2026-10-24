"""命令行工具。在项目根目录运行：

    python -m syntherisk.cli status                                   各模块接的是什么、库里有多少数据
    python -m syntherisk.cli seed-demo                                建演示课题组和账号（账号密码写在 .env 里）
    python -m syntherisk.cli load-precedents --source uspto --file …   导入反应先例（M5）
    python -m syntherisk.cli load-catalog --source zinc --file …       导入在售目录（M4）
    python -m syntherisk.cli check-hazards                            把内置试剂表的危害分类和 PubChem 对一遍（M6）
    python -m syntherisk.cli fill-literature --source academic13k      按 DOI 给期刊先例补年份和题目（M5）
    python -m syntherisk.cli calibrate                               用已授权的实验回填做一次校准统计（M14）
    python -m syntherisk.cli export-consented --output data.json      导出已授权的匿名回填数据（给产率模型用）

这个文件负责什么
    给维护后端和数据的人用的命令行工具：不经过 HTTP 接口，直接操作数据库和参考数据。
    每条命令都会先建一份运行时上下文（和启动后端时一样：读 .env、连数据库、需要时建表），做完就退出。
    界面上没有对应的按钮。

每条命令对应的模块、读写的东西
    status            各模块现状（和 GET /api/meta/modules 相同）加上库里的数据量。只读
    seed-demo         M0、M4：写 organizations、users、memberships、projects、inventory_items
    load-precedents   M5：读 .rsmi / CSV / “DOI|反应 SMILES” 文件，写 reaction_precedents、library_versions；
                      带 --replace 时先删掉这个来源原有的数据
    load-catalog      M4：读文件，写 catalog_compounds
    check-hazards     M6：联网查 PubChem，和 data/compounds.csv 比对。只打印差异，不改任何文件和数据库
    fill-literature   M5：联网查 Crossref / OpenAlex，给有 DOI 的先例补年份和题目，写 reaction_precedents
    calibrate         M14：读已授权的 experiment_records，写一行 calibration_runs，打印统计
    export-consented  M14：读已授权的 experiment_records，写一个 JSON 文件（--output 指定的路径）

谁调用它、它依赖谁
    人在命令行里运行；tests/ 里直接调 cli.main([...]) 和 cli.cmd_check_hazards(...)。
    它依赖 context.py（build_context）、loaders.py（导入和统计）、m14_backfill.py（校准和导出）、
    m04_availability.py（往库存里加东西）、m06_safety.py（compare_with_pubchem）、
    external.py（PubChemClient）、security.py（hash_password）。

本文件里有什么
    DEMO_INVENTORY       seed-demo 放进演示库存的 8 种试剂
    _print()             不会因为终端编码而报错的打印函数
    cmd_status()、cmd_seed_demo()、cmd_load_precedents()、cmd_load_catalog()、cmd_calibrate()、
    cmd_export_consented()、cmd_check_hazards()、cmd_fill_literature()
                         八条命令各一个函数，参数都是 (ctx, args)
    main()               解析命令行参数，调对应的 cmd_ 函数
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select

from syntherisk import loaders, models
from syntherisk.context import AppContext, build_context
from syntherisk.external import ExternalServiceError, PubChemClient
from syntherisk.modules import m14_backfill
from syntherisk.modules.m04_availability import InventoryService
from syntherisk.modules.m06_safety import compare_with_pubchem
from syntherisk.security import hash_password

# 和 PPT 库存页示意图里的那几样一致，方便演示“库存覆盖”
# 每一项是 (data/compounds.csv 里的 key, 数量, 存放位置)。数量和位置只是显示用的文字，后端不解析
DEMO_INVENTORY = [
    ("acetic_anhydride", "500 mL", "试剂柜 A3"),
    ("diethyl_ether", "1 L", "防爆柜"),
    ("2_methyltetrahydrofuran", "500 mL", "防爆柜"),
    ("salicylic_acid", "100 g", "药品柜 2"),
    ("4_hydroxyacetophenone", "25 g", "药品柜 1"),
    ("magnesium", "100 g", "药品柜 1"),
    ("hydrochloric_acid", "2.5 L", "酸柜"),
    ("sodium_borohydride", "25 g", "干燥器"),
]


def _print(text: str) -> None:
    """打印一行文字，遇到当前终端显示不了的字符也不报错。命令行里所有的输出都走它。

    参数：
        text  要打印的内容（末尾自动加换行）。
    说明：
        它也被当作“进度回调”传给 loaders.load_precedents() 和 load_catalog()（参数 progress=_print），
        导入过程中的进度就是通过它打印出来的。
    """
    # Windows 的命令行窗口默认不是 UTF-8，遇到显示不了的字符就替换掉，不要因为打印而报错
    # 做法：先按终端的编码把文字转成字节，转不了的字符换成 “?”（第二个参数 "replace"），再转回文字输出
    encoding = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(encoding, "replace").decode(encoding) + "\n")
    sys.stdout.flush()  # 输出被重定向到文件时也要马上看得到进度


def cmd_status(ctx: AppContext, _args) -> None:
    """status 命令：打印各模块现在接的是什么，以及库里有多少数据。只读。

    输出是一段 JSON，有两部分：
        modules  ctx.describe_modules() 的结果：M1–M14、MQ、database 各一项（和 GET /api/meta/modules 相同）
        data     loaders.summarize() 的结果：
                   precedents              先例库各来源的条数，比如 {"uspto": 1750279}
                   precedents_with_center  其中带“反应中心”的条数；先例库有数据而这里是 0，说明要重新导入
                   catalog                 在售目录各来源的个数，比如 {"seed": …, "zinc": …}
    参数：
        ctx    运行时上下文。
        _args  用不到（这条命令没有选项），留着是为了和其他命令的参数形式一致。
    """
    _print(json.dumps({"modules": ctx.describe_modules(), "data": loaders.summarize(ctx)}, ensure_ascii=False, indent=2))


def cmd_seed_demo(ctx: AppContext, _args) -> None:
    """seed-demo 命令：建一个演示课题组、一个账号、一个项目，并往库存里放 8 种试剂。

    账号和密码不写在代码里，取自配置：DEMO_USER_EMAIL、DEMO_USER_PASSWORD（至少 8 位）、
    DEMO_ORG_NAME（默认 “示例课题组”）。
    做的事和注册接口（POST /api/auth/register）差不多，只是多放了库存：
        organizations 一行、users 一行（密码存的是 scrypt 哈希）、memberships 一行（角色 owner）、
        projects 一行（“演示项目”）、inventory_items 八行（DEMO_INVENTORY，内容加密）。
    可以重复运行：这个邮箱已经有账号的话什么都不做，只提示一句。
    抛出：
        SystemExit  没配邮箱或密码，或者密码不到 8 位。
        KeyError    DEMO_INVENTORY 里的某个 key 在 data/compounds.csv 里找不到（那张表被改过时）。
    """
    settings = ctx.settings
    if not (settings.demo_user_email and settings.demo_user_password):
        raise SystemExit("先在 .env 里设置 DEMO_USER_EMAIL 和 DEMO_USER_PASSWORD（密码至少 8 位），再运行这个命令。")
    if len(settings.demo_user_password) < 8:
        raise SystemExit("DEMO_USER_PASSWORD 至少要 8 位。")
    # 邮箱统一转成小写再存、再比。注册和登录接口也是这样处理的，所以登录时邮箱不分大小写
    email = settings.demo_user_email.strip().lower()
    with ctx.session_factory() as session:
        # 只查 id 这一列：查得到就说明这个邮箱已经有账号了
        if session.scalar(select(models.User.id).where(models.User.email == email)):
            _print(f"账号 {email} 已经存在，没有改动。")
            return
        org = models.Organization(name=settings.demo_org_name, settings={})
        user = models.User(email=email, display_name="演示用户", password_hash=hash_password(settings.demo_user_password))
        session.add_all([org, user])
        # flush：把上面两行先发给数据库（还没提交），这样 org.id 和 user.id 才有值，下面建关联的行要用
        session.flush()
        session.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
        session.add(models.Project(org_id=org.id, name="演示项目", created_by=user.id))
        # InventoryService 按“这个课题组、这个人”来写库存
        inventory = InventoryService(session, org.id, user.id)
        for key, quantity, location in DEMO_INVENTORY:
            # by_key：按 compounds.csv 的 key 列找试剂，拿到它的中文名、CAS 号和结构
            compound = ctx.reference.compounds.by_key[key]
            # upsert：这个结构已经在库存里就更新那一条，否则新建一条
            inventory.upsert(name=compound.display_name, cas=compound.cas, smiles=compound.smiles,
                             quantity=quantity, location=location, source="manual")
        # 到这里才一次提交：中途出错的话，课题组、账号、库存一样都不会留下
        session.commit()
    _print(f"已建好课题组“{settings.demo_org_name}”和账号 {email}，库存里放了 {len(DEMO_INVENTORY)} 种试剂。")


def cmd_load_precedents(ctx: AppContext, args) -> None:
    """load-precedents 命令：把一个反应数据文件导入先例库（M5）。真正的工作在 loaders.load_precedents() 里。

    命令行选项（解析后在 args 上）：
        --source   数据来源编号，必须先在 data/data_sources.toml 里登记并允许使用，比如 uspto、academic13k
        --file     文件路径：Lowe 整理的 USPTO .rsmi（制表符分隔）、带表头的 CSV，
                   或者每行 “DOI|反应 SMILES” 的期刊反应文件。压缩包要先解压
        --limit    只导入前 N 行（试跑用）；不写就全部导入
        --replace  先删掉这个来源原有的数据再导入。重新导同一个文件时要加，不然同样的数据会有两份
        --workers  用几个进程算指纹和反应中心；不写（默认 0）就用一半的 CPU 核，至少 1 个
    打印：
        导入过程中的进度；最后是读了多少行、导入多少条、跳过和重复各多少、多少条算出了反应中心。
    抛出：
        loaders.LoaderError  来源没登记、文件找不到、文件是压缩包、文件没有表头等。
                             main() 会把它换成一句提示后退出。
    """
    # os.cpu_count() 是 CPU 的核数（查不到时返回 None，这里按 2 算）；// 2 取一半，max(1, …) 保证至少 1 个进程
    workers = args.workers or max(1, (os.cpu_count() or 2) // 2)
    stats = loaders.load_precedents(ctx, Path(args.file), args.source, limit=args.limit, replace=args.replace,
                                    workers=workers, progress=_print)
    # stats 的键：read 读了几行；loaded 导入了几条；skipped 读不出来跳过的；duplicates 重复的；
    # failed 处理时出错的；with_center 算出了反应中心的。{…:,} 给数字加千位分隔符
    _print(f"完成：读了 {stats['read']:,} 行，导入 {stats['loaded']:,} 条，"
           f"读不出来跳过 {stats['skipped']:,} 条，重复 {stats['duplicates']:,} 条。")
    _print(f"导入的先例里有 {stats['with_center']:,} 条带原子映射、算出了反应中心（能按反应中心比较同类反应），"
           f"其余 {stats['loaded'] - stats['with_center']:,} 条只能按指纹比较。")
    if stats["failed"]:
        _print(f"另有 {stats['failed']:,} 行处理时出错，已跳过。")


def cmd_load_catalog(ctx: AppContext, args) -> None:
    """load-catalog 命令：把一份在售化合物清单导入在售目录（M4 的 catalog_compounds 表）。

    命令行选项：
        --source  数据来源编号（同样要在 data/data_sources.toml 里登记并允许使用），比如 zinc；
                  它会记在每一行的 source 列里
        --file    文件路径：每行一个 InChIKey 的文本文件，或者带 inchi_key / smiles 列的 CSV
    已经在目录里的化合物会跳过，所以同一个文件可以重复导入。真正的工作在 loaders.load_catalog() 里。
    打印：
        进度，以及最后读了多少行、新增多少个、已有多少个。
    抛出：
        loaders.LoaderError（来源没登记、文件找不到等）。
    """
    stats = loaders.load_catalog(ctx, Path(args.file), args.source, progress=_print)
    _print(f"完成：读了 {stats['read']:,} 行，新增 {stats['loaded']:,} 个，已有 {stats['existing']:,} 个。")


def cmd_fill_literature(ctx: AppContext, args, client=None) -> None:
    """fill-literature 命令：按 DOI 去 Crossref / OpenAlex 查，给期刊先例补上年份和题目（M5）。

    命令行选项：
        --source  只补这个来源的先例，比如 academic13k；不写就是全部有 DOI 的先例
        --limit   最多查多少个 DOI（试跑用）
    真正的工作在 loaders.fill_literature_metadata() 里。中途被限流或断网会停下，再运行一次就接着查。
    参数：
        client  文献信息的客户端；命令行运行时不传，这里按配置里的 CONTACT_EMAIL 建一个；测试传假的。
    打印：
        进度，以及最后查了多少个 DOI、查到多少个、改了多少行；中途停下时说明停在哪、为什么。
    """
    from syntherisk.external import LiteratureClient

    client = client or LiteratureClient(ctx.settings.contact_email, timeout=20)
    stats = loaders.fill_literature_metadata(ctx, client, source=args.source, limit=args.limit, progress=_print)
    _print(f"完成：查了 {stats['dois']:,} 个 DOI，查到 {stats['found']:,} 个，"
           f"两边都没有 {stats['missing']:,} 个，改了 {stats['updated_rows']:,} 行。")
    if stats["stopped_at"]:
        _print(f"在 {stats['stopped_at']} 停下了（{stats['error']}）。过一阵再运行同一条命令，会从没补的接着查。")


def cmd_calibrate(ctx: AppContext, _args) -> None:
    """calibrate 命令：用已授权的实验回填记录做一次校准统计（M14），结果存库并打印出来。

    读 experiment_records 里 consent_for_training 为真的记录；写一行 calibration_runs。
    打印的是统计结果的 JSON：记录数、各种实验结果的条数、按证据等级和操作标签分组的结果、
    实际产率和文献产率的平均偏差等。
    只出统计，不会自动改评分的权重。
    """
    with ctx.session_factory() as session:
        run = m14_backfill.run_calibration(session)
        # run_calibration 只把新的一行加进会话；这里提交了才真正写进数据库
        session.commit()
        _print(json.dumps(run.stats, ensure_ascii=False, indent=2))


def cmd_export_consented(ctx: AppContext, args) -> None:
    """export-consented 命令：把已授权的回填记录导出成一个匿名的 JSON 文件（留给 M8 产率模型当训练数据）。

    命令行选项：
        --output  要写的文件路径。文件已经存在的话会被覆盖。
    文件内容是一个列表，每条记录只有这几项：reaction_smiles、outcome、actual_yield、step_score、dimensions、
    evidence_level、reported_yield_median、operation_tags。不含课题组、人员和自由填写的备注。
    注意：
        导出的文件是明文的（这些内容在数据库里是加密存的），里面有反应的结构，导出以后要自己保管好。
    """
    with ctx.session_factory() as session:
        dataset = m14_backfill.consented_dataset(session)
    # 数据已经取到内存里了，会话在上面的 with 结束时关掉；写文件用不着数据库
    Path(args.output).write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    _print(f"导出了 {len(dataset)} 条已授权的匿名记录到 {args.output}")


def cmd_check_hazards(ctx: AppContext, args, pubchem: PubChemClient | None = None) -> None:
    """check-hazards 命令：把内置试剂表（data/compounds.csv）的危害分类逐个和 PubChem 对一遍，列出不一致的行。

    只打印，不改表：改不改由人看过以后决定（PubChem 上的分类有时对应的是另一种形态，见 README）。
    比较时只看 H 编码的前四位（H360FD 和 H360 算同一条）。
    命令行选项（解析后在 args 上）：
        --only        只查这几个，用逗号分开，写 compounds.csv 里的 key，比如 --only toluene,acetone
        --start-from  从这个 key 开始往后查（上次中途停下时，照提示接着查）
    参数：
        ctx      运行时上下文。
        args     解析好的命令行参数，用到 args.only 和 args.start_from。
        pubchem  PubChem 客户端。命令行运行时不传，这里自己建一个很慢的（3 秒一次请求）；测试传假的。
    每个物质最多打印一行（一致的不打印）：
        不一致    表里和 PubChem 的 H 编码有出入：列出表里没有的、表里多出来的
        待填      表里写的是 “?”（不知道），而 PubChem 有数据
        没有数据  PubChem 上没有它的分类
      最后两行是合计和一句提醒。
    抛出：
        SystemExit  --only 或 --start-from 给的 key 在表里没有；或者中途 PubChem 请求失败（限流、断网），
                    这时会说明查到哪一个停下了、已经查完的统计，以及接着查该用的命令。
    读写：
        联网请求 PubChem。只读内存里的参考数据（compounds.csv，以及 scoring.toml 里的 pubchem_min_share），
        不写数据库，也不写 PubChem 的缓存表。
    """
    # 故意查得很慢（三秒一次请求，全表大约十五分钟）。PubChem 会按 IP 限流，而且看的是一段时间里的总量：
    # 实测一秒半一次、连着查四百多次以后又被限流了
    pubchem = pubchem or PubChemClient(timeout=20, min_interval=3.0)
    compounds = ctx.reference.compounds.compounds
    known = ctx.reference.compounds.by_key
    # --only 的值拆成 key 的列表；没给就是空列表
    wanted = [key.strip() for key in (args.only or "").split(",") if key.strip()]
    # 先检查用户给的 key 表里都有。[*wanted, *(…)] 把 --only 的各个 key 和 --start-from 的那个 key 拼成一个列表
    for key in [*wanted, *([args.start_from] if args.start_from else [])]:
        if key not in known:
            raise SystemExit(f"data/compounds.csv 里没有 key 为 {key} 的物质。")
    if wanted:
        compounds = [known[key] for key in wanted]
    if args.start_from:
        keys = [compound.key for compound in compounds]
        # 从 start_from 那一项切到末尾。它不在这次要查的范围里时（比如同时给了 --only 又不包含它）就不切
        compounds = compounds[keys.index(args.start_from):] if args.start_from in keys else compounds
    # 企业申报的分类要占到全部申报的百分之多少才采用。和分析时用的是同一个阈值（scoring.toml 里现在是 30）
    min_share = float(ctx.reference.scoring.get("safety", {}).get("pubchem_min_share", 30))

    # 三个计数器：一致的、不一致的（“待填”也算在内）、PubChem 没有数据的
    same = different = no_data = 0
    for compound in compounds:
        try:
            # diff 是 None（PubChem 没有它的分类数据），或者一个字典，其中：
            #   pubchem  按上面的规则从 PubChem 取到的 H 编码
            #   missing  PubChem 有、表里没有的        extra  表里有、PubChem 没有的
            diff = compare_with_pubchem(compound, pubchem, min_share)
        except ExternalServiceError as exc:
            # 请求失败（多半是被限流了）：马上停下，不再往下查。SystemExit 里的文字会显示在命令行里
            raise SystemExit(
                f"查到“{compound.display_name}”时停下了：{exc}\n"
                f"已经查完的：一致 {same} 个，不一致 {different} 个，PubChem 没有数据 {no_data} 个。\n"
                f"过一阵再接着查：python -m syntherisk.cli check-hazards --start-from {compound.key}"
            ) from exc
        label = f"{compound.display_name}（{compound.key}）"
        if diff is None:
            no_data += 1
            # 把表里的写法还原出来：None 是 “?”；空列表是 “空”（表示不属于危险品）；否则用分号连起来
            ours = "?" if compound.h_codes is None else ";".join(compound.h_codes) or "空"
            _print(f"没有数据  {label}：PubChem 上没有它的分类，表里写的是 {ours}")
        elif compound.h_codes is None:
            different += 1
            _print(f"待填      {label}：表里写的是 ?，PubChem 是 {';'.join(diff['pubchem']) or '不属于危险品'}")
        elif diff["missing"] or diff["extra"]:
            different += 1
            # parts 最多两段：“表里没有 …”“表里多出 …”，哪一种有就写哪一种
            parts = [f"表里没有 {'、'.join(diff['missing'])}"] if diff["missing"] else []
            parts += [f"表里多出 {'、'.join(diff['extra'])}"] if diff["extra"] else []
            _print(f"不一致    {label}：{'；'.join(parts)}。PubChem 是 {';'.join(diff['pubchem']) or '不属于危险品'}")
        else:
            same += 1
    _print(f"查了 {len(compounds)} 个：一致 {same} 个，不一致 {different} 个，PubChem 没有数据 {no_data} 个。")
    _print("不一致不一定是表错了：PubChem 上的分类有时对应的是另一种形态（比如氯化氢气体和盐酸、镍粉和雷尼镍）。")


def main(argv: list[str] | None = None) -> None:
    """命令行入口：解析参数，建运行时上下文，调对应的 cmd_ 函数。

    参数：
        argv  命令行参数的列表，比如 ["load-precedents", "--source", "uspto", "--file", "x.rsmi"]。
              None（正常运行时）表示用真正的命令行参数；测试里直接传列表。
    做法：
        用的是 argparse 的“子命令”写法：每条命令用 add_parser 建一个子解析器，登记它自己的选项，
        再用 set_defaults(run=函数) 把处理函数挂上去。解析完以后，args.run 就是要调的那个函数，
        各个选项的值在 args 的属性上（--start-from 对应 args.start_from，横线变成下划线）。
    抛出：
        SystemExit  参数不对（argparse 打印用法后退出）；命令里主动退出；
                    导入数据时的 LoaderError 也在这里换成 SystemExit，只显示一句说明，不显示一屏堆栈。
    副作用：
        build_context() 会读配置、连数据库；数据库是空的时候还会建表、写入内置的在售试剂。
    """
    parser = argparse.ArgumentParser(prog="python -m syntherisk.cli", description="Syntherisk 后端的命令行工具")
    # dest="command"：选中的子命令的名字存到 args.command；required=True：不给子命令就报错并打印用法
    commands = parser.add_subparsers(dest="command", required=True)

    # 没有选项的命令：建子解析器的同时挂上处理函数
    commands.add_parser("status", help="各模块接的是什么、库里有多少数据").set_defaults(run=cmd_status)
    commands.add_parser("seed-demo", help="建演示课题组和账号").set_defaults(run=cmd_seed_demo)

    # required=True：必须给；type=int：把参数转成整数；
    # action="store_true"：这个选项不带值，写了就是 True，不写就是 False
    precedents = commands.add_parser("load-precedents", help="导入反应先例（USPTO .rsmi、CSV，或每行 DOI|反应 SMILES 的文件）")
    precedents.add_argument("--source", required=True,
                            help="data/data_sources.toml 里登记的来源编号，比如 uspto、academic13k")
    precedents.add_argument("--file", required=True)
    precedents.add_argument("--limit", type=int, default=None, help="只导入前 N 行，试跑时用")
    precedents.add_argument("--replace", action="store_true", help="先删掉这个来源原有的数据")
    precedents.add_argument("--workers", type=int, default=0, help="用几个进程算指纹；不写就用一半的 CPU 核")
    precedents.set_defaults(run=cmd_load_precedents)

    catalog = commands.add_parser("load-catalog", help="导入在售目录（每行一个 InChIKey，或带 smiles 列的 CSV）")
    catalog.add_argument("--source", required=True, help="比如 zinc")
    catalog.add_argument("--file", required=True)
    catalog.set_defaults(run=cmd_load_catalog)

    literature = commands.add_parser("fill-literature", help="按 DOI 给先例补年份和题目（联网查 Crossref / OpenAlex）")
    literature.add_argument("--source", default=None, help="只补这个来源的先例，比如 academic13k")
    literature.add_argument("--limit", type=int, default=None, help="最多查多少个 DOI，试跑时用")
    literature.set_defaults(run=cmd_fill_literature)

    hazards = commands.add_parser("check-hazards", help="把内置试剂表的危害分类和 PubChem 对一遍（联网，全表约十五分钟）")
    hazards.add_argument("--only", default=None, help="只查这几个，用逗号分开，写 compounds.csv 里的 key")
    hazards.add_argument("--start-from", default=None, help="从这个 key 开始接着查（上次中途停下时用）")
    hazards.set_defaults(run=cmd_check_hazards)

    commands.add_parser("calibrate", help="用已授权的实验回填做一次校准统计").set_defaults(run=cmd_calibrate)
    export = commands.add_parser("export-consented", help="导出已授权的匿名回填数据")
    export.add_argument("--output", required=True)
    export.set_defaults(run=cmd_export_consented)

    args = parser.parse_args(argv)
    try:
        # 每条命令都是先建上下文，再把 (ctx, args) 交给处理函数
        args.run(build_context(), args)
    except loaders.LoaderError as exc:
        raise SystemExit(str(exc)) from exc


# python -m syntherisk.cli … 时走这里
if __name__ == "__main__":
    main()
