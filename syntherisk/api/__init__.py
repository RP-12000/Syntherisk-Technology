"""M0 应用服务：接收前端的所有请求。

每个路由文件开头都列了它对应界面上的哪些按钮（A1–G4）。完整对照表见 docs/API.md。

这个包是后端对外的唯一入口（HTTP 接口层）。它自己不做化学计算，只做四件事：
    1. 检查登录和权限（谁在调、是哪个课题组的）；
    2. 检查请求内容的格式；
    3. 调用 syntherisk/modules 下的各个模块（M1–M14），或者把长任务交给任务队列（MQ）；
    4. 把结果整理成前端要的 JSON 返回。

怎么接进应用：
    syntherisk/main.py 的 create_app() 依次把 core、agreement、structures、analyses、inventory、reports
    六个文件里的 router 挂到 FastAPI 应用上，统一加前缀 /api。
    所以文件里写的 @router.get("/projects")，完整路径是 GET /api/projects。
    只有健康检查 GET /health 和首页 GET /（跳转到接口文档 /docs）定义在 main.py 里，不带 /api 前缀。

本包里有什么（建议按这个顺序读）：
    deps.py        所有接口共用的“依赖”：取运行时上下文、开数据库会话、认出当前登录的人、
                   按课题组取项目和分析（别的课题组的数据一律当作不存在，返回 404）。
    schemas.py     请求体的格式（Pydantic 模型）：每个 POST / PUT / PATCH 接口收哪些字段、有什么限制。
    views.py       把存好的分析结果整理成前端各个位置（R2–R9）要的 JSON；不查库、不联网。
    core.py        M0：注册登录、课题组成员、项目和项目历史、草稿（A6）、预设（A15、D2、D3），
                   以及前端启动时读的配置（评分公式、选项、模块状态、数据来源）。
    agreement.py   内测协议：读协议、同意协议、下载协议全文和同意确认。没同意之前别的接口都返回 403。
    structures.py  M1 / M2 / M4：结构校验、上传、名称查询、画图、结构差异（A1–A5、C2），
                   一句话要求解析（A14），原料可得性（C4）。
    analyses.py    分析任务：提交、进度推送、取消、重跑（A16–A19）；
                   结果页和证据卡（B1–B12、C1、C2、C5）。
    inventory.py   M4 实验室库存：导入、确认、手动添加、搜索、共享（E1–E5）。
    reports.py     M10 / M13 / M14：路线对比（D1、D4、D5）、导出报告（F1–F3）、回填实验结果（G1–G4）。

所有接口共同的约定：
    登录    除注册、登录以外都要带请求头 Authorization: Bearer <登录拿到的 access_token>。
            除注册、登录、/api/auth/me 和内测协议的几个接口以外，还要已经同意当前版本的内测协议，
            否则返回 403，detail 是 {"code": "agreement_required", "message": …}（api/deps.py 的 require_agreement）。
            进度推送接口（SSE）另外也接受把令牌放在网址里：?access_token=。
    隔离    所有客户数据都属于某个课题组（代码里叫组织，Organization），
            接口只返回当前登录者所在课题组的数据。
    出错    返回 {"detail": ...}。状态码：401 没登录，403 没权限或者还没同意内测协议，404 没有这条数据，
            409 现在不能做这个操作，413 上传的文件太大，415 附件类型不支持，422 输入有问题。
    写库    接口里改了数据要自己 db.commit()（见 deps.get_db）。
    加密    目标结构、约束、分析结果、库存、草稿、实验记录在数据库里是加密的（models.py 里的
            EncryptedJSON 列）；接口代码读写的是解密后的普通字典，加解密由那个列类型自动完成。
            报告和实验附件的文件也是加密后才写到磁盘上的（storage.py）。
"""
