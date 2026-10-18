"""大模型调用层的公共部分：错误类型、返回值的格式、厂商要满足的接口、按配置选厂商、业务模块用的入口。

厂商是怎么从 .env 选出来的、哪些模块在调用大模型，见同目录 __init__.py 的文件头。

调用关系
    业务模块（M2 约束解析、M11 写说明、M12 问答）
        → LLMService.complete_json()      统一的入口；负责把每次调用的用量记进 llm_calls 表
            → provider.complete_json()    具体厂商：AnthropicProvider 或 OpenAICompatibleProvider
                → 厂商的接口（联网）
    厂商这一层只负责三件事：发请求、把回复变成 LLMResponse、把各种失败变成 LLMError。
    核对模型写的内容（出处是不是有效、数字是不是有依据）是业务模块自己的事。

本文件里有什么
    LLMError        调用失败
    LLMRefusal      模型拒答（LLMError 的一种）
    LLMResponse     一次成功调用的结果
    LLMProvider     厂商适配类要长成什么样（协议）
    build_provider  按配置建厂商适配对象
    LLMService      业务模块用的入口
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from syntherisk import i18n, models
from syntherisk.config import Settings


class LLMError(RuntimeError):
    """大模型调用失败。业务模块收到后应当退回规则或模板，而不是让整个请求失败。

    厂商适配类把限流、网络不通、接口报错、输出被截断、返回的不是合法 JSON 等情况都转成这个错误，
    报错信息是可以直接给人看的中文。没有配置大模型时调用 LLMService.complete_json，抛的也是它。
    接住它的地方：modules/m02_constraints.py、modules/m11_explain.py、modules/m12_qa.py。
    """


class LLMRefusal(LLMError):
    """模型出于安全策略拒绝回答。

    是 LLMError 的子类，所以只写了 except LLMError 的地方也接得住它，处理办法一样：退回规则或模板。
    """


@dataclass
class LLMResponse:
    """一次成功调用的结果。由厂商适配类填好；业务模块读 data、provider、model，其余三项用来记用量。"""

    # 模型返回的 JSON 对象，已经解析成字典。内容是按调用时给的 JSON Schema 要求模型生成的；
    # 厂商不支持按 Schema 约束输出时不保证完全符合，所以业务模块拿到以后还会自己核对
    data: dict[str, Any]
    # 厂商适配类的名字："anthropic" 或 "openai_compatible"
    provider: str
    # 实际作答的型号（接口在回复里报的那个）
    model: str
    # 输入、输出各用了多少 token，取自接口返回的用量
    input_tokens: int = 0
    output_tokens: int = 0
    # 这次调用从发出请求到拿到结果用了多少毫秒
    latency_ms: int = 0


class LLMProvider(Protocol):
    """厂商适配类要长成什么样。

    Protocol 是“按长相认”的接口：一个类只要有下面这两个属性和这个方法，就算是 LLMProvider，
    不需要继承它（AnthropicProvider、OpenAICompatibleProvider 都没有继承）。
    它只在类型标注里用，给类型检查工具和读代码的人看。

    属性：
        name   厂商适配类的名字。记进 llm_calls 表的 provider 列，也出现在 LLMService.describe() 里。
        model  配置的型号。
    """

    name: str
    model: str

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        """按给定的 JSON Schema 返回结构化结果。失败时抛 LLMError。

        参数（都必须用关键字传）：
            system       系统提示词：模型的角色和必须遵守的规则。同一类调用每次都一样。
            user         用户消息：这一次的具体内容（证据卡的 JSON、用户的一句话要求等）。
            schema       JSON Schema（字典），规定返回的 JSON 对象有哪些字段。
            schema_name  这个 Schema 的名字（英文标识，比如 "step_explanation"）；有的厂商接口要求给。
        返回：
            LLMResponse。data 是解析好的字典；provider、model 要填；
            input_tokens、output_tokens、latency_ms 能拿到就填，拿不到留 0。
        抛出：
            LLMRefusal  模型拒答。
            LLMError    其他任何失败（限流、连不上、接口报错、被截断、不是合法 JSON……）。
                        厂商 SDK 或 HTTP 库自己的异常不要漏出去：业务模块只接 LLMError。
        副作用：
            发网络请求。不写数据库，用量由 LLMService 记。
        """
        ...


def build_provider(settings: Settings) -> LLMProvider | None:
    """按配置（.env）建厂商适配对象。整个后端只有这里知道一共有哪几家厂商。

    参数：
        settings  运行配置（config.py 的 Settings）。用到 llm_provider、llm_model、llm_api_key、
                  llm_base_url、llm_effort、llm_max_tokens、llm_timeout、llm_json_mode，
                  分别对应 .env 里同名的大写变量。
    返回：
        LLM_PROVIDER=anthropic          → AnthropicProvider
        LLM_PROVIDER=openai_compatible  → OpenAICompatibleProvider
        LLM_PROVIDER=offline（默认）     → None，表示不调大模型
    抛出：
        RuntimeError  选了 openai_compatible，但 LLM_BASE_URL、LLM_MODEL、LLM_API_KEY 没有配齐。
                      后端因此在启动时就报错，而不是等到第一次调用才发现。
    副作用：
        不联网。建 AnthropicProvider 时还不创建 SDK 客户端（第一次调用时才建）；
        建 OpenAICompatibleProvider 时会创建一个 HTTP 客户端，但不发请求。
    谁在用：
        context.py 的 build_context（测试里可以直接塞一个假的厂商对象，那时不走这里）。
    """
    if settings.llm_provider == "anthropic":
        # 用到哪家才导入哪家的模块：不用 Claude 时就不加载 anthropic 这个包
        from syntherisk.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            # 型号留空就用默认型号；密钥和地址留空就传 None，由 SDK 自己决定
            model=settings.llm_model or AnthropicProvider.DEFAULT_MODEL,
            api_key=settings.llm_api_key or None,
            base_url=settings.llm_base_url or None,
            effort=settings.llm_effort,
            max_tokens=settings.llm_max_tokens,
            timeout=settings.llm_timeout,
        )
    if settings.llm_provider == "openai_compatible":
        from syntherisk.llm.openai_compatible import OpenAICompatibleProvider

        # 这三项没有默认值可用，缺哪一项都没法调用
        if not (settings.llm_base_url and settings.llm_model and settings.llm_api_key):
            raise RuntimeError("LLM_PROVIDER=openai_compatible 需要同时设置 LLM_BASE_URL、LLM_MODEL 和 LLM_API_KEY")
        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
            json_mode=settings.llm_json_mode,
        )
    # offline：不调大模型
    return None


class LLMService:
    """业务模块用的入口：调用大模型，并把每次调用的用量记进 llm_calls 表。

    每个进程只有一个，在 ctx.llm 上（context.py 的 build_context 建的）。

    属性：
        provider  厂商适配对象；None 表示没有配置大模型（LLM_PROVIDER=offline）。
    业务模块里的用法：
        先看 llm.enabled；是 True 才调用 llm.complete_json(...)，并且用 except LLMError 接住失败、
        退回规则或模板。
    """

    def __init__(self, provider: LLMProvider | None, session_factory: sessionmaker[Session] | None = None):
        """记下厂商适配对象和记用量要用的会话工厂。这里不联网，也不碰数据库。

        参数：
            provider         厂商适配对象（build_provider 的返回值）；None 表示不调大模型。
            session_factory  数据库会话工厂，用来往 llm_calls 表记用量。不给就不记（测试里常这样）。
        """
        self.provider = provider
        self._session_factory = session_factory

    @property
    def enabled(self) -> bool:
        """有没有配置大模型。业务模块先看它：是 False 就直接用规则或模板，不去调用 complete_json。

        加了 @property，用的时候写 llm.enabled，不带括号。
        谁在用：
            modules/m02_constraints.py、modules/m11_explain.py、modules/m12_qa.py；
            pipeline.py 的 _explain（决定第 8 阶段的进度说明怎么写）。
        """
        return self.provider is not None

    def describe(self) -> dict[str, Any]:
        """现在接的是哪家、什么型号。

        返回：
            没配置大模型：{"provider": "offline", "model": None, "note": "未配置大模型：用规则和模板生成"}
            配置了：      {"provider": 厂商适配类的名字, "model": 配置的型号}
        谁在用：
            context.py 的 AppContext.describe_modules（M2、M11、M12 三项），也就是
            GET /api/meta/modules 和 python -m syntherisk.cli status 输出里的内容。
        """
        if self.provider is None:
            return {"provider": "offline", "model": None,
                    "note": i18n.tr("未配置大模型：用规则和模板生成",
                                    "No language model is configured: rules and templates are used instead")}
        return {"provider": self.provider.name, "model": self.provider.model}

    def complete_json(
        self,
        *,
        module: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        org_id: str | None = None,
        analysis_id: str | None = None,
    ) -> LLMResponse:
        """调用一次大模型，要它按 JSON Schema 返回一个 JSON 对象，并记下这次调用的用量。

        参数（都必须用关键字传）：
            module       哪个模块在调用："M2" / "M11" / "M12"。记进 llm_calls 表，按模块统计用量用。
            system       系统提示词。
            user         用户消息。
            schema       JSON Schema（字典）。
            schema_name  Schema 的名字。
            org_id       哪个课题组的调用，记用量用，可以不给。
            analysis_id  属于哪次分析，记用量用；不属于某次分析的调用（比如约束解析）不给。
        返回：
            LLMResponse，就是厂商适配对象返回的那个。
        抛出：
            LLMError    没有配置大模型；或者厂商那一层调用失败（原样往上抛）。
            LLMRefusal  模型拒答（LLMError 的子类）。
        副作用：
            发网络请求；往 llm_calls 表写一行（成功、失败都写；没给 session_factory 时不写）。
        谁在用：
            modules/m02_constraints.py 的 ConstraintParser.parse；
            modules/m11_explain.py 的 Explainer.explain_step、explain_route；
            modules/m12_qa.py 的 QuestionAnswerer.answer。
        """
        if self.provider is None:
            # 错误说明会被业务模块拼进返回值的 note，所以用 i18n.tr 配上英文（厂商适配类里的说明也一样）
            raise LLMError(i18n.tr("没有配置大模型", "no language model is configured"))
        try:
            response = self.provider.complete_json(system=system, user=user, schema=schema, schema_name=schema_name)
        except LLMError as exc:
            # 失败也记一行（ok=False；错误信息最多留 300 个字符，和 llm_calls.error 列的长度一致），
            # 再把错误原样抛出去。只接 LLMError：厂商适配类已经把自己那一层的失败都转成了它
            self._record(module, org_id, analysis_id, ok=False, error=str(exc)[:300])
            raise
        self._record(module, org_id, analysis_id, ok=True, response=response)
        return response

    def _record(self, module, org_id, analysis_id, *, ok: bool, response: LLMResponse | None = None, error: str | None = None):
        """往 llm_calls 表记一行这次调用的用量。

        参数：
            module、org_id、analysis_id  同 complete_json。
            ok        调用是否成功。
            response  成功时的结果，从里面取型号、token 数和耗时。
            error     失败时的错误信息。
        副作用：
            自己开一个数据库会话、写一行、马上提交，不借用调用方的会话：
            调用方后来回滚的话，用量记录也还在。没给 session_factory 时什么都不做。
            失败的调用记的是配置的型号，token 数和耗时都记 0。
        谁在用：
            本类的 complete_json。
        """
        # 没给会话工厂（比如测试里）就不记
        if self._session_factory is None:
            return
        # with 结束时会话自动关闭；里面 add 一行、commit 一次
        with self._session_factory() as session:
            session.add(
                models.LLMCall(
                    org_id=org_id,
                    analysis_id=analysis_id,
                    module=module,
                    provider=self.provider.name,
                    # 成功时记接口实际作答的型号；失败时没有回复，记配置的型号
                    model=response.model if response else self.provider.model,
                    input_tokens=response.input_tokens if response else 0,
                    output_tokens=response.output_tokens if response else 0,
                    latency_ms=response.latency_ms if response else 0,
                    ok=ok,
                    error=error,
                )
            )
            session.commit()
