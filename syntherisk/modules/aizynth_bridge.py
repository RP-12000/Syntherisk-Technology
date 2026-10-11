"""AiZynthFinder 桥接脚本。

后端每次搜索都另起一个进程来跑这个脚本，而不是在自己的进程里调用 AiZynthFinder：
一次搜索要占 3 GB 多内存（主要是 1700 万条在售清单），跑完随进程一起还给系统；取消分析时也能直接结束它。

    <装了 aizynthfinder 的 python> aizynth_bridge.py --request request.json --output result.json

现在 AiZynthFinder 和后端装在同一个环境里（Python 3.12、NumPy 低于 2），所以用的就是后端自己的 Python。

这个文件只用标准库和 aizynthfinder，不依赖后端的其他代码，可以单独拿出去运行。
写法对照 AiZynthFinder 4.4.1 的源码核对过。

request.json 的字段：
    config              AiZynthFinder 的 config.yml 路径
    target_smiles       目标分子
    stock               在售清单的名字（config.yml 里定义的，公开版是 zinc）
    expansion_policy    单步模型的名字（公开版是 uspto）
    filter_policy       过滤模型的名字，可以为空
    max_steps           最多几步
    iteration_limit     最多搜几轮
    time_limit          最长搜多少秒
    stock_mode          inventory_only 时只把客户库存当作终点
    inventory_inchikeys 客户库存的 InChIKey 列表：碰到这些就不再往下拆
    catalog_inchikeys   我们自己的在售试剂表的 InChIKey 列表。公开的 ZINC 清单里没有碘甲烷这类小分子试剂，
                        不补上的话，本来能走通的路线会被当成“原料买不到”
    excluded_smiles     用户排除的原料：不把它们当作可用的终点

对应 PPT 的 M3（逆合成搜索）。它是 m03_retrosynthesis.AiZynthEngine 背后真正干活的部分，有两种被调用的方式：
    另起进程（默认）  AiZynthEngine._run_subprocess 把输入写成 request.json，用 Python 运行本文件（走 main()），
                      等它结束以后读回 result.json
    进程内调用        AiZynthEngine.search 直接 import 本文件、调 run()
两种方式做的事完全一样，都在 run() 里。

几个词：
    在售清单（stock）  一批“买得到”的分子的 InChIKey。搜索把目标分子一层层往回拆，拆出来的分子在清单里就不再往下拆
    终点              上面说的“不再往下拆”的分子，也就是路线的起始原料
    单步模型（expansion policy）  给一个分子提出若干种“它可以由哪些前体一步做出来”的神经网络
    过滤模型（filter policy）     给单步模型提出的拆法把一道关，去掉看着不可行的

result.json 的字段（也就是 run() 的返回值）：
    engine    固定是 "aizynthfinder"
    version   装着的 aizynthfinder 包的版本号；查不到时是 null
    stats     AiZynthFinder 给的搜索统计，原样带回（里面有 number_of_nodes、search_time 等）
    routes    路线的列表，每条是
              {"tree": 路线树, "score": 引擎给这条路线的分数, "solved": 这条路线的原料是不是都在选中的清单里}
              路线树的格式见 m03_retrosynthesis.py 文件头
    warnings  给用户看的提示，中文句子的列表：哪份清单没加上、哪个过滤模型没加载、哪个原料没能排除

副作用：加载模型和在售清单（3 GB 多内存）；给客户库存、内置在售试剂表各写一个临时的 .txt 文件，搜索完删掉；
        main() 另外读 request.json、写 result.json。不读写数据库。

本文件里有什么：
    _set_search_option  设一个搜索参数（新旧版本的 AiZynthFinder 放的位置不一样）
    _json_safe          把结果里 JSON 存不了的值（numpy 的数字、NaN、元组、集合……）换成存得了的
    _version            装着的 aizynthfinder 的版本号
    run                 做一次搜索：输入是 request 字典，输出是上面说的结果字典
    main                命令行入口：读 request.json → run() → 写 result.json
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from typing import Any


def _set_search_option(finder, name: str, value: Any) -> None:
    """4.x 的搜索参数在 config.search 下面，更早的版本直接挂在 config 上。

    参数：finder  AiZynthFinder 对象；name  参数名（iteration_limit、time_limit、max_transforms）；value  要设的值。
    没有返回值，直接改 finder 的配置。
    谁在用：run()。
    """
    # getattr(对象, "search", 默认值)：有 search 这个属性就取它，没有就用后面的 finder.config 本身
    target = getattr(finder.config, "search", finder.config)
    # setattr(对象, 名字, 值) 相当于 对象.名字 = 值，只是名字可以是变量
    setattr(target, name, value)


def _json_safe(value: Any) -> Any:
    """把一个值里所有 JSON 存不了的东西换成存得了的，一层层递归处理。

    AiZynthFinder 返回的统计数字和路线树里可能夹着 numpy 的数字、集合这类值，json.dump 遇到会报错；
    NaN 和无穷大它虽然写得出来，却不是合规的 JSON。所以结果在返回之前先统一换一遍：
        字典            键都转成字符串，值递归处理
        列表、元组、集合  都变成列表，元素递归处理
        字符串、整数、布尔值、None  原样
        小数            正常的原样；NaN、正负无穷换成 None（标准 JSON 里没有这几个值）
        numpy 的标量    用 .item() 取出普通的 Python 数字，再处理一遍
        其他            转成字符串
    例（tests/test_integrations.py）：
        {"a": (1, 2.5), "b": NaN, 3: {"x"}}  →  {"a": [1, 2.5], "b": None, "3": ["x"]}
    谁在用：run()（返回之前把整个结果过一遍）。
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        # value == value 只有 NaN 不成立（NaN 和自己不相等）；abs(value) != inf 排除正负无穷
        return value if value == value and abs(value) != float("inf") else None
    if hasattr(value, "item"):  # numpy 标量
        return _json_safe(value.item())
    return str(value)


def _version() -> str | None:
    """装着的 aizynthfinder 包的版本号，比如 "4.4.1"；查不到这个包的安装信息时返回 None。

    importlib.metadata 读的是包安装时留下的信息，不用导入包本身。
    谁在用：run()（写进结果的 version，最后存进 Analysis.result 的 engine.version）。
    """
    from importlib import metadata

    try:
        return metadata.version("aizynthfinder")
    except metadata.PackageNotFoundError:
        return None


def run(request: dict[str, Any]) -> dict[str, Any]:
    """做一次 AiZynthFinder 搜索。

    参数：request  一个字典，各个键见文件头 “request.json 的字段”。
    返回：结果字典，各个键见文件头 “result.json 的字段”；已经用 _json_safe 处理过，可以直接写成 JSON。
    步骤：
        1. 读 config.yml，加载模型和在售清单（AiZynthFinder(configfile=…)）。
        2. 把客户库存、内置在售试剂表各加成一份额外的清单，决定搜索时哪几份清单算“终点”：
               仅用库存（stock_mode 是 inventory_only）而且库存不是空的   只有客户库存
               其余情况                                                 引擎自己的清单 + 内置在售试剂表 + 客户库存
        3. 选单步模型和过滤模型，设搜索参数：最多几轮、最长多少秒、最多几步。
        4. 设目标分子，建搜索树，加上用户排除的原料，搜索，整理出路线和统计数字。
        5. 删掉第 2 步写的临时文件（不管前面成没成功）。
        6. 每条路线取出分数和“是否拆到了底”，连同版本号、统计数字、提示一起返回。
    会抛：config.yml 读不了、单步模型或清单的名字不对、目标分子不合法、搜索本身出错，这些异常都不在这里接。
          另起进程时它们让脚本以非 0 退出码结束，AiZynthEngine._run_subprocess 从日志里取报错；
          进程内调用时由 AiZynthEngine.search 包成 EngineError。
          加清单、加载过滤模型、排除某个原料失败不算错：记一条 warnings，搜索照常进行。
    副作用：加载模型和清单要占 3 GB 多内存；在系统临时目录里写最多两个 .txt 文件，结束时删掉。
    谁在用：main()（另起进程的方式）；m03_retrosynthesis.AiZynthEngine.search（进程内调用的方式）；
            tests/test_integrations.py 用一个假的 AiZynthFinder 测它。
    """
    # 在函数里面才 import：这样没装 aizynthfinder 的环境也能 import 本文件（比如只为了用 _json_safe）
    from aizynthfinder.aizynthfinder import AiZynthFinder

    warnings: list[str] = []
    # 第 1 步。按配置文件建搜索器；config.yml 里写的模型和在售清单在这一步加载
    finder = AiZynthFinder(configfile=request["config"])
    # 下面写的临时文件的路径，最后统一删
    temp_files: list[str] = []

    def add_stock(name: str, label: str, keys: list[str]) -> bool:
        """把一组 InChIKey 加成一个额外的“在售清单”。加不上也要让搜索继续。

        参数：name  这份清单在 AiZynthFinder 里的名字（"inventory" 或 "catalog"），后面 select 时用；
              label  出问题时写进提示里的中文名；keys  InChIKey 的列表，可以是 None 或空的。
        返回：加上了是 True；keys 是空的、或者加的过程中出了错是 False（出错时往 warnings 里记一条）。
        做法：AiZynthFinder 的清单要从文件读，所以把这些 InChIKey 一行一个写进一个临时的 .txt 文件，
              再让它按“每行一个 InChIKey 的文本文件”读进来。
        """
        # 去掉空的；keys 是 None 时当成空列表
        keys = [key for key in keys or [] if key]
        if not keys:
            return False
        try:
            from aizynthfinder.context.stock import InMemoryInchiKeyQuery

            # delete=False：关闭以后文件还留在磁盘上，下面 AiZynthFinder 要按路径把它重新打开来读；
            # 文件名记进 temp_files，由 run() 最后的 finally 删掉
            handle = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii")
            handle.write("\n".join(keys))
            handle.close()
            temp_files.append(handle.name)
            finder.stock.load(InMemoryInchiKeyQuery(handle.name), name)
            return True
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label}没能加进搜索终点：{exc}")
            return False

    # 第 2 步。stocks：搜索时把哪几份清单里的分子当作终点
    has_inventory = add_stock("inventory", "客户库存", request.get("inventory_inchikeys"))
    if request.get("stock_mode") == "inventory_only" and has_inventory:
        stocks = ["inventory"]
    else:
        if request.get("stock_mode") == "inventory_only":
            warnings.append("选了“仅用库存”，但库存是空的，搜索时仍按在售目录判断终点")
        # 引擎自己的清单（config.yml 里定义的那份，默认叫 zinc）排第一，后面跟我们补的两份
        stocks = [request.get("stock") or "zinc"]
        if add_stock("catalog", "内置在售试剂表", request.get("catalog_inchikeys")):
            stocks.append("catalog")
        if has_inventory:
            stocks.append("inventory")

    try:
        # 第 3 步。select 是“在已经加载的几个里选用哪个 / 哪几个”
        finder.stock.select(stocks)
        finder.expansion_policy.select(request.get("expansion_policy") or "uspto")
        # 过滤模型可以不用；指定了却加载不上时只记一条提示，不用它继续搜
        if request.get("filter_policy"):
            try:
                finder.filter_policy.select(request["filter_policy"])
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"过滤模型 {request['filter_policy']} 没有加载：{exc}")

        # 三个搜索参数：最多搜几轮、最长搜多少秒（哪个先到就停）、一条路线最多几步（AiZynthFinder 叫 max_transforms）。
        # request 里没给时用的 100、120、6 和 AiZynthFinder 自己的默认值相同
        _set_search_option(finder, "iteration_limit", int(request.get("iteration_limit", 100)))
        _set_search_option(finder, "time_limit", int(request.get("time_limit", 120)))
        _set_search_option(finder, "max_transforms", int(request.get("max_steps", 6)))

        # 第 4 步
        finder.target_smiles = request["target_smiles"]
        # prepare_tree() 会清空排除名单（然后，目标分子本身在在售清单里的话，把它排除掉，
        # 免得“直接买目标分子”也算一条路线），
        # 所以用户排除的原料要在它之后加；后面的 tree_search() 看到树已经建好，不会再清一次
        finder.prepare_tree()
        if request.get("excluded_smiles"):
            from aizynthfinder.chem import Molecule

            # stock.exclude：把这个分子记进排除名单。之后搜索问“它在不在清单里”，一律答不在，
            # 也就是不把它当成买得到的原料（终点）
            for smiles in request["excluded_smiles"]:
                try:
                    finder.stock.exclude(Molecule(smiles=smiles))
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"没能排除 {smiles}：{exc}")
        # tree_search 做真正的搜索（最花时间的一步）；build_routes 从搜索树里挑出若干条路线；
        # extract_statistics 给出这次搜索的统计数字（一个字典）
        finder.tree_search()
        finder.build_routes()
        stats = finder.extract_statistics()

        # 把路线取成字典。include_scores、include_metadata 让每棵路线树的根节点上多带两项：
        # "scores"（{打分方法的名字: 分数}）和 "metadata"（里面有 is_solved）
        try:
            raw_routes = finder.routes.dict_with_extra(include_metadata=True, include_scores=True)
        except Exception:  # noqa: BLE001 - 老版本没有这个方法
            raw_routes = list(finder.routes.dicts)
    finally:
        # 第 5 步。finally 里的代码不管上面是正常做完还是中途抛了异常都会执行
        for path in temp_files:
            if os.path.exists(path):
                os.unlink(path)

    # 第 6 步
    routes = []
    for tree in raw_routes:
        scores = tree.get("scores") or {}
        routes.append(
            {
                "tree": tree,
                # 取 scores 里的第一个分数；没有分数（比如走了上面老版本的那条路）时是 None。
                # next(iter(字典.values()), None)：取第一个值，一个都没有就给 None
                "score": next(iter(scores.values()), None),
                # 原料是不是都在选中的清单里；拿不到这一项时是 None
                "solved": (tree.get("metadata") or {}).get("is_solved"),
            }
        )
    return _json_safe(
        {
            "engine": "aizynthfinder",
            "version": _version(),
            "stats": stats,
            "routes": routes,
            "warnings": warnings,
        }
    )


def main() -> None:
    """命令行入口：读 request.json，调 run()，把结果写成 result.json。

    用法（m03_retrosynthesis.AiZynthEngine._run_subprocess 就是这样起子进程的）：
        python aizynth_bridge.py --request request.json --output result.json
    两个参数都必须给。run() 里出了没接住的异常时，脚本带着报错信息以非 0 的退出码结束，
    不会写出 result.json；调用方靠“退出码不是 0，或者没有结果文件”知道失败了。
    副作用：读一个文件、写一个文件；加上 run() 的那些。
    """
    parser = argparse.ArgumentParser(description="跑一次 AiZynthFinder 搜索，把路线写成 JSON")
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.request, encoding="utf-8") as handle:
        request = json.load(handle)
    result = run(request)
    # ensure_ascii=False：中文按原样写进文件，不转成 \uXXXX 这样的转义
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False)


# 只有把这个文件当脚本直接运行（python aizynth_bridge.py …）时才执行 main()；
# 被别的代码 import 时（进程内调用的方式、测试）不执行
if __name__ == "__main__":
    main()
