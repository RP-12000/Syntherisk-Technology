"""M3 逆合成搜索：给目标分子找候选路线。

PPT 的选型是 AstraZeneca 开源的 AiZynthFinder（单步模型 + 树搜索）。这里有三种引擎：

    aizynth_bridge     每次搜索另起一个进程去跑 AiZynthFinder（推荐）。它和后端装在同一个环境里，
                       用的就是后端自己的 Python；装在别的环境里时用 AIZYNTH_PYTHON 指过去
    aizynth_inprocess  在后端进程里直接调用（搜索占的 3 GB 多内存不会还回去，一般不用）
    demo               内置的演示路线，只认 data/demo_routes 里的几个分子（布洛芬、阿司匹林）

M3_ENGINE=auto 时：设了 AIZYNTH_CONFIG、并且找得到 aizynthfinder，就用 aizynth_bridge，否则退回 demo。
不管哪种引擎，返回的路线树都是 AiZynthFinder 的格式，后面的模块不用区分。

对应 PPT 的 M3，是“开始分析”（A16）之后八个阶段里的第 2 阶段，也是最慢的一步。
界面上和它有关的还有：A18 先看初步结果（看的就是这一阶段的产出）、A19 取消（要能把正在跑的搜索停下来）、
B11 把路线全排除以后的重新搜索。

逆合成搜索是什么：从目标分子出发往回拆。“单步模型”对一个分子提出若干种拆法——它可以由哪几个前体一步做出来；
“树搜索”把这些拆法一层层接下去，直到每一支都拆到买得到的分子（在售清单或库存里有的）为止。
从目标分子一直通到原料的一套完整拆法，就是一条路线。

谁调用它：
    context.build_context                服务启动时调 build_engine(settings) 建好一个引擎，放在 AppContext.retro_engine 上
    pipeline.AnalysisRunner._search      第 2 阶段：组一个 SearchRequest，调引擎的 search()，结果交给 routes.build_skeleton
    context.AppContext.describe_modules  调引擎的 describe()，给 GET /api/meta/modules 和命令行的 status 用
它依赖：config.Settings（M3_ENGINE 和 AIZYNTH_ 开头的那几项配置）、m01_structure（演示引擎认目标分子）、
        aizynth_bridge.py（真正调用 AiZynthFinder 的脚本，见那个文件的说明）。

数据从哪来到哪去：
    进来  SearchRequest：目标分子、步数上限、搜几轮、最长多少秒、原料来源，以及库存、在售试剂表、要排除的原料
    出去  SearchResult：若干条路线树、统计数字、提示
    这个文件不读写数据库。演示引擎启动时读 data/demo_routes/*.json。
    AiZynthFinder 引擎默认另起一个进程，并在系统的临时目录里放三个临时文件（请求、结果、日志），搜索结束就删掉。

路线树长什么样（AiZynthFinder 的输出格式；演示路线也照这个格式写）：分子节点和反应节点一层套一层
    {"type": "mol", "smiles": "目标分子", "in_stock": false, "children": [
        {"type": "reaction", "metadata": {…}, "children": [
            {"type": "mol", "smiles": "前体甲", "in_stock": true},
            {"type": "mol", "smiles": "前体乙", "in_stock": false, "children": [ {再往下的一步反应} ]}
        ]}
    ]}
    没有 children 的分子节点是这条路线的起始原料。in_stock 是引擎按它自己的在售清单判断的“买不买得到”。
    反应节点的 metadata：AiZynthFinder 给 mapped_reaction_smiles（带原子映射的这一步，写成 产物>>反应物）、
    policy_probability（单步模型给这种拆法的概率）、template_hash、classification 等；
    演示路线给 name（反应名）、reagents（试剂）、conditions（条件说明）。
    读这棵树的是 routes._Builder，这里只负责把它原样带回去。

本文件里有什么：
    BRIDGE_SCRIPT          桥接脚本 aizynth_bridge.py 的位置
    EngineError            引擎配置不对，或者搜索失败
    SearchCancelled        搜索中途被用户取消
    SearchRequest          一次搜索的输入
    SearchResult           一次搜索的输出
    RetrosynthesisEngine   一个“引擎”必须有的方法（协议）
    DemoEngine             演示引擎
    AiZynthEngine          AiZynthFinder 引擎：另起进程，或者在本进程里调用
    _BRIDGE_WARNINGS、_bridge_warning         给桥接脚本的提示配英文
    _from_project_root、_aizynth_importable   两个小工具
    build_engine           按配置选一种引擎
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from syntherisk import i18n
from syntherisk.config import PROJECT_ROOT, Settings
from syntherisk.modules import m01_structure as chem
from syntherisk.reference import register_english

# 桥接脚本的完整路径：和本文件在同一个文件夹里的 aizynth_bridge.py。
# Path(__file__) 是本文件的路径，with_name 把最后的文件名换掉。子进程就是用 Python 去运行这个脚本
BRIDGE_SCRIPT = Path(__file__).with_name("aizynth_bridge.py")


class EngineError(RuntimeError):
    """逆合成引擎用不了，或者搜索失败。消息是给人看的中文，会写明是哪一项配置或者哪一步出了问题。
    消息都是用 i18n.tr() 拼的，所以每一句同时记下了英文（请求要英文时由 i18n.localize() 整句换掉）。

    什么时候抛：AiZynthFinder 的配置文件或 Python 解释器找不到（AiZynthEngine._check_files）；
               子进程起不来、异常退出、超时，或者进程内调用出错（AiZynthEngine.search / _run_subprocess）；
               M3_ENGINE 指定了 AiZynthFinder、配置却不全（build_engine）。
    谁来接：AiZynthEngine.describe 接住，变成返回值里的 problem；
            流水线不专门接它，pipeline.run_analysis 兜底的 except 把它记成这次分析的失败原因（Analysis.error）；
            build_engine 是在服务启动时调的，它抛出来的话服务启动失败。
    """

    pass


class SearchCancelled(RuntimeError):
    """搜索中途被用户取消（A19）。

    AiZynthEngine._run_subprocess 等子进程的时候，每半秒问一次 should_cancel()；
    问到“要取消”，就结束子进程并抛这个异常。pipeline.AnalysisRunner._search 接住它，换成 AnalysisCancelled。
    """

    pass


@dataclass
class SearchRequest:
    """一次逆合成搜索的输入。由 pipeline.AnalysisRunner._search 按约束和库存组出来，交给引擎的 search()。

    演示引擎只看 target_smiles；其余各项是给 AiZynthFinder 的（经 AiZynthEngine._payload 写进 request.json）。
    """

    # 目标分子的 SMILES
    target_smiles: str
    # 路线最多几步。流水线传的是合并约束以后的步数上限（EffectiveConstraints.max_steps）
    max_steps: int = 6
    # 最多搜几轮。流水线传的是配置 AIZYNTH_ITERATION_LIMIT（默认 400）；
    # 同一个分析再跑一遍时（比如 B11 把路线全排除以后重新搜索），第 2 遍传 2 倍、第 3 遍传 3 倍
    iteration_limit: int = 100
    # 最长搜多少秒，到时间就停。流水线传的是配置 AIZYNTH_TIME_LIMIT（默认 120）
    time_limit: int = 120
    # 原料来源：inventory_only / inventory_first / any（Constraints.stock_source）。
    # 桥接脚本只区分是不是 inventory_only：是的话（而且库存不是空的），搜索时只有客户库存里的分子算“到手了、不用再拆”
    stock_mode: str = "inventory_first"
    # 客户库存里各个物质的完整 InChIKey。搜索碰到这些分子就不再往下拆
    inventory_inchikeys: list[str] = field(default_factory=list)
    catalog_inchikeys: list[str] = field(default_factory=list)  # 我们自己的在售试剂表，补引擎清单里没有的小分子试剂
    # 用户在结果页排除的原料（B11）的 SMILES：搜索时不把它们当成可用的原料
    excluded_smiles: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    """一次逆合成搜索的输出。三种引擎返回的都是它，routes.build_skeleton 接着把它整理成后端自己的结构。"""

    # 哪个引擎给的："demo" 或 "aizynthfinder"。存进 Analysis.engine，流水线也按它决定进度消息怎么写
    engine: str
    # 引擎的版本号（AiZynthFinder 的包版本，比如 "4.4.1"）；演示引擎是 None
    version: str | None
    # 每条里还可能有 "solved"：引擎认为这条路线的原料是不是都买得到。演示引擎的 score 是 None，没有 solved
    routes: list[dict[str, Any]]  # 每条：{"tree": AiZynthFinder 格式的路线树, "score": ..., "name": ...}
    # 搜索的统计数字。AiZynthFinder 给的里面有 number_of_nodes（探索了多少个节点，进度消息里用）；
    # 演示引擎给的是 {"number_of_routes": 条数, "demo": True}
    stats: dict[str, Any] = field(default_factory=dict)
    # 给用户看的提示（中文句子）。routes.build_skeleton 把它们并进 Analysis.result 的 warnings
    warnings: list[str] = field(default_factory=list)


class RetrosynthesisEngine(Protocol):
    """一个“逆合成引擎”必须有的东西。DemoEngine、AiZynthEngine 都符合；测试里的替身引擎也照这个写。

    Protocol 是“协议”：只规定要有哪些属性和方法，不要求继承它。任何对象只要有 name、search、describe，
    就可以当引擎用（放进 AppContext.retro_engine）。下面两个方法只有签名、没有实现（函数体是 ...）。

        name       引擎的名字，"demo" 或 "aizynthfinder"
        search()   做一次搜索。
                   参数 request 是 SearchRequest；should_cancel 是一个不带参数的函数，返回 True 表示用户要取消，
                   搜索时间长的引擎应当隔一会儿问它一次（流水线传的是 AnalysisRunner._cancelled，它去查数据库）。
                   返回 SearchResult。找不到路线不算错，返回 routes 为空的结果、原因写在 warnings 里。
                   搜索本身失败抛 EngineError，被取消抛 SearchCancelled。
        describe() 说明这个引擎现在接的是什么、能不能用，返回一个字典，至少有 "engine" 一项。
                   GET /api/meta/modules 和命令行的 status 把它原样显示在 M3 一栏。
    """

    name: str

    # 做一次搜索，说明见上面类的文档
    def search(self, request: SearchRequest, should_cancel: Callable[[], bool] | None = None) -> SearchResult: ...

    # 说明引擎的状态，说明见上面类的文档
    def describe(self) -> dict[str, Any]: ...


class DemoEngine:
    """演示引擎：不做搜索，只把 data/demo_routes/ 里事先写好的路线原样拿出来。

    没有配置 AiZynthFinder（或者 M3_ENGINE=demo）时用它，测试也用它。现在只有布洛芬、阿司匹林两个分子的路线。
    每个 JSON 文件是一个目标分子：
        {"target": {"smiles": …, "name": …}, "source_note": 一句来源说明,
         "routes": [{"name": 路线名, "tree": 路线树}, …]}
    文件里的中文（分子名、路线名、反应名、条件说明、来源说明）旁边都配了英文，键名多一个 _en 后缀
    （name_en、conditions_en、source_note_en）。读文件时把它们登记进中英对照（i18n.py）并从数据里拿掉，
    所以交出去的路线树里没有这些键，和加英文之前一样。
    """

    # 引擎的名字。存进 Analysis.engine；流水线和证据卡看到 "demo" 会注明这是演示数据
    name = "demo"

    def __init__(self, routes_dir: Path):
        """把 routes_dir 下所有的 *.json 读进内存，按目标分子的 key14 建索引。

        参数：routes_dir  放演示路线的文件夹（build_engine 传的是 data/demo_routes）。
        副作用：读这个文件夹里的文件。
        哪个文件里目标分子的 SMILES 读不出来，就抛 ValueError。引擎是服务启动时建的，所以这种错在启动时就会暴露。
        """
        # {目标分子的 key14: 这个文件的全部内容}
        self._targets: dict[str, dict[str, Any]] = {}
        # sorted：按文件名排序，保证每次读的先后一样
        for path in sorted(routes_dir.glob("*.json")):
            # register_english：登记文件里配的英文（xxx_en）并把这些键拿掉，见 reference.py
            data = register_english(json.loads(path.read_text(encoding="utf-8")))
            key = chem.key14_of(data["target"]["smiles"])
            if key is None:
                raise ValueError(f"{path.name}：目标分子的 SMILES 读不出来")
            self._targets[key] = data

    @property
    def supported(self) -> list[str]:
        """有演示路线的那几个分子的名字（文件里 target.name），比如 ["阿司匹林", "布洛芬"]。

        谁在用：search()（找不到路线时把它们列在提示里）、describe()。
        """
        return [data["target"]["name"] for data in self._targets.values()]

    def search(self, request: SearchRequest, should_cancel=None) -> SearchResult:
        """按目标分子取出事先写好的路线。

        参数：request 里只用 target_smiles；should_cancel 用不到（瞬间就返回）。
        返回：SearchResult（engine="demo"，version=None）。
            目标分子有演示路线  routes 里每条是 {"tree": 路线树, "name": 路线名, "score": None}，
                                warnings 里是文件的 source_note（说明这是演示数据）
            没有                routes 是空的，warnings 里说明演示引擎只有哪几个分子的路线
        目标分子按 key14 认，所以写法不同、立体构型不同的同一个分子都认得出来。不抛异常。
        谁在用：pipeline.AnalysisRunner._search（通过 AppContext.retro_engine）；tests/test_reference.py。
        """
        # key14_of 读不出 SMILES 时给 None，换成空字符串去查，查不到就是 None
        data = self._targets.get(chem.key14_of(request.target_smiles) or "")
        if data is None:
            # i18n.join：中文用顿号连，英文那一句用逗号、并且用各个分子名的英文
            names = i18n.join(self.supported)
            return SearchResult(
                engine=self.name,
                version=None,
                routes=[],
                warnings=[i18n.tr("还没有接入 AiZynthFinder；内置演示引擎只有这几个分子的路线：{names}",
                                  "AiZynthFinder is not connected yet; the built-in demo engine only has routes for: {names}",
                                  names=names)],
            )
        # 演示路线没有引擎打的分，score 填 None；路线名用文件里写的（比如“格氏羧基化”）
        routes = [{"tree": r["tree"], "name": r["name"], "score": None} for r in data["routes"]]
        return SearchResult(
            engine=self.name,
            version=None,
            routes=routes,
            stats={"number_of_routes": len(routes), "demo": True},
            warnings=[data.get("source_note") or i18n.tr("演示数据", "Demo data")],
        )

    def describe(self) -> dict[str, Any]:
        """引擎的现状。返回 {"engine": "demo", "ready": True, "supported_targets": [分子名…], "note": 一句说明}。

        谁在用：context.AppContext.describe_modules（GET /api/meta/modules 和命令行 status 的 M3 一栏）。
        """
        return {"engine": self.name, "ready": True, "supported_targets": self.supported,
                "note": i18n.tr("演示引擎：只返回内置的教科书路线，不做真正的逆合成搜索",
                                "Demo engine: returns built-in textbook routes only; no real retrosynthesis search")}


class AiZynthEngine:
    """in_process=False 时每次搜索另起一个进程，True 时在当前进程里直接调用。

    默认另起进程，用的就是后端自己的这个 Python（AiZynthFinder 和后端装在同一个环境里）：
    一次搜索要占 3 GB 多内存，跑完随进程一起还给系统；用户取消分析时也能直接把它结束掉。
    AiZynthFinder 装在别的环境里时，用 AIZYNTH_PYTHON 指过去。

    两种方式最后都落到 aizynth_bridge.run()：另起进程时是子进程运行那个脚本，进程内时是直接调那个函数。
    对象在服务启动时由 build_engine 建一次，之后每次分析都用它；它自己不加载模型，
    模型和在售清单是每次搜索时在 aizynth_bridge.run 里加载的（实测加载加搜索一共 25–60 秒，见 README）。
    """

    # 引擎的名字。存进 Analysis.engine；证据卡的路线来源按它显示“AiZynthFinder”
    name = "aizynthfinder"

    def __init__(self, settings: Settings, *, in_process: bool):
        """参数：
            settings    运行配置。用到 AIZYNTH_CONFIG、AIZYNTH_PYTHON、AIZYNTH_STOCK、
                        AIZYNTH_EXPANSION_POLICY、AIZYNTH_FILTER_POLICY 这几项
            in_process  False：每次搜索另起一个进程（aizynth_bridge 模式）；True：在当前进程里直接调用
        这里只记下路径，不检查文件在不在（检查在 _check_files 里，搜索前和 describe 时做）。
        """
        self.settings = settings
        self.in_process = in_process
        # .env 里可以写相对路径（相对项目根目录），这样不管从哪个目录启动都找得到
        self.config_path = str(_from_project_root(settings.aizynth_config))
        # AIZYNTH_PYTHON 留空时用 sys.executable：正在运行后端的这个 Python 解释器的完整路径
        self.python_path = str(_from_project_root(settings.aizynth_python)) if settings.aizynth_python else sys.executable

    def _check_files(self) -> None:
        """确认搜索要用的文件都在：AiZynthFinder 的配置文件；另起进程时还有那个 Python 解释器。

        缺哪个就抛 EngineError，消息里写明路径和对应的配置项名字。都在就什么也不返回。
        只看文件在不在，不检查配置文件的内容，也不检查那个 Python 里是不是真的装了 aizynthfinder。
        谁在用：search()（每次搜索之前）、describe()。
        """
        if not Path(self.config_path).is_file():
            raise EngineError(i18n.tr(
                "找不到 AiZynthFinder 的配置文件：{path}（AIZYNTH_CONFIG）。公开模型下载完才会有这个文件。",
                "AiZynthFinder config file not found: {path} (AIZYNTH_CONFIG). "
                "It only exists once the public models have been downloaded.",
                path=self.config_path))
        if not self.in_process and not Path(self.python_path).is_file():
            raise EngineError(i18n.tr("找不到装了 AiZynthFinder 的 Python：{path}（AIZYNTH_PYTHON）",
                                      "Python with AiZynthFinder installed not found: {path} (AIZYNTH_PYTHON)",
                                      path=self.python_path))

    def _payload(self, request: SearchRequest) -> dict[str, Any]:
        """把一次搜索的全部输入拼成一个字典，也就是交给 aizynth_bridge 的 request.json 的内容。

        config、stock、expansion_policy、filter_policy 四项来自配置（配置文件的路径，在售清单、单步模型、
        过滤模型的名字），其余来自 SearchRequest。每个键的含义见 aizynth_bridge.py 文件头的说明。
        谁在用：search()。
        """
        settings = self.settings
        return {
            "config": self.config_path,
            "target_smiles": request.target_smiles,
            "stock": settings.aizynth_stock,
            "expansion_policy": settings.aizynth_expansion_policy,
            "filter_policy": settings.aizynth_filter_policy,
            "max_steps": request.max_steps,
            "iteration_limit": request.iteration_limit,
            "time_limit": request.time_limit,
            "stock_mode": request.stock_mode,
            "inventory_inchikeys": request.inventory_inchikeys,
            "catalog_inchikeys": request.catalog_inchikeys,
            "excluded_smiles": request.excluded_smiles,
        }

    def search(self, request: SearchRequest, should_cancel=None) -> SearchResult:
        """用 AiZynthFinder 做一次逆合成搜索。

        参数：
            request        搜索的输入（SearchRequest）
            should_cancel  不带参数的函数，返回 True 表示用户要取消；可以是 None。
                           只有另起进程的方式会用它：等子进程的时候每半秒问一次。进程内调用时中途停不下来
        返回：SearchResult（engine="aizynthfinder"）。routes 里每条是
              {"tree": 路线树, "score": 引擎给这条路线的分数, "solved": 原料是不是都在引擎的在售清单里}。
              注意：引擎凑不够完整的路线时也会给出没拆到底的（solved 是 False），流水线照样往下评估。
        会抛：EngineError（文件找不到、子进程起不来或异常退出、超时、进程内调用出错）；SearchCancelled（被取消）。
        副作用：另起进程的方式会起一个子进程，占 3 GB 多内存，一般要 25–60 秒（README 里的实测）；
                在系统临时目录里写三个文件，用完删掉。不读写数据库。
                后端自己不为搜索发网络请求；模型和在售清单是 AiZynthFinder 的 config.yml 里指向的本地文件。
        谁在用：pipeline.AnalysisRunner._search（通过 AppContext.retro_engine）。
        """
        self._check_files()
        payload = self._payload(request)
        if self.in_process:
            # 到用的时候才 import：aizynth_bridge 本身只依赖标准库，真正的 aizynthfinder 在它的 run() 里才引入
            from syntherisk.modules import aizynth_bridge

            try:
                data = aizynth_bridge.run(payload)
            except Exception as exc:  # noqa: BLE001
                # 不管它里面出的是什么错，都包成 EngineError；“from exc” 把原来的错误留在后面，日志里看得到
                raise EngineError(i18n.tr("AiZynthFinder 搜索失败：{error}", "AiZynthFinder search failed: {error}",
                                          error=str(exc))) from exc
        else:
            data = self._run_subprocess(payload, request.time_limit, should_cancel)
        # data 是 aizynth_bridge.run() 返回（或者写进 result.json）的那个字典；缺哪一项就用空的顶上
        return SearchResult(
            engine=self.name,
            version=data.get("version"),
            routes=data.get("routes", []),
            stats=data.get("stats", {}),
            warnings=[_bridge_warning(text) for text in data.get("warnings", [])],
        )

    def _run_subprocess(self, payload: dict[str, Any], time_limit: int, should_cancel) -> dict[str, Any]:
        """另起一个进程运行 aizynth_bridge.py，等它跑完，把结果读回来。

        参数：
            payload        _payload() 拼好的字典，会写成 request.json 交给子进程
            time_limit     搜索的时间上限（秒）。等子进程的总时限是它再加 300 秒
            should_cancel  同 search()；None 表示不检查取消
        返回：子进程写在 result.json 里的字典（键：engine、version、stats、routes、warnings）。
        会抛：
            EngineError       解释器启动不了；等过了时限（先结束子进程）；
                              子进程的退出码不是 0，或者没有写出 result.json（消息里带上它日志的最后 1500 个字符）
            SearchCancelled   should_cancel() 返回了 True（先结束子进程）
        和子进程之间靠文件传数据。三个文件都放在一个临时文件夹里，函数返回（或者抛异常）时一起删掉：
            request.json  输入        result.json  输出        log.txt  子进程打印的全部内容
        谁在用：search()。
        """
        # TemporaryDirectory：在系统的临时目录里建一个文件夹，离开 with 时连同里面的文件一起删掉
        # （不管是正常返回还是抛异常）。prefix 是文件夹名的开头，方便认出来是谁建的
        with tempfile.TemporaryDirectory(prefix="syntherisk-aizynth-") as tmp:
            request_path, output_path, log_path = (Path(tmp) / n for n in ("request.json", "result.json", "log.txt"))
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            # 相当于在命令行里敲：python aizynth_bridge.py --request …/request.json --output …/result.json
            # 写成列表、不拼成一个字符串：路径里有空格也不会被拆开
            command = [self.python_path, str(BRIDGE_SCRIPT), "--request", str(request_path), "--output", str(output_path)]
            # 日志写文件而不是管道：AiZynthFinder 输出很多，管道写满会卡住
            with log_path.open("w", encoding="utf-8") as log:
                try:
                    # Popen 启动子进程后立刻返回，不等它结束。stdout=log：子进程打印的内容写进日志文件；
                    # stderr=subprocess.STDOUT：报错信息也并到同一个文件里
                    process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                except OSError as exc:
                    raise EngineError(i18n.tr("启动不了 AiZynthFinder 的 Python 解释器：{error}",
                                              "Could not start the Python interpreter for AiZynthFinder: {error}",
                                              error=str(exc))) from exc
                # time.monotonic() 是只增不减的计时器（不受改系统时间影响），用来算“最晚等到什么时候”
                deadline = time.monotonic() + time_limit + 300  # 加载模型本身就要几十秒
                # process.poll() 在子进程还没结束时返回 None。每半秒看一次：要不要取消、有没有超时
                while process.poll() is None:
                    if should_cancel and should_cancel():
                        # kill() 直接结束子进程，它占的内存随之还给系统
                        process.kill()
                        raise SearchCancelled()
                    if time.monotonic() > deadline:
                        process.kill()
                        raise EngineError(i18n.tr("AiZynthFinder 搜索超时", "AiZynthFinder search timed out"))
                    time.sleep(0.5)
            # 到这里子进程已经结束，日志文件也关上了。退出码 0 并且写出了结果文件才算成功
            if process.returncode != 0 or not output_path.exists():
                # errors="replace"：日志里有不是 UTF-8 的字节时换成占位符，不要因为读日志再报一次错
                log_text = log_path.read_text(encoding="utf-8", errors="replace")
                hint = ""
                # 认出最常见的一种失败（README“踩过的坑”里的版本冲突），在报错后面补一段怎么修
                if "pyarrow requires NumPy" in log_text:
                    hint = i18n.tr(
                        "\n这是环境里 pyarrow 和 NumPy 的版本冲突：pip 装了只认 NumPy 2 的新版 pyarrow，"
                        "而 AiZynthFinder 要求 NumPy 低于 2。把 pyarrow 换成 25.0.0（照 requirements.lock.txt 装就不会有这个问题），见 README。",
                        "\nThis is a pyarrow / NumPy version conflict in the environment: pip installed a newer pyarrow "
                        "that only supports NumPy 2, while AiZynthFinder requires NumPy below 2. Switch pyarrow to 25.0.0 "
                        "(installing from requirements.lock.txt avoids this); see the README.")
                # log_text[-1500:]：只带日志的最后 1500 个字符，真正的报错一般在最后。
                # hint 是上面那句（有英文对照）或者空的，当参数传进去，英文句子里用它的英文
                raise EngineError(i18n.tr("AiZynthFinder 搜索失败（退出码 {code}）：\n{log}{hint}",
                                          "AiZynthFinder search failed (exit code {code}):\n{log}{hint}",
                                          code=process.returncode, log=log_text[-1500:], hint=hint))
            # 成功：读回结果。读完以后离开最外层的 with，临时文件夹被删掉
            return json.loads(output_path.read_text(encoding="utf-8"))

    def describe(self) -> dict[str, Any]:
        """引擎的现状：配置的文件在不在、用的是哪种方式。不做搜索，也不加载模型。

        返回的字典：
            engine   "aizynthfinder"
            ready    True 表示配置文件（和另起进程用的 Python）都找得到
            problem  找不到时是 _check_files 给的那句说明；没问题是 None
            mode     "bridge"（每次另起进程）或 "in_process"（在后端进程里调用）
            config   AiZynthFinder 配置文件的完整路径
            python   另起进程用的 Python 解释器的路径；in_process 方式是 None
        ready 是 True 只说明文件在，不保证搜索一定跑得通（环境问题要到真的搜索时才暴露）。
        谁在用：context.AppContext.describe_modules（GET /api/meta/modules 和命令行 status 的 M3 一栏）。
        """
        try:
            self._check_files()
            problem = None
        except EngineError as exc:
            problem = str(exc)
        return {
            "engine": self.name,
            "ready": problem is None,
            "problem": problem,
            "mode": "in_process" if self.in_process else "bridge",
            "config": self.config_path,
            "python": None if self.in_process else self.python_path,
        }


# 桥接脚本（aizynth_bridge.py）给的提示是在另一个进程里拼好的中文句子，那边记不了英文。
# 这里按它的四种写法认出来，每种配一句英文模板；(.+) 抓的是句子里会变的部分（名称、出错原因）。
# 那个脚本里的提示改了写法的话，这里要跟着改；认不出来的提示原样留着（只有中文）。
_BRIDGE_WARNINGS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"^(.+)没能加进搜索终点：(.*)$", re.S),
     "{a}没能加进搜索终点：{b}", "{a} could not be added as search stock: {b}"),
    (re.compile(r"^过滤模型 (.+) 没有加载：(.*)$", re.S),
     "过滤模型 {a} 没有加载：{b}", "Filter model {a} was not loaded: {b}"),
    (re.compile(r"^没能排除 (.+)：(.*)$", re.S),
     "没能排除 {a}：{b}", "Could not exclude {a}: {b}"),
]
# 上面第一种提示里的两个名称，和不带参数的那一句提示
i18n.register("客户库存", "Customer inventory")
i18n.register("内置在售试剂表", "Built-in purchasable reagent table")
i18n.register("选了“仅用库存”，但库存是空的，搜索时仍按在售目录判断终点",
              "“Inventory only” was selected but the inventory is empty; the search still uses the purchasable catalog as stock")


def _bridge_warning(text: str) -> str:
    """给桥接脚本的一句提示配上英文：原样返回这句中文，同时把它的英文记进当前的对照表。

    参数：text  aizynth_bridge.run() 给的一句提示。
    返回：text 本身。认不出是哪一种提示时什么都不记。
    谁在用：AiZynthEngine.search()。
    """
    for pattern, chinese, english in _BRIDGE_WARNINGS:
        match = pattern.match(text)
        if match:
            # 用同一句中文模板重新拼一遍，得到的和 text 一字不差；tr() 顺便记下英文
            return i18n.tr(chinese, english, a=match.group(1), b=match.group(2))
    return text


def _from_project_root(path: str) -> Path:
    """把配置里写的路径变成确定的路径：绝对路径原样用，相对路径接在项目根目录后面。

    例：.env 里写 AIZYNTH_CONFIG=data/external/aizynth/config.yml，得到的是 <项目根目录>/data/external/aizynth/config.yml，
    这样不管服务是从哪个目录启动的都找得到。PROJECT_ROOT（项目根目录）定义在 config.py。
    谁在用：AiZynthEngine.__init__。
    """
    return Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path


def _aizynth_importable() -> bool:
    """后端自己的这个 Python 环境里装没装 aizynthfinder。

    find_spec 只是查这个包找不找得到，不会真的把它导入进来。
    谁在用：build_engine()；测试里会把它换成固定返回 True / False 的替身。
    """
    import importlib.util

    return importlib.util.find_spec("aizynthfinder") is not None


def build_engine(settings: Settings) -> RetrosynthesisEngine:
    """按配置选一种逆合成引擎。服务启动时调一次（context.build_context）。

    参数：settings  运行配置，看的是 M3_ENGINE、AIZYNTH_CONFIG、AIZYNTH_PYTHON 三项。
    返回：DemoEngine 或 AiZynthEngine。
    怎么选（M3_ENGINE 的四个取值）：
        demo               演示引擎
        aizynth_bridge     AiZynthFinder，每次搜索另起进程。要求设了 AIZYNTH_CONFIG，
                           并且设了 AIZYNTH_PYTHON 或者后端的环境里就装着 aizynthfinder；不满足就抛 EngineError
        aizynth_inprocess  AiZynthFinder，在后端进程里调用。要求设了 AIZYNTH_CONFIG，
                           并且后端的环境里装着 aizynthfinder；不满足就抛 EngineError
        auto（默认）       上面 aizynth_bridge 的要求满足就用它，否则退回演示引擎，不报错
    这里只看配置项填没填，不检查文件在不在：文件不在的话引擎照样建出来，
    describe() 里 ready 是 False，真去搜索时才抛 EngineError。
    会抛：EngineError（见上）。在启动时抛出来，服务就起不来。
    谁在用：context.build_context()；tests/test_integrations.py。
    """
    choice = settings.m3_engine
    # 在本进程里调用的条件：填了配置文件，并且这个环境里装着 aizynthfinder
    inprocess_ready = bool(settings.aizynth_config) and _aizynth_importable()
    # 另起进程用哪个 Python：AIZYNTH_PYTHON 指定的那个；没指定、而当前环境里就装着 aizynthfinder，就用后端自己的
    bridge_ready = bool(settings.aizynth_config) and bool(settings.aizynth_python or _aizynth_importable())

    # 明确指定了 aizynth_bridge，或者 auto 而条件满足。明确指定而条件不满足的，在里面报错
    if choice == "aizynth_bridge" or (choice == "auto" and bridge_ready):
        if not bridge_ready:
            raise EngineError(i18n.tr(
                "M3_ENGINE=aizynth_bridge 需要设置 AIZYNTH_CONFIG；"
                "aizynthfinder 没装在后端这个环境里的话，还要用 AIZYNTH_PYTHON 指到装了它的那个 Python",
                "M3_ENGINE=aizynth_bridge requires AIZYNTH_CONFIG; if aizynthfinder is not installed in the backend "
                "environment, also point AIZYNTH_PYTHON to the Python that has it",
            ))
        return AiZynthEngine(settings, in_process=False)
    # 明确指定了 aizynth_inprocess，同样是条件不满足就报错。
    # （auto 走不到这一支：inprocess_ready 成立时 bridge_ready 一定也成立，上面已经返回了）
    if choice == "aizynth_inprocess" or (choice == "auto" and inprocess_ready):
        if not inprocess_ready:
            raise EngineError(i18n.tr(
                "M3_ENGINE=aizynth_inprocess 需要当前环境装了 aizynthfinder，并设置 AIZYNTH_CONFIG",
                "M3_ENGINE=aizynth_inprocess requires aizynthfinder installed in the current environment and AIZYNTH_CONFIG set",
            ))
        return AiZynthEngine(settings, in_process=True)
    # 其余情况：M3_ENGINE=demo，或者 auto 而 AiZynthFinder 没配好。settings.data_dir 默认是项目下的 data/
    return DemoEngine(settings.data_dir / "demo_routes")
