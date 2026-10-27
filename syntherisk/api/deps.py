"""接口共用的依赖：数据库会话、当前登录的人、按组织取数据。

权限的基本规则只有一条：所有客户数据都属于某个组织，接口只返回当前登录者所在组织的数据。

对应模块：M0 应用服务里“权限”的那一部分。它不对应界面上的某个按钮：每个需要登录的接口都经过这里。
（“组织”就是界面上说的课题组，对应 organizations 表。）

“依赖”是 FastAPI 的写法。接口函数的参数写成

    db: Session = Depends(get_db)

FastAPI 就会在调用接口函数之前先调用 get_db()，把结果当作参数 db 传进去。依赖还可以再依赖别的依赖
（get_principal 依赖 get_db 和 get_ctx）。同一个请求里，同一个依赖只执行一次、结果共用，
所以 get_principal 里用的 db 和接口函数自己拿到的 db 是同一个数据库会话。

谁调用它：
    api/core.py、structures.py、analyses.py、inventory.py、reports.py 里的接口函数。
它依赖谁：
    syntherisk/context.py   AppContext：配置、数据库会话工厂等，启动时由 main.py 放进 app.state.ctx
    syntherisk/security.py  decode_access_token：校验登录令牌
    syntherisk/models.py    User、Membership、Organization、Project、Analysis 这几张表
数据从哪来到哪去：
    请求头里的令牌 → authenticate() 查 users / memberships / organizations 三张表
    → Principal（是谁、哪个组织、什么角色）
    → 接口用 principal.org_id 过滤数据，或者用 load_project / load_analysis 取一条属于本组织的记录。

本文件里有什么：
    _bearer                 从请求头里读 “Authorization: Bearer <令牌>” 的工具
    get_ctx                 取运行时上下文 AppContext
    get_db                  开一个数据库会话，请求结束时关闭
    Principal               当前登录的人：用户、所在组织、角色
    authenticate            令牌 → Principal；不合格一律 401
    require_agreement       要求已经同意当前版本的内测协议；没同意 403
    get_signed_in           接口用的依赖：只要求登录（内测协议的几个接口和 /api/auth/me 用）
    get_principal           接口用的依赖：要求登录，并且已经同意当前版本的内测协议
    require_admin           接口用的依赖：要求是课题组管理员
    _owned                  按主键取一行，并确认它属于当前组织
    load_project            取本组织的一个项目
    load_analysis           取本组织的一次分析
    load_scored_analysis    取本组织的一次分析，并要求已经评完分（结果页上的操作用）
    load_finished_analysis  取本组织的一次分析，并要求整个跑完（会改写结果的操作用）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from syntherisk import i18n, models
from syntherisk.context import AppContext
from syntherisk.security import decode_access_token

# HTTPBearer 是 FastAPI 自带的工具：从请求头里读 “Authorization: Bearer <令牌>”，
# 并且让接口文档页面（/docs）出现填令牌用的 “Authorize” 按钮。
# auto_error=False：请求头缺失或者不是 Bearer 格式时，它不自己报错，而是交回 None，
# 由下面的 authenticate() 统一返回 401 和中文提示。
_bearer = HTTPBearer(auto_error=False)


def get_ctx(request: Request) -> AppContext:
    """取运行时上下文：配置、数据库会话工厂、参考数据、任务队列、大模型等都挂在这一个对象上。

    参数：
        request  FastAPI 传进来的当前请求。
    返回：
        AppContext。应用启动时由 syntherisk/main.py 的 lifespan 建好，放在 app.state.ctx 上；
        整个进程只有这一份，所有请求共用。
    谁在用：
        作为依赖（Depends(get_ctx)）被本文件的 get_db、get_principal 和各个接口函数使用。
    """
    return request.app.state.ctx


def get_db(ctx: AppContext = Depends(get_ctx)) -> Iterator[Session]:
    """接口里写了数据要自己 db.commit()；这里只负责出错时回滚和最后关闭。

    这是一个“带 yield 的依赖”：yield 之前的代码在接口函数运行之前执行，yield 出去的会话就是
    接口函数拿到的 db，yield 之后的代码在接口函数结束以后执行。

    参数：
        ctx  运行时上下文，由 get_ctx 提供。
    产出：
        一个 SQLAlchemy 数据库会话（Session），一次请求用一个。会话工厂的两个设置
        （syntherisk/db.py 的 make_session_factory）会影响接口代码的写法：
            expire_on_commit=False  commit 以后，对象上的值还能继续读，不会重新查库；
            autoflush=False         查询之前不会自动把还没提交的新增和改动先发给数据库。
                                    需要它们先发过去的时候（比如要用新行的 id，id 是插入时才生成的），
                                    得自己调 db.flush()。
    副作用：
        接口函数抛出异常时，回滚这个会话里还没提交的改动；不论成功失败，最后都关闭会话。
        不会自动提交：接口函数忘了 db.commit()，改动就丢了。
    谁在用：
        作为依赖被 get_principal 和绝大多数接口函数使用。
        进度推送接口（analyses.stream_events）不用它：那个接口的连接会开很久，
        它每次查数据库时自己现开现关一个会话。
    """
    session = ctx.session_factory()
    try:
        # 把会话交给接口函数。接口函数运行期间，这个函数就停在这一行
        yield session
    except Exception:
        # 接口函数里抛了异常：撤销还没提交的改动，再把异常原样抛出去，让 FastAPI 去生成错误响应
        session.rollback()
        raise
    finally:
        # 成功也好、失败也好，都要关闭会话，把数据库连接还回连接池
        session.close()


@dataclass
class Principal:
    """当前登录的人：是谁、在哪个组织（课题组）、是什么角色。每个需要登录的接口都会拿到一个。

    属性：
        user  users 表里他的那一行（models.User）。
        org   他所在的组织，organizations 表里的一行（models.Organization）。
        role  他在这个组织里的角色，来自 memberships 表：
              owner（建这个课题组的人）、admin（管理员）、member（普通成员）。
    由 authenticate() 创建。接口里最常用的是 principal.org_id（按组织过滤数据）
    和 principal.user_id（记下是谁做的）。
    """

    user: models.User
    org: models.Organization
    role: str

    @property
    def user_id(self) -> str:
        """当前用户的编号（users.id，32 位十六进制字符串）。"""
        return self.user.id

    @property
    def org_id(self) -> str:
        """当前组织的编号（organizations.id）。查客户数据时都要带上它，这就是“按组织隔离”。"""
        return self.org.id

    @property
    def is_admin(self) -> bool:
        """是不是课题组管理员：角色是 owner 或 admin 时为 True。require_admin 靠它判断。"""
        return self.role in ("owner", "admin")


def authenticate(session: Session, ctx: AppContext, token: str | None) -> Principal:
    """检查登录令牌，认出当前登录的人。

    参数：
        session  数据库会话，用来查用户和成员关系。
        ctx      运行时上下文，这里只用到 ctx.settings.secret_key（给令牌签名的密钥）。
        token    登录接口发的 access_token（JWT）；请求里没带令牌时传 None。
    返回：
        Principal（用户、组织、角色）。
    抛出：
        HTTPException 401 “请先登录”，响应头带 WWW-Authenticate: Bearer。出现在这几种情况：
        没带令牌；令牌是伪造的或者已经过期；用户不存在或者被停用（is_active 为 False）；
        用户已经不是令牌里写的那个组织的成员。
    副作用：
        只读数据库（users、memberships、organizations 三张表），不写。
    谁在用：
        本文件的 get_principal（普通接口）；
        api/analyses.py 的 stream_events（进度推送接口，令牌可以放在网址里，所以它自己调）。
    """
    # 先把要抛的 401 准备好，下面两处都用它
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, i18n.tr("请先登录", "Please sign in"), headers={"WWW-Authenticate": "Bearer"})
    # 解开令牌。签名不对或者已经过期时 decode_access_token 返回 None。
    # 解出来的 claims 是个字典：sub 是用户编号，org 是组织编号（见 security.create_access_token）
    claims = decode_access_token(token, ctx.settings.secret_key) if token else None
    if not claims:
        raise unauthorized
    # session.get(模型, 主键)：按主键取一行，没有就是 None
    user = session.get(models.User, claims.get("sub"))
    # 查成员关系：令牌里的这个人，现在还是不是令牌里那个组织的成员。
    # session.scalars(查询) 把结果的每一行直接变成模型对象；.first() 取第一个，没有就是 None
    membership = session.scalars(
        select(models.Membership).where(
            models.Membership.user_id == claims.get("sub"), models.Membership.org_id == claims.get("org")
        )
    ).first()
    if user is None or not user.is_active or membership is None:
        raise unauthorized
    # 角色不写在令牌里，每次请求都从 memberships 表现读
    return Principal(user=user, org=session.get(models.Organization, membership.org_id), role=membership.role)


def require_agreement(session: Session, ctx: AppContext, principal: Principal) -> None:
    """要求这个人已经同意了当前版本的内测协议（data/agreements/beta.toml 的 version）。

    参数：
        session    数据库会话。
        ctx        运行时上下文，用到 ctx.reference.agreement.version。
        principal  当前登录的人。
    抛出：
        HTTPException 403，detail 是 {"code": "agreement_required", "message": "请先阅读并同意内测协议", "version": 当前版本号}。
        前端看到这个 code 就弹出协议（见 docs/前端对接手册.md）。
    副作用：
        只读 agreement_acceptances 表。
    谁在用：
        本文件的 get_principal；api/analyses.py 的 stream_events（进度推送自己认令牌）。
    """
    version = ctx.reference.agreement.version
    accepted = session.scalar(
        select(models.AgreementAcceptance.id)
        .where(models.AgreementAcceptance.user_id == principal.user_id, models.AgreementAcceptance.version == version)
        .limit(1)
    )
    if accepted is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {
            "code": "agreement_required",
            "message": i18n.tr("请先阅读并同意内测协议", "Please read and accept the beta agreement first"),
            "version": version,
        })


def get_signed_in(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
) -> Principal:
    """接口用的依赖：只要求登录，不管有没有同意内测协议。返回当前登录的人。

    只有还没同意协议的人也得能用的接口才用它：GET /api/auth/me 和 api/agreement.py 里的几个接口
    （读协议、同意协议、下载协议全文）。其余接口都用 get_principal。

    参数（都由 FastAPI 自动提供）：
        credentials  _bearer 从请求头 Authorization 里读出来的令牌；没带或者格式不对时是 None。
        db           数据库会话。
        ctx          运行时上下文。
    返回：
        Principal。
    抛出：
        HTTPException 401（见 authenticate）。
    """
    # credentials.credentials 就是请求头里 “Bearer ” 后面的那一串令牌
    return authenticate(db, ctx, credentials.credentials if credentials else None)


def get_principal(
    principal: Principal = Depends(get_signed_in),
    db: Session = Depends(get_db),
    ctx: AppContext = Depends(get_ctx),
) -> Principal:
    """接口用的依赖：要求登录，并且已经同意当前版本的内测协议，返回当前登录的人。

    接口函数的参数里写 principal: Principal = Depends(get_principal)，就表示“这个接口要登录”。
    只要求登录、但用不到“这个人是谁”的接口，把参数名写成下划线：_: Principal = Depends(get_principal)。

    参数（都由 FastAPI 自动提供）：
        principal  get_signed_in 认出来的人（同一个请求里 get_db、get_ctx 只执行一次，这里拿到的是同一个会话）。
        db         数据库会话。
        ctx        运行时上下文。
    返回：
        Principal。
    抛出：
        HTTPException 401（见 authenticate）；403 还没同意当前版本的内测协议（见 require_agreement）。
    谁在用：
        api/ 下除注册、登录、进度推送、内测协议、/api/auth/me 以外的所有接口；本文件的 require_admin。
    """
    require_agreement(db, ctx, principal)
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    """接口用的依赖：要求登录，并且是课题组管理员（角色是 owner 或 admin）。

    参数：
        principal  当前登录的人，由 get_principal 提供（所以没登录时先在那里返回 401）。
    返回：
        同一个 Principal。
    抛出：
        HTTPException 403 “只有课题组管理员可以做这个操作”。
    谁在用：
        api/core.py 的 add_member（管理员给组里的同学开账号）。
    """
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, i18n.tr("只有课题组管理员可以做这个操作", "Only group admins can do this"))
    return principal


def _owned(session: Session, model, object_id: str, org_id: str, what: str):
    """按主键取一行，并确认它属于当前组织；不属于就当作不存在。

    参数：
        session    数据库会话。
        model      要查的表对应的模型类，表里要有 org_id 这一列
                   （现在用到的是 models.Project 和 models.Analysis）。
        object_id  主键，也就是网址里的 project_id、analysis_id。
        org_id     当前登录者所在组织的编号。
        what       报错信息里用的名字，比如“项目”“分析”。调用处用 i18n.tr("项目", "Project") 传进来，
                   英文的报错里才会是它的英文。
    返回：
        那一行（模型对象）。
    抛出：
        HTTPException 404 “没有这个{what}”：编号不存在，或者是别的组织的数据。
    谁在用：
        本文件的 load_project、load_analysis。
    """
    row = session.get(model, object_id)
    if row is None or row.org_id != org_id:
        # 别的组织的数据一律当作不存在，不透露“有但你看不了”
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这个{what}", "{what} not found", what=what))
    if isinstance(row, models.Analysis) and isinstance(row.result, dict):
        # 把这次分析自己的“中文 → 英文”对照表接进当前请求：返回英文时要查它；
        # 重新评分时新拼出来的句子也会记进去，随结果一起保存（见 i18n.add_memory）
        i18n.add_memory(row.result.setdefault("_en", {}))
    return row


def load_project(session: Session, principal: Principal, project_id: str) -> models.Project:
    """取本组织的一个项目。

    参数：
        session     数据库会话。
        principal   当前登录的人。
        project_id  项目编号（projects.id）。
    返回：
        models.Project。
    抛出：
        HTTPException 404 “没有这个项目”（编号不存在，或者是别的组织的）。
    谁在用：
        api/core.py 的 project_history、create_draft、save_draft；api/analyses.py 的 create_analysis。
    """
    return _owned(session, models.Project, project_id, principal.org_id, i18n.tr("项目", "Project"))


def load_analysis(session: Session, principal: Principal, analysis_id: str) -> models.Analysis:
    """取本组织的一次分析，不管它跑到了哪一步。

    参数：
        session      数据库会话。
        principal    当前登录的人。
        analysis_id  分析编号（analyses.id）。
    返回：
        models.Analysis。它的 target、constraints、user_exclusions、result 几列在数据库里是加密的，
        这里读出来的已经是解密后的字典。
    抛出：
        HTTPException 404 “没有这个分析”（编号不存在，或者是别的组织的）。
    谁在用：
        api/analyses.py 的 get_analysis、cancel_analysis、retry_analysis、stream_events、
        get_preliminary、llm_usage；api/reports.py 的 list_experiments；
        本文件的 load_scored_analysis。
    """
    return _owned(session, models.Analysis, analysis_id, principal.org_id, i18n.tr("分析", "Analysis"))


def load_scored_analysis(session: Session, principal: Principal, analysis_id: str) -> models.Analysis:
    """结果页上的操作都要等评分完成（第 7 阶段）之后才能做。

    参数：
        同 load_analysis。
    返回：
        models.Analysis，它的 result 里已经有每条路线的分数、名次和排除原因。
    抛出：
        HTTPException 404 “没有这个分析”；
        HTTPException 409 “这次分析还没有评分结果”：result["stage"] 既不是 scored 也不是 final。
    说明：
        result["stage"] 是流水线写在结果里的进度标记（syntherisk/pipeline.py）：
            preliminary  第 2 阶段搜索完成，只有初步路线树；
            scored       第 7 阶段评分完成，结果页可以打开；
            final        第 8 阶段说明也写完了（一条路线都没搜到时，也直接标成 final）。
    谁在用：
        api/analyses.py 的 get_result、relax_constraints、get_route、get_step_card、
        get_step_precedents、ask_about_step；
        api/reports.py 的 compare、export_comparison、create_report、preview_materials；
        本文件的 load_finished_analysis。
    """
    analysis = load_analysis(session, principal, analysis_id)
    # 还没跑出结果的分析（第 2 阶段搜索完成之前）result 是 None，这里当成空字典处理
    result = analysis.result or {}
    if result.get("stage") not in ("scored", "final"):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("这次分析还没有评分结果", "This analysis has no scored results yet"))
    return analysis


def load_finished_analysis(session: Session, principal: Principal, analysis_id: str) -> models.Analysis:
    """会改写结果的操作（排除分支、标记为我有、回填）要等分析跑完，免得和后台任务互相覆盖。

    参数：
        同 load_analysis。
    返回：
        models.Analysis，已经评完分，并且状态是 succeeded（后台任务已经正常结束）。
    抛出：
        HTTPException 404 “没有这个分析”；
        HTTPException 409 “这次分析还没有评分结果”（见 load_scored_analysis）；
        HTTPException 409 “分析还在进行中，等它完成后再操作”：analysis.status 不是 succeeded
        （排队中、运行中是这一句，失败、已取消的分析也是这一句）。
    谁在用：
        api/analyses.py 的 add_exclusion、remove_exclusion、mark_owned；
        api/reports.py 的 record_experiment。
    """
    analysis = load_scored_analysis(session, principal, analysis_id)
    # status 的五种取值：queued 排队中、running 运行中、succeeded 成功、failed 失败、cancelled 已取消
    if analysis.status != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("分析还在进行中，等它完成后再操作", "The analysis is still in progress; try again once it has finished"))
    return analysis
