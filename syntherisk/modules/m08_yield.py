"""M8 产率模型：暂时不实现，这里只留接口。

现在产率这一项只用文献报道值（M5 从先例里取），没有报道值的步骤这一项不计分。
等团队的产率模型准备好之后：写一个实现了 YieldPredictor 的类，
在 syntherisk/context.py 的 build_context() 里换掉 NullYieldPredictor，
再在 syntherisk/pipeline.py 第 6 阶段把预测结果写进 reaction["yield"] 即可，其余模块不用动。

PPT 附录 G 对上线的要求：在没见过的底物上比“同类反应的中位数”更准才上线；
显示成 <30% / 30–70% / >70% 三档，并标“模型估计”。

对应关系
    PPT 模块：M8 产率模型。界面上没有专门的按钮；产率显示在证据卡（R6）的“产率”一栏、
    结果页汇总条（R7）的“总产率”，报告的物料清单也按它放大原料用量。
    流水线：第 6 阶段“条件、产率、绿色化学”（syntherisk/pipeline.py 的 AnalysisRunner._conditions）。
    那里现在只有一行注释，标出了以后调用预测器的位置。

谁在用（现在的情况）
    syntherisk/context.py
        build_context() 建一个 NullYieldPredictor，放进 AppContext.yield_predictor；
        AppContext.describe_modules() 用 isinstance 看它是不是 NullYieldPredictor，得出 M8 的 "enabled"，
        通过 GET /api/meta/modules 和 python -m syntherisk.cli status 显示出来。
    现在没有任何代码调用 predict()，也没有任何代码创建 YieldPrediction。
    本文件不读写数据库，不联网，不依赖项目里的其他模块。

reaction["yield"] 现在的样子（由 M5 的 PrecedentSearch._yield_info() 生成，流水线第 4 阶段写入）
    有报道值：{"source": "literature" 或 "similar_literature", "low": 85.0, "high": 92.0, "median": 88.5,
               "n": 2, "label": "文献报道 85–92%（2 个先例）", "refs": ["P1", "P2"]}
               low / high / median 都是 0–100 的百分数，n 是报道了产率的先例个数，refs 是这些先例的编号。
    没有报道值：{"source": "none", "label": "无产率记录", "refs": []}

谁在读 reaction["yield"]（接入模型、往里写预测值之前，先看这几处怎么用它）
    m10_scoring.base_dimensions()    source 是 literature 或 similar_literature 时，median 才当作产率这一维的分数
    m10_scoring._summary()           把各步的 median 连乘成总产率，不看 source
    m13_reports.bill_of_materials()  用 median 倒推原料用量，不看 source
    cards.build_card()               原样放进证据卡的 yield 字段；M11、M12 再从卡里读 label、refs 和 source
    m14_backfill.save_record()       把 median 记进实验回填的快照（reported_yield_median）
    pipeline.AnalysisRunner._conditions()  数有几步的 source 不是 "none"，写进第 6 阶段的进度说明

本文件里有什么
    YieldPrediction     一次产率预测的结果（数据类，五个字段）
    YieldPredictor      预测器的接口（Protocol）：只有一个 predict() 方法
    NullYieldPredictor  占位实现：predict() 一律返回 None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class YieldPrediction:
    """产率模型对一步反应的一次预测结果。

    @dataclass 会按下面的字段自动生成 __init__、__repr__ 和 __eq__，所以可以直接写
    YieldPrediction(band="30–70%", low=..., high=..., reliability=..., model=...)。
    现在没有任何代码创建或读取它，它是留给以后的产率模型用的返回类型。
    """

    # 显示给用户看的档位，三选一（文件头：PPT 附录 G 要求显示成这三档）
    band: str  # "<30%" / "30–70%" / ">70%"
    # 字段名的意思是预测区间的下限。现在没有代码读写它，具体含义和单位要在接入模型时定下来；
    # 项目里其余的产率都用 0–100 的百分数（比如 reaction["yield"]["median"]），应当保持一致
    low: float
    # 预测区间的上限，说明同 low
    high: float
    reliability: str  # 按反应类别标的可靠度
    # 给出这个预测的模型的标识。具体填名字还是版本号，现在没有约定
    model: str


class YieldPredictor(Protocol):
    """产率预测器的接口。

    Protocol 是“结构化类型”：一个类只要有签名相同的 predict() 方法，就算实现了这个接口，
    不需要继承 YieldPredictor。AppContext.yield_predictor 的类型标注用的就是它（syntherisk/context.py），
    所以换实现以后，PyCharm 或 mypy 做类型检查时能发现签名对不上的情况。

    predict() 的约定（方法体只有 ...，表示这里只声明签名，没有实现）：
        参数：
            reaction_smiles  这一步的反应 SMILES。现在没有调用方；流水线里现成的是
                             reaction["reaction_smiles"]，写法是“反应物>试剂>产物”，同一侧的多个分子用 . 隔开
                             （见 m01_structure.build_reaction_smiles）。
            reaction_class   反应类别，可以是 None。具体传哪个字段（比如 reaction["classification"]），
                             要在接入模型时定。
        返回：
            YieldPrediction；对这一步给不出预测时返回 None。
    """

    # 接口方法：只有签名，没有实现。参数和返回值的说明写在上面类的文档字符串里。
    def predict(self, reaction_smiles: str, reaction_class: str | None) -> YieldPrediction | None: ...


class NullYieldPredictor:
    """没有模型：一律不预测。

    YieldPredictor 的占位实现。build_context() 默认把它放进 AppContext.yield_predictor；
    AppContext.describe_modules() 看到产率预测器是这个类，就把 M8 的 "enabled" 报告成 false。
    没有属性，不保存任何状态。
    """

    def predict(self, reaction_smiles: str, reaction_class: str | None) -> YieldPrediction | None:
        """不看参数，直接返回 None，表示“没有预测”。

        参数的含义见 YieldPredictor。没有副作用，不抛错。现在没有代码调用它。
        """
        return None
