"""接口的请求体。约束（Constraints）定义在 syntherisk/constraints.py。

这个文件只描述“前端发过来的 JSON 长什么样”：每个类是一种请求体，类里的每一行是一个字段。
这些类都继承 Pydantic 的 BaseModel。接口函数的参数写成 body: schemas.某个类，FastAPI 就会在调用
接口函数之前按这里的定义检查请求体：缺字段、类型不对、超出长度或范围，直接返回 422，接口函数
根本不会被调用。检查通过以后，接口函数里用 body.字段名 读取各个字段。

对应模块和按钮：M0 应用服务的输入格式。每个类的说明里写了它是哪个接口、哪个按钮用的。
这些说明会显示在接口文档页面（/docs）的 Schemas 部分，所以是写给调用接口的人看的。

谁调用它：api/core.py、structures.py、analyses.py、inventory.py、reports.py 里的接口函数。
它依赖谁：syntherisk/constraints.py 的 Constraints（新建分析第 2 步的全部约束）。
返回给前端的 JSON 不在这里定义：接口直接返回字典，字典的样子见各接口的说明和 api/views.py。

读这个文件要认得的几种写法：
    x: str                           必填的字符串
    x: str | None = None             可以不填，不填就是 None（JSON 里的 null）
    x: str = ""                      可以不填，不填就是空字符串
    Literal["a", "b"]                只能是列出的这几个值之一
    Field(min_length=, max_length=)  字符串或列表的长度限制
    Field(ge=, le=, gt=)             数字的范围：ge 大于等于，le 小于等于，gt 大于
    Field(pattern=)                  字符串要符合这个正则表达式
    Field(default_factory=函数)       不填时调用这个函数生成默认值（列表、对象这类默认值要这样写）
    @model_validator(mode="after")   各个字段都检查通过以后再做的整体检查；
                                     在里面抛 ValueError，同样是返回 422

本文件里有什么（按阅读顺序）：
    _USERNAME  用户名的正则表达式
    M0（账号、项目、草稿、预设）
        RegisterRequest   注册                    LoginRequest   登录
        AgreementAccept   同意内测协议
        MemberCreate      管理员加成员             ProjectCreate  建项目
        DraftSave         保存草稿（A6）           PresetSave     保存预设（A15、D3）
    M1 / M2（结构、约束解析）
        StructureInput            校验结构（A1、A2、A5）
        StructureDiffRequest      比较两个结构（C2）
        RequirementParseRequest   解析一句话要求（A14）
    分析
        TargetInput           目标分子                AnalysisCreate    开始分析（A16）
        RelaxRequest          放宽条件（B6）           ExclusionCreate   排除分支或试剂（B11）
        MarkOwnedRequest      标记为我有（B12）        AskRequest        问一问（C5）
        CompareRequest        路线对比（D1）           CompareExportRequest  导出对比（D4）
    报告、回填
        ReportCreate          生成报告（F1–F3）
        ExperimentCreate      记录实验结果（G1–G4）    ExperimentUpdate  修改实验记录（G3）
    库存
        InventoryItemCreate   手动添加（E3）           InventoryItemUpdate   修改一条库存
        ImportRowAction       处理导入里不确定的行（E2）  InventoryShare    共享给团队（E5）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from syntherisk import i18n
from syntherisk.constraints import Constraints


def _invalid(zh: str, en: str) -> ValueError:
    """造一个校验方法里要抛的 ValueError，说明是中文，并登记它的英文。

    校验方法抛的 ValueError 会被 FastAPI 变成 422，说明出现在返回的 detail[].msg 里，
    而且 Pydantic 会在前面加上 "Value error, "。返回英文时是按整句查对照的（见 syntherisk/i18n.py），
    所以这里把带前缀的整句也登记一遍，英文请求拿到的就是 "Value error, <英文说明>"。

    参数：
        zh  中文说明（不带前缀）。
        en  对应的英文说明。
    返回：
        ValueError(zh)。用法：raise _invalid("…", "…")。
    谁在用：
        本文件各个请求体的 model_validator 方法。
    """
    i18n.register(zh, en)
    i18n.register(f"Value error, {zh}", f"Value error, {en}")
    return ValueError(zh)

# 用户名：一个以上不是空白的字符（中间不能有空格），最长 200 个字符。旧账号的邮箱也符合这条规则
_USERNAME = r"^\S+$"


# ---------------------------------------------------------------- M0


class RegisterRequest(BaseModel):
    """注册账号的请求体（`POST /api/auth/register`）。注册的同时会新建一个课题组。

    - `username`：登录用的用户名，中间不能有空格，最长 200 个字符。不分大小写，保存时转成小写
    - `password`：密码，8–200 个字符。后端只保存它的哈希值，不保存原文
    - `display_name`：显示的名字，可以不填，最长 200 个字符
    - `org_name`：课题组的名字，必填，1–200 个字符
    """

    # username、display_name 和 org_name 的 200 不超过 users.email（用户名存在这一列）、users.display_name、
    # organizations.name 三列的长度一致（见 syntherisk/models.py）
    username: str = Field(pattern=_USERNAME, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=200)
    org_name: str = Field(min_length=1, max_length=200)


class LoginRequest(BaseModel):
    """登录的请求体（`POST /api/auth/login`）。

    - `username`：用户名，不分大小写
    - `password`：密码

    这里不检查格式和长度：用户名或密码不对时，接口统一返回 401“用户名或密码不对”。
    """

    username: str
    password: str


class MemberCreate(BaseModel):
    """管理员给组里的同学开账号的请求体（`POST /api/org/members`）。

    - `username`：新成员登录用的用户名，格式和长度的要求同注册；已经用过的用户名不能再用
    - `password`：管理员给他设的密码，8–200 个字符
    - `display_name`：显示的名字，可以不填，最长 200 个字符
    - `role`：角色。`member` 普通成员（默认）；`admin` 管理员，可以再给别人开账号
    """

    username: str = Field(pattern=_USERNAME, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(default="", max_length=200)
    # 这里选不了 owner：owner 只在建课题组的时候产生（注册接口，或者命令行的 seed-demo）
    role: Literal["admin", "member"] = "member"


class AgreementAccept(BaseModel):
    """同意内测协议的请求体（`POST /api/agreement/accept`）。前三项原样带回 `GET /api/agreement` 给的值。

    - `version`：同意的是哪一版，比如 `V0.5`
    - `language`：看的是哪种语言的那一份（`zh`、`en`）
    - `sha256`：那一份内容的哈希。和服务器上的对不上（协议刚改过）时返回 409，要重新读
    - `checked`：勾了的选项（协议里 `checkboxes` 的 `key`），每一项都要勾上
    """

    version: str = Field(max_length=40)
    language: str = Field(max_length=8)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked: list[str] = Field(max_length=20)


class ProjectCreate(BaseModel):
    """新建项目的请求体（`POST /api/projects`）。

    - `name`：项目名，必填，1–200 个字符
    - `description`：项目说明，可以不填
    """

    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class DraftSave(BaseModel):
    """A6：草稿内容由前端决定（目标分子、约束、停在第几步），后端原样加密保存。

    `POST /api/drafts`（新建）和 `PUT /api/drafts/{id}`（覆盖保存）的请求体。

    - `project_id`：草稿属于哪个项目，可以不填；填了就必须是本课题组的项目
    - `payload`：草稿内容，任意的 JSON 对象。后端不检查里面有什么，读草稿时原样返回
    """

    project_id: str | None = None
    payload: dict[str, Any]


class PresetSave(BaseModel):
    """A15 约束预设（kind=constraints），D2 / D3 权重预设（kind=weights）。

    `POST /api/presets` 的请求体。同一个课题组里，种类和名字都相同的预设只有一份，再存一次就是覆盖。

    - `kind`：预设的种类。`constraints` 约束预设，`weights` 权重预设
    - `name`：预设的名字，1–200 个字符
    - `payload`：预设的内容，格式取决于 `kind`。
      约束预设：和开始分析时的 `constraints` 字段格式相同，没写的项会补上默认值再保存。
      权重预设：六个维度的权重，键只能是 `literature` `operation` `safety` `availability` `yield` `green`，
      值是不小于 0 的数字，加起来要大于 0（不要求加起来等于 1，评分时按比例折算）
    - `is_default`：是否设为这一种预设的默认项，默认 `false`。设为默认时，同种类的其他预设自动取消默认
    """

    kind: Literal["constraints", "weights"]
    name: str = Field(min_length=1, max_length=200)
    # payload 这里只要求是 JSON 对象；里面的内容由 api/core.py 的 save_preset 按 kind 再检查
    payload: dict[str, Any]
    is_default: bool = False


# ---------------------------------------------------------------- M1 / M2


class StructureInput(BaseModel):
    """校验一个结构的请求体（`POST /api/structures/validate`；A1 画结构、A2 粘贴 SMILES、A5 起始骨架）。

    - `input`：结构的文本：一段 SMILES，或者 MOL / SDF 文件的内容。最长 20 万个字符
    - `format`：`input` 是哪种格式。`auto`（默认）按内容自动判断；`smiles`、`mol`、`sdf` 是明确指定
    """

    input: str = Field(max_length=200_000)
    format: Literal["auto", "smiles", "mol", "sdf"] = "auto"


class StructureDiffRequest(BaseModel):
    """比较两个结构的请求体（`POST /api/structures/diff`；C2 相似先例的结构差异）。

    - `query`：要比较的结构（SMILES），比如这一步的产物
    - `reference`：拿来对照的结构（SMILES），比如先例里的产物
    """

    query: str
    reference: str


class RequirementParseRequest(BaseModel):
    """解析一句话补充要求的请求体（`POST /api/constraints/parse`；A14）。

    - `text`：用户写的要求原话，比如“不要用钯催化剂，尽量不过柱色谱”。最长 1000 个字符
    """

    text: str = Field(max_length=1000)


# ---------------------------------------------------------------- 分析


class TargetInput(BaseModel):
    """目标分子：SMILES 或 MOL 文本二选一（画出来的结构由编辑器导出成这两种之一）。

    - `smiles`：目标分子的 SMILES
    - `molblock`：目标分子的 MOL 文件内容
    - `name`：给目标分子起的名字，可以不填，最长 200 个字符。不填时用内置试剂表里的名字，表里没有就没有名字

    `smiles` 和 `molblock` 至少给一个；两个都给时用 `smiles`。
    """

    smiles: str | None = None
    molblock: str | None = None
    name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _one_structure(self):
        """整体检查：smiles 和 molblock 不能都空着（空字符串也算空）。

        Pydantic 在各个字段都通过检查以后自动调用它，接口代码不直接调。
        返回：检查通过时原样返回 self（Pydantic 要求这样写）。
        抛出：ValueError；FastAPI 会把它变成 422 响应。
        """
        if not (self.smiles or self.molblock):
            raise _invalid("需要给出目标分子的 smiles 或 molblock", "Provide the target molecule as smiles or molblock")
        return self


class AnalysisCreate(BaseModel):
    """开始一次分析的请求体（`POST /api/analyses`；A16）。

    - `project_id`：这次分析放在哪个项目下，必须是本课题组的项目
    - `target`：目标分子，见 `TargetInput`
    - `constraints`：新建分析第 2 步的全部约束：复杂度档位、原料来源、做不了的条件、安全要求、规模、
      高级参数、起始骨架、确认过的一句话要求。可以不填，不填就全部用默认值
    - `draft_id`：这次分析是从哪份草稿来的，可以不填。填了以后，分析建好时这份草稿会被删掉
    """

    project_id: str
    target: TargetInput
    constraints: Constraints = Field(default_factory=Constraints)
    draft_id: str | None = None


class RelaxRequest(BaseModel):
    """B6：放宽哪些约束。可以直接给约束编号，也可以给一条被排除的路线（放宽挡住它的全部约束）。

    `POST /api/analyses/{id}/relax` 的请求体。两个字段可以同时给，效果叠加。

    - `constraints`：要放宽的约束编号。编号就是结果里每条排除原因（`exclusion_reasons`）的 `constraint` 字段，
      比如 `condition:pressurized_gas`、`safety:acute_toxic`、`max_steps`、`stock`
    - `route_id`：一条被排除的路线的编号（如 `r3`）：放宽挡住它的全部约束。用户自己右键排除的不算在内
    """

    constraints: list[str] = Field(default_factory=list)
    route_id: str | None = None


class ExclusionCreate(BaseModel):
    """B11：排除一个分支（某条路线的某一步）或一种试剂。

    `POST /api/analyses/{id}/exclusions` 的请求体。

    - `type`：`branch` 排除一个分支，`substance` 排除一种试剂
    - `route_id`：`type` 是 `branch` 时必填：路线编号（如 `r1`）
    - `step_index`：`type` 是 `branch` 时必填：这条路线的第几步，从 1 数起
    - `smiles`：`type` 是 `substance` 时必填：要排除的物质的 SMILES
    """

    type: Literal["branch", "substance"]
    route_id: str | None = None
    # ge=1：步骤从 1 数起，0 和负数直接 422
    step_index: int | None = Field(default=None, ge=1)
    smiles: str | None = None

    @model_validator(mode="after")
    def _complete(self):
        """整体检查：按 type 确认该给的字段都给了。

        branch 要有 route_id 和 step_index；substance 要有 smiles。
        Pydantic 在各个字段都通过检查以后自动调用它。
        返回：检查通过时原样返回 self。
        抛出：ValueError；FastAPI 会把它变成 422 响应。
        """
        if self.type == "branch" and not (self.route_id and self.step_index):
            raise _invalid("排除分支需要 route_id 和 step_index", "Excluding a branch requires route_id and step_index")
        if self.type == "substance" and not self.smiles:
            raise _invalid("排除试剂需要 smiles", "Excluding a reagent requires smiles")
        return self


class MarkOwnedRequest(BaseModel):
    """B12：标记为我有。

    `POST /api/analyses/{id}/mark-owned` 的请求体：把一种原料记进库存。

    - `smiles`：这种原料的 SMILES
    - `name`：库存里显示的名字，可以不填；不填时用分析结果或内置试剂表里的名字，都没有就用分子式
    - `quantity`：数量，自由填写的文字，比如“250 mL”，可以不填
    - `location`：存放位置，自由填写的文字，比如“试剂柜 B1”，可以不填
    """

    smiles: str
    name: str | None = None
    quantity: str | None = None
    location: str | None = None


class AskRequest(BaseModel):
    """C5“问一问”的请求体（`POST /api/analyses/{id}/routes/{route_id}/steps/{n}/ask`）。

    - `question`：针对这一步的问题，1–500 个字符
    """

    question: str = Field(min_length=1, max_length=500)


class CompareRequest(BaseModel):
    """D1 路线对比的请求体（`POST /api/analyses/{id}/compare`）。

    - `route_ids`：要对比的路线编号，1–6 条
    - `weights`：六个维度的权重，可以不填。填了就按这组权重重新算分，只影响这一次返回的结果，不保存。
      键只能是 `literature` `operation` `safety` `availability` `yield` `green`，没写的维度按 0 算；
      值不能为负，加起来要大于 0
    """

    # min_length / max_length 用在列表上，限制的是列表里有几项
    route_ids: list[str] = Field(min_length=1, max_length=6)
    # 这里只要求“键是字符串、值是数字”；键对不对、值是否为负由 api/reports.py 的 _weighted_result 检查
    weights: dict[str, float] | None = None


class CompareExportRequest(CompareRequest):
    """D4 导出对比表的请求体（`POST /api/analyses/{id}/compare/export`）。

    `route_ids` 和 `weights` 同 `CompareRequest`，另外多一个字段：

    - `format`：导出的文件格式，`xlsx`（默认）或 `csv`
    """

    format: Literal["xlsx", "csv"] = "xlsx"


# ---------------------------------------------------------------- 报告、回填


class ReportCreate(BaseModel):
    """F1 选内容、F2 选格式、F3 生成。免责声明固定包含，不在可选项里。

    `POST /api/analyses/{id}/reports` 的请求体。

    - `route_id`：给哪条路线出报告
    - `sections`：报告里包含哪些部分，不填就是全部五项。`route` 推荐路线与路线树，`evidence_cards`
      每一步的证据卡，`literature` 文献列表，`materials` 试剂与物料清单，`safety` 安全汇总。
      格式选 `sdf` 时这一项不起作用
    - `format`：文件格式。`pdf`（默认）、`docx`（Word）、`sdf`（SDF + RXN 结构文件，打成一个 zip）
    - `filename`：下载时的文件名，可以不填，最长 200 个字符。不填时是“目标分子_路线名_日期”；
      扩展名没写或不对时会自动补上
    - `target_mg`：物料清单按得到多少毫克目标物折算，要大于 0。不填时按这次分析的规模档位
    """

    route_id: str
    # default_factory 里的 lambda 每次都生成一个新的列表（五项全选），不同请求之间不会共用同一个列表对象
    sections: list[Literal["route", "evidence_cards", "literature", "materials", "safety"]] = Field(
        default_factory=lambda: ["route", "evidence_cards", "literature", "materials", "safety"]
    )
    format: Literal["pdf", "docx", "sdf"] = "pdf"
    filename: str | None = Field(default=None, max_length=200)
    target_mg: float | None = Field(default=None, gt=0)


class ExperimentCreate(BaseModel):
    """G1 结果、G2 产率和备注、G3 授权（默认不勾）。

    `POST /api/analyses/{id}/routes/{route_id}/steps/{n}/experiments` 的请求体（G4 保存）。

    - `outcome`：实验结果。`success` 成功，`partial` 部分成功，`failure` 失败
    - `actual_yield`：实际产率，百分数，0–100，可以不填
    - `condition_changes`：和推荐的条件相比改了什么，自由填写，最长 2000 个字符
    - `notes`：备注，自由填写，最长 5000 个字符
    - `consent_for_training`：是否授权把这条记录用于改进模型，默认 `false`；保存以后可以随时再改
    """

    outcome: Literal["success", "partial", "failure"]
    actual_yield: float | None = Field(default=None, ge=0, le=100)
    condition_changes: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=5000)
    consent_for_training: bool = False


class ExperimentUpdate(BaseModel):
    """修改一条实验记录的请求体（`PATCH /api/experiments/{id}`；G3 撤回或补上授权也用它）。

    每个字段的含义和限制同 `ExperimentCreate`，都可以不带：只带要改的字段，没带的保持原样。

    - `outcome`：实验结果；传 `null` 等于没带
    - `actual_yield`：实际产率；传 `null` 表示清空
    - `condition_changes`：条件变化；传 `null` 表示清空
    - `notes`：备注；传 `null` 表示清空
    - `consent_for_training`：是否授权用于改进模型；传 `null` 等于没带
    """

    # 每个字段的默认值都是 None。“没带”和“带了 null”在这里看起来一样，
    # 接口里用 model_dump(exclude_unset=True) 区分（见 api/reports.py 的 update_experiment）
    outcome: Literal["success", "partial", "failure"] | None = None
    actual_yield: float | None = Field(default=None, ge=0, le=100)
    condition_changes: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)
    consent_for_training: bool | None = None


# ---------------------------------------------------------------- 库存


class InventoryItemCreate(BaseModel):
    """E3 手动添加：名称、CAS 号、SMILES 至少给一个。

    `POST /api/inventory/items` 的请求体。

    - `name`：名称，最长 300 个字符
    - `cas`：CAS 号，最长 20 个字符
    - `smiles`：结构的 SMILES。给了就必须读得出来，否则返回 422
    - `quantity`：数量，自由填写的文字，比如“100 g”，最长 100 个字符
    - `location`：存放位置，最长 200 个字符
    - `note`：备注，最长 1000 个字符
    """

    name: str | None = Field(default=None, max_length=300)
    cas: str | None = Field(default=None, max_length=20)
    smiles: str | None = None
    quantity: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _identifiable(self):
        """整体检查：name、cas、smiles 三个里至少要有一个不为空，否则没法知道这是什么东西。

        Pydantic 在各个字段都通过检查以后自动调用它。
        返回：检查通过时原样返回 self。
        抛出：ValueError；FastAPI 会把它变成 422 响应。
        """
        if not (self.name or self.cas or self.smiles):
            raise _invalid("名称、CAS 号、SMILES 至少要给一个", "Provide at least one of name, CAS number or SMILES")
        return self


class InventoryItemUpdate(BaseModel):
    """修改一条库存的请求体（`PATCH /api/inventory/items/{id}`）。

    只带要改的字段；没带的（或者是 `null` 的）字段保持原样。

    - `name`：名称，最长 300 个字符
    - `quantity`：数量，最长 100 个字符
    - `location`：存放位置，最长 200 个字符
    - `note`：备注，最长 1000 个字符
    - `smiles`：改成另一个结构。必须读得出来，否则返回 422
    """

    name: str | None = Field(default=None, max_length=300)
    quantity: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    smiles: str | None = None


class ImportRowAction(BaseModel):
    """E2：confirm 接受系统的猜测，set_structure 自己指定结构，ignore 忽略这一行。

    `POST /api/inventory/imports/{id}/rows/{row_id}` 的请求体。

    - `action`：怎么处理这一行，三种之一，含义见上
    - `smiles`：`action` 是 `set_structure` 时要给：用户指定的结构。其他两种操作不看这个字段
    - `name`：记进库存时用的名称，可以不填；不填时用表格里原来的名称，表格里没有就用系统猜出来的名称
    """

    action: Literal["confirm", "set_structure", "ignore"]
    smiles: str | None = None
    name: str | None = None


class InventoryShare(BaseModel):
    """E5：team 表示组里所有人可见，private 表示只有自己可见。

    `POST /api/inventory/share` 的请求体。

    - `visibility`：把自己名下的全部库存改成哪种可见范围
    """

    visibility: Literal["team", "private"]
