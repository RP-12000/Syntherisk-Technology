"""后台 Worker：QUEUE_BACKEND=redis 时单独起这个进程，从 Redis 里取任务来跑。

    python -m syntherisk.worker

可以同时起多个。本地开发用默认的进程内队列，不需要它。

这个文件负责什么
    生产部署时，API 进程只把任务放进 Redis（见 taskqueue.py 的 RedisQueue），真正执行任务的是这个进程：
    它自己建一份运行时上下文（连的是同一个数据库），然后循环“等任务 -> 执行 -> 再等”。
    任务的进度和结果都写进数据库，API 进程从数据库里读出来推给前端，两个进程之间不直接通信。

对应 PPT 里的哪个模块
    MQ 任务队列里 Worker 那一半。执行的任务有两种：分析（A16 等）和大表格的库存导入（E1）。

谁调用它、它依赖谁
    正式使用时由人或部署脚本用 python -m syntherisk.worker 起进程；测试直接调 run_worker()。
    它依赖 context.py（build_context）和 taskqueue.py（RedisQueue、run_job）。

部署时要注意
    - Worker 和 API 要用同一份配置：同一个 DATABASE_URL、REDIS_URL，以及同一把 DATA_ENCRYPTION_KEY
      （否则 Worker 解不开 API 写进数据库的加密内容）。分析是在 Worker 里跑的，
      所以 data/ 目录和 AiZynthFinder 的模型也要在 Worker 所在的机器上。
    - 一个 Worker 进程一次只跑一个任务；要同时跑几个分析，就起几个进程。
    - 收到 Ctrl+C（SIGINT）或 SIGTERM 时不会打断手上的任务，跑完才退出。

本文件里有什么
    logger         名字是 "syntherisk.worker" 的日志器
    run_worker()   取任务、执行任务的循环
    main()         进程入口：建上下文、连 Redis、挂信号处理、进循环
"""

from __future__ import annotations

import logging
import signal
import threading

from syntherisk.context import AppContext, build_context
from syntherisk.taskqueue import RedisQueue, run_job

logger = logging.getLogger("syntherisk.worker")


def run_worker(ctx: AppContext, queue: RedisQueue, stop: threading.Event, *, poll_seconds: int = 5) -> int:
    """一直取任务来跑，直到 stop 被置位。返回跑了几个任务。

    参数：
        ctx           运行时上下文，传给每个任务的处理函数。
        queue         Redis 队列（用它的 pop 方法取任务）。
        stop          停止开关（threading.Event）。别处调 stop.set() 以后，循环在下一次检查时退出。
        poll_seconds  每次最多等多少秒任务（只能用关键字传）。所以收到停止信号以后，
                      空闲的 Worker 最多再过这么多秒就退出。
    返回：
        这次运行一共执行了几个任务（成功、失败都算）。
    说明：
        stop 只在两个任务之间检查：正在执行的任务不会被打断，run_job 返回以后才看要不要停。
        任务失败不会让循环停下（run_job 自己把异常记进日志，不往外抛）。
    谁在用：
        本文件的 main()；tests/test_integrations.py（用 fakeredis 验证“API 入队、Worker 执行”）。
    """
    handled = 0
    while not stop.is_set():
        # 阻塞等任务，最多等 poll_seconds 秒；没等到就回到循环开头，顺便再看一眼 stop
        job = queue.pop(timeout=poll_seconds)
        if job is None:
            continue
        # job 是 (任务类型, 参数字典)，比如 ("analysis.run", {"analysis_id": "…"})
        job_type, payload = job
        logger.info("开始任务 %s %s", job_type, payload)
        run_job(ctx, job_type, payload)
        handled += 1
    return handled


def main() -> None:
    """Worker 进程的入口（python -m syntherisk.worker）。

    步骤：
        1. 配置日志；建运行时上下文（读配置、连数据库、加载参考数据，和 API 进程启动时做的一样）；
        2. 确认 QUEUE_BACKEND=redis，否则直接退出并说明原因；
        3. 建 Redis 队列，并挂到 ctx.queue 上，和 API 进程里一样指向 Redis；
        4. 登记信号处理：收到 SIGINT / SIGTERM 时置位 stop，循环跑完手上的任务后退出；
        5. 进入 run_worker() 循环；循环结束后关闭 Redis 连接。
    抛出：
        SystemExit  配置的不是 redis 队列时。
        另外，build_context() 可能抛的错误见 context.py。
    """
    # 日志格式和 API 进程一样：时间 级别 日志器的名字: 内容
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ctx = build_context()
    if ctx.settings.queue_backend != "redis":
        # raise SystemExit("…")：把这句话打印到标准错误输出，进程以退出码 1 结束
        raise SystemExit("Worker 只在 QUEUE_BACKEND=redis 时使用；现在的配置是进程内队列，任务由 API 进程自己跑。")
    queue = RedisQueue(ctx.settings.redis_url)
    ctx.queue = queue

    # threading.Event 是一个线程安全的开关：set() 置位，is_set() 查询
    stop = threading.Event()

    def request_stop(*_args) -> None:
        """信号处理函数：只置位 stop，不做别的。

        Python 调用它时会传两个参数（信号编号和当时的栈帧），这里都用不到，所以用 *_args 接住。
        """
        logger.info("收到停止信号，跑完手上的任务就退出")
        stop.set()

    # SIGINT：在终端里按 Ctrl+C；SIGTERM：kill 命令、systemd、Docker 停进程时发的信号。
    # 两个都改成“请求停止”，而不是默认的立刻终止
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    logger.info("Worker 已启动，等待任务")
    run_worker(ctx, queue, stop)
    queue.close()


# python -m syntherisk.worker 时走这里
if __name__ == "__main__":
    main()
