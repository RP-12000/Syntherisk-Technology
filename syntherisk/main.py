"""FastAPI 应用入口。

启动：python main.py（项目根目录），或者 uvicorn syntherisk.main:app
接口文档：启动后打开 http://127.0.0.1:8000/docs

这个文件负责什么
    把后端组装成一个 FastAPI 应用对象（文件末尾的 app）：
      1. 登记“启动时 / 关闭时”要做的事（lifespan）：建运行时上下文、建任务队列、把上次没跑完的分析重新排队；
      2. 加上跨域（CORS）设置，让前端开发服务器能调接口；
      3. 把 syntherisk/api/ 下五组接口挂到 /api 前缀下面；
      4. 另外提供两个不在 /api 下的地址：/（跳到接口文档）和 /health（健康检查）。

对应 PPT 里的哪个模块、界面上的哪些按钮
    它是 M0 应用服务的外壳，自己不实现任何按钮。按钮的接口分别在（完整的对照表见 docs/API.md）：
      api/core.py        账号、课题组、项目、草稿（A6）、预设（A15、D2、D3）、前端要读的配置
      api/structures.py  结构校验和查询（A1–A5）、一句话要求（A14）、结构差异（C2）、原料可得性（C4）
      api/analyses.py    开始分析、进度、取消（A16–A19）、结果页（B1–B12）、证据卡（C1、C2、C5）
      api/inventory.py   实验室库存（E1–E5，以及“已导入库存 N 种”的计数）
      api/reports.py     路线对比（D1、D4、D5）、导出报告（F1–F3）、回填实验结果（G1–G4）

谁调用它、它依赖谁
    uvicorn 按 "syntherisk.main:app" 导入本文件末尾的 app（根目录的 main.py 就是这样启动的）；
    测试用 create_app(ctx) 传入一份准备好的上下文（tests/conftest.py）。
    它依赖 context.py（build_context）、taskqueue.py（build_queue）、pipeline.py（recover_interrupted）、
    config.py（Settings，只为了读跨域设置）和 api/ 下的五个接口文件。

数据从哪来到哪去
    本文件自己不读写业务数据。应用启动时 build_context() 会读配置、连数据库、建表、读 data/ 下的参考数据；
    建好的上下文放在 app.state.ctx 上，每个接口通过 api/deps.py 的 get_ctx() 取到它。
    进程内队列模式下，启动时还会改写 analyses 表里没跑完的分析（见 pipeline.recover_interrupted）。

本文件里有什么
    logger          名字是 "syntherisk" 的日志器
    create_app()    组装并返回 FastAPI 应用；它里面定义了下面三个内部函数
      lifespan()      应用启动时和关闭时各运行一段
      root()          GET /        跳转到 /docs
      health()        GET /health  健康检查，不用登录
    app             模块级的应用对象，uvicorn 导入的就是它
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from syntherisk import __version__
from syntherisk.api import analyses, core, inventory, reports, structures
from syntherisk.config import Settings
from syntherisk.context import AppContext, build_context
from syntherisk.pipeline import recover_interrupted
from syntherisk.taskqueue import build_queue

# 日志器。输出到哪里、什么格式，由启动入口里的 logging.basicConfig 决定（见根目录的 main.py）
logger = logging.getLogger("syntherisk")


def create_app(ctx: AppContext | None = None) -> FastAPI:
    """组装并返回 FastAPI 应用。ctx 留空时，在应用启动的时候按配置建一份；测试里传一份准备好的进来。

    参数：
        ctx  运行时上下文（syntherisk/context.py 的 AppContext）。
             None（默认）：正式运行。上下文推迟到应用启动时（下面的 lifespan）才建，
             所以仅仅 import 本文件不会去连数据库。
             给了：测试用。直接用这一份，里面的数据库、队列、逆合成引擎都可以是替身。
    返回：
        配好生命周期、跨域和全部路由的 FastAPI 对象。
    副作用：
        调用本函数时不连数据库、不建队列；只在 ctx 留空时读一次配置（建一个 Settings，为了拿跨域设置）。
        连数据库、建表、建队列、重新排队这些事发生在应用启动时，见里面的 lifespan。
    谁在用：
        本文件末尾的 app = create_app()；tests/conftest.py 的 client 夹具（create_app(ctx)）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用的生命周期：yield 之前的代码在启动时运行一次，yield 之后的代码在关闭时运行一次。

        这是 FastAPI 的约定：把这样一个异步上下文管理器交给 FastAPI(lifespan=...)，
        它在开始接收请求之前跑到 yield，停止服务时再接着跑 yield 后面的部分。

        启动时做的事：
            1. 取得运行时上下文：用 create_app 传进来的；没有就调 build_context() 现建
               （读配置、连数据库、建表、读参考数据、选逆合成引擎）；
            2. 上下文里还没有任务队列的话，按 QUEUE_BACKEND 建一个挂上去；
            3. 把上下文放到 app.state.ctx，接口通过 api/deps.py 的 get_ctx() 取用；
            4. 进程内队列模式下，把上次进程退出时没跑完的分析重新排队。
        关闭时做的事：
            关掉任务队列。
        """
        # ctx 是外层 create_app 的参数。测试传了就用测试的；正式运行时到这里才真正建上下文
        context = ctx or build_context()
        if context.queue is None:
            # 队列要拿着上下文才能执行任务，所以是上下文建好以后再建队列、挂回上下文
            context.queue = build_queue(context)
        # app.state 是 FastAPI 留给应用放共享对象的地方；整个进程只有这一份上下文
        app.state.ctx = context
        if context.settings.queue_backend == "inprocess":
            # 进程内队列不落盘：上次没跑完的分析在这里重新排队
            # recover_interrupted 返回要重跑的分析编号；已经重试过的那些它直接标成失败，不在返回值里。
            # 只有 inprocess 模式做这一步：redis 模式下排着队的任务存在 Redis 里，
            # sync 模式（测试用）任务在入队时当场跑完
            for analysis_id in recover_interrupted(context):
                context.queue.enqueue("analysis.run", {"analysis_id": analysis_id})
        logger.info("Syntherisk 后端已启动，逆合成引擎：%s", context.retro_engine.name)
        # 到这里启动完成，开始接收请求；服务停止时从 yield 后面接着执行
        yield
        # 进程内队列：还没开始跑的任务取消，正在跑的不等它；Redis 队列：关掉连接
        context.queue.close()

    # title、version、description 显示在接口文档页（/docs）的顶部；lifespan 就是上面那个函数
    app = FastAPI(
        title="Syntherisk 后端",
        version=__version__,
        description="给 AI 生成的合成路线逐步找依据、打分、排序。按钮和接口的对照表见 docs/API.md。",
        lifespan=lifespan,
    )
    # 跨域设置要在建应用的时候就定下来，而正式运行时上下文要等到启动才建，
    # 所以没有传 ctx 时单独读一次配置，只为了拿允许的前端地址
    settings = ctx.settings if ctx else Settings()
    # CORS（跨域资源共享）：浏览器默认不让一个地址上的页面（比如 http://localhost:5173 的前端）
    # 去调另一个地址的接口（http://127.0.0.1:8000），要后端在响应头里声明“允许这些来源”才行。
    #   allow_origins      允许的前端地址，来自配置 CORS_ORIGINS（逗号分隔）
    #   allow_credentials  允许请求带 Cookie、Authorization 这类凭证
    #   allow_methods / allow_headers 写 "*"：不限制 HTTP 方法和请求头
    #   expose_headers     跨域时浏览器脚本默认只读得到少数几个响应头。下载文件的接口（报告、对比表、附件）
    #                      把文件名放在 Content-Disposition 里，要在这里点名，前端的脚本才读得到
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    # include_in_schema=False：这个地址不出现在接口文档里
    @app.get("/", include_in_schema=False)
    def root():
        """打开根地址时跳转到接口文档页 /docs。"""
        return RedirectResponse("/docs")

    @app.get("/health", tags=["配置"])
    def health():
        """健康检查：服务在运行就返回 status = "ok" 和后端版本号。不用登录。"""
        # 只说明进程活着、能响应请求；不检查数据库、Redis、逆合成引擎是不是可用。
        # version 取自 syntherisk/__init__.py 的 __version__
        return {"status": "ok", "version": __version__}

    # 五组接口都挂在 /api 下面：比如 api/analyses.py 里写的 "/analyses"，实际地址是 /api/analyses。
    # 每个文件里各有一个 router（APIRouter），include_router 把登记在它上面的接口并进应用
    for module in (core, structures, analyses, inventory, reports):
        app.include_router(module.router, prefix="/api")
    return app


# 模块级的应用对象，uvicorn 按 "syntherisk.main:app" 找到的就是它。
# 这里没有传 ctx，所以 import 本文件时只读一次配置；连数据库等到服务真正启动时才做
app = create_app()
