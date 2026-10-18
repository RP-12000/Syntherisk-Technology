"""兼容 OpenAI Chat Completions 协议的厂商：OpenAI、阿里云百炼（Qwen）、DeepSeek、Gemini 的兼容端点等。

这些厂商的官方 SDK 各不相同，但 /chat/completions 这个 HTTP 接口是通用的，所以直接用 httpx 调，
换厂商只需要改 LLM_BASE_URL、LLM_MODEL 和 LLM_API_KEY。

LLM_JSON_MODE：
    json_schema   厂商支持按 Schema 约束输出时用（更可靠）
    json_object   只支持“输出 JSON”时用；Schema 会写进提示词里，再由后端校验

什么时候用到这个文件
    .env 里 LLM_PROVIDER=openai_compatible 时，base.py 的 build_provider() 建一个 OpenAICompatibleProvider
    （LLM_BASE_URL、LLM_MODEL、LLM_API_KEY 三项必须都有），之后 M2、M11、M12 的每次大模型调用
    都经 LLMService 走到这里的 complete_json()。
    LLM_TIMEOUT 是一次请求的超时秒数。LLM_EFFORT、LLM_MAX_TOKENS 对这个文件不起作用：
    请求里不带这两项。

和 anthropic_provider.py 的不同
    - 不用 SDK：自己拼请求、自己读回复；失败不重试。
    - 密钥放在请求头 “Authorization: Bearer <密钥>” 里。

本文件里有什么
    OpenAICompatibleProvider   厂商适配类
        __init__               拼好接口地址，建 HTTP 客户端（不发请求）
        complete_json          发一次 POST 请求，返回 LLMResponse；各种失败都转成 LLMError
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from syntherisk import i18n
from syntherisk.llm.base import LLMError, LLMRefusal, LLMResponse


class OpenAICompatibleProvider:
    """兼容 OpenAI Chat Completions 协议的厂商适配类，满足 base.py 里 LLMProvider 的要求。

    类属性：
        name       "openai_compatible"。记进 llm_calls 表的 provider 列，也填在 LLMResponse.provider 里。
    实例属性：
        model      型号，原样放进请求的 model 字段（各家的型号名以各家的文档为准）。
        json_mode  "json_schema" 或 "json_object"，含义见文件头。
    """

    # 厂商适配类的名字（LLMProvider 要求有这个属性）
    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        json_mode: str = "json_schema",
        transport: httpx.BaseTransport | None = None,
    ):
        """拼好接口地址，建一个带密钥的 HTTP 客户端。这里不发请求。

        参数（都必须用关键字传）：
            base_url   接口地址，写到 /v1 这一层，比如 https://api.openai.com/v1；末尾带不带斜杠都行。
            api_key    密钥。
            model      型号。
            timeout    一次请求最多等多少秒。
            json_mode  "json_schema"（默认）或 "json_object"。
            transport  httpx 的传输层。测试里传 httpx.MockTransport 来模拟厂商的回复，这样不联网；
                       正常使用时不传（None），走真实网络。
        """
        self.model = model
        self.json_mode = json_mode
        # 完整的接口地址：去掉末尾的斜杠，再接上 /chat/completions
        self._url = base_url.rstrip("/") + "/chat/completions"
        # 这个客户端在对象存在期间一直复用；headers 里的内容每个请求都会带上，这里是密钥
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {api_key}"}
        )

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> LLMResponse:
        """向厂商的 /chat/completions 发一次请求，要它返回一个 JSON 对象。

        参数（都必须用关键字传）：
            system       系统提示词。json_object 模式下，后面会接上一段“只输出 JSON、要符合这个 Schema”的要求。
            user         用户消息。
            schema       JSON Schema。json_schema 模式下放进请求的 response_format，由厂商按它约束输出；
                         json_object 模式下只写进提示词，厂商只保证输出是 JSON。
            schema_name  Schema 的名字，json_schema 模式下放进请求。
        返回：
            LLMResponse：data 是解析好的 JSON 对象（一定是字典）；provider 是 "openai_compatible"；
            model 是回复里报的型号，回复里没有就用配置的；input_tokens、output_tokens 取自回复的
            usage.prompt_tokens、usage.completion_tokens，没有就是 0；latency_ms 是这次请求花的毫秒数。
        抛出：
            LLMRefusal  回复的 message 里有 refusal（模型拒答）。
            LLMError    连不上或超时；状态码不是 200（报错信息里带状态码和回复的前 200 个字符）；
                        回复不是预期的结构；content 不是合法 JSON；JSON 不是对象。
        副作用：
            发一次 HTTP POST（不重试）。不写数据库。
        谁在用：
            base.py 的 LLMService.complete_json（通过 LLMProvider 这个接口）；tests/test_llm.py。
        """
        if self.json_mode == "json_schema":
            # 让厂商按 Schema 约束输出；strict=True 是要求严格照 Schema 来
            response_format: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": schema, "strict": True},
            }
        else:
            # 厂商只保证“输出是一个 JSON 对象”，所以把 Schema 的原文附在系统提示词后面告诉模型。
            # ensure_ascii=False：Schema 里的中文原样写出，不转成 \uXXXX 这样的转义
            response_format = {"type": "json_object"}
            # 这句要求跟着系统提示词的语言走：业务模块在要英文时发的是英文提示词（见 m11_explain.py），
            # 后面接一句中文容易把模型带回中文。中文时这一段和以前一字不差
            if i18n.current_lang() == "en":
                demand = "Output a single JSON object and nothing else. It must conform to the following JSON Schema:"
            else:
                demand = "只输出一个 JSON 对象，不要有任何其他文字。它必须符合下面的 JSON Schema："
            system = (
                f"{system}\n\n{demand}\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": response_format,
        }
        # monotonic 是只增不减的计时器，不受系统改时间的影响，适合算耗时
        started = time.monotonic()
        try:
            # json=body：httpx 把字典转成 JSON 放进请求体
            response = self._http.post(self._url, json=body)
        except httpx.HTTPError as exc:
            # 网络层面的失败（连不上、超时、连接中断……）。报错里只留错误类型的名字。
            # 错误说明都用 i18n.tr 写：业务模块会把它拼进返回值的 note，要英文时用的是这里配的英文
            # （英文句首不大写，因为它是嵌在 note 那句话的括号里的）
            raise LLMError(i18n.tr("连不上大模型接口：{error}", "could not connect to the model API: {error}",
                                   error=type(exc).__name__)) from exc
        if response.status_code != 200:
            # 密钥不对、限流、型号写错等都走这里；带上回复的前 200 个字符方便排查
            raise LLMError(i18n.tr("大模型接口返回错误（HTTP {status}）：{detail}", "the model API returned an error (HTTP {status}): {detail}",
                                   status=response.status_code, detail=response.text[:200]))
        try:
            payload = response.json()
            # 回复的结构：{"choices": [{"message": {"content": "…"}}], "usage": {…}, "model": "…"}
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            # TypeError：整段回复不是字典，或者 choices 是 null（有的网关出错时也回 200，但内容是这样的）
            raise LLMError(i18n.tr("大模型接口返回的格式不对", "the model API returned a response in an unexpected format")) from exc
        if not isinstance(message, dict):
            raise LLMError(i18n.tr("大模型接口返回的格式不对", "the model API returned a response in an unexpected format"))
        if message.get("refusal"):
            raise LLMRefusal(i18n.tr("模型出于安全策略没有回答", "the model declined to answer under its safety policy"))
        try:
            # content 是模型写的文本，应当是一段 JSON；是空的或者没有时按空字符串解析，同样算不合法
            data = json.loads(message.get("content") or "")
        except (ValueError, TypeError) as exc:
            # TypeError：content 不是一段文本（有的网关把它拆成了一个列表）
            raise LLMError(i18n.tr("模型返回的不是合法 JSON", "the model did not return valid JSON")) from exc
        # 合法的 JSON 也可能是列表或数字；业务模块按字典来用，所以在这里卡一道
        if not isinstance(data, dict):
            raise LLMError(i18n.tr("模型返回的 JSON 不是对象", "the JSON returned by the model is not an object"))
        usage = payload.get("usage") or {}
        return LLMResponse(
            data=data,
            provider=self.name,
            model=payload.get("model") or self.model,
            # 这个协议里输入、输出的 token 数叫 prompt_tokens、completion_tokens
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
