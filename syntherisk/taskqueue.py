"""MQ 任务队列：长任务（开始分析、导入大表格）放到后台跑。

    inprocess  默认。任务在 API 进程自己的线程池里跑，本地开发不需要装任何东西。
    redis      生产用。API 把任务推进 Redis，单独的 Worker 进程（python -m syntherisk.worker）取出来跑。
    sync       测试用。enqueue 时直接在当前线程跑完。

进度不走队列：Worker 把每个阶段的进度写进数据库的 analysis_events 表，
SSE 接口从表里读出来推给前端，所以 API 和 Worker 分开部署也没问题。

这个文件负责什么
    接口收到“开始分析”这类要跑几十秒的请求时，不能让浏览器一直等着。做法是：接口只在数据库里记一行
    （状态 queued），调 ctx.queue.enqueue("analysis.run", {"analysis_id": …}) 把任务交给队列，立刻返回；
    真正的活由后台去干。这个文件定义了队列的三种模式，以及“拿到一个任务以后怎么执行”（run_job）。

对应 PPT 里的哪个模块、界面上的哪些按钮
    MQ 任务队列。现在有两种任务：
      "analysis.run"      A16 开始分析（以及失败后的重跑、B11 把路线全排除后的重新搜索、重启后的恢复）
                          -> pipeline.run_analysis(ctx, analysis_id=…)
      "inventory.import"  E1 导入库存，行数超过 inventory_import.SYNC_ROW_LIMIT 的大表格
                          -> inventory_import.run_import(ctx, import_id=…)

谁调用它、它依赖谁
    建队列  syntherisk/main.py 的 lifespan 调 build_queue()；worker.py 自己直接建 RedisQueue
    入队    api/analyses.py（create_analysis、retry_analysis、add_exclusion）、
            api/inventory.py（import_inventory）、syntherisk/main.py（启动时把中断的分析重新排队）
    执行    进程内队列自己的线程；Redis 模式下是 worker.py 的 run_worker()
    本文件开头不导入后端的其他模块：pipeline、inventory_import 和 redis 包都是用到时才在函数里导入。

任务长什么样
    一个任务 = 任务类型（字符串）+ 参数（字典，只放编号这类小东西，不放数据本身）。
    参数字典里的键就是处理函数的关键字参数名。
    Redis 模式下任务存成一段 JSON：{"type": "analysis.run", "payload": {"analysis_id": "…"}}，
    都放在 Redis 里名叫 syntherisk:jobs 的列表中。

本文件里有什么
    logger           名字是 "syntherisk.queue" 的日志器
    QUEUE_KEY        Redis 里存任务的那个列表的名字
    JobHandler       “任务处理函数”的类型
    TaskQueue        队列的接口约定：enqueue() 和 close()
    job_handlers()   任务类型 -> 处理函数 的对照表
    run_job()        执行一个任务，出错只记日志
    InProcessQueue   进程内队列（inprocess 和 sync 两种模式）
    RedisQueue       Redis 队列；多一个给 Worker 用的 pop()
    build_queue()    按 QUEUE_BACKEND 选一种队列
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Protocol

logger = logging.getLogger("syntherisk.queue")

# Redis 里存任务的那个列表（list）的键名。API 往它的左端放，Worker 从右端取，所以先放进去的先被取走
QUEUE_KEY = "syntherisk:jobs"
# 任务处理函数的类型：第一个位置参数是 ctx，其余是关键字参数，没有返回值
JobHandler = Callable[..., None]


class TaskQueue(Protocol):
    """任务队列的接口约定。InProcessQueue 和 RedisQueue 都符合它；测试里的替身也只要有这两个方法。

    Protocol 是 typing 里的“结构化类型”：不需要继承它，一个类只要有同名、同参数的方法，
    类型检查就认为它是 TaskQueue。这里的两个方法体只有 ...，没有实现，所以说明写在这里：

    enqueue(job_type, payload)
        把一个任务放进队列，立刻返回（sync 模式例外：当场跑完才返回）。
        job_type  任务类型，必须是 job_handlers() 里登记过的："analysis.run" 或 "inventory.import"。
        payload   交给处理函数的关键字参数，比如 {"analysis_id": "…"}。Redis 模式下要能转成 JSON。
    close()
        服务停止时调用：关掉线程池或者 Redis 连接。
    """

    # 放进一个任务。参数和行为见上面类的文档
    def enqueue(self, job_type: str, payload: dict[str, Any]) -> None: ...

    # 服务停止时关掉队列
    def close(self) -> None: ...


def job_handlers() -> dict[str, JobHandler]:
    """任务类型 -> 处理函数 的对照表。要加一种新的后台任务，在这里登记一行。

    返回：
        {"analysis.run": pipeline.run_analysis, "inventory.import": inventory_import.run_import}
        两个处理函数的调用方式都是 函数(ctx, **参数)，成功还是失败都由它们自己写进数据库。
    说明：
        import 写在函数里面：导入本文件时不会连带导入 pipeline 和 inventory_import
        （以及它们依赖的一长串模块），第一次执行任务时才导入。
    谁在用：
        本文件的 run_job()。
    """
    from syntherisk import inventory_import, pipeline

    return {
        "analysis.run": pipeline.run_analysis,
        "inventory.import": inventory_import.run_import,
    }


def run_job(ctx, job_type: str, payload: dict[str, Any]) -> None:
    """执行一个任务：按类型找到处理函数，把 payload 里的键值当作关键字参数传进去。

    参数：
        ctx       运行时上下文（AppContext），原样传给处理函数。
        job_type  任务类型。
        payload   处理函数的关键字参数。
    返回：
        None。不管任务成功、失败还是类型不认识，都正常返回，不往外抛异常。
    出错时：
        类型不认识：记一条 error 日志，任务被丢弃。
        处理函数抛了异常：记一条带堆栈的日志。任务的状态由处理函数自己写进数据库
        （run_analysis 把分析标成 failed 后正常返回；run_import 把导入标成 failed 后再把异常抛出来，
        由这里记进日志）。这里不重试。
    谁在用：
        InProcessQueue.enqueue()（在线程池里执行，或者当场执行）；worker.py 的 run_worker()。
    """
    handler = job_handlers().get(job_type)
    if handler is None:
        logger.error("不认识的任务类型：%s", job_type)
        return
    try:
        # **payload 把字典展开成关键字参数：{"analysis_id": "abc"} 相当于 handler(ctx, analysis_id="abc")
        handler(ctx, **payload)
    except Exception:  # noqa: BLE001 - 任务自己负责把失败写进数据库，这里只保证 Worker 不被带崩
        # logger.exception 会把当前异常的完整堆栈一起写进日志
        logger.exception("任务 %s 失败：%s", job_type, payload)


class InProcessQueue:
    """进程内队列：任务就在 API 进程里跑，不需要 Redis。QUEUE_BACKEND 是 inprocess（默认）或 sync 时用它。

    两种模式：
        线程池（sync=False）  enqueue 把任务交给一个固定大小的线程池就返回；同时最多跑 workers 个任务，
                             多出来的在池子里排队。任务只存在内存里：进程一退出，没跑的、跑到一半的都没了，
                             所以启动时要靠 pipeline.recover_interrupted() 把中断的分析找回来。
        同步（sync=True）     enqueue 直接在调用它的线程里把任务跑完才返回。测试用：
                             提交分析的那个请求返回时，分析已经结束了。
    谁在用：
        本文件的 build_queue()。
    """

    def __init__(self, ctx, workers: int = 2, sync: bool = False):
        """建队列。

        参数：
            ctx      运行时上下文，执行任务时传给处理函数。
            workers  线程池里的线程数（同时跑几个任务）。来自配置 WORKER_THREADS，默认 2。sync=True 时用不到。
            sync     True 表示同步模式。
        """
        self._ctx = ctx
        self._sync = sync
        # 同步模式不建线程池。thread_name_prefix 让这些线程在日志和调试器里显示成 syntherisk-job_0、_1……
        self._executor = None if sync else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="syntherisk-job")

    def enqueue(self, job_type: str, payload: dict[str, Any]) -> None:
        """提交一个任务。线程池模式立刻返回；同步模式当场执行完才返回。

        两种模式都不会把任务里的异常抛给调用方（run_job 把异常记进日志后吞掉了）。
        """
        if self._sync:
            run_job(self._ctx, job_type, payload)
        else:
            # submit 把“调用 run_job(ctx, job_type, payload)”排进线程池。它返回的 Future 对象这里用不到
            self._executor.submit(run_job, self._ctx, job_type, payload)

    def close(self) -> None:
        """关闭线程池。服务停止时由 syntherisk/main.py 的 lifespan 调用。

        wait=False：不等正在跑的任务结束，这个调用马上返回。
        cancel_futures=True：还在排队、没开始跑的任务直接取消。
        正在跑的任务不会被这里打断。被取消的分析在数据库里仍是 queued 状态，
        下次启动时由 recover_interrupted() 处理。
        """
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)


class RedisQueue:
    """Redis 队列：QUEUE_BACKEND=redis 时用。API 进程只管往里放，另起的 Worker 进程（worker.py）往外取。

    用的是 Redis 的列表（list）：LPUSH 从左端放，BRPOP 从右端取，合起来就是先进先出。
    任务在 Redis 里是一段 JSON 文本：{"type": 任务类型, "payload": 参数字典}。
    任务被 Worker 取走的那一刻就从 Redis 里删掉了，没有“处理完再确认”这一步：
    Worker 取到任务以后如果崩溃，这个任务不会自动回到队列里。
    谁在用：
        本文件的 build_queue()（API 进程）；worker.py 的 main()（Worker 进程）。
    """

    def __init__(self, redis_url: str, client=None):
        """建队列。

        参数：
            redis_url  Redis 地址，来自配置 REDIS_URL，比如 redis://localhost:6379/0（最后的 0 是库号）。
            client     现成的 Redis 客户端。正式运行不传，按 redis_url 新建；
                       测试传 fakeredis 的假客户端，这时 redis_url 不起作用。
        说明：
            redis.Redis.from_url 只是建客户端对象，这时还没有真的连 Redis；第一次放、取任务时才连。
        """
        if client is None:
            # 用到 Redis 时才导入 redis 这个包：它是可选依赖，本地开发用进程内队列时可以不装
            import redis

            client = redis.Redis.from_url(redis_url)
        self._redis = client

    def enqueue(self, job_type: str, payload: dict[str, Any]) -> None:
        """把任务转成 JSON，放到列表左端。立刻返回，任务由 Worker 进程稍后取走。

        抛出：连不上 Redis 时抛 redis 包的连接错误。这里不处理，异常会抛给调用它的接口。
        """
        self._redis.lpush(QUEUE_KEY, json.dumps({"type": job_type, "payload": payload}))

    def pop(self, timeout: int = 5) -> tuple[str, dict[str, Any]] | None:
        """Worker 用：阻塞等一个任务，超时返回 None。

        参数：
            timeout  最多等几秒（整数）。等待期间队列里一有任务就立刻返回。
        返回：
            (任务类型, 参数字典)，比如 ("analysis.run", {"analysis_id": "…"})；
            等到超时还没有任务就返回 None。
        副作用：
            取到的任务同时从 Redis 里删除。
        谁在用：
            worker.py 的 run_worker()。
        """
        # BRPOP 是阻塞版的“从右端取出一个”。有任务时返回 (列表的键名, 元素)，所以任务的 JSON 在 item[1]；
        # 超时返回 None
        item = self._redis.brpop(QUEUE_KEY, timeout=timeout)
        if item is None:
            return None
        job = json.loads(item[1])
        return job["type"], job["payload"]

    def close(self) -> None:
        """关闭 Redis 客户端的连接。"""
        self._redis.close()


def build_queue(ctx) -> TaskQueue:
    """按配置 QUEUE_BACKEND 建任务队列。

    参数：
        ctx  运行时上下文。读它的 settings.queue_backend、redis_url、worker_threads；
             进程内队列还会把 ctx 存起来，执行任务时传给处理函数。
    返回：
        redis      -> RedisQueue（任务放进 Redis，要另起 python -m syntherisk.worker 才有人执行）
        inprocess  -> InProcessQueue，线程池模式
        sync       -> InProcessQueue，同步模式（测试用）
    谁在用：
        syntherisk/main.py 的 lifespan；tests/conftest.py 的 ctx 夹具。
        Worker 进程不走这里，它在 worker.py 里直接建 RedisQueue。
    """
    backend = ctx.settings.queue_backend
    if backend == "redis":
        return RedisQueue(ctx.settings.redis_url)
    # 剩下 inprocess 和 sync 两种，区别只在 sync 这个开关
    return InProcessQueue(ctx, workers=ctx.settings.worker_threads, sync=backend == "sync")
