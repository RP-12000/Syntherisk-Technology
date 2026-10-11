"""Claude（Anthropic 官方 SDK）。

默认型号是 PPT 里“便宜”一档列出的 Claude Haiku 5.5；要换型号改 LLM_MODEL。
几个和这个型号有关的写法：
  - 结构化输出用 output_config.format，保证返回的是符合 Schema 的 JSON；
  - 思考默认开启（自适应），深度用 output_config.effort 控制，不传 temperature；
  - 模型可能因安全策略拒答（stop_reason == "refusal"），这里转成 LLMRefusal，由调用方退回模板；
  - 系统提示词在一次分析的十几次调用里是相同的，所以打了缓存标记。

什么时候用到这个文件
    .env 里 LLM_PROVIDER=anthropic 时，base.py 的 build_provider() 建一个 AnthropicProvider，
    之后 M2、M11、M12 的每次大模型调用都经 LLMService 走到这里的 complete_json()。
    相关的配置：LLM_MODEL（型号，留空用 DEFAULT_MODEL）、LLM_API_KEY（留空则由 SDK 自己找凭证）、
    LLM_BASE_URL（一般留空）、LLM_EFFORT（思考深度）、LLM_MAX_TOKENS（一次回复的 token 上限）、
    LLM_TIMEOUT（一次请求的超时秒数）。LLM_JSON_MODE 对这个文件不起作用。

本文件里有什么
    AnthropicProvider   Claude 的厂商适配类
        __init__        记下型号和创建客户端要用的参数（不联网）
        _get_client     第一次用到时创建 SDK 客户端
        complete_json   发一次请求，返回 LLMResponse；各种失败都转成 LLMError
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from syntherisk.llm.base import LLMError, LLMRefusal, LLMResponse


class AnthropicProvider:
    """Claude 的厂商适配类，满足 base.py 里 LLMProvider 的要求（有 name、model 和 complete_json）。

    类属性：
        name           "anthropic"。记进 llm_calls 表的 provider 列，也填在 LLMResponse.provider 里。
        DEFAULT_MODEL  LLM_MODEL 留空时用的型号。
    实例属性：
        model       这个对象用的型号。
        effort      思考深度："low" / "medium" / "high"。
        max_tokens  一次回复最多生成多少 token；不够用时这次调用按失败处理（见 complete_json）。
    """

    # 厂商适配类的名字（LLMProvider 要求有这个属性）
    name = "anthropic"
    # LLM_MODEL 留空时用的型号
    DEFAULT_MODEL = "claude-haiku-5-5"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        effort: str = "low",
        max_tokens: int = 8192,
        timeout: float = 60.0,
        client: Any | None = None,
    ):
        """记下型号和创建客户端要用的参数。这里不联网，也还不创建 SDK 客户端。

        参数（都必须用关键字传）：
            model       型号，比如 AnthropicProvider.DEFAULT_MODEL。
            api_key     密钥。None 表示不显式给，由 SDK 自己找凭证。
            base_url    接口地址。None 表示用 SDK 默认的地址。
            effort      思考深度，原样放进请求的 output_config.effort。
            max_tokens  一次回复的 token 上限。
            timeout     一次请求最多等多少秒。
            client      现成的客户端对象。测试里传一个假的进来，这样完全不联网；正常使用时不传。
        """
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client
        # 创建 SDK 客户端时用的参数。max_retries=2：连不上、超时、限流、服务端出错时，SDK 自己再试两次，
        # 所以最坏情况下一次调用要等大约三个 timeout 那么久
        self._client_options: dict[str, Any] = {"timeout": timeout, "max_retries": 2}
        # 没给的就不放进参数里，让 SDK 用它自己的默认做法
        if api_key:
            self._client_options["api_key"] = api_key
        if base_url:
            self._client_options["base_url"] = base_url

    def _get_client(self):
        """取 SDK 客户端：第一次调用时才创建，之后一直用同一个。

        测试里通过 __init__ 的 client 参数塞了假客户端时，这里直接返回它。
        谁在用：
            本类的 complete_json。
        """
        # 没有显式给 key 时，SDK 会自己从环境变量（ANTHROPIC_API_KEY 等）或本机登录的 profile 里找凭证
        if self._client is None:
            self._client = anthropic.Anthropic(**self._client_options)
        return self._client

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        """向 Claude 发一次请求，要它按 schema 返回一个 JSON 对象。

        参数（都必须用关键字传）：
            system       系统提示词。带缓存标记发出去。
            user         用户消息，作为唯一的一条 user 消息。
            schema       JSON Schema，放进请求的 output_config.format，由接口保证输出符合它。
            schema_name  这里没有用到（请求里的 json_schema 格式不带名字）；
                         留着是为了和 LLMProvider 规定的参数一致。
        返回：
            LLMResponse：data 是解析好的 JSON；provider 是 "anthropic"；model 是接口在回复里报的型号；
            input_tokens、output_tokens 取自回复的 usage；
            latency_ms 是这次调用（含 SDK 自己的重试）花的毫秒数。
        抛出：
            LLMRefusal  模型因安全策略拒答（stop_reason 是 "refusal"）。
            LLMError    限流；接口返回错误状态码；连不上；SDK 的其他错误；
                        输出被 max_tokens 截断；回复里没有文本；文本不是合法 JSON。
        副作用：
            发网络请求（SDK 自己最多重试 2 次）。不写数据库。
        谁在用：
            base.py 的 LLMService.complete_json（通过 LLMProvider 这个接口）；tests/test_llm.py。
        """
        # monotonic 是只增不减的计时器，不受系统改时间的影响，适合算耗时
        started = time.monotonic()
        try:
            response = self._get_client().messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # 系统提示词写成“内容块”的形式，才能带上 cache_control：同样的提示词在缓存有效期内
                # （ephemeral 默认 5 分钟）再次发送时，接口可以读缓存，这部分输入按更低的价格计费。
                # 提示词短于型号规定的最小可缓存长度时，这个标记不起作用，也不会报错
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                # effort 控制思考深度；format 要求输出是符合 schema 的 JSON（结构化输出）
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        # 下面几个 except 从具体到笼统排列：RateLimitError 是 APIStatusError 的子类，要排在它前面才轮得到；
        # 最后的 AnthropicError 是 SDK 所有错误的基类，兜住其余情况。统一转成 LLMError；
        # “from exc” 把原来的错误留在异常链里，看日志时还找得到
        except anthropic.RateLimitError as exc:
            raise LLMError("大模型接口限流，稍后再试") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"大模型接口返回错误（HTTP {exc.status_code}）：{exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("连不上大模型接口") from exc
        except anthropic.AnthropicError as exc:
            raise LLMError(f"大模型调用失败：{exc}") from exc

        # 先看停止原因，再读内容：拒答和被截断时内容不完整，不能当正常结果用
        if response.stop_reason == "refusal":
            raise LLMRefusal("模型出于安全策略没有回答")
        if response.stop_reason == "max_tokens":
            raise LLMError("模型输出被截断（LLM_MAX_TOKENS 太小）")
        # 开了思考时，回复前面可能有 thinking 块，所以按类型找文本，不能直接取第一个
        text = next((block.text for block in response.content if block.type == "text"), None)
        if not text:
            raise LLMError("模型没有返回内容")
        try:
            data = json.loads(text)
        except ValueError as exc:
            # json.JSONDecodeError 是 ValueError 的子类，这里接的就是“不是合法 JSON”
            raise LLMError("模型返回的不是合法 JSON") from exc
        return LLMResponse(
            data=data,
            provider=self.name,
            # 用接口在回复里报的型号，不用配置里写的那个
            model=response.model,
            # usage.input_tokens 只算没走缓存的输入；写入缓存和读缓存的 token 数在 usage 的
            # 另外两个字段里，这里没有记
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
