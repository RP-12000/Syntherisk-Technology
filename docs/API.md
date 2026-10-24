# 按钮 → 接口对照表

编号和产品设计 PPT 一致：按钮 A1–G4，后端模块 M0–M14 和任务队列 MQ，结果回到界面的位置 R1–R12。

所有接口都在 `/api` 下面，除了注册和登录都要带 `Authorization: Bearer <登录拿到的 access_token>`。
启动后端后打开 <http://127.0.0.1:8000/docs> 可以看到每个接口的参数并直接试调。
每个接口的请求、返回的例子和对接时要注意的地方，在 [前端对接手册](前端对接手册.md) 里；接口在代码的哪个文件、哪个函数，在 [后端手册](后端手册.md) 里。

出错时返回 `{"detail": …}`：
401 没登录，403 没权限，404 没有这条数据（别的课题组的数据也是 404），409 现在不能做这个操作，413 上传的文件太大，422 输入有问题。
`detail` 一般是一句中文说明，前端可以直接显示。两种情况例外：请求体或参数不合格式时，是 FastAPI 自带的错误列表
`[{"type", "loc", "msg", …}]`；`POST /analyses` 的目标分子有问题时，是 `{"message": 中文说明, "errors": [具体问题]}`。

## A 新建分析

| 按钮 | 做什么 | 接口 | 模块 | 回到界面 |
|---|---|---|---|---|
| A1 画结构 | 编辑器导出 MOL 或 SMILES，边画边校验 | `POST /structures/validate` | M1 | R1 结构预览和校验 |
| A2 粘贴 SMILES | 同上 | `POST /structures/validate` | M1 | R1 |
| A3 名称或 CAS 号 | 查候选结构，点选填入 | `GET /structures/lookup?q=` | M1 + PubChem | R1 |
| A4 上传文件 | MOL 或 SDF | `POST /structures/upload` | M1 | R1 |
| A5 指定起始骨架 | 校验后放进 `constraints.starting_materials` | `POST /structures/validate` | M1 | 随分析生效 |
| A6 下一步 | 自动保存草稿 | `POST /drafts`、`PUT /drafts/{id}` | M0 | — |
| A7 复杂度三档 | `constraints.complexity`：`concise` / `balanced` / `explore` | 随 A16 提交 | M3、M10 | R8 |
| A8 原料来源三档 | `constraints.stock_source`：`inventory_only` / `inventory_first` / `any` | 随 A16 提交 | M4、M10 | R8 |
| A9 管理库存 | 跳到库存页；“已导入库存 N 种”用 | `GET /inventory/summary` | M4 | — |
| A10 做不了的条件 | `constraints.unavailable_conditions` | 随 A16 提交 | M7 → M10 | R8 |
| A11 安全要求 | `constraints.safety` | 随 A16 提交 | M6 → M10 | R8 |
| A12 规模 | `constraints.scale`：`mg` / `hundred_mg` / `g` | 随 A16 提交 | M13 | 物料清单 |
| A13 高级参数 | `constraints.advanced` | 随 A16 提交 | M10 | R8 |
| A14 一句话补充要求 | 解析成约束标签；用户确认后放进 `constraints.requirements` | `POST /constraints/parse` | M2（大模型） | R2 约束标签 |
| A15 团队预设 | 读取、保存常用约束 | `GET /presets?kind=constraints`、`POST /presets` | M0 | 整页约束 |
| A16 开始分析 | 建任务、进队列，立刻返回 | `POST /analyses` | M0 → MQ → M3–M11 | R3 → R4 R5 R7 R8 |
| A17 进度面板 | 8 个阶段逐个打勾 | `GET /analyses/{id}/events`（SSE），或轮询 `GET /analyses/{id}` | MQ | R3 |
| A18 先看初步结果 | 搜索完就能看的路线树 | `GET /analyses/{id}/preliminary` | M3 | R5（评估中） |
| A19 取消 | 停止这次分析 | `POST /analyses/{id}/cancel` | MQ | R3 |

选项的编号和显示文字从 `GET /meta/options` 取，不用在前端写死。

## B 结果页

| 按钮 | 做什么 | 接口 | 模块 | 回到界面 |
|---|---|---|---|---|
| B1 排序 | 前端用已有分数排；也可以让后端排 | `GET /analyses/{id}/result?sort=` | 前端 / M10 | R4 |
| B2 对比 | 跳到对比页 | — | — | — |
| B3 导出报告 | 打开导出对话框 | — | — | — |
| B4 路线卡片 | 画布换成这条路线 | `GET /analyses/{id}/routes/{route_id}` | 读已存结果 | R5 R7 |
| B5 快速筛选 | 前端用卡片上的字段筛 | — | 前端 | R4 |
| B6 被排除的路线 | 看原因；临时放宽条件 | `POST /analyses/{id}/relax` | M10（不重新搜索） | R4 标“放宽后” |
| B7 画布工具 | 缩放、适应画布 | — | 前端 | — |
| B8 分叉标签 | 切换分支；结果里的 `graph` 给出每个分支分数最高的路线 | — | 前端 | R4 R5 R7 |
| B9 高亮最薄弱环节 | 每条路线的 `weakest` 字段 | — | 前端 | R5 |
| B10 点击某一步 | 打开证据卡；说明没写过时补写 | `GET /analyses/{id}/routes/{route_id}/steps/{n}` | 读 M5–M9 结果；缺说明时 M11 | R6 |
| B11 右键 · 排除 | 排除分支或试剂，重新排序；一条不剩时重新搜索 | `POST /analyses/{id}/exclusions`，撤销用 `DELETE …/exclusions/{eid}` | M10；全排除时 M3 | R4 R5 R8 |
| B12 右键 · 标记为我有 | 原料加入库存 | `POST /analyses/{id}/mark-owned` | M4 | R5 节点变绿、R7 库存覆盖 |

前端自己重算（B1、B8、B9、D1）要用的公式和参数：`GET /scoring/config`。

## C 证据卡

| 按钮 | 做什么 | 接口 | 模块 | 回到界面 |
|---|---|---|---|---|
| C1 文献列表 | 相似度、产率、原文链接；期刊论文和专利分开检索，期刊在前，专利那一组默认折叠（不计入证据等级），`?doc_type=journal` / `patent` 只取一种 | `GET …/steps/{n}/precedents` | M5 | R6 |
| C2 相似先例 | 同上，带结构差异 | `GET …/steps/{n}/precedents`、`POST /structures/diff` | M5 + M1 | R6 |
| C3 替代分支 | 证据卡的 `alternatives` 字段；跳回分叉点 | — | — | — |
| C4 原料标签 | 库存、在售、价格（第一版“待询价”） | `GET /materials/availability?smiles=` | M4 | R6 浮层 |
| C5 问一问 | 只根据这张卡回答，附出处 | `POST …/steps/{n}/ask` | M12（大模型） | R6 |
| C6 记录实验结果 | 打开回填对话框 | — | — | — |

## D 路线对比

| 按钮 | 做什么 | 接口 | 模块 | 回到界面 |
|---|---|---|---|---|
| D1 六个权重滑杆 | 前端按公式重算；也可以把权重发给后端算 | `POST /analyses/{id}/compare` | 前端 / M10 | R9 |
| D2 预设 | 读取权重预设 | `GET /presets?kind=weights` | M0 | 滑杆 |
| D3 保存为团队预设 | | `POST /presets` | M0 | 滑杆 |
| D4 导出对比 | xlsx 或 csv | `POST /analyses/{id}/compare/export` | M13 | R11 下载 |
| D5 差异步骤 | 对比结果里每一步带 `shared` 标记 | — | 前端 | R9 |

## E 实验室库存

| 按钮 | 做什么 | 接口 | 模块 | 回到界面 |
|---|---|---|---|---|
| E1 导入 Excel / CSV | 名称、CAS、SMILES 任一列都行 | `POST /inventory/imports`，大文件看进度 `GET /inventory/imports/{id}` | M1 + M4 | R10 已匹配 / 需确认 / 未识别 |
| E2 确认匹配 | 确认、改结构或忽略 | `POST /inventory/imports/{id}/rows/{row_id}` | M1 + M4 | R10 |
| E3 手动添加 | | `POST /inventory/items` | M1 + M4 | R10 |
| E4 搜索 | 名称、CAS 或子结构 | `GET /inventory/items?q=&substructure=` | M1 + M4 | R10 |
| E5 共享给团队 | 只改可见范围 | `POST /inventory/share` | M0 | — |

## F 导出报告 · G 回填实验结果

| 按钮 | 做什么 | 接口 | 模块 | 回到界面 |
|---|---|---|---|---|
| F1 选择内容 | `sections`：`route` `evidence_cards` `literature` `materials` `safety`；免责声明固定包含 | 随 F3 提交 | M13 | — |
| F2 选择格式 | `format`：`pdf` / `docx` / `sdf`（SDF + RXN 打成 zip） | 随 F3 提交 | M13 | — |
| F3 生成并下载 | 存进项目历史 | `POST /analyses/{id}/reports` → `GET /reports/{id}/download` | M13 | R11 |
| G1 结果 | `outcome`：`success` / `partial` / `failure` | 随 G4 提交 | M14 | — |
| G2 产率、条件、备注 | 附件单独传 | 随 G4 提交；`POST /experiments/{id}/attachments` | M14 | — |
| G3 授权用于改进模型 | 默认不勾；可撤回 | 随 G4 提交；`PATCH /experiments/{id}` | M14 | — |
| G4 保存 | | `POST /analyses/{id}/routes/{route_id}/steps/{n}/experiments` | M14 | R12 “已做 · 实际 78%” |

项目历史（分析记录、生成过的报告）：`GET /projects/{id}/history`。

## 账号和其他

| 用途 | 接口 |
|---|---|
| 注册（同时建课题组）、登录、当前用户 | `POST /auth/register`、`POST /auth/login`、`GET /auth/me` |
| 组内成员 | `GET /org/members`、`POST /org/members`（管理员） |
| 项目 | `GET /projects`、`POST /projects` |
| 分析列表（可按项目筛） | `GET /analyses?project_id=` |
| 失败或取消以后重跑 | `POST /analyses/{id}/retry` |
| 我的草稿列表、删除草稿 | `GET /drafts`、`DELETE /drafts/{id}` |
| 删除预设 | `DELETE /presets/{id}` |
| 画结构图（返回 SVG 图片） | `GET /structures/depict?smiles=` |
| 改、删一条库存 | `PATCH /inventory/items/{id}`、`DELETE /inventory/items/{id}` |
| 报告里的物料清单预览 | `GET /analyses/{id}/reports/materials?route_id=` |
| 这次分析的回填记录 | `GET /analyses/{id}/experiments` |
| 实验附件的列表和下载 | `GET /experiments/{id}/attachments`、`GET /experiments/attachments/{attachment_id}` |
| 各模块现在接的是什么 | `GET /meta/modules` |
| 数据来源和许可登记表 | `GET /meta/data-sources` |
| 这次分析的大模型用量 | `GET /analyses/{id}/llm-usage` |
| 健康检查（不用登录） | `GET /health` |

## 中文和英文

返回的文字默认是中文。请求带 `?lang=en` 或请求头 `X-Language: en` 时，标签、原因、进度说明、报错是英文；字段名和枚举值不变。
进度推送用 `…/events?access_token=…&lang=en`。详见 [前端对接手册](前端对接手册.md) 3.11。

## 进度推送（R3）

`GET /api/analyses/{id}/events` 是 Server-Sent Events。事件依次是：

```
event: status             data: {"status": "running"}
event: stage              data: {"index": 1, "status": "running", "message": ""}
event: stage              data: {"index": 1, "status": "done", "message": "合并 9 条约束，其中 2 条来自一句话要求"}
…
event: preliminary_ready  （第 2 阶段完成：可以先看初步结果）
event: scored             （第 7 阶段完成：结果页可以打开了）
event: done               data: {"status": "succeeded", "error": null}
event: end
```

浏览器自带的 `EventSource` 不能加请求头，所以这个接口也接受 `?access_token=`。
断线重连时带上 `Last-Event-ID`，会从断开的地方接着发。关掉页面不影响分析，它在后台继续跑。

同一次分析可以跑好几轮（失败或取消以后重跑、路线被排光以后重新搜索、服务重启以后接着跑）。
除了第一轮，每一轮开头会多一条 `event: status`、`data: {"status": "queued"}`。新连上来（不带 `Last-Event-ID`）时
只发最近一轮的事件，所以收到的 `done` 一定是这一轮的；收到 `end` 就可以关掉连接。

下载文件的接口（报告、对比表、附件）和 `GET /structures/depict` 只认请求头里的令牌，不能直接写成 `<a href>` 或 `<img src>`，
要用带请求头的请求取回来再用。接口返回的时间都是带时区的 ISO 8601 字符串（`2026-10-11T08:30:00+00:00`，UTC）。

## 评分公式（前端重算用）

`GET /api/scoring/config` 返回权重和系数。每条路线的每一步都带六个维度的分数（`dimensions`，没有数据的是 `null`）和 `soft_penalty`：

```
步骤分 = Σ(维度分 × 权重) ÷ Σ(有数据的维度的权重) − soft_penalty     小于 0 时取 0，保留一位小数
路线分 = 0.6 × 各步步骤分的平均 + 0.4 × 最低的步骤分                  用保留过一位小数的步骤分算，结果再保留一位小数
```

要在浏览器里算出和后端一模一样的数（求和、取整的细节，`thin` 的步骤怎么处理，名次怎么排），
照 [前端对接手册](前端对接手册.md) 第 8 节的公式和代码做。

每一步还带两个字段，说明这个分数有多少依据：

- `coverage`：有数据的维度占了多少权重（0–1）。比如只有可得性和绿色化学有数据时是 0.2。
- `thin`：`coverage` 低于配置里的 `min_assessed_weight`（现在是 0.3）时为 `true`。这样的步骤分数只是拿小项凑出来的，
  界面上应当标出来；它算低置信度，并且不计入路线分（除非整条路线每一步都这样）。

维度分是 `null` 表示“没评估”，不是 0 分也不是满分：先例库没导入时的文献依据、没有文献产率时的产率、
用到的物质都查不到危害数据时的安全、不知道试剂和条件时的操作复杂度（`operation_label` 是“操作 未评估”）。

排序：按路线分从高到低；除“探索”档外，低置信度路线不排在高置信度路线前面。

专利不计入证据等级（2026-10-24 起）：证据等级、星数、文献依据这一维的分数只算期刊论文（`data/scoring.toml` 的 `literature_doc_types`）。
置信度、“最多几步没有先例”和“最低证据等级”两条筛选看把专利也算上的等级：证据卡里的 `evidence.level_with_patents`，
路线卡片每一步的 `precedent_level`。专利照样列在先例里，证据卡的 `precedent_groups` 里专利那一组 `collapsed` 为 `true`。

## 证据卡里和“同类反应”有关的字段

证据卡（`GET …/steps/{n}`）和先例列表（`GET …/steps/{n}/precedents`）里：

- `center`：这一步改了哪些键，比如 `F:C.acyl~N:1|L:C.acyl~Cl:1`（成了酰基碳–氮键，酰基碳上的氯掉了）。
  逆合成引擎给了原子映射才有，演示路线是 `null`。写法见 `syntherisk/modules/m01_structure.py` 里的 `reaction_center`。
- `evidence.compared_by`：这一步是怎么判断“同类反应”的。`center` 按反应中心（准），`fingerprint` 按指纹（粗）。
- 每条先例的 `compared_by`：`exact` 同一反应；`center` 按反应中心认定；`fingerprint` 按指纹认定。
- 每条先例的 `same_class`：`true` 是同类反应；`false` 是相关反应（成的键一样、离去基团不一样，
  或者先例没有原子映射、没法确认），界面上应当和同类反应分开显示。相关反应不参与产率统计，最多把这一步定到 E3 的最低一段。
- 每条先例的 `similarity` 是底物（产物结构）的相似度，`class_similarity` 是反应一致的程度（1 表示反应中心完全相同）。
