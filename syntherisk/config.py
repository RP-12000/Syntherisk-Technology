"""运行配置。所有配置都能用环境变量或项目根目录的 .env 覆盖，变量名就是字段名的大写。

这个文件负责什么
    定义后端全部的配置项（Settings 类），并在启动时把两把密钥补齐（resolve_secrets）。
    它不对应 PPT 里的某一个业务模块：M0–M14 和 MQ 用到的开关、地址、密钥都集中在这里。

配置从哪来（同一项在几处都写了时，排在上面的优先）
    1. 代码里直接传的参数          load_settings(queue_backend="sync")，只有测试这么用
    2. 操作系统的环境变量          比如在 PowerShell 里先执行 $env:PORT = "9000" 再启动
    3. 项目根目录的 .env 文件       平时改这里；照着 .env.example 填
    4. 本文件里写的默认值          所以什么都不配也能跑
    变量名不分大小写：字段 database_url 对应 .env 里的 DATABASE_URL。
    .env 里出现本文件没有定义的变量会被忽略，不报错。

谁调用它、它依赖谁
    context.py 的 build_context() 调 load_settings()，建好的 Settings 放在 AppContext.settings 上；
    其他代码一律从 ctx.settings 读，不自己再读 .env。
    例外有两处，直接建 Settings()（只读其中几项，不补密钥）：
        根目录的 main.py                    读 HOST、PORT、APP_ENV
        syntherisk/main.py 的 create_app()   读 CORS_ORIGINS
    modules/m03_retrosynthesis.py 还用到这里的 PROJECT_ROOT（把 .env 里写的相对路径换成绝对路径）。
    它依赖 pydantic-settings（读环境变量和 .env、做类型转换和检查）和 cryptography（生成 Fernet 密钥）。

读写哪些文件
    读    项目根目录的 .env
    读写  var/dev_secrets.json（只在开发环境、并且没有配全密钥的时候）
    建    var/ 目录（load_settings 里）

本文件里有什么
    PROJECT_ROOT        项目根目录的绝对路径
    Settings            全部配置项；另有四个算出来的属性和一个方法：
      cors_origin_list       CORS_ORIGINS 拆成列表
      jurisdictions          CONTROLLED_JURISDICTIONS 拆成列表
      resolved_database_url  实际使用的数据库地址（没配就是本地 SQLite）
      files_dir              报告和附件存放的文件夹
      resolve_secrets()      补齐登录令牌的签名密钥和数据加密密钥
    load_settings()     建 Settings、建 var 目录、补密钥；正式运行和测试都从这里进
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录的绝对路径。__file__ 是本文件（…/syntherisk/config.py），.parent 是 syntherisk 文件夹，
# 再 .parent 就是项目根目录。.env、data/、var/ 都按它来定位，所以从哪个目录启动都找得到
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """后端的全部配置项。每个字段对应一个同名（大写）的环境变量，或者 .env 里的一行。

    BaseSettings 来自 pydantic-settings：建对象时它自己去读环境变量和 .env，并按字段的类型做转换和检查。
    比如 PORT=abc 会在启动时直接报错；类型写成 Literal[...] 的字段只接受列出来的那几个值。
    布尔型的字段在 .env 里写 true / false（1 / 0、yes / no 也认）。

    下面每个字段上方的注释写明：它管什么、.env 里叫什么、可以填什么、谁在读它。
    标了“.env.example 里没有列出”的几项同样可以写进 .env，只是示例文件里没有。

    谁在用：
        load_settings() 建它，再由 context.py 的 build_context() 放进 AppContext.settings。
    """

    # pydantic-settings 的读取规则：
    #   env_file            .env 的位置。写的是绝对路径，所以不管从哪个目录启动，读的都是项目根目录那一份
    #   env_file_encoding   .env 按 UTF-8 读（里面可以写中文，比如 DEMO_ORG_NAME）
    #   extra="ignore"      .env 里有本类没定义的变量时忽略它，不报错
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # --- 服务本身 ---
    # APP_ENV：运行环境，三个值之一。
    #   development（默认）  用根目录的 main.py 启动时开启热重载（改代码自动重启）；
    #                       没配密钥就自动生成，存进 var/dev_secrets.json
    #   production          不热重载；SECRET_KEY 和 DATA_ENCRYPTION_KEY 缺任何一个，启动就报错
    #   test                不热重载；没配密钥就临时随机生成，不写文件（tests/conftest.py 用这个值）
    # 谁在读：根目录的 main.py、本文件的 resolve_secrets()
    app_env: Literal["development", "production", "test"] = "development"
    # HOST / PORT：服务监听的地址和端口。127.0.0.1 表示只有本机能访问。
    # 只有根目录的 main.py 读这两项；直接用 uvicorn 命令启动时它们不起作用
    host: str = "127.0.0.1"
    port: int = 8000
    # CORS_ORIGINS：允许调接口的前端页面地址，多个用英文逗号分开。浏览器的跨域限制要靠它放行。
    # 默认值是两个常见的前端开发服务器地址（Vite 默认用 5173 端口，Create React App、Next.js 默认用 3000）。
    # 谁在读：下面的 cors_origin_list 属性 -> syntherisk/main.py 的 create_app()
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # --- 数据层 ---
    # 本地开发默认 SQLite；生产用 PostgreSQL，例如
    # postgresql+psycopg://user:password@host:5432/syntherisk
    # DATABASE_URL：SQLAlchemy 的数据库连接串。留空 = 用本地文件 <var_dir>/syntherisk.db。
    # 谁在读：下面的 resolved_database_url 属性 -> context.py -> db.py 的 make_engine()
    database_url: str = ""
    # DATA_DIR：参考数据所在的文件夹，默认是项目根目录下的 data/（.env.example 里没有列出）。
    # 里面是 compounds.csv、controlled_substances.csv、scoring.toml、condition_rules.toml、vocabulary.toml、
    # data_sources.toml，以及 demo_routes/（内置演示路线）。
    # 谁在读：context.py（ReferenceData.load）、modules/m03_retrosynthesis.py（演示引擎读 demo_routes）
    data_dir: Path = PROJECT_ROOT / "data"
    # VAR_DIR：运行时数据所在的文件夹，默认是项目根目录下的 var/（.env.example 里没有列出）。
    # 里面有 syntherisk.db（没配 DATABASE_URL 时的 SQLite 数据库）、files/（加密后的报告和附件）、
    # dev_secrets.json（开发环境自动生成的密钥）。测试把它指到临时目录
    var_dir: Path = PROJECT_ROOT / "var"

    # --- 安全 ---
    # SECRET_KEY：给登录令牌（JWT）签名的密钥，任意一段足够长的随机字符串。
    # 换掉它以后，已经发出去的令牌全部失效，所有人要重新登录；数据不受影响。
    # 谁在读：api/core.py（签发令牌）、api/deps.py（验证令牌）
    secret_key: str = ""  # 登录令牌签名
    # DATA_ENCRYPTION_KEY：加密客户数据的密钥，必须是 Fernet 格式（32 个随机字节的 base64，44 个字符）。
    # 生成方法见 .env.example。数据库里的加密列、var/files 下的文件、库存的盲索引都靠它：
    # 换掉或者丢了，已有的加密数据就解不开，所以生产环境一定要备份。
    # 谁在读：context.py 的 build_context()（用它建 security.Cipher）
    data_encryption_key: str = ""  # 客户库存、目标结构、实验回填的加密密钥（Fernet）
    # ACCESS_TOKEN_MINUTES：登录令牌的有效期，单位分钟。默认 12 × 60 = 720 分钟，也就是 12 小时，
    # 过期后要重新登录（.env.example 里没有列出）。谁在读：api/core.py 的 _token_response()
    access_token_minutes: int = 12 * 60
    # MAX_UPLOAD_MB：上传文件的大小上限，单位 MB，默认 20（.env.example 里没有列出）。
    # 管着 A4 上传结构文件、E1 导入库存表格、G2 上传实验附件；超过返回 HTTP 413。
    # 谁在读：storage.py 的 read_upload()
    max_upload_mb: int = 20

    # --- MQ 任务队列 ---
    # QUEUE_BACKEND：后台任务（分析、大表格导入）在哪里跑。
    #   inprocess（默认）  在 API 进程自己的线程池里跑，不需要装别的东西；进程退出时没跑完的任务会丢，
    #                     下次启动时由 pipeline.recover_interrupted() 找回来
    #   redis             API 只把任务推进 Redis，要另起 python -m syntherisk.worker 来执行
    #   sync              入队时当场跑完再返回
    # 谁在读：taskqueue.py 的 build_queue()、worker.py、syntherisk/main.py 的 lifespan、context.py
    queue_backend: Literal["inprocess", "redis", "sync"] = "inprocess"  # sync 只给测试用
    # REDIS_URL：Redis 的地址，只在 QUEUE_BACKEND=redis 时用到。
    # 默认值是本机的 Redis（6379 是 Redis 的默认端口，最后的 /0 是库号）。谁在读：taskqueue.py、worker.py
    redis_url: str = "redis://localhost:6379/0"
    # WORKER_THREADS：进程内队列的线程数，也就是 API 进程里最多同时跑几个后台任务，默认 2
    # （.env.example 里没有列出）。只对 inprocess 模式有用。谁在读：taskqueue.py 的 build_queue()
    worker_threads: int = 2

    # --- M3 逆合成搜索 ---
    # auto：配了 AiZynthFinder 就用它，否则退回内置演示路线（只认布洛芬、阿司匹林）
    # M3_ENGINE：用哪个逆合成引擎。
    #   auto（默认）        设了 AIZYNTH_CONFIG，并且找得到 aizynthfinder（装在当前环境里，或者用
    #                      AIZYNTH_PYTHON 指到了别的环境），就用 aizynth_bridge；否则用 demo
    #   demo               内置演示路线（data/demo_routes/ 下的几个分子），不做真正的搜索
    #   aizynth_bridge     每次搜索另起一个进程去跑 AiZynthFinder；条件不满足时启动就报错
    #   aizynth_inprocess  在后端进程里直接调用 AiZynthFinder（占的内存不会还回去，一般不用）
    # 谁在读：modules/m03_retrosynthesis.py 的 build_engine()（由 context.py 调用）
    m3_engine: Literal["auto", "demo", "aizynth_bridge", "aizynth_inprocess"] = "auto"
    # AIZYNTH_PYTHON：装了 aizynthfinder 的那个 Python 解释器的路径，可以写相对项目根目录的路径
    aizynth_python: str = ""  # 留空 = 用后端自己的 Python（aizynthfinder 和后端装在一起）；装在别的环境里才要填
    # AIZYNTH_CONFIG：AiZynthFinder 配置文件的路径，可以写相对项目根目录的路径，
    # 比如 data\external\aizynth\config.yml。留空（并且 M3_ENGINE=auto）时用演示引擎
    aizynth_config: str = ""  # AiZynthFinder 的 config.yml
    # 下面三项是 config.yml 里定义的名字，原样传给 AiZynthFinder；默认值对应它的公开模型。
    #   AIZYNTH_STOCK              在售清单的名字（公开版是 zinc）
    #   AIZYNTH_EXPANSION_POLICY   单步逆合成模型的名字（公开版是 uspto）
    #   AIZYNTH_FILTER_POLICY      过滤模型的名字（公开版是 uspto）；留空表示不用过滤模型
    # 谁在读：modules/m03_retrosynthesis.py 的 AiZynthEngine
    aizynth_stock: str = "zinc"
    aizynth_expansion_policy: str = "uspto"
    aizynth_filter_policy: str = "uspto"
    # AiZynthFinder 自己的默认值是 100 轮。实测布洛芬 100 轮找不到完整路线，400 轮（约 35 秒）能找到 10 条
    # AIZYNTH_ITERATION_LIMIT：一次搜索最多搜几轮。重新搜索（B11 把路线全排除以后）时，流水线会把它
    # 乘以 2、3……多搜几轮。谁在读：pipeline.py 的 _search()
    aizynth_iteration_limit: int = 400
    # AIZYNTH_TIME_LIMIT：一次搜索最长搜多少秒，和轮数上限谁先到就按谁停。
    # 另起进程搜索时，后端在这个时间之外再等 300 秒（加载模型要几十秒），还没结束才判为超时。
    # 谁在读：pipeline.py 的 _search()
    aizynth_time_limit: int = 120  # 秒；到时间就停，不管搜了几轮
    # AIZYNTH_CUTOFF_NUMBER：单步模型每一步最多试多少个反应模板；AIZYNTH_CUTOFF_CUMULATIVE：模型给的概率
    # 从高到低累计到这个数就不再往下试。两个条件先到哪个算哪个。AiZynthFinder 自己的默认是 50 和 0.995。
    # 实测（2026-10-22，README 的“AiZynthFinder”一节有表）：每步 50 个时，1,4-二苯甲基苯合理的傅-克拆法排第 253 位、
    # 根本试不到，给的是六七步的绕远路线；300 个时给出 3 步的傅-克路线，每次搜索多用 10%–80% 的时间，峰值内存多约 0.5 GB。
    # 累计概率要同时放开到 1.0，否则累计到 0.995 就停，数字放大了也不起作用。
    # 谁在读：modules/m03_retrosynthesis.py 的 AiZynthEngine._payload()，交给 aizynth_bridge.run() 设到单步模型上
    aizynth_cutoff_number: int = 300
    aizynth_cutoff_cumulative: float = 1.0

    # --- 外部免费接口 ---
    # ENABLE_PUBCHEM：要不要用 PubChem（按名称或 CAS 号查结构、查 GHS 危害分类、给没有名字的中间体取名）。
    # 关掉后 ctx.pubchem 是 None：名称查询只用内置试剂表和 OPSIN，危害分类只用内置试剂表和数据库里已有的缓存。
    # 谁在读：context.py 的 build_context()
    enable_pubchem: bool = True
    # PUBCHEM_TIMEOUT：每次请求最多等几秒，默认 8（.env.example 里没有列出）。
    # 注意：OPSIN 的客户端用的也是这一项，没有单独的 OPSIN 超时配置
    pubchem_timeout: float = 8.0
    # ENABLE_OPSIN：要不要用 OPSIN（把英文系统命名转成结构）。关掉后 ctx.opsin 是 None
    enable_opsin: bool = True
    # OPSIN_URL_TEMPLATE：OPSIN 接口的地址模板，{name} 的位置会换成要查的名称
    # （.env.example 里没有列出，换成自己架设的 OPSIN 服务时才需要改）
    opsin_url_template: str = "https://www.ebi.ac.uk/opsin/ws/{name}.json"
    # ENABLE_LITERATURE_METADATA：打开后，先例列表接口（C1、C2）会按 DOI 去 Crossref 查题目、作者、年份，
    # Crossref 查不到再问 OpenAlex。只对有 DOI、库里又没有题目的先例起作用。默认关
    enable_literature_metadata: bool = False  # Crossref / OpenAlex 补文献题目
    # CONTACT_EMAIL：联系邮箱，作为 mailto 参数随请求发给 Crossref / OpenAlex。
    # 只在 ENABLE_LITERATURE_METADATA=true 时用到；留空就不带这个参数
    contact_email: str = ""

    # --- 大模型（M2 / M11 / M12）---
    # offline：不调大模型，用规则和模板生成，结果会标明“未调用大模型”
    # LLM_PROVIDER：用哪家大模型。
    #   offline（默认）     不调大模型
    #   anthropic          Claude，走 Anthropic 官方 SDK（要装 anthropic 这个包）
    #   openai_compatible  任何兼容 OpenAI Chat Completions 的接口；这时 LLM_BASE_URL、LLM_MODEL、
    #                      LLM_API_KEY 三项都必须填，缺了启动就报错
    # 下面这一组都由 llm/base.py 的 build_provider() 读（context.py 启动时调用）
    llm_provider: Literal["offline", "anthropic", "openai_compatible"] = "offline"
    # LLM_MODEL：模型的型号。anthropic 留空时用 AnthropicProvider.DEFAULT_MODEL；openai_compatible 必填
    llm_model: str = ""
    # LLM_API_KEY：接口密钥。openai_compatible 必填；anthropic 留空时由 SDK 自己从环境里找凭证
    llm_api_key: str = ""
    # LLM_BASE_URL：接口地址。openai_compatible 必填，比如 https://api.openai.com/v1；anthropic 一般留空
    llm_base_url: str = ""
    # LLM_EFFORT：思考深度 low / medium / high。只传给 anthropic；openai_compatible 不用这一项
    llm_effort: Literal["low", "medium", "high"] = "low"
    # LLM_MAX_TOKENS：一次回答最多输出多少 token，默认 8192（.env.example 里没有列出）。
    # 只传给 anthropic；太小的话回答会被截断，那次调用按失败处理
    llm_max_tokens: int = 8192
    # LLM_TIMEOUT：一次调用最多等几秒，默认 60（.env.example 里没有列出）。两种厂商都用
    llm_timeout: float = 60.0
    # LLM_JSON_MODE：只对 openai_compatible 有用。
    #   json_schema（默认）  厂商支持“按 JSON Schema 约束输出”时用
    #   json_object         厂商只支持“输出 JSON”时用：Schema 写进提示词，结果由后端自己校验
    llm_json_mode: Literal["json_schema", "json_object"] = "json_schema"
    # EXPLAIN_TOP_ROUTES：分析的第 8 阶段给排名前几的路线写说明，默认 3。
    # 排在后面的路线，等用户点开证据卡时才现写。谁在读：pipeline.py 的 _explain()
    explain_top_routes: int = 3

    # --- M6 管制清单 ---
    # CONTROLLED_JURISDICTIONS：按哪些地区的管制清单提示，逗号分开的地区代码（大小写都行）。
    # 清单本身在 data/controlled_substances.csv，这里只决定报哪些地区的。
    # 谁在读：下面的 jurisdictions 属性 -> pipeline.py 的 _safety()、context.py 的 describe_modules()
    controlled_jurisdictions: str = "CA,US,CN,TW"

    # --- M13 报告 ---
    # REPORT_FONT_PATH：生成 PDF 报告用的中文字体文件（TrueType）的路径。
    # 谁在读：api/reports.py 的 create_report() -> modules/m13_reports.py 的 render_pdf()
    report_font_path: str = ""  # 留空则自动找系统里的中文字体

    # --- 演示账号（python -m syntherisk.cli seed-demo 用）---
    # DEMO_USER_EMAIL / DEMO_USER_PASSWORD：演示账号的邮箱和密码。两项都填了 seed-demo 才肯运行，
    # 密码至少 8 位（cli.py 里检查）。账号密码只放在 .env 里，不写进代码。
    # DEMO_ORG_NAME：演示课题组的名字。
    # 谁在读：cli.py 的 cmd_seed_demo()
    demo_user_email: str = ""
    demo_user_password: str = ""
    demo_org_name: str = "示例课题组"

    @property
    def cors_origin_list(self) -> list[str]:
        """把 CORS_ORIGINS（逗号分隔的一串）拆成列表，去掉每一项两头的空格，丢掉空项。

        例："http://a.com, http://b.com," -> ["http://a.com", "http://b.com"]
        谁在用：syntherisk/main.py 的 create_app()，交给 CORSMiddleware 的 allow_origins。
        """
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jurisdictions(self) -> list[str]:
        """把 CONTROLLED_JURISDICTIONS 拆成地区代码的列表，去掉空格，统一成大写。

        例："ca, us,CN" -> ["CA", "US", "CN"]
        谁在用：pipeline.py 第 5 阶段（交给 M6 的 SafetyService，只报这些地区的管制清单条目）；
                context.py 的 describe_modules()（“各模块现在接的是什么”里的 M6 一项）。
        """
        return [j.strip().upper() for j in self.controlled_jurisdictions.split(",") if j.strip()]

    @property
    def resolved_database_url(self) -> str:
        """实际使用的数据库地址（SQLAlchemy 的连接串）。

        配了 DATABASE_URL 就原样用它；没配就用本地 SQLite 文件 <var_dir>/syntherisk.db，
        拼成 sqlite:///C:/…/var/syntherisk.db 这样的形式（as_posix() 把 Windows 路径里的反斜杠换成正斜杠）。
        谁在用：context.py 的 build_context()（建数据库引擎）和 describe_modules()（只取冒号前面的数据库类型）。
        """
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.var_dir / 'syntherisk.db').as_posix()}"

    @property
    def files_dir(self) -> Path:
        """报告、对比表和实验附件存放的文件夹：<var_dir>/files。里面的文件都是加密过的，文件名是随机的。

        谁在用：storage.py 的 save_file() / load_file()；context.py 的 build_context() 负责把这个文件夹建出来。
        """
        return self.var_dir / "files"

    def resolve_secrets(self) -> None:
        """补齐两把密钥。

        生产环境必须显式配置。开发环境没配时自动生成并存进 var/dev_secrets.json，
        这样重启后令牌和已加密的数据仍然可用。这个文件丢了，已加密的数据就解不开。

        两把密钥：
            secret_key           给登录令牌（JWT）签名用
            data_encryption_key  加密客户数据用（Fernet 密钥）
        两把都已经配好时什么都不做。否则按运行环境分三种情况：
            production   抛 RuntimeError，服务起不来
            test         缺的那把临时随机生成，只留在内存里，不写文件
            development  缺的那把从 var/dev_secrets.json 里取；文件里也没有，就生成一把并写回文件
        只配了其中一把时：配了的那把用配的，没配的那把按上面的规则补。

        返回：None。结果直接写回 self.secret_key 和 self.data_encryption_key。
        副作用：开发环境下可能新建或改写 var/dev_secrets.json（var 目录不存在时一并建出来）。
        抛出：RuntimeError（生产环境缺密钥）。
        谁在用：本文件的 load_settings()。
        """
        # 两把都配了（来自环境变量或 .env）：不用补
        if self.secret_key and self.data_encryption_key:
            return
        # 生产环境不自动生成：缺了直接报错，让部署的人自己配
        if self.app_env == "production":
            raise RuntimeError("生产环境必须设置 SECRET_KEY 和 DATA_ENCRYPTION_KEY")
        if self.app_env == "test":
            # `a or b`：a 非空就用 a，否则用 b，所以已经配了的那把不会被覆盖。
            # token_urlsafe(32) 是 32 个随机字节编成的文本（约 43 个字符）；
            # Fernet.generate_key() 生成 Fernet 专用格式的密钥（字节串），decode() 转成字符串
            self.secret_key = self.secret_key or secrets.token_urlsafe(32)
            self.data_encryption_key = self.data_encryption_key or Fernet.generate_key().decode()
            return
        # 以下是开发环境。密钥存在 var/dev_secrets.json 里，内容形如
        #   {"secret_key": "…", "data_encryption_key": "…"}
        path = self.var_dir / "dev_secrets.json"
        stored: dict = {}
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
        # 文件里缺哪一把就现生成哪一把；changed 记着要不要把文件写回去
        changed = False
        if not stored.get("secret_key"):
            stored["secret_key"] = secrets.token_urlsafe(32)
            changed = True
        if not stored.get("data_encryption_key"):
            stored["data_encryption_key"] = Fernet.generate_key().decode()
            changed = True
        if changed:
            # parents=True：上级目录不存在就一起建；exist_ok=True：目录已经有了也不报错
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
        # 环境变量或 .env 里配了的优先，没配的用文件里的
        self.secret_key = self.secret_key or stored["secret_key"]
        self.data_encryption_key = self.data_encryption_key or stored["data_encryption_key"]


def load_settings(**overrides) -> Settings:
    """建一份可以直接用的配置：读环境变量和 .env，建好 var 目录，补齐两把密钥。

    参数：
        overrides  直接指定某几项配置，优先级最高（盖过环境变量和 .env）。键是 Settings 的字段名。
                   正式运行时不传；测试用它换成临时数据库、同步队列等（见 tests/conftest.py 的 make_settings）。
                   还可以传 pydantic-settings 的特殊参数 _env_file=None，表示这一次不读 .env。
    返回：
        Settings 对象，其中 secret_key 和 data_encryption_key 一定非空。
    副作用：
        建 var_dir 目录；开发环境下可能生成 var/dev_secrets.json（见 resolve_secrets）。
    抛出：
        配置的值不合法时 pydantic 抛 ValidationError（比如 QUEUE_BACKEND 写了不认识的值）；
        生产环境缺密钥时抛 RuntimeError。
    谁在用：
        context.py 的 build_context()（调用方没有传 settings 时）；tests/conftest.py 的 make_settings()。
    """
    settings = Settings(**overrides)
    # 先建 var 目录：没配 DATABASE_URL 时，SQLite 数据库文件就建在这个目录里
    settings.var_dir.mkdir(parents=True, exist_ok=True)
    settings.resolve_secrets()
    return settings
