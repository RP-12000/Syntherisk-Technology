# syntherisk.modules：产品设计 PPT 里编号的业务模块 M1–M14，基本上一个模块一个文件。
#
# 这个文件里没有任何代码。它的存在只是告诉 Python “modules 这个文件夹是一个包”，
# 别处才能写 from syntherisk.modules import m01_structure。
# 说明写成 # 注释、没有写成文档字符串，是为了让这个文件保持“一行代码都没有”。
#
# 包里有什么（各文件自己的文件头有详细说明）：
#   m01_structure.py       M1   结构工具（RDKit）：解析和校验结构、画图、指纹、反应中心。
#                               别的文件几乎都用它，惯例是 from syntherisk.modules import m01_structure as chem
#   m01_resolver.py        M1   名称 / CAS 号 → 结构：内置试剂表 → PubChem → OPSIN
#   m02_constraints.py     M2   一句话补充要求 → 约束标签（大模型，或者关键词规则）
#   m03_retrosynthesis.py  M3   逆合成搜索：按配置选引擎、发起搜索
#   aizynth_bridge.py      M3   真正调用 AiZynthFinder 的脚本，默认由 m03 另起一个进程来运行
#   m04_availability.py    M4   可得性与库存
#   m05_precedents.py      M5   文献先例、证据等级
#   m06_safety.py          M6   毒性与法规
#   m07_conditions.py      M7   条件与复杂度
#   m08_yield.py           M8   产率模型（暂不实现，只留接口）
#   m09_green.py           M9   绿色化学
#   m10_scoring.py         M10  评分与排序
#   m11_explain.py         M11  解释（大模型）
#   m12_qa.py              M12  问答（大模型）
#   m13_reports.py         M13  报告生成
#   m14_backfill.py        M14  回填与校准
#
# 谁来调这些模块：
#   syntherisk/pipeline.py  “开始分析”之后的八个阶段，依次用到 M3 → M4 → M5 → M6 → M7、M9 → M10 → M11
#   syntherisk/api/         接口层，直接用到 M1、M2、M4、M10、M11、M12、M13、M14
# 不在这个包里的：M0 应用服务是 syntherisk/api/，MQ 任务队列是 syntherisk/taskqueue.py；
# 约束的数据结构（Constraints 等）在 syntherisk/constraints.py。
