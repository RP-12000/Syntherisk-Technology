"""运行时上下文：配置、数据库、参考数据、外部客户端、各个引擎，集中在一个对象里。

API 进程和 Worker 进程各自调用 build_context() 建一份。测试里可以把外部依赖换成假的。

这个文件负责什么
    后端各处都要用的那些“长期存在的东西”——配置、数据库连接、加密器、data/ 下的参考数据、
    PubChem 等外部接口的客户端、大模型、逆合成引擎、先例库的内存索引、任务队列——每个进程只建一次，
    装进一个 AppContext 对象。需要它们的代码拿到这个对象（习惯上叫 ctx）就够了，
    不用各自去读配置、连数据库。
    它不对应 PPT 里的某一个模块：M0–M14 和 MQ 都从这里取自己要的东西。

谁调用它、它依赖谁
    建：syntherisk/main.py 的 lifespan（API 进程启动时）、worker.py 的 main()（Worker 进程）、
        cli.py 的 main()（每条命令行命令）、tests/conftest.py 的 ctx 夹具。
    用：接口通过 api/deps.py 的 get_ctx() 从 app.state.ctx 取到它；后台任务由 taskqueue.py 的 run_job()
        把它作为第一个参数传给 pipeline.run_analysis() 和 inventory_import.run_import()。
    它依赖 config.py、db.py、security.py、reference.py、external.py、llm/，以及 M1（名称解析）、
    M3（逆合成引擎）、M5（先例索引）、M8（产率预测的占位实现）。

数据从哪来到哪去（都发生在 build_context() 里）
    读   环境变量和 .env（load_settings）、data/ 目录下的参考数据（ReferenceData.load）
    写   数据库：建表、给旧库补列、在售目录为空时写入内置试剂表里可购买的物质（db.init_db）
    建   var/ 和 var/files/ 两个文件夹
    不联网：外部接口的客户端只是建好对象，真正发请求是在用到的时候。

本文件里有什么
    _UNSET            一个哨兵对象，用来区分“没传这个参数”和“特意传了 None”
    AppContext        装着全部共享对象的数据类；另有 resolver 属性和 describe_modules() 方法
    build_context()   按配置把上面这些东西建出来，返回 AppContext
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from syntherisk import i18n
from syntherisk.config import Settings, load_settings
from syntherisk.db import init_db, make_engine, make_session_factory
from syntherisk.external import LiteratureClient, OpsinClient, PubChemClient
from syntherisk.llm import LLMProvider, LLMService, build_provider
from syntherisk.modules.m01_resolver import NameResolver
from syntherisk.modules.m03_retrosynthesis import RetrosynthesisEngine, build_engine
from syntherisk.modules.m05_precedents import PrecedentIndex
from syntherisk.modules.m08_yield import NullYieldPredictor, YieldPredictor
from syntherisk.reference import ReferenceData
from syntherisk.security import Cipher, set_cipher

# 哨兵对象：build_context() 的几个可选参数拿它当默认值。
# 这几个参数传 None 是有含义的（比如 pubchem=None 表示“不用 PubChem”），所以不能再用 None 表示“没传”；
# 用一个谁也不会传进来的独立对象做默认值，再用 `is _UNSET` 判断是不是没传
_UNSET: Any = object()


@dataclass
class AppContext:
    """一个进程里共享的全部运行时对象。API 进程、Worker 进程、每次命令行调用各有自己的一份。

    这是一个普通的数据类（dataclass）：下面每个字段就是一个属性。建好以后正式代码不再换它们；
    测试会直接替换其中几个（比如 ctx.retro_engine、ctx.queue、ctx.pubchem、ctx.llm）来模拟各种情况。

    谁在用：几乎所有模块。接口函数参数里的 ctx: AppContext = Depends(get_ctx) 拿到的就是它。
    """

    # 配置（config.py 的 Settings），两把密钥已经补齐
    settings: Settings
    # SQLAlchemy 的数据库引擎，管着连接池。业务代码不直接用它，都是通过下面的 session_factory
    engine: Engine
    # 会话工厂：要读写数据库时调 ctx.session_factory() 得到一个新的 Session（会话），用完关闭。
    # 接口里的 db、后台任务里的 session 都是从这里来的
    session_factory: sessionmaker[Session]
    # 加密器（security.py 的 Cipher）：加解密文件、算库存的盲索引。数据库的加密列用的也是同一个对象
    cipher: Cipher
    # data/ 目录下的参考数据（reference.py）：常用试剂表、管制清单、评分配置、条件规则、词表、数据来源登记表
    reference: ReferenceData
    # PubChem 客户端。ENABLE_PUBCHEM=false 时是 None：名称查询和危害分类只用内置试剂表和数据库里已有的缓存
    pubchem: PubChemClient | None
    # OPSIN 客户端（英文系统命名转结构）。ENABLE_OPSIN=false 时是 None
    opsin: OpsinClient | None
    # Crossref / OpenAlex 客户端（按 DOI 补文献题目）。ENABLE_LITERATURE_METADATA=false（默认）时是 None
    literature: LiteratureClient | None
    # 大模型的统一入口（llm/base.py 的 LLMService）。没配大模型时 llm.enabled 是 False，
    # M2、M11、M12 自己退回规则和模板
    llm: LLMService
    # M3 逆合成引擎：AiZynthEngine 或 DemoEngine，由 M3_ENGINE 等配置决定
    retro_engine: RetrosynthesisEngine
    # M5 先例库的内存索引。建上下文时是空的，第一次检索先例（分析的第 4 阶段）时才从数据库加载
    precedent_index: PrecedentIndex
    # M8 产率预测器。现在固定是 NullYieldPredictor（一律不预测），流水线里也还没有调用它
    yield_predictor: YieldPredictor
    # 任务队列（taskqueue.py）。队列要拿着上下文才能执行任务，所以不在 build_context() 里建：
    # API 进程在 syntherisk/main.py 的 lifespan 里挂上，Worker 进程在 worker.py 的 main() 里挂上；
    # 命令行（cli.py）不挂，一直是 None
    queue: Any = None  # TaskQueue，建好上下文之后再挂上来

    @property
    def resolver(self) -> NameResolver:
        """M1 的名称解析器：把名称或 CAS 号变成结构，顺序是内置试剂表 → PubChem → OPSIN。

        每次访问都新建一个 NameResolver（它只是把三个数据来源包在一起，很轻），
        所以测试里换掉 ctx.pubchem 以后，下一次 ctx.resolver 用的就是新的。
        谁在用：api/structures.py（A3 名称查询、A14 一句话要求里的物质名）、api/inventory.py（E3 手动添加）、
                inventory_import.py（E1 导入库存时逐行识别）。
        """
        return NameResolver(self.reference, self.pubchem, self.opsin)

    def describe_modules(self) -> dict[str, Any]:
        """各模块现在接的是什么：真模型、演示数据，还是没配置。前端和排查问题时都用得上。

        返回：
            一个字典，键是模块编号（"M1" … "M14"、"MQ"）外加 "database"，值是这个模块的现状：
              M1          rdkit 恒为 True；pubchem、opsin 表示这两个外部接口有没有启用
              M2/M11/M12  三项内容相同，都是大模型的现状：{"provider": 厂商, "model": 型号}；
                          没配时 provider 是 "offline"，另带一句说明
              M3          逆合成引擎自己的描述：engine（demo / aizynthfinder）、ready（能不能用）等
              M4          在售目录和价格的说明文字
              M5          怎么导入先例库的说明文字（库里有多少条不在这里，在命令行 status 的 data 一栏）
              M6          pubchem_ghs（有没有启用 PubChem 查危害分类）、jurisdictions（提示哪些地区的管制清单）
              M7          rules：条件规则的条数
              M8          enabled：产率模型接没接（现在恒为 False）
              M9          固定的说明：算原子经济性，溶剂评级用 CHEM21
              M10         scoring_version：data/scoring.toml 里的 version
              M13         支持导出的格式
              M14         怎么触发校准的说明文字
              MQ          backend：inprocess / redis / sync
              database    数据库类型，取连接串第一个冒号前面的部分，比如 "sqlite"、"postgresql+psycopg"
        副作用：
            没有。只读内存里已有的对象，不查数据库、不联网（M3 一项会看一眼配置文件在不在磁盘上）。
        谁在用：
            GET /api/meta/modules（api/core.py 的 modules()）；python -m syntherisk.cli status（cli.py）。
        """
        settings = self.settings
        return {
            "M1": {"rdkit": True, "pubchem": self.pubchem is not None, "opsin": self.opsin is not None},
            "M2": self.llm.describe(),
            "M3": self.retro_engine.describe(),
            # 下面几句说明文字用 i18n.tr 写：返回的还是中文，同时登记英文（GET /api/meta/modules 带 ?lang=en 时用）
            "M4": {"catalog": i18n.tr("内置常用试剂表；ZINC 在售目录用 python -m syntherisk.cli load-catalog 导入",
                                      "Built-in table of common reagents; import the ZINC in-stock catalog with "
                                      "python -m syntherisk.cli load-catalog"),
                   "prices": i18n.tr("待询价", "Price on request")},
            "M5": {"note": i18n.tr("先例库用 python -m syntherisk.cli load-precedents 导入 USPTO / ORD",
                                   "Import USPTO / ORD into the precedent library with "
                                   "python -m syntherisk.cli load-precedents")},
            "M6": {"pubchem_ghs": self.pubchem is not None, "jurisdictions": settings.jurisdictions},
            "M7": {"rules": len(self.reference.condition_rules)},
            # isinstance(…, NullYieldPredictor)：现在的预测器是不是那个“不预测”的占位实现；不是才算接入了
            "M8": {"enabled": not isinstance(self.yield_predictor, NullYieldPredictor),
                   "note": i18n.tr("产率模型暂未接入：产率只用文献报道值",
                                   "No yield model connected yet: yields come from literature-reported values only")},
            "M9": {"atom_economy": True, "solvent_guide": "CHEM21"},
            "M10": {"scoring_version": self.reference.scoring.get("version")},
            "M11": self.llm.describe(),
            "M12": self.llm.describe(),
            "M13": {"formats": ["pdf", "docx", "sdf", "xlsx", "csv"]},
            "M14": {"note": i18n.tr("授权数据的校准用 python -m syntherisk.cli calibrate 触发",
                                    "Calibration on licensed data is triggered with python -m syntherisk.cli calibrate")},
            "MQ": {"backend": settings.queue_backend},
            # 连接串形如 "sqlite:///…/syntherisk.db" 或 "postgresql+psycopg://用户:密码@主机/库名"。
            # split(":", 1)[0] 只取第一个冒号前面的部分，所以连接串里的密码不会出现在返回值里
            "database": settings.resolved_database_url.split(":", 1)[0],
        }


def build_context(
    settings: Settings | None = None,
    *,
    pubchem: PubChemClient | None = _UNSET,
    llm_provider: LLMProvider | None = _UNSET,
    retro_engine: RetrosynthesisEngine | None = _UNSET,
) -> AppContext:
    """按配置把运行时要用的对象都建出来，装进一个 AppContext 返回。每个进程启动时调用一次。

    参数：
        settings      配置。不传就调 load_settings() 读环境变量和 .env；测试传一份指向临时目录的。
        pubchem       PubChem 客户端的替身（星号后面的三个参数只能用关键字传）。
                      不传：按 ENABLE_PUBCHEM 决定建不建；传 None：不用 PubChem；
                      传一个对象：就用它（测试里传的是接着假 HTTP 传输的客户端）。
        llm_provider  大模型厂商适配器的替身。不传：按 LLM_PROVIDER 等配置建；传 None：不调大模型。
        retro_engine  逆合成引擎的替身。不传：按 M3_ENGINE 等配置建。
    返回：
        AppContext。其中 queue 还是 None，由调用方建好队列以后挂上（见 AppContext.queue 上方的注释）。
    副作用：
        - 把加密器登记成全局的（security.set_cipher），数据库的加密列从此可以读写；
        - 把 data/ 目录下的全部参考数据读进内存；
        - 连数据库、建表、给旧库补列，在售目录为空时写入内置的在售试剂（db.init_db）；
        - 建 var/files 文件夹（load_settings 里还会建 var 文件夹，开发环境可能生成密钥文件）。
        不发任何网络请求。
    抛出（都会让进程启动失败，报错信息里写明了问题在哪）：
        RuntimeError   生产环境缺密钥；LLM_PROVIDER=openai_compatible 但地址、型号、密钥没配全
        ValueError     DATA_ENCRYPTION_KEY 不是合法的 Fernet 密钥；
                       参考数据有问题（比如 compounds.csv 里有读不出来的 SMILES、条件规则引用了不存在的物质）
        EngineError    M3_ENGINE 指定了 AiZynthFinder 但条件不满足（m03_retrosynthesis.py）
        ImportError    LLM_PROVIDER=anthropic 而当前环境没有装 anthropic 这个包
    谁在用：
        syntherisk/main.py 的 lifespan、worker.py 的 main()、cli.py 的 main()、tests/ 里的夹具和个别测试。
    """
    settings = settings or load_settings()
    # 加密器建好后登记成全局的：models.py 里的加密列（EncryptedJSON）读写时通过 security.get_cipher() 找它
    cipher = Cipher(settings.data_encryption_key)
    set_cipher(cipher)

    # 参考数据：data/ 下的 CSV 和 TOML 一次读进内存，之后不再读盘，所以改了这些文件要重启才生效
    reference = ReferenceData.load(settings.data_dir)
    # 数据库：建引擎（连接池）-> 建会话工厂 -> 建表、补列、在售目录为空时写入内置在售试剂
    engine = make_engine(settings.resolved_database_url)
    session_factory = make_session_factory(engine)
    init_db(engine, session_factory, reference)
    # 报告和附件的存放目录 var/files
    settings.files_dir.mkdir(parents=True, exist_ok=True)

    # 外部接口的客户端：对应的开关关着就是 None，所以用到它们的地方都要先判断是不是 None。
    # 注意 OPSIN 的超时用的也是 PUBCHEM_TIMEOUT 这一项；文献客户端用它自己的默认超时
    if pubchem is _UNSET:
        pubchem = PubChemClient(timeout=settings.pubchem_timeout) if settings.enable_pubchem else None
    opsin = OpsinClient(settings.opsin_url_template, timeout=settings.pubchem_timeout) if settings.enable_opsin else None
    literature = LiteratureClient(settings.contact_email) if settings.enable_literature_metadata else None
    # 大模型的厂商适配器：LLM_PROVIDER=offline 时 build_provider 返回 None
    provider = build_provider(settings) if llm_provider is _UNSET else llm_provider

    return AppContext(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        cipher=cipher,
        reference=reference,
        pubchem=pubchem,
        opsin=opsin,
        literature=literature,
        # LLMService 把厂商适配器包一层：每次调用的用量要记进 llm_calls 表，所以把会话工厂也交给它
        llm=LLMService(provider, session_factory),
        retro_engine=build_engine(settings) if retro_engine is _UNSET else retro_engine,
        # 先例索引这时只是个空壳，第一次检索时才加载；产率预测现在是“不预测”的占位实现
        precedent_index=PrecedentIndex(),
        yield_predictor=NullYieldPredictor(),
    )
