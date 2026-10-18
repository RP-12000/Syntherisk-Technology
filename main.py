"""在 PyCharm 里直接运行这个文件（Shift+F10）就能启动后端。

启动后：
    接口文档   http://127.0.0.1:8000/docs
    健康检查   http://127.0.0.1:8000/health

配置写在项目根目录的 .env 里（照着 .env.example 填）；什么都不配也能跑，
这时用的是 SQLite、进程内任务队列、内置演示路线，不调大模型。

这个文件负责什么
    只做一件事：读出监听地址和端口，用 uvicorn（运行 FastAPI 应用的 Web 服务器）把后端跑起来。
    真正的应用对象在 syntherisk/main.py 里（那个文件末尾的 app），这里用字符串 "syntherisk.main:app" 指过去。
    它不对应 PPT 里的某一个业务模块，也不对应界面上的按钮：它是整个后端（M0–M14、MQ）的启动入口。

谁调用它、它依赖谁
    在 PyCharm 里运行，或者在项目根目录执行 python main.py。
    另外，uvicorn main:app 会导入它，只为了拿 app（见下面“另外几种启动方式”）。
    它依赖 syntherisk/config.py 的 Settings（只读 HOST、PORT、APP_ENV 三项）、syntherisk/main.py 的 app 和 uvicorn。

另外几种启动方式
    uvicorn syntherisk.main:app    不经过这个文件。这时 .env 里的 HOST、PORT 不起作用，
                                   监听地址和端口要用 uvicorn 自己的 --host、--port 参数给。
    uvicorn main:app               同上，只是从这个文件拿 app。部署到 Railway 时，它的构建工具 Railpack
                                   认出 FastAPI 项目后固定用这条命令启动（加上 --host 0.0.0.0 --port $PORT），
                                   所以这里要有 app。
    python -m syntherisk.worker    后台 Worker 进程，只在 QUEUE_BACKEND=redis 时才需要另外起
                                   （见 syntherisk/worker.py）。

本文件里有什么
    没有函数和类，只有从 syntherisk/main.py 引过来的 app，和文件末尾的一段启动代码（if __name__ == "__main__"）。
"""

import logging

import uvicorn

from syntherisk.config import Settings
# 给 uvicorn main:app 用的。import 它只建应用对象、读一次配置，不连数据库（见 syntherisk/main.py 的 create_app）
from syntherisk.main import app  # noqa: F401

# 只有“直接运行这个文件”时才启动服务；被别的代码 import 时，下面这段不执行
if __name__ == "__main__":
    # 日志打到控制台，INFO 及以上级别都显示。每行的格式是：时间 级别 日志器的名字: 内容。
    # 后端自己的日志器名字都以 syntherisk 开头（syntherisk、syntherisk.pipeline、syntherisk.queue……）
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # 这里只为了拿监听地址、端口和运行环境，所以直接建 Settings（它会读环境变量和 .env），
    # 没有走 load_settings()：不建 var 目录，也不补密钥。那些事在应用启动时由 build_context() 做
    settings = Settings()
    # 第一个参数写成 "模块:变量" 的字符串，而不是直接传 app 对象：开了 reload 以后，
    # uvicorn 要靠这个字符串在子进程里重新导入应用。
    # reload 只在开发环境打开（APP_ENV=development，这是默认值）：改了代码，服务自动重启。
    # APP_ENV 是 production 或 test 时不打开
    uvicorn.run("syntherisk.main:app", host=settings.host, port=settings.port, reload=settings.app_env == "development")
