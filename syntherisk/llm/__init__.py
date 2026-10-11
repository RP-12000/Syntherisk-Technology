"""大模型调用层。

PPT 的要求：把调用封装成一层，换厂商只改一处。这一处就是配置（.env 里的 LLM_PROVIDER 等几项），
业务模块（M2 约束解析、M11 写说明、M12 问答）只认 LLMService，不知道背后是哪家。

    LLM_PROVIDER=offline             不调大模型，用规则和模板（默认）
    LLM_PROVIDER=anthropic           Claude，走 Anthropic 官方 SDK
    LLM_PROVIDER=openai_compatible   任何兼容 OpenAI Chat Completions 的接口
                                     （OpenAI、阿里云百炼 Qwen、DeepSeek、Gemini 的兼容端点等）

数据政策（PPT 附录 B）：客户的分子结构只走默认不拿 API 数据训练的付费接口。

从 .env 到一次调用（厂商是怎么选出来的）
    1. config.py 的 Settings 读 .env 里的这几项（变量名就是字段名的大写）：
           LLM_PROVIDER    offline / anthropic / openai_compatible，默认 offline
           LLM_MODEL       型号。anthropic 可以留空（用 AnthropicProvider.DEFAULT_MODEL）；
                           openai_compatible 必填
           LLM_API_KEY     密钥。anthropic 可以留空（SDK 自己从环境变量或本机登录的 profile 里找凭证）；
                           openai_compatible 必填
           LLM_BASE_URL    接口地址。anthropic 一般留空；openai_compatible 必填，写到 /v1 这一层，
                           后端会在后面接上 /chat/completions
           LLM_EFFORT      low / medium / high，思考深度。只对 anthropic 起作用
           LLM_MAX_TOKENS  一次回复最多多少 token。只对 anthropic 起作用
           LLM_TIMEOUT     一次请求最多等多少秒
           LLM_JSON_MODE   json_schema / json_object。只对 openai_compatible 起作用
    2. 启动时 context.py 的 build_context() 调用 build_provider(settings)（在 base.py 里）：
       按 LLM_PROVIDER 建一个 AnthropicProvider 或 OpenAICompatibleProvider；是 offline 就返回 None。
    3. 再把它包进 LLMService(provider, session_factory)，放在 ctx.llm 上，整个进程共用这一个。
    4. 业务模块只和 ctx.llm 打交道：先看 llm.enabled，是 True 才调用 llm.complete_json(...)；
       调用抛出 LLMError（包括模型拒答 LLMRefusal）时，各模块退回自己的规则或模板，请求不会因此失败。
    每次调用不管成功还是失败，LLMService 都往 llm_calls 表记一行用量；
    GET /api/analyses/{id}/llm-usage 按分析把它们汇总出来。

哪些地方会调用大模型
    M2  约束解析  modules/m02_constraints.py  POST /api/constraints/parse（按钮 A14）
    M11 写说明    modules/m11_explain.py      流水线第 8 阶段；打开证据卡时补写（按钮 B10）；
                                              生成报告时补写路线的推荐理由
    M12 问答      modules/m12_qa.py           POST …/steps/{n}/ask（按钮 C5）
    三处都要求模型按给定的 JSON Schema 返回一个 JSON 对象（结构化输出），不是自由文本。

要加一家新厂商
    写一个类，有 name、model 两个属性和 complete_json() 方法（要求见 base.py 的 LLMProvider），
    再在 base.py 的 build_provider() 里加一个分支。业务模块不用动。

这个包里有什么
    base.py                LLMError、LLMRefusal、LLMResponse、LLMProvider、build_provider、LLMService
    anthropic_provider.py  AnthropicProvider：Claude，走 Anthropic 官方 SDK
    openai_compatible.py   OpenAICompatibleProvider：兼容 OpenAI Chat Completions 协议的厂商，直接发 HTTP 请求
    下面的 import 把 base.py 里的名字提到包这一层：别的文件写 from syntherisk.llm import LLMService 就行。
    __all__ 列出的是这个包对外公开的名字。
"""

from syntherisk.llm.base import LLMError, LLMProvider, LLMRefusal, LLMResponse, LLMService, build_provider

__all__ = ["LLMError", "LLMProvider", "LLMRefusal", "LLMResponse", "LLMService", "build_provider"]
