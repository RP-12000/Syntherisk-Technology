"""Syntherisk 后端。

模块编号沿用产品设计 PPT：
    M0  应用服务（账号、项目、草稿、预设、权限）      -> syntherisk.api
    MQ  任务队列（后台跑长任务，进度推给前端）          -> syntherisk.taskqueue
    M1  结构工具            M2  约束解析（大模型）
    M3  逆合成搜索          M4  可得性与库存
    M5  文献先例            M6  毒性与法规
    M7  条件与复杂度        M8  产率模型（暂不实现，只留接口）
    M9  绿色化学            M10 评分与排序
    M11 解释（大模型）      M12 问答（大模型）
    M13 报告生成            M14 回填与校准

这个文件负责什么
    它让 syntherisk 这个文件夹成为一个 Python 包，并且给出后端的版本号 __version__。
    别的什么都不做：只 import syntherisk 不会读配置，也不会连数据库。

编号对应的代码在哪
    M0         syntherisk/api/ 下的五组接口：core、structures、analyses、inventory、reports
    MQ         syntherisk/taskqueue.py（队列）、syntherisk/worker.py（Redis 模式下的后台进程）、
               syntherisk/pipeline.py（“开始分析”之后的八个阶段）
    M1–M14     syntherisk/modules/ 下的 m01_*.py … m14_backfill.py，文件名里的数字就是模块编号；
               aizynth_bridge.py 是 M3 另起进程跑 AiZynthFinder 用的脚本
    大模型调用  syntherisk/llm/（M2、M11、M12 共用；只有这里知道用的是哪家厂商）

不属于某一个业务模块的公共文件（都在 syntherisk/ 下）
    main.py              FastAPI 应用入口
    config.py            运行配置（.env）
    context.py           把配置、数据库、各个引擎装进一个对象
    db.py                数据库连接和建表
    models.py            全部数据表
    security.py          密码、登录令牌、加密
    storage.py           报告和附件的文件存储
    cli.py               命令行工具
    routes.py            把逆合成引擎返回的路线树整理成后端自己的结构
    cards.py             证据卡
    constraints.py       约束：前端提交的原样，和合并后实际执行的那一份
    reference.py         data/ 下的参考数据
    external.py          PubChem、OPSIN、Crossref / OpenAlex 的客户端
    loaders.py           导入先例库和在售目录
    inventory_import.py  导入库存表格

谁在用 __version__
    syntherisk/main.py：显示在接口文档页（/docs）的标题旁边，也出现在 GET /health 的返回值里。
    注意 pyproject.toml 的 [project] version 里另外写着一个版本号，两处各写各的，改版本时要一起改。
"""

# 后端的版本号（字符串）
__version__ = "0.1.0"
