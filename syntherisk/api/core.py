"""M0：账号、课题组、项目、草稿、预设，以及前端启动时要读的配置。

对应按钮：
    A6   下一步（自动保存草稿）        PUT  /api/drafts/{id}
    A15  团队预设                      GET / POST /api/presets?kind=constraints
    D2   预设（权重）                  GET  /api/presets?kind=weights
    D3   保存为团队预设                POST /api/presets
    B1 B8 B9 D1 前端自己重算要用的公式  GET  /api/scoring/config

本文件的全部接口（main.py 挂载时统一加了前缀 /api；除注册、登录以外都要登录）：
    账号   POST   /api/auth/register           注册，同时建课题组和一个“默认项目”
           POST   /api/auth/login              登录，换取 access_token
           GET    /api/auth/me                 当前登录的是谁
           GET    /api/org/members             本课题组的成员
           POST   /api/org/members             管理员给同学开账号
    项目   GET    /api/projects                本课题组的项目
           POST   /api/projects                新建项目
           GET    /api/projects/{id}/history   项目历史：分析记录、生成过的报告和对比表（R11）
    草稿   GET    /api/drafts                  我的草稿
           POST   /api/drafts                  新建草稿（A6 第一次保存）
           PUT    /api/drafts/{id}             覆盖保存（A6）
           DELETE /api/drafts/{id}             删除草稿
    预设   GET    /api/presets?kind=           本课题组的预设（A15、D2）
           POST   /api/presets                 保存预设，同名覆盖（A15、D3）
           DELETE /api/presets/{id}            删除预设
    配置   GET    /api/scoring/config          评分公式的参数（B1、B8、B9、D1）
           GET    /api/meta/options            约束页上各个选项的编号和显示文字
           GET    /api/meta/modules            各模块现在接的是什么
           GET    /api/meta/data-sources       数据来源和许可登记表

谁调用它：前端。syntherisk/main.py 的 create_app() 把本文件的 router 挂到应用上。
它依赖谁：
    api/deps.py             登录、数据库会话、按组织取项目
    api/schemas.py          请求体的格式
    api/views.py            analysis_list_item（项目历史里的分析条目）、iso（时间的格式）
    security.py             hash_password / verify_password（密码）、create_access_token（登录令牌）
    constraints.py          Constraints（检查约束预设）、STOCK_LABELS / SCALE_LABELS（选项的显示文字）
    modules/m10_scoring.py  DIMENSIONS（六个评分维度的编号）
    context.py              ctx.reference（data/ 目录下的评分配置、词表、数据来源表）、ctx.describe_modules()
数据从哪来到哪去：
    账号、项目、草稿、预设：请求体 → organizations、users、memberships、projects、drafts、presets 这几张表；
    项目历史另外只读 analyses、reports 两张表。
    配置接口不查数据库：读的是启动时从 data/*.toml 读进内存的参考数据（ctx.reference）。

本文件里有什么（按阅读顺序）：
    _token_response   登录成功后返回给前端的内容：令牌 + 用户 + 课题组 + 角色
    register、login、me、list_members、add_member            账号和成员
    _project_view     项目的 JSON 样子
    list_projects、create_project、project_history           项目
    _draft_view       草稿的 JSON 样子
    _load_draft       取“我自己的”一份草稿
    list_drafts、create_draft、save_draft、delete_draft      草稿（A6）
    _preset_view      预设的 JSON 样子
    list_presets、save_preset、delete_preset                 预设（A15、D2、D3）
    scoring_config、constraint_options、modules、data_sources  前端要读的配置
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from syntherisk import i18n, models
from syntherisk.api import schemas
from syntherisk.api.deps import Principal, get_ctx, get_db, get_principal, load_project, require_admin
from syntherisk.api.views import analysis_list_item, iso
from syntherisk.constraints import SCALE_LABELS, STOCK_LABELS, Constraints
from syntherisk.context import AppContext
from syntherisk.modules.m10_scoring import DIMENSIONS
from syntherisk.security import create_access_token, hash_password, verify_password

# 本文件的所有接口都登记在这个 router 上；main.py 用 app.include_router(router, prefix="/api") 挂进应用。
# 每个接口上的 tags=[...] 只决定它在接口文档页面（/docs）上归到哪一组。
router = APIRouter()


def _token_response(ctx: AppContext, user: models.User, org: models.Organization, role: str) -> dict[str, Any]:
    """注册或登录成功后返回给前端的内容：签发一个登录令牌，附上用户、课题组和角色。

    参数：
        ctx   运行时上下文。用到 settings.secret_key（给令牌签名的密钥）
              和 settings.access_token_minutes（令牌的有效期，单位是分钟，默认 12 小时）。
        user  登录的用户。
        org   他所在的课题组。
        role  他在这个课题组里的角色：owner / admin / member。
    返回：
        字典：
            access_token  登录令牌（JWT）。之后每个请求都放在请求头里：Authorization: Bearer <access_token>
            token_type    固定是 "bearer"
            user          {"id": 用户编号, "email": 邮箱, "display_name": 显示的名字}
            org           {"id": 课题组编号, "name": 课题组名}
            role          角色
    副作用：
        没有。令牌不存数据库：校验时只看签名和有效期，再查一次这个人还在不在组里（api/deps.py 的 authenticate）。
    谁在用：
        本文件的 register、login。
    """
    # 令牌里写着三样东西：是谁（user_id）、哪个课题组（org_id）、什么时候过期；用 secret_key 签名，别人伪造不了
    token = create_access_token(
        secret=ctx.settings.secret_key, user_id=user.id, org_id=org.id, minutes=ctx.settings.access_token_minutes
    )
    return {
        "access_token": token,
        # token_type 是 OAuth2 的习惯写法：告诉调用方这个令牌要放在请求头 “Authorization: Bearer ” 的后面
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name},
        "org": {"id": org.id, "name": org.name},
        "role": role,
    }


# ---------------------------------------------------------------- 账号


@router.post("/auth/register", status_code=status.HTTP_201_CREATED, tags=["账号"])
def register(body: schemas.RegisterRequest, db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """注册一个账号，同时建一个课题组，注册的人是组长。

    不需要登录。请求体见 `RegisterRequest`。新课题组下会自动建一个名叫“默认项目”的项目。

    成功返回 201，内容和登录接口一样，所以注册完不用再登录一次：

    - `access_token`：登录令牌，之后的请求放在请求头 `Authorization: Bearer <access_token>` 里
    - `token_type`：固定是 `bearer`
    - `user`：`{id, email, display_name}`
    - `org`：`{id, name}`
    - `role`：固定是 `owner`（组长）

    出错：409 这个邮箱已经注册过了（邮箱不分大小写）；422 邮箱格式不对、密码不足 8 位，或者没填课题组名。
    """
    # 实现：往 organizations、users、memberships、projects 四张表各写一行，一次提交。
    # 邮箱统一转成小写再存、再比较，所以 Anna@Example.com 和 anna@example.com 是同一个账号
    email = body.email.strip().lower()
    # db.scalar(查询)：只取结果第一行的第一列。这里查的是 id，查不到就是 None
    if db.scalar(select(models.User.id).where(models.User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("这个邮箱已经注册过了", "This email is already registered"))
    # 建课题组和用户。密码不存原文，存的是 scrypt 算出来的哈希值（security.hash_password）
    org = models.Organization(name=body.org_name.strip(), settings={})
    user = models.User(email=email, display_name=body.display_name.strip(), password_hash=hash_password(body.password))
    db.add_all([org, user])
    # flush：把上面两行先发给数据库（还没提交）。这一步之后 org.id 和 user.id 才有值，下面两行要用
    db.flush()
    # 成员关系：这个人是这个课题组的 owner。再建一个“默认项目”：每次分析都必须挂在某个项目下面。
    # 项目名存的是中文；i18n.tr 顺手登记它的英文，英文界面上显示成 Default project
    db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
    db.add(models.Project(org_id=org.id, name=i18n.tr("默认项目", "Default project"), created_by=user.id))
    db.commit()
    return _token_response(ctx, user, org, "owner")


@router.post("/auth/login", tags=["账号"])
def login(body: schemas.LoginRequest, db: Session = Depends(get_db), ctx: AppContext = Depends(get_ctx)):
    """登录：用邮箱和密码换取登录令牌。

    不需要登录。请求体见 `LoginRequest`。

    成功返回 200，内容同注册接口：`access_token`、`token_type`、`user`、`org`、`role`。
    令牌有有效期（默认 12 小时），过期以后接口返回 401，需要重新登录。

    出错：401 邮箱或密码不对（账号被停用时也是这个提示）；403 这个账号不属于任何课题组。
    """
    # 实现：只读 users、memberships、organizations 三张表，不写。
    # 邮箱转成小写去查。账号不存在、被停用、密码不对，三种情况返回同一句话，不透露是哪一种
    user = db.scalars(select(models.User).where(models.User.email == body.email.strip().lower())).first()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, i18n.tr("邮箱或密码不对", "Incorrect email or password"))
    # 取这个人所在的课题组（取查到的第一条成员关系）。
    # 注册和加成员都不接受已经注册过的邮箱，所以通过这些接口建的账号只会属于一个课题组
    membership = db.scalars(select(models.Membership).where(models.Membership.user_id == user.id)).first()
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, i18n.tr("这个账号不属于任何课题组", "This account does not belong to any research group"))
    return _token_response(ctx, user, db.get(models.Organization, membership.org_id), membership.role)


@router.get("/auth/me", tags=["账号"])
def me(principal: Principal = Depends(get_principal)):
    """当前登录的是谁：用户、课题组、角色。

    需要登录。带着令牌调它，可以确认这个令牌还有效，并拿到它对应的用户、课题组和角色。返回：

    - `user`：`{id, email, display_name}`
    - `org`：`{id, name, settings}`。`settings` 是课题组级别的设置，没有设置过时是空对象
    - `role`：`owner`、`admin` 或 `member`

    出错：401 没登录，或者令牌已经失效。
    """
    # 不查库：登录检查（get_principal）已经把用户和课题组读出来放在 principal 上了。
    # org.settings 现在只有一个会被后端读的键：inventory_default_visibility（新加的库存默认给谁看，
    # 见 m04_availability.InventoryService.upsert）；目前没有接口能修改 settings
    return {
        "user": {"id": principal.user.id, "email": principal.user.email, "display_name": principal.user.display_name},
        "org": {"id": principal.org.id, "name": principal.org.name, "settings": principal.org.settings or {}},
        "role": principal.role,
    }


@router.get("/org/members", tags=["账号"])
def list_members(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """本课题组的成员列表。

    需要登录，组里任何人都能看。返回一个数组，每个成员一项：`{user_id, email, display_name, role}`。
    """
    # 一次查出“成员关系 + 用户”：join 把 memberships 和 users 两张表按用户编号对上，where 只留本课题组的。
    # select 里写了两个模型，所以 db.execute(...).all() 返回的每一行是一个二元组 (Membership, User)
    rows = db.execute(
        select(models.Membership, models.User)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(models.Membership.org_id == principal.org_id)
    ).all()
    # 把每一行拆成 membership 和 user 两个变量，挑出要返回的字段；密码的哈希值不返回
    return [
        {"user_id": user.id, "email": user.email, "display_name": user.display_name, "role": membership.role}
        for membership, user in rows
    ]


@router.post("/org/members", status_code=status.HTTP_201_CREATED, tags=["账号"])
def add_member(body: schemas.MemberCreate, principal: Principal = Depends(require_admin), db: Session = Depends(get_db)):
    """管理员给组里的同学开账号。

    需要登录，并且角色是 `owner` 或 `admin`。请求体见 `MemberCreate`。新账号直接加入当前课题组，
    那位同学用这里设的邮箱和密码就能登录。

    成功返回 201：`{user_id, email, display_name, role}`。

    出错：403 不是管理员；409 这个邮箱已经注册过了；422 邮箱格式不对，或者密码不足 8 位。
    """
    # 实现：权限由依赖 require_admin 检查（不是管理员时在那里就返回 403 了）。
    # 往 users、memberships 两张表各写一行；和注册的区别是不建新课题组，而是加入管理员所在的这个
    email = body.email.strip().lower()
    if db.scalar(select(models.User.id).where(models.User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, i18n.tr("这个邮箱已经注册过了", "This email is already registered"))
    user = models.User(email=email, display_name=body.display_name.strip(), password_hash=hash_password(body.password))
    db.add(user)
    # flush 以后 user.id 才有值，建成员关系要用
    db.flush()
    db.add(models.Membership(org_id=principal.org_id, user_id=user.id, role=body.role))
    db.commit()
    return {"user_id": user.id, "email": user.email, "display_name": user.display_name, "role": body.role}


# ---------------------------------------------------------------- 项目


def _project_view(project: models.Project) -> dict[str, Any]:
    """把一个项目（projects 表的一行）整理成返回给前端的字典。

    参数：
        project  models.Project。
    返回：
        字典：
            id           项目编号
            name         项目名
            description  项目说明
            archived     是否已归档。新建时是 False，现在没有接口会改它
            created_at   创建时间，带时区的 ISO 8601 字符串
    谁在用：
        本文件的 list_projects、create_project、project_history。
    """
    return {"id": project.id, "name": project.name, "description": project.description,
            "archived": project.archived, "created_at": iso(project.created_at)}


@router.get("/projects", tags=["项目"])
def list_projects(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """本课题组的全部项目，最新建的排在前面。

    需要登录。返回一个数组，每项是 `{id, name, description, archived, created_at}`。
    """
    # where 按课题组过滤（只看得到本组的），order_by(...desc()) 按创建时间倒着排
    projects = db.scalars(
        select(models.Project).where(models.Project.org_id == principal.org_id).order_by(models.Project.created_at.desc())
    )
    return [_project_view(project) for project in projects]


@router.post("/projects", status_code=status.HTTP_201_CREATED, tags=["项目"])
def create_project(body: schemas.ProjectCreate, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """新建一个项目。

    需要登录，组里任何人都可以建。请求体见 `ProjectCreate`。
    成功返回 201：`{id, name, description, archived, created_at}`。
    """
    # 往 projects 表写一行，记下是哪个课题组的、谁建的
    project = models.Project(org_id=principal.org_id, name=body.name.strip(), description=body.description, created_by=principal.user_id)
    db.add(project)
    db.commit()
    return _project_view(project)


@router.get("/projects/{project_id}/history", tags=["项目"])
def project_history(project_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """项目历史：这个项目下的分析，以及生成过的报告和对比表（R11）。

    需要登录。返回：

    - `project`：`{id, name, description, archived, created_at}`
    - `analyses`：这个项目下的分析，最新的在前。每项：`id`、`project_id`、`status`、`target`（目标分子）、
      `route_count`（路线条数）、`best_route`（排第一的路线 `{name, score}`；还没有排名时是 `null`）、
      `created_at`、`finished_at`
    - `reports`：生成过的报告和对比表，最新的在前。每项：`id`、`analysis_id`、`kind`（`report` 报告 /
      `comparison` 对比表）、`format`、`filename`、`size_bytes`、`created_at`、`download_url`（下载地址）

    出错：404 没有这个项目（别的课题组的项目也是 404）。
    """
    # 实现：只读。load_project 顺便确认这个项目是本课题组的；然后各查一次 analyses 和 reports 表
    project = load_project(db, principal, project_id)
    # 这个项目下的分析，最新的在前
    analyses = db.scalars(
        select(models.Analysis).where(models.Analysis.project_id == project.id).order_by(models.Analysis.created_at.desc())
    )
    # 这个项目下生成过的文件（报告和对比表都记在 reports 表里），最新的在前
    reports = db.scalars(
        select(models.Report).where(models.Report.project_id == project.id).order_by(models.Report.created_at.desc())
    )
    return {
        "project": _project_view(project),
        # 每次分析只给列表上要显示的几项（views.analysis_list_item）
        "analyses": [analysis_list_item(a) for a in analyses],
        # 报告这一段的字段和 api/reports.py 的 _report_view 相同
        "reports": [
            {"id": r.id, "analysis_id": r.analysis_id, "kind": r.kind, "format": r.format, "filename": r.filename,
             "size_bytes": r.size_bytes, "created_at": iso(r.created_at), "download_url": f"/api/reports/{r.id}/download"}
            for r in reports
        ],
    }


# ---------------------------------------------------------------- 草稿（A6）


def _draft_view(draft: models.Draft) -> dict[str, Any]:
    """把一份草稿（drafts 表的一行）整理成返回给前端的字典。

    参数：
        draft  models.Draft。
    返回：
        字典：
            id          草稿编号
            project_id  属于哪个项目；没指定时是 None
            payload     草稿内容：前端存的是什么，就原样返回什么
                        （数据库里是加密的，这里拿到的已经是解密后的字典）
            updated_at  最后一次保存的时间，带时区的 ISO 8601 字符串
    谁在用：
        本文件的 list_drafts、create_draft、save_draft。
    """
    return {"id": draft.id, "project_id": draft.project_id, "payload": draft.payload, "updated_at": iso(draft.updated_at)}


def _load_draft(db: Session, principal: Principal, draft_id: str) -> models.Draft:
    """取“我自己的”一份草稿。草稿是个人的：同一个课题组的其他人也看不到、改不了。

    参数：
        db         数据库会话。
        principal  当前登录的人。
        draft_id   草稿编号（drafts.id）。
    返回：
        models.Draft。
    抛出：
        HTTPException 404 “没有这份草稿”：编号不存在、是别的课题组的，或者是同组其他人的。
    谁在用：
        本文件的 save_draft、delete_draft。
    """
    draft = db.get(models.Draft, draft_id)
    # 比项目、分析多查一项 user_id：不光要是本课题组的，还要是本人的
    if draft is None or draft.org_id != principal.org_id or draft.user_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这份草稿", "Draft not found"))
    return draft


@router.get("/drafts", tags=["草稿"])
def list_drafts(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """我的草稿，最近保存的排在前面。

    需要登录。只返回当前登录者自己的草稿。每项是 `{id, project_id, payload, updated_at}`。
    """
    drafts = db.scalars(
        select(models.Draft)
        .where(models.Draft.org_id == principal.org_id, models.Draft.user_id == principal.user_id)
        .order_by(models.Draft.updated_at.desc())
    )
    return [_draft_view(draft) for draft in drafts]


@router.post("/drafts", status_code=status.HTTP_201_CREATED, tags=["草稿"])
def create_draft(body: schemas.DraftSave, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """A6 新建一份草稿（第一次自动保存时调；之后用 `PUT /api/drafts/{id}` 覆盖）。

    需要登录。请求体见 `DraftSave`。成功返回 201：`{id, project_id, payload, updated_at}`；
    前端要记住返回的 `id`，后面覆盖保存和开始分析时都要用。

    出错：404 `project_id` 不是本课题组的项目。
    """
    if body.project_id:
        # 只是确认这个项目存在、并且是本课题组的（不是就在这里返回 404），返回值用不到
        load_project(db, principal, body.project_id)
    # payload 整个加密后存进 drafts 表（EncryptedJSON 列），后端不关心里面有什么
    draft = models.Draft(org_id=principal.org_id, user_id=principal.user_id, project_id=body.project_id, payload=body.payload)
    db.add(draft)
    db.commit()
    return _draft_view(draft)


@router.put("/drafts/{draft_id}", tags=["草稿"])
def save_draft(draft_id: str, body: schemas.DraftSave, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """A6 覆盖保存一份草稿。

    需要登录，只能改自己的草稿。请求体见 `DraftSave`：`payload` 整个替换成新的；`project_id` 给了才改，
    没给就保持原来的项目。返回保存后的 `{id, project_id, payload, updated_at}`。

    出错：404 没有这份草稿（别人的草稿也是 404），或者 `project_id` 不是本课题组的项目。
    """
    draft = _load_draft(db, principal, draft_id)
    if body.project_id:
        load_project(db, principal, body.project_id)
        draft.project_id = body.project_id
    # 整个赋一个新字典：加密列（EncryptedJSON）只有整体重新赋值才会被保存
    draft.payload = body.payload
    db.commit()
    return _draft_view(draft)


@router.delete("/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["草稿"])
def delete_draft(draft_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """删除一份草稿。

    需要登录，只能删自己的草稿。成功返回 204，没有内容。出错：404 没有这份草稿。

    用草稿开始分析时不用调这个接口：`POST /api/analyses` 带上 `draft_id`，草稿会自动删掉。
    """
    db.delete(_load_draft(db, principal, draft_id))
    db.commit()


# ---------------------------------------------------------------- 预设（A15、D2、D3）


def _preset_view(preset: models.Preset) -> dict[str, Any]:
    """把一个预设（presets 表的一行）整理成返回给前端的字典。

    参数：
        preset  models.Preset。
    返回：
        字典：
            id          预设编号
            kind        种类：constraints 约束预设 / weights 权重预设
            name        预设的名字
            payload     预设的内容：约束预设是一份完整的约束（字段见 constraints.Constraints），
                        权重预设是 {维度编号: 权重}
            is_default  是不是这一种预设里的默认项
            updated_at  最后一次保存的时间，带时区的 ISO 8601 字符串
    谁在用：
        本文件的 list_presets、save_preset。
    """
    return {"id": preset.id, "kind": preset.kind, "name": preset.name, "payload": preset.payload,
            "is_default": preset.is_default, "updated_at": iso(preset.updated_at)}


@router.get("/presets", tags=["预设"])
def list_presets(kind: str | None = None, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """本课题组的预设，按名字排序（A15 约束预设、D2 权重预设）。

    需要登录。预设是全组共用的，组里任何人都能看到。

    - 查询参数 `kind`：只要某一种，`constraints` 或 `weights`；不带就两种都返回

    返回一个数组，每项是 `{id, kind, name, payload, is_default, updated_at}`。
    """
    # 先写好“本课题组的预设”这个查询，带了 kind 再加一个条件；到 db.scalars(...) 才真正去查库
    query = select(models.Preset).where(models.Preset.org_id == principal.org_id)
    if kind:
        query = query.where(models.Preset.kind == kind)
    return [_preset_view(preset) for preset in db.scalars(query.order_by(models.Preset.name))]


@router.post("/presets", tags=["预设"])
def save_preset(body: schemas.PresetSave, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """同名预设直接覆盖。组里所有人都能用、都能改。

    A15 保存约束预设、D3 保存为团队预设（权重）。需要登录。请求体见 `PresetSave`。

    返回 200 和保存后的预设：`{id, kind, name, payload, is_default, updated_at}`。
    约束预设返回的 `payload` 是补全了默认值的完整约束。

    出错：422 约束预设的内容不符合约束的格式；或者权重预设里有不认识的维度、有负数、
    有不是数字的值、全是 0，或者是空的。
    """
    # 实现：第一步按种类检查内容并整理成要存的 payload；第二步在 presets 表里“有则改、无则加”；
    # 第三步处理默认项。
    if body.kind == "constraints":
        try:
            # 用 Constraints 模型检查一遍，再 model_dump() 变回字典：没写的字段会补上默认值。
            # 所以存进去的是一份完整的约束，前端套用预设时可以一次填好整页
            payload = Constraints.model_validate(body.payload).model_dump()
        except ValidationError as exc:
            # exc.errors() 是出错字段的列表，这里只把第一条的说明告诉前端
            raise HTTPException(422, i18n.tr("约束预设的内容不对：{msg}", "Invalid constraint preset: {msg}",
                                             msg=exc.errors()[0]["msg"])) from exc
    else:
        # 权重预设。unknown：不在六个维度（DIMENSIONS）里的键，用集合相减求出来
        unknown = set(body.payload) - set(DIMENSIONS)
        values = list(body.payload.values())
        # 不合格的情况：有不认识的键；一项都没有；有值不是数字或者是负数；加起来不大于 0
        if unknown or not values or any(not isinstance(v, (int, float)) or v < 0 for v in values) or sum(values) <= 0:
            raise HTTPException(422, i18n.tr("权重预设只能包含这几项，且不能为负：{dims}",
                                             "A weight preset may only contain these keys, none negative: {dims}",
                                             dims=i18n.join(DIMENSIONS)))
        # 统一存成小数。不要求加起来等于 1：评分时按权重之间的比例算（m10_scoring.weighted_score）
        payload = {key: float(value) for key, value in body.payload.items()}

    # 同一个课题组里，种类和名字都相同的预设只有一份（presets 表上有这个唯一约束）：先找有没有
    preset = db.scalars(
        select(models.Preset).where(
            models.Preset.org_id == principal.org_id, models.Preset.kind == body.kind, models.Preset.name == body.name
        )
    ).first()
    if preset is None:
        # 没有同名的：新建一条，记下是谁建的
        preset = models.Preset(org_id=principal.org_id, kind=body.kind, name=body.name, payload=payload, created_by=principal.user_id)
        db.add(preset)
    else:
        # 已经有同名的：只换内容，创建人（created_by）不变
        preset.payload = payload
    if body.is_default:
        # 每种预设只留一个默认项：先把本课题组同一种预设的默认标记都去掉，下面再给这一个标上
        for other in db.scalars(select(models.Preset).where(models.Preset.org_id == principal.org_id, models.Preset.kind == body.kind)):
            other.is_default = False
    # 注意：请求里没带 is_default（默认 False）时，这一行会把原来的默认标记去掉
    preset.is_default = body.is_default
    db.commit()
    return _preset_view(preset)


@router.delete("/presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["预设"])
def delete_preset(preset_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)):
    """删除一个预设。

    需要登录，组里任何人都可以删。成功返回 204，没有内容。
    出错：404 没有这个预设（别的课题组的预设也是 404）。
    """
    preset = db.get(models.Preset, preset_id)
    if preset is None or preset.org_id != principal.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, i18n.tr("没有这个预设", "Preset not found"))
    db.delete(preset)
    db.commit()


# ---------------------------------------------------------------- 前端要读的配置


@router.get("/scoring/config", tags=["配置"])
def scoring_config(ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """评分公式的全部参数。前端排序、切换分支、拖权重时按同一个公式自己重算，所以是即时的。

    需要登录。对应按钮 B1、B8、B9、D1。返回：

    - `version`：评分配置的版本号
    - `dimensions`：六个评分维度的编号，顺序固定：
      `literature` `operation` `safety` `availability` `yield` `green`
    - `dimension_labels`：每个维度的中文名
    - `weights`：每个维度的默认权重
    - `route`：路线分的两个系数 `{mean_coefficient, weakest_coefficient}`
    - `soft_preference_penalty`：软偏好每命中一次，在步骤分上扣多少分
    - `formula`：三条公式的文字说明：`step` 步骤分、`route` 路线分、`ranking` 排序规则
    - `evidence_levels`：证据等级 E1–E4 各自的名称（`label`）、星数（`stars`）、文献分的分数段（`band`）
    - `complexity`：复杂度三档各自的名称、步数上限、最多允许几步没有先例
    - `confidence`：判断置信度用的几个阈值
    - `status`：一句提醒：这些权重和系数还是初稿

    出错：401 没登录。
    """
    # 这些值都来自 data/scoring.toml：启动时读进内存（ctx.reference.scoring），这里只是挑出前端要的几项。
    # 参数名写成下划线的 _：只要求登录，用不到“登录的是谁”。
    # scoring.get("version") 用 get：万一配置文件里没写版本号，返回 None 而不是报错
    scoring = ctx.reference.scoring
    return {
        "version": scoring.get("version"),
        # 六个维度的编号写在代码里（m10_scoring.DIMENSIONS）；下面的名称、权重、系数都取自配置文件
        "dimensions": DIMENSIONS,
        "dimension_labels": scoring["dimension_labels"],
        "weights": scoring["weights"],
        "route": scoring["route"],
        "soft_preference_penalty": scoring["soft_preference"]["penalty"],
        # 三条公式的文字版。真正的计算在 modules/m10_scoring.py，前端按同样的公式自己重算
        "formula": {
            "step": i18n.tr(
                "步骤分 = Σ(维度分 × 权重) ÷ Σ(有数据的维度的权重) − soft_penalty；维度分为 null 表示没有数据，不计入",
                "Step score = Σ(dimension score × weight) ÷ Σ(weights of dimensions with data) − soft_penalty; "
                "a null dimension score means no data and is left out"),
            "route": i18n.tr("路线分 = mean_coefficient × 各步平均分 + weakest_coefficient × 最弱一步",
                             "Route score = mean_coefficient × mean step score + weakest_coefficient × weakest step"),
            # 档位名“探索”取自 scoring.toml，当参数拼进去：英文句子里用的就是词表给这一档登记的英文
            "ranking": i18n.tr(
                "按路线分从高到低；除“{tier}”档外，低置信度路线不排在高置信度路线前面",
                "Sorted by route score, highest first; except in the “{tier}” tier, "
                "a low-confidence route never ranks above a high-confidence one",
                tier=scoring["complexity"]["explore"]["label"]),
        },
        # 下面三项原样取自 scoring.toml 的 [evidence.levels.*]、[complexity.*]、[confidence] 三处
        "evidence_levels": scoring["evidence"]["levels"],
        "complexity": scoring["complexity"],
        "confidence": scoring["confidence"],
        "status": i18n.tr("权重和系数都是初稿，待用历史案例回测和化学家盲评校准",
                          "Weights and coefficients are a first draft, pending calibration by back-testing on "
                          "historical cases and blind review by chemists"),
    }


@router.get("/meta/options", tags=["配置"])
def constraint_options(ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """约束页（新建分析第 2 步）上各个选项的编号和显示文字。

    需要登录。前端用它画出各个选项，不用把选项写死在前端。返回的每一组都是数组，每项至少有
    `key`（提交分析时用的编号）和 `label`（显示的文字）：

    - `complexity`：A7 复杂度三档；另带 `max_steps`（步数上限）和 `max_steps_without_precedent`
      （最多允许几步没有先例）
    - `stock_source`：A8 原料来源三档
    - `scale`：A12 规模三档；另带 `target_mg`（物料清单默认按多少毫克目标物折算）
    - `unavailable_conditions`：A10 做不了的条件
    - `safety`：A11 安全要求。`key` 是 `acute_toxic`、`cmr`、`controlled`，提交时对应
      `constraints.safety` 里的 `exclude_acute_toxic`、`exclude_cmr`、`exclude_controlled` 三个开关
    - `purifications`：可以避免的纯化方式（A13 高级参数、A14 一句话要求用）
    - `reagent_classes`：可以排除的试剂类别（A13 高级参数、A14 一句话要求用）

    出错：401 没登录。
    """
    # 数据来源：vocabulary 是 data/vocabulary.toml（约束词表），scoring 是 data/scoring.toml；
    # STOCK_LABELS、SCALE_LABELS 是写在 syntherisk/constraints.py 里的两张小表
    vocabulary, scoring = ctx.reference.vocabulary, ctx.reference.scoring

    def options(group: str) -> list[dict[str, str]]:
        """把词表里的一组条目变成 [{"key": 编号, "label": 显示文字}, ...]。

        参数 group 是词表里的组名：condition、safety、purification、reagent_class。
        词表里没有这一组时返回空列表。只在外层的 constraint_options 里用。
        """
        return [{"key": key, "label": spec["label"]} for key, spec in vocabulary.get(group, {}).items()]

    return {
        # 复杂度三档：编号是 concise / balanced / explore，名称和两个上限来自 scoring.toml 的 [complexity.*]
        "complexity": [
            {"key": key, "label": tier["label"], "max_steps": tier["max_steps"],
             "max_steps_without_precedent": tier["max_steps_without_precedent"]}
            for key, tier in scoring["complexity"].items()
        ],
        "stock_source": [{"key": key, "label": label} for key, label in STOCK_LABELS.items()],
        # 规模三档：target_mg 来自 scoring.toml 的 [scale]（现在是 mg 10、hundred_mg 100、g 1000）
        "scale": [{"key": key, "label": label, "target_mg": scoring["scale"][key]} for key, label in SCALE_LABELS.items()],
        "unavailable_conditions": options("condition"),
        "safety": options("safety"),
        "purifications": options("purification"),
        "reagent_classes": options("reagent_class"),
    }


@router.get("/meta/modules", tags=["配置"])
def modules(ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """各模块现在接的是什么（真引擎、演示数据，还是没配置）。

    需要登录。返回一个对象，键是模块编号 `M1`–`M14`、`MQ`，另加一个 `database`，值是这个模块当前的状态。
    几个常用的：`M3.engine` 是 `demo`（内置演示路线）还是 `aizynthfinder`；`M2.provider` 是 `offline`
    表示没有配置大模型；`MQ.backend` 是任务队列的类型；`database` 是数据库的种类（如 `sqlite`）。

    出错：401 没登录。
    """
    # 内容由 context.AppContext.describe_modules() 现场拼出来；命令行的 status 命令用的也是它
    return ctx.describe_modules()


@router.get("/meta/data-sources", tags=["配置"])
def data_sources(ctx: AppContext = Depends(get_ctx), _: Principal = Depends(get_principal)):
    """数据来源和许可登记表。

    需要登录。返回一个数组，每个数据来源一项：`id`（来源编号，如 `uspto`）、`name`（名称）、
    `used_for`（用在哪）、`license`（许可）、`commercial_use`（能不能商用）、`allowed`（是否允许使用）、
    `notes`（备注，有的来源没有这一项）。

    出错：401 没登录。
    """
    # 内容来自 data/data_sources.toml 的各个 [source.xxx] 小节。
    # {"id": key, **spec}：新建一个字典，先放进小节名（当作 id），再把这一节里的各项展开放进去
    return [{"id": key, **spec} for key, spec in ctx.reference.data_sources.items()]
