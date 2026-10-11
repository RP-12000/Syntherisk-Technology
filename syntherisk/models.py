"""数据库表。SQLite 和 PostgreSQL 通用。

加密列（EncryptedJSON）放客户数据：目标结构、分析结果、库存、实验回填。
没加密的只有账号、项目名、状态这类元数据，以及公开的参考数据（先例、在售目录）。

这个文件负责什么
    用 SQLAlchemy 2 的“声明式”写法定义全部 20 张表：每个类是一张表，类里的每个属性是一列。
    这里只有表的定义，没有任何业务逻辑；建表在 db.py 的 init_db()，读写分散在各个模块里。

20 张表分五组
    M0 账号、项目、草稿、预设   organizations、users、memberships、projects、drafts、presets
    分析任务（MQ + 流水线）     analyses、analysis_events、llm_calls
    M4 库存                    inventory_items、inventory_imports、inventory_import_rows
    M13 报告、M14 回填          reports、experiment_records、experiment_attachments、calibration_runs
    公开参考数据                reaction_precedents、library_versions、catalog_compounds、substance_hazards

几条通用的约定
    编号      大多数表的主键 id 是 32 个十六进制字符的随机串（new_id）；
              analysis_events、llm_calls、reaction_precedents 三张表用自增整数；
              catalog_compounds 和 substance_hazards 直接拿结构的 key14 当主键，library_versions 拿名字当主键。
    按组织隔离  客户数据的表都有 org_id 列。接口取数据时一律核对它等于当前登录者的课题组，
              别的课题组的数据当作不存在（见 api/deps.py）。
    时间      created_at / updated_at 存的是 UTC 时间。
    加密      类型写 EncryptedJSON 的列，数据库里存的是一长串密文（见 security.py）；
              读写时自动解密、加密，代码里拿到的是普通的字典或列表。
              加密列没法在 SQL 里按内容查询，所以需要筛选的字段（状态、所属组织、结构的盲索引）都另设明文列。
    改 JSON   SQLAlchemy 只在“给属性重新赋值”时才知道这一列变了，直接改字典、列表里面的内容不会被保存。
              所以代码里要么整体换一个新对象（row.payload = {**row.payload, "k": v}），
              要么改完以后调 flag_modified(row, "列名")。JSON 列和加密列都是这样。
    没有迁移工具  表是启动时按这里的定义建的；给已有的表加列，要同时在 db.py 的 _ADDED_COLUMNS 里登记。
    没有级联删除  外键只保证“指向的那一行存在”，这里没有定义删一行时连带删别的行。

怎么读一列的定义
    name: Mapped[str] = mapped_column(String(200), default="", index=True)
      Mapped[str]          这一列在 Python 里的类型。写成 Mapped[str | None] 表示数据库里可以为空（NULL）
      String(200)          数据库里的类型：最长 200 个字符的字符串。Text 是不限长度的文本，
                           JSON 是存成 JSON 的字典或列表，LargeBinary 是二进制数据
      primary_key=True     主键
      default=…            新增一行时没给值就用它；给的是函数（new_id、utcnow、dict、list）时，每新增一行现调一次
      onupdate=utcnow      这一行每次被修改时，自动更新成当时的时间
      index=True           给这一列建索引，按它查得快；unique=True 另外保证不重复
      ForeignKey("表.列")   外键：值必须是那张表里已有的那一列的值（列的类型也跟着那一列）

谁调用它、它依赖谁
    几乎所有读写数据库的代码都 from syntherisk import models。它只依赖 security.py 的 EncryptedJSON。

本文件里有什么（按阅读顺序）
    new_id()、utcnow()   主键和时间的默认值
    Base                 所有表的共同基类
    Organization、User、Membership、Project、Draft、Preset            M0
    Analysis、AnalysisEvent、LLMCall                                  分析任务
    InventoryItem、InventoryImport、InventoryImportRow                M4 库存
    Report、ExperimentRecord、ExperimentAttachment、CalibrationRun    M13 报告、M14 回填
    ReactionPrecedent、LibraryVersion、CatalogCompound、SubstanceHazard  公开参考数据
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from syntherisk.security import EncryptedJSON


def new_id() -> str:
    """生成一个新的编号：32 个十六进制字符的随机串（UUID 第 4 版去掉连字符）。

    大多数表的主键 id 用它做默认值（default=new_id）：新的一行第一次写进数据库时由 SQLAlchemy 调用。
    所以对象刚建出来时 id 还是 None，要 session.flush() 或 commit() 之后才有值。
    谁在用：本文件各表的 id 列；api/analyses.py（取前 10 位，当“排除记录”的编号）；
            m05_precedents.py 的 mark_library_changed()（当先例库的版本号）。
    """
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """现在的时间，带 UTC 时区。各表的 created_at / updated_at 用它做默认值，代码里记时间也统一用它。

    注意：存进 SQLite 再读出来的时间不带时区信息（值仍然是 UTC），发给前端之前由下面的 iso() 补上。
    """
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    """把时间变成带时区的 ISO 8601 字符串（形如 "2026-10-11T08:30:00+00:00"），返回给前端用。

    接口返回的每一个时间都要经过它，前端拿到的才是同一种格式：不带时区的时间字符串，
    浏览器的 new Date() 会当成本地时间，显示出来就差了几个小时。

    参数：
        value  数据库里读出来的时间；可以是 None（比如还没结束的分析的 finished_at）。
    返回：
        ISO 8601 字符串；value 是 None 时返回 None。
    说明：
        数据库里存的都是 UTC 时间（utcnow）。SQLite 读出来的时间丢了时区信息，这里把 UTC 补上。
    谁在用：
        api/views.py（analysis_view、analysis_list_item；它还以同一个名字 iso 转给 api/core.py、api/reports.py）；
        modules/m04_availability.py 的 InventoryService.to_dict；modules/m14_backfill.py 的 record_view。
    """
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite 读出来的时间不带时区，存的时候是 UTC
        # replace 只是给它贴上 UTC 的标签，不改变时刻本身
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class Base(DeclarativeBase):
    """所有数据表的共同基类（SQLAlchemy 2 的声明式写法）。

    继承它的每个类对应数据库里的一张表：__tablename__ 是表名，带 mapped_column(...) 的类属性是列。
    Base.metadata 汇总了全部的表，db.py 的 init_db() 用它来建表。
    """

    pass


# ---------------------------------------------------------------- M0 账号、项目、草稿、预设


class Organization(Base):
    """课题组。所有客户数据都挂在组织下面，查询时一律按组织过滤。

    加密：没有加密列（只有名字和设置）。
    谁写：注册接口 POST /api/auth/register（api/core.py，注册时顺带建一个课题组）；
          命令行 seed-demo（cli.py）。现在没有改名、删除课题组的接口。
    谁读：api/deps.py 的 authenticate()（每个请求确认登录者属于哪个组）等。
    """

    __tablename__ = "organizations"

    # 课题组编号。其他表的 org_id 指向它
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 课题组的名字（注册时填的 org_name）
    name: Mapped[str] = mapped_column(String(200))
    # 课题组级别的设置，明文 JSON，建组时是空字典。现在代码只读其中一个键：
    #   inventory_default_visibility  新加的库存默认是 "team"（全组可见）还是 "private"（m04_availability.py 读）
    # 还没有接口能修改它
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 建组时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    """登录账号。一个账号对应一个人。

    加密：没有加密列。密码不是明文，存的是 scrypt 哈希（security.hash_password）。
    谁写：注册接口（api/core.py 的 register）、管理员加成员（add_member）、命令行 seed-demo。
          现在没有改密码、改资料、停用账号的接口。
    谁读：登录（login）、每个请求的身份验证（api/deps.py 的 authenticate）。
    """

    __tablename__ = "users"

    # 用户编号。登录令牌里的 sub 字段就是它
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 登录邮箱，存之前统一转成小写、去掉两头空格；全库唯一。
    # 320 是通常引用的邮箱地址长度上限（@ 前面 64 个字符 + @ + 域名 255 个字符）
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    # 显示名，可以为空字符串
    display_name: Mapped[str] = mapped_column(String(200), default="")
    # 密码的哈希，形如 "scrypt$16384$8$1$<盐>$<哈希>"，不到 100 个字符
    password_hash: Mapped[str] = mapped_column(String(300))
    # 账号是否可用。False 时登录不了，已经发出的令牌也不再被接受。
    # 现在没有接口会把它改成 False，只能直接改数据库
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 注册时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Membership(Base):
    """成员关系：哪个用户属于哪个课题组、是什么角色。

    表的设计允许一个用户属于多个课题组（下面的唯一约束只是不许同一个组里重复出现同一个人），
    但现在的接口每个账号只会有一行：注册时建一个新组并成为 owner，或者被管理员加进某个组；
    登录时取的是这个用户的第一条成员关系。
    加密：没有加密列。
    谁写：register、add_member（api/core.py）、命令行 seed-demo。
    谁读：login（决定令牌里写哪个课题组）、authenticate（确认令牌里的用户仍在这个组里，并取角色）。
    """

    __tablename__ = "memberships"
    # 同一个课题组里，同一个用户只能有一行
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 哪个用户
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # 角色。owner：注册时建组的人；admin、member：由管理员加成员时指定。
    # owner 和 admin 算管理员（可以给组里加成员），member 是普通成员
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner / admin / member
    # 加入时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    """项目：把一个课题组的分析和报告分门别类。每次分析都属于一个项目。

    加密：没有加密列（项目名和说明是明文）。
    谁写：注册时自动建一个“默认项目”，seed-demo 建一个“演示项目”，POST /api/projects 新建。
          现在没有改名、归档、删除项目的接口。
    谁读：GET /api/projects、GET /api/projects/{id}/history（项目历史）。
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 项目名
    name: Mapped[str] = mapped_column(String(200))
    # 项目说明，可以为空
    description: Mapped[str] = mapped_column(Text, default="")
    # 谁建的（用户编号）
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 是否已归档。接口会把它返回给前端，但现在没有接口会把它改成 True
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # 建项目的时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    """A6：新建分析的草稿，点“下一步”时自动保存。

    草稿是个人的：只有保存它的人自己能看到和修改（接口同时核对 org_id 和 user_id）。
    加密：payload 加密。
    谁写：POST /api/drafts（新建）、PUT /api/drafts/{id}（更新），都在 api/core.py。
    谁删：DELETE /api/drafts/{id}；提交分析时带了 draft_id 的话，api/analyses.py 的 create_analysis() 把它删掉。
    """

    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 谁的草稿
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # 打算放进哪个项目；还没选时为空
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    # 【加密】草稿内容。里面放什么由前端决定（目标分子、约束、停在第几步……），后端原样保存、原样返回
    payload: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # 最后一次保存的时间：新建时写入，每次修改自动更新
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Preset(Base):
    """A15 约束预设、D2/D3 权重预设。组里所有人可用。

    加密：没有加密列，payload 是明文 JSON。
          注意约束预设里可以带结构（起始骨架、要排除的物质的 SMILES），它们在这张表里是明文存的。
    谁写：POST /api/presets（api/core.py 的 save_preset）：同一个组里 kind 和 name 都相同时直接覆盖。
    谁删：DELETE /api/presets/{id}。
    谁读：GET /api/presets?kind=…。
    """

    __tablename__ = "presets"
    # 同一个课题组里，同一种预设不能重名
    __table_args__ = (UniqueConstraint("org_id", "kind", "name"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 预设的种类。constraints：新建分析第 2 步的整页约束（A15）；weights：对比页的六个权重（D2、D3）
    kind: Mapped[str] = mapped_column(String(20))  # constraints / weights
    # 预设的名字
    name: Mapped[str] = mapped_column(String(200))
    # 预设的内容。constraints：一份完整的约束（存之前补齐了默认值，结构同 constraints.Constraints）；
    # weights：{维度名: 权重}，维度名是 literature、operation、safety、availability、yield、green
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 是不是这个组、这一种预设的默认项。保存时把某一条设为默认，同组同种的其他预设会被取消默认
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # 第一次保存它的人；之后别人覆盖内容，这一列不变
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 最后一次保存的时间
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- 分析任务


class Analysis(Base):
    """一次分析：用户点“开始分析”（A16）建一行，后台流水线（pipeline.py）逐步把它填满。

    这是整个后端最核心的表。列分三类：
        明文的元数据  属于谁、状态、进度、错误信息——接口要按它们筛选，SSE 要读它们
        加密的输入    target（目标分子）、constraints（约束）、user_exclusions（用户排除的分支和试剂）
        加密的输出    result（分析结果：molecules、reactions、routes 三张互相引用的表，见 routes.py 的文件头）

    status 的变化：
        queued     接口建好这一行、任务进了队列，还没开始跑
        running    流水线正在跑（pipeline.run_analysis 开始时写）
        succeeded  八个阶段跑完（一条路线都没找到也算跑完）
        failed     中途出错，原因在 error 列
        cancelled  用户取消（A19）
      另外三种回到 queued 的情况：失败或取消后重跑（POST …/retry）；
      B11 把可选路线全排除了，重新搜索；服务重启后恢复被中断的分析。

    谁写：api/analyses.py（新建、取消、重跑、排除、标记为我有、补写说明）、pipeline.py（跑分析的全过程）、
          api/reports.py 和 m14_backfill.py（把实验结果挂到 result 里的对应步骤上）。
    谁读：api/analyses.py、api/reports.py、api/core.py（项目历史）、api/views.py（整理成前端要的样子）。
    现在没有删除分析的接口。
    """

    __tablename__ = "analyses"

    # 分析编号，出现在接口地址里：/api/analyses/{id}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组（权限检查用）
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 属于哪个项目
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    # 谁提交的。流水线取库存时按这个人的可见范围取（全组共享的 + 他自己私有的）
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))

    # 当前状态，取值和含义见上面类的文档
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    # queued / running / succeeded / failed / cancelled
    # 最近一次更新进度的是第几阶段（1–8）；还没开始是 0。流水线的 _set_stage() 写
    current_stage: Mapped[int] = mapped_column(Integer, default=0)
    # 八个阶段的进度表（明文 JSON），进度面板 R3 直接显示它。每一项是一个字典：
    #   index、key、name、module、hint   固定的阶段信息（来自 pipeline.STAGES）
    #   status       pending / running / done / skipped / failed / cancelled
    #   message      这一阶段的一句结果，比如 “找到 12 条完整路线”
    #   started_at / finished_at   ISO 格式的 UTC 时间字符串，没有则为 null
    # 流水线的 fresh_stages()、_set_stage()、_close_running_stages() 写
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # 用户是否请求了取消。取消接口对“正在跑”的分析把它置为 True，流水线在各个检查点查到后停下；
    # 重跑和重新搜索时接口把它改回 False
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # 这一行被流水线跑过几次：每次开始跑加 1；重跑接口（…/retry）把它清零。两处用到：
    # 服务重启恢复时，已经跑过 2 次的不再重试；逆合成搜索的轮数按 (1 + 已重跑次数) 放大
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # 失败原因，形如 “异常类型: 说明”，最多 2000 个字符；没有失败时为空。明文
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 这次用的逆合成引擎的名字："demo" 或 "aizynthfinder"。流水线第 2 阶段写
    engine: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 【加密】目标分子：{smiles（规范写法）, key14, name, formula, mol_weight, messages（校验时的提示）}。
    # 提交分析时由 api/analyses.py 的 create_analysis() 写，之后不变
    target: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # 【加密】提交时的约束原样（constraints.Constraints 转成的字典）：复杂度档位、原料来源、做不了的条件、
    # 安全要求、规模、高级参数、起始骨架、一句话要求及其确认后的标签。提交时写，之后不变
    constraints: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # 【加密】用户在结果页右键排除的东西（B11），一个列表。每项是：
    #   排除分支  {"type": "branch", "step_key", "name", "id", "created_at"}
    #   排除试剂  {"type": "substance", "key14", "smiles", "name", "id", "created_at"}
    # api/analyses.py 的 add_exclusion()、remove_exclusion() 写
    user_exclusions: Mapped[list[dict[str, Any]]] = mapped_column(EncryptedJSON, default=list)
    # 【加密】分析结果，一个很大的字典；第 2 阶段完成之前为空。主要的键：
    #   stage       "preliminary"（只有路线树）/ "scored"（评完分）/ "final"（说明也写完了）
    #   target、engine、warnings、constraint_tags
    #   molecules   {key14: 分子}        reactions   {步骤编号: 一步反应和它的评估结果}
    #   routes      [路线]              ranking / excluded   可选路线的名次、被排除的路线
    #   weights、scoring_version
    # 各阶段往里写什么，见 pipeline.py 里 AnalysisRunner 各方法的文档
    result: Mapped[dict[str, Any] | None] = mapped_column(EncryptedJSON, nullable=True)

    # 提交时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 最近一次开始跑的时间（重跑时会被刷新）；还没跑过为空
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 最近一次结束（成功、失败或取消）的时间；正在跑或者还没跑为空
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AnalysisEvent(Base):
    """进度事件。SSE 接口从这张表读，所以 API 和 Worker 分进程跑也能推进度。

    流水线每推进一步就加一行；GET /api/analyses/{id}/events 每半秒查一次“编号比上次大的行”，推给前端。
    加密：不加密。payload 里只有阶段编号、状态、一句进度说明，以及分析结束时的错误信息。
    谁写：pipeline.py 的 add_event()（流水线各处，以及取消一个还在排队的分析时）。
    谁读：api/analyses.py 的 stream_events()。
    只增不删：同一次分析重跑时，上一轮的事件还留在表里。
    """

    __tablename__ = "analysis_events"

    # 自增编号。它同时是 SSE 里的事件 id：前端断线重连时带上 Last-Event-ID，后端从这个编号之后接着发
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 哪一次分析
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    # 事件类型，也就是 SSE 里 event: 后面的词：
    #   status / stage / preliminary_ready / scored / done（各自的含义见 pipeline.add_event 的文档）
    type: Mapped[str] = mapped_column(String(40))
    # 事件内容（明文 JSON），原样发给前端。比如 stage 事件是 {"index": 2, "status": "done", "message": "…"}
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 事件发生的时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMCall(Base):
    """每次大模型调用的用量，用来核对“每次分析约 16 次调用、约 1 美分”。

    只记数量，不记内容：提示词和模型的回答都不存。
    加密：不加密。
    谁写：llm/base.py 的 LLMService._record()，每次调用（成功或失败）一行，用它自己的会话立刻提交。
          没配大模型（offline）时不会有记录。
    谁读：GET /api/analyses/{id}/llm-usage（api/analyses.py）。
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 哪个课题组发起的；调用方没有给时为空。注意这一列和下一列都不是外键
    org_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 属于哪一次分析；和分析无关的调用（比如新建分析页解析一句话要求）为空
    analysis_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 哪个模块发起的调用
    module: Mapped[str] = mapped_column(String(10))  # M2 / M11 / M12
    # 厂商适配器的名字："anthropic" 或 "openai_compatible"
    provider: Mapped[str] = mapped_column(String(40))
    # 模型的型号（成功时是接口返回的型号，失败时是配置的型号）
    model: Mapped[str] = mapped_column(String(100))
    # 输入、输出各用了多少 token；调用失败时记 0
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # 这次调用花了多少毫秒；失败时记 0
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # 调用是否成功
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    # 失败时的错误说明，最多 300 个字符；成功时为空
    error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 调用的时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- M4 库存


class InventoryItem(Base):
    """实验室库存里的一条：一种试剂或原料（R10 库存表的一行）。

    加密：名称、CAS 号、结构、数量、位置、备注都在加密的 payload 里。
          明文的只有“属于谁、谁可见、从哪来”，以及结构的盲索引 structure_index。
    按结构查找：不解密整张表，而是比对盲索引（见 security.Cipher.blind_index）。
          按名称、CAS 号、子结构搜索则是把这个组的库存全部解密后在内存里筛。
    谁写：m04_availability.py 的 InventoryService.upsert()——E3 手动添加、E1/E2 导入、B12 标记为我有、
          命令行 seed-demo 都走它；另外 PATCH /api/inventory/items/{id} 改内容，
          POST /api/inventory/share 改可见范围（E5）。
    谁删：DELETE /api/inventory/items/{id}。
    谁读：库存页的接口（api/inventory.py）；流水线第 2、3 阶段（哪些原料库存里有）。
    """

    __tablename__ = "inventory_items"
    # 组合索引：按“组织 + 结构盲索引”查某个结构在不在库存里时用它
    __table_args__ = (Index("ix_inventory_org_structure", "org_id", "structure_index"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 谁加进来的
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 可见范围。team：组里所有人都看得到；private：只有 owner_id 这个人看得到
    visibility: Mapped[str] = mapped_column(String(10), default="team")  # team / private
    # 结构的盲索引：HMAC(组织 + InChIKey 前 14 位)，没有结构的条目（混合物）为空
    # 64 个十六进制字符。同一个组里同一个结构（不分立体异构）算出来的值相同，所以能用等号查
    structure_index: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 名称、CAS、SMILES、数量、位置、备注
    # 【加密】键是 name、cas、smiles（规范写法，没有结构时为 null）、quantity、location、note。
    # 数量和位置是随手填的文字（比如 “500 mL”“试剂柜 A3”），后端不解析
    payload: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # 这一条是怎么来的。import：表格导入；manual：手动添加；marked_owned：在结果页点“标记为我有”
    source: Mapped[str] = mapped_column(String(20), default="manual")  # import / manual / marked_owned
    # 表格导入的，记是哪一次导入（inventory_imports.id，不是外键）；其他来源为空。现在的代码只写不读
    import_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 第一次加进库存的时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 最后一次修改的时间；库存列表按它从新到旧排
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InventoryImport(Base):
    """一次库存表格导入（E1）：用户上传一个 Excel / CSV 文件，这里记这次导入的整体进度。

    表格里的每一行另存在 inventory_import_rows 里。上传的文件本身不保存。
    加密：没有加密列（文件名是明文）。
    谁写：inventory_import.py 的 create_import()（建）、process_import()（进度）、run_import()（失败时）。
    谁读：GET /api/inventory/imports/{id}（inventory_import.import_view）。
    """

    __tablename__ = "inventory_imports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 谁上传的。自动匹配进库存的条目算在他名下（需要确认的行，算在点确认的那个人名下）
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 上传的文件名
    filename: Mapped[str] = mapped_column(String(300))
    # 处理到哪了。行数少的表格在上传的请求里当场处理完；行数多的进任务队列，前端轮询这一列
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued / running / done / failed
    # 表格里一共有多少数据行（空行不算）
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    # 已经识别完多少行，进度条用
    processed_rows: Mapped[int] = mapped_column(Integer, default=0)
    # 失败原因（“异常类型: 说明”，最多 1000 个字符）；没有失败时为空
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 上传时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InventoryImportRow(Base):
    """导入表格里的一行，以及系统对它的识别结果（R10 的“已匹配 / 需确认 / 未识别”三栏）。

    加密：原始内容和识别结果都在加密的 payload 里。
    谁写：inventory_import.py 的 create_import()（建，bucket 是 pending）、process_import()（识别后分栏）、
          confirm_row()（E2 用户确认、改结构或忽略）。
    谁读：inventory_import.import_view()、row_view()。
    """

    __tablename__ = "inventory_import_rows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪一次导入
    import_id: Mapped[str] = mapped_column(ForeignKey("inventory_imports.id"), index=True)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 这一行在原表格里是第几行（从 1 数起，表头也算一行），方便用户回去对照
    row_number: Mapped[int] = mapped_column(Integer)
    # matched / needs_confirmation / unrecognized，以及处理后的 confirmed / ignored
    # 这一行现在在哪一栏。刚建好、还没识别时是 pending；识别后是上面注释里的前三种之一：
    #   matched 已经直接进了库存；needs_confirmation 系统有个猜测，等用户确认；unrecognized 认不出来。
    # 用户处理（E2）之后：confirmed 确认或改了结构，已进库存；ignored 这一行不要了
    bucket: Mapped[str] = mapped_column(String(24), index=True)
    # 【加密】{"raw": 表格里的原样（name、cas、smiles、quantity、location、note）,
    #          "proposal": 识别结果（status、name、cas、smiles、reason、source、notes），还没识别时为 null}
    payload: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # 这一行变成了库存里的哪一条（inventory_items.id，不是外键）；没有进库存时为空
    item_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------- M13 报告、M14 回填


class Report(Base):
    """生成过的报告和对比表，就是“项目历史”里的下载记录。

    这张表只记“有这么一个文件”；文件内容加密后存在 var/files/<storage_name>（见 storage.py）。
    加密：表里没有加密列（文件名、选项是明文）；文件本身是加密的。
    谁写：api/reports.py 的 _store_report()——F3 生成报告、D4 导出对比表时各建一行。
    谁读：GET /api/reports/{id}/download、GET /api/projects/{id}/history。
    现在没有删除报告的接口。
    """

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组（下载时核对）
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 属于哪个项目（项目历史按它列）
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    # 根据哪一次分析生成的
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    # 种类。report：一条路线的报告（F）；comparison：几条路线的对比表（D4）
    kind: Mapped[str] = mapped_column(String(20))  # report / comparison
    # 用户选的格式。注意 sdf 实际是一个 zip 包（route.sdf 加上每一步的 .rxn）
    format: Mapped[str] = mapped_column(String(10))  # pdf / docx / sdf / xlsx / csv
    # 下载时显示的文件名，可以有中文
    filename: Mapped[str] = mapped_column(String(300))
    # 生成时的选项（明文 JSON）。报告：{route_id, sections, target_mg}；对比表：{route_ids, weights}。
    # 现在的代码只写不读
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 文件在 var/files 下的随机文件名（storage.save_file 返回的）
    storage_name: Mapped[str] = mapped_column(String(100))
    # 文件大小（加密之前的字节数）
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 谁生成的
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # 生成时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExperimentRecord(Base):
    """G1–G4：某一步实际做出来的结果。只在本组织可见；授权后才进入校准和训练数据。

    同一步可以记多次（每次保存新建一行）；路线树上显示的是最近一次保存或修改的那条。
    加密：产率、条件变化、备注和当时的评估快照都在加密的 payload 里；
          明文的是“哪一步、结果是成功还是失败、是否授权”。
    谁写：m14_backfill.py 的 save_record()（G4 保存）、api/reports.py 的 update_experiment()（修改、改授权）。
    谁读：GET /api/analyses/{id}/experiments；m14_backfill.py 的 consented_dataset()（只取已授权的）。
    """

    __tablename__ = "experiment_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪个课题组
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 哪一次分析
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    # 哪条路线，结果里的路线编号，比如 "r1"
    route_id: Mapped[str] = mapped_column(String(20))
    # 哪一步，结果里 reactions 的键（16 个十六进制字符）。同一步出现在几条路线里时，编号相同
    step_key: Mapped[str] = mapped_column(String(64), index=True)
    # 这一步在那条路线里是第几步（从 1 数起）
    step_index: Mapped[int] = mapped_column(Integer)
    # 谁记的
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    # G1 结果
    outcome: Mapped[str] = mapped_column(String(20))  # success / partial / failure
    # G3 是否授权用于改进模型。默认不授权，可以随时改。只有 True 的记录才进入校准统计和导出的数据集
    consent_for_training: Mapped[bool] = mapped_column(Boolean, default=False)
    # 最近一次设置授权的时间（新建时也会写）
    consent_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 实际产率、条件变化、备注，以及当时这一步的反应和系统给的评估快照
    # 【加密】键是 step_name、actual_yield（百分数，可以为 null）、condition_changes、notes，
    # 以及 snapshot：{reaction_smiles, step_score, dimensions, evidence_level, reported_yield_median,
    # operation_tags}。snapshot 是保存那一刻系统对这一步的判断，校准时拿来和实际结果对比
    payload: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    # 保存时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 最后一次修改的时间
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExperimentAttachment(Base):
    """TLC、NMR 照片或谱图。文件本身加密后存在 var/files 下。

    只接受图片和 PDF（api/reports.py 检查上传文件的 content_type）。
    加密：表里没有加密列（文件名是明文）；文件本身是加密的。
    谁写：POST /api/experiments/{id}/attachments（api/reports.py 的 add_attachment，G2）。
    谁读：GET /api/experiments/{id}/attachments、GET /api/experiments/attachments/{id}（下载）。
    """

    __tablename__ = "experiment_attachments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 属于哪一条实验记录
    record_id: Mapped[str] = mapped_column(ForeignKey("experiment_records.id"), index=True)
    # 属于哪个课题组（下载时核对）
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 上传时的文件名（文件名里不允许的字符已经换成下划线，见 m13_reports.safe_filename）
    filename: Mapped[str] = mapped_column(String(300))
    # 文件类型（浏览器上传时报的 Content-Type，比如 image/png），下载时原样返回
    content_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    # 文件在 var/files 下的随机文件名
    storage_name: Mapped[str] = mapped_column(String(100))
    # 文件大小（加密之前的字节数）
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    # 上传时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CalibrationRun(Base):
    """M14 定期校准的结果。只统计已授权的回填记录，不含组织和人员信息。

    每运行一次校准（python -m syntherisk.cli calibrate）加一行，留作历史。
    加密：不加密（里面只有汇总的统计数字）。
    谁写：m14_backfill.py 的 run_calibration()。
    谁读：现在没有接口读它；命令行在运行校准后把这次的 stats 打印出来。
    """

    __tablename__ = "calibration_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    # 这次统计用了多少条已授权的记录
    n_records: Mapped[int] = mapped_column(Integer, default=0)
    # 统计结果（明文 JSON）：各种实验结果的条数、按结果分组的平均步骤分、按证据等级和操作标签分组的结果、
    # 各维度的平均分、实际产率和文献产率的平均偏差等。键的清单见 m14_backfill.run_calibration()
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 运行校准的时间
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- 公开参考数据


class ReactionPrecedent(Base):
    """M5 的先例库：USPTO 专利反应、Open Reaction Database 等公开数据。

    一行是一条已经报道过的反应。全量 USPTO 约 175 万行。这是公开数据，不属于任何课题组，没有 org_id。
    加密：不加密。
    谁写：loaders.py 的 load_precedents()（命令行 load-precedents 批量导入；带 --replace 时先删掉同一来源的旧数据）。
          每一列的值由 m05_precedents.py 的 precedent_values() 从反应 SMILES 算出来。
    谁读：m05_precedents.py——PrecedentIndex 把 id、两种指纹和反应中心读进内存做检索，
          PrecedentSearch 再按 reaction_key 或 id 取出命中的整行。
    """

    __tablename__ = "reaction_precedents"

    # 自增编号。内存索引里用它指回数据库的行
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 数据来源，比如 "uspto"、"ord"。没在登记表里登记并允许使用的来源导不进来
    source: Mapped[str] = mapped_column(String(40), index=True)  # data_sources.toml 里登记的 id
    # 文献类型，决定证据卡上显示“专利 / 期刊论文 / 数据集”，以及原文链接怎么拼
    doc_type: Mapped[str] = mapped_column(String(20), default="patent")  # patent / journal / dataset
    # 文献编号。原始数据没给时，导入程序用 DOI 或者 “来源-行号” 顶替
    document_id: Mapped[str] = mapped_column(String(100))  # 专利号等
    # 年份，原始数据没有则为空
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # DOI，有的话原文链接指向 https://doi.org/…
    doi: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 文献题目，原始数据没有则为空
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 整理后的反应 SMILES：“反应物>试剂>产物”，去掉了原子映射号，产物只留主产物
    reaction_smiles: Mapped[str] = mapped_column(Text)
    # 反应的“身份”：反应物（各取最大片段的 key14，排序）和产物的 key14 拼起来算的 SHA-1，40 个十六进制字符。
    # 不看试剂，也不分立体异构。流水线里的一步和它相同，证据等级就是 E1（文献直接报道）
    reaction_key: Mapped[str] = mapped_column(String(40), index=True)  # 产物 + 反应物，判断“同一反应”
    # 主产物的 key14（盐取最大的片段）。导入时写入，现在的检索没有用到这一列
    product_key: Mapped[str] = mapped_column(String(14), index=True)
    # 文献报道的产率（百分数）；没有记录则为空
    yield_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 这条先例用的试剂。流水线会把最好的先例的试剂借给没有试剂的步骤
    agents: Mapped[str | None] = mapped_column(Text, nullable=True)  # 试剂、溶剂、催化剂的 SMILES，用 . 连接
    # 条件说明的文字；USPTO 的 .rsmi 文件里没有这一项，CSV 来源可以带
    conditions_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 反应温度，摄氏度；没有则为空
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 下面两种指纹是按位打包的字节串，检索时整库读进内存，用 numpy 批量算相似度。
    # fp_diff 长 128 字节（1024 位），fp_product 长 256 字节（2048 位）；定义在 m01_structure.py
    fp_diff: Mapped[bytes] = mapped_column(LargeBinary)  # 反应差异指纹：这是哪类反应
    fp_product: Mapped[bytes] = mapped_column(LargeBinary)  # 产物指纹：底物像不像
    # 反应中心：按原子映射算出来的“改了哪些键”（m01_structure.reaction_center）。原始数据不带映射时为空
    # 比如 "F:C.acyl~N:1|L:C.acyl~Cl:1"。这一列是后来才加的，旧数据库由 db.py 的 _add_missing_columns() 补上
    center: Mapped[str | None] = mapped_column(Text, nullable=True)


class LibraryVersion(Base):
    """先例库每导入一次就换一个版本号。后端进程靠它知道内存里的索引过时了
    （重新导入同一个文件，行数和编号都不变，光看行数看不出来）。

    加密：不加密。
    谁写：m05_precedents.py 的 mark_library_changed()，由 loaders.load_precedents() 在导入结束时调用。
    谁读：m05_precedents.py 的 PrecedentIndex.refresh()：每次分析开始检索先例之前比一下行数和版本号，
          有变化就重新加载索引。所以导入数据以后不用重启后端。
    """

    __tablename__ = "library_versions"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)  # 目前只有 "precedents"
    # 版本号：一个随机串，每次导入换一个新的。只用来比“变没变”，本身没有顺序含义
    version: Mapped[str] = mapped_column(String(32), default=new_id)
    # 最近一次导入的时间
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CatalogCompound(Base):
    """M4 的在售目录：ZINC 在售子集，加上 data/compounds.csv 里的常用试剂。

    一行是一个“能买到”的化合物。判断原料可得性时，按 key14 查它在不在这张表里。
    公开数据，不属于任何课题组。
    加密：不加密。
    谁写：db.py 的 init_db()（表为空时写入内置试剂，source 是 "seed"）；
          loaders.py 的 load_catalog()（命令行 load-catalog，source 是命令行给的来源编号，已有的跳过）。
    谁读：m04_availability.py 的 AvailabilityChecker.preload()；loaders.summarize()（统计各来源的个数）。
    """

    __tablename__ = "catalog_compounds"

    # 结构的 key14（InChIKey 前 14 位：只看原子怎么连，不分立体异构）。拿它当主键，所以同一个结构只有一行
    key14: Mapped[str] = mapped_column(String(14), primary_key=True)
    # SMILES；只给了 InChIKey 的清单导进来时为空
    smiles: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 名称；只有内置试剂表来的那些行有
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # 这一行是从哪来的："seed"（内置试剂表）或者导入时给的来源编号（比如 "zinc"）
    source: Mapped[str] = mapped_column(String(40), default="seed")


class SubstanceHazard(Base):
    """从 PubChem 查到的 GHS 分类缓存，避免每次分析重复请求。

    内置试剂表里没有危害分类的物质才会去查 PubChem；查完不管有没有查到分类都记一行，下次直接用
    （请求失败的那次不记）。
    公开数据，不属于任何课题组，所有课题组共用。
    加密：不加密。
    谁写、谁读：都是 m06_safety.py 的 SafetyService._from_pubchem()（流水线第 5 阶段）。
    没有过期机制：一旦缓存就一直用，想重新查只能删掉对应的行。
    """

    __tablename__ = "substance_hazards"

    # 结构的 key14
    key14: Mapped[str] = mapped_column(String(14), primary_key=True)
    # PubChem 上对应的化合物编号（CID）；PubChem 没有这个物质时为空。现在的代码只写不读
    cid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # PubChem 上的名称（英文）。分析结果里没有名字的中间体会借用它
    name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # GHS 的 H 编码列表，比如 ["H225", "H319"]。空列表配合 found=True 表示“查到了，不属于危险品”
    h_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 有没有查到分类数据。和 h_codes 一起区分“不属于危险品”和“没有数据”
    found: Mapped[bool] = mapped_column(Boolean, default=True)  # False：PubChem 上没有它的分类数据
    # 查询的时间。现在的代码只写不读
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
