# 可选模型接口与验证边界

文档核查：2026-10-21（上海日期）。本轮使用官方 API 文档/官方文档搜索摘要，
不是旧聊天记忆。下表描述本项目选择的文本子集，不代表供应商全部 API。
所有模型 ID 都必须由操作者从自己的账号控制台/模型目录复制；没有预填模型。
**本轮未用真实密钥，全部已实现配置只做协议模拟，不是实际供应商联通验收。**

## 已实现配置与官方来源

| 配置名 | 协议 / 鉴权 | 输出上限与解析 | 模型、区域、套餐限制与差异 |
|---|---|---|---|
| `openai` | Responses / Bearer | `max_output_tokens`；`output[].content[].text` | `store=false`；只接受 completed 文本。模型访问取决于项目授权。[官方参考](https://developers.openai.com/api/reference/resources/responses/methods/create) |
| `openai-chat` | Chat / Bearer | `max_completion_tokens`；choices | 不使用旧 max_tokens 代替推理模型上限；模型参数能力独立确认。[官方参考](https://platform.openai.com/docs/api-reference/chat/object) |
| `deepseek` | Chat / Bearer | `max_tokens`；choices；缓存命中/未命中统计 | 当前文档有 thinking 开关；本配置明确 disabled。不复用思考原文。[官方参考](https://api-docs.deepseek.com/api/create-chat-completion/)、[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/) |
| `mimo` | Chat / Bearer | `max_tokens`；choices | 官方 SDK 示例使用 `api.xiaomimimo.com/v1`；只做单轮提议，不实现带工具历史的 reasoning_content 回传。文档正文跳转抓取受限，地址依据官方索引摘要；未验证所有套餐入口。[官方说明](https://platform.xiaomimimo.com/docs/en-US/usage-guide/passing-back-reasoning_content) |
| `gemini` | 原生 generateContent / x-goog-api-key | `maxOutputTokens`；candidates；usageMetadata | 选择原生接口；忽略 thought 内容，记录 thoughtsTokenCount；地区、免费/付费层和模型资格须在项目确认，不假设能关闭所有模型思考。[鉴权](https://ai.google.dev/api)、[接口](https://ai.google.dev/api/generate-content) |
| `anthropic` | 原生 Messages / x-api-key + anthropic-version | `max_tokens`；text blocks；input/output/cache usage | identity-linked key 可能还需 workspace header，提供服务端 WORKSPACE_ID；不启用 beta 和工具。[Messages](https://platform.claude.com/docs/en/api/typescript/messages/create)、[鉴权](https://platform.claude.com/docs/en/manage-claude/authentication) |
| `qwen` | DashScope Chat / Bearer | `max_tokens`；choices | 默认北京标准按量入口；新 workspace 域、国际/美国区域必须按账号改 ENDPOINT 和密钥；Coding Plan 不视作通用按量 API。[官方兼容说明](https://docs.modelstudio.console.alibabacloud.com/en/model-studio/compatibility-of-openai-with-dashscope) |
| `moonshot` | Kimi Chat / Bearer | `max_tokens`；choices | 官方页面已跳转 Kimi 平台；示例仍列 api.moonshot.cn。思考/采样参数取决于模型，本项目不发送 temperature。[官方参考](https://platform.kimi.com/docs/api/chat) |
| `zhipu` | Chat / Bearer | `max_tokens`；choices | 标准 paas/v4 入口；不自动切 Coding Plan；JSON 仅模型支持时另行验证。[官方参考](https://docs.bigmodel.cn/api-reference/模型-api/对话补全) |
| `siliconflow` | 聚合 Chat / Bearer | `max_tokens`；choices | 模型 ID 含平台命名空间；免费配额和地区不能从协议推断。[官方参考](https://docs.siliconflow.cn/docs/api/chat-completions-post) |
| `openrouter` | 聚合 Chat / Bearer | `max_tokens`；choices | 请求设 allow_fallbacks=false、require_parameters=true；不自动跨供应商回退。若需固定上游运营商，须后续增加经过验证的路由配置；本项目不声称已经固定路由。[官方参考](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request) |
| `groq` | Chat / Bearer | `max_completion_tokens`；choices | 限流、结构化输出能力因模型和账号不同。[官方参考](https://console.groq.com/docs/api-reference) |
| `mistral` | Chat / Bearer | `max_tokens`；choices | 仅文本；模型目录和上下文上限由账号确认。[官方参考](https://docs.mistral.ai/api/endpoint/chat) |
| `xai` | Chat / Bearer | `max_tokens`；choices | 本项目未实现其 Responses 扩展或搜索工具；套餐/模型另验。[官方参考](https://docs.x.ai/developers/rest-api-reference/inference/chat-completions) |
| `together` | 聚合 Chat / Bearer | `max_tokens`；choices | 本轮官方文档地址为 api.together.ai/v1；serverless 与 dedicated 模型权限不同。[官方兼容矩阵](https://docs.together.ai/docs/inference/openai-compatibility) |
| `azure` | Chat / api-key | `max_completion_tokens`；choices | 不猜资源域、deployment 和 api-version；完整 ENDPOINT 由资源控制台提供。Entra 动态令牌刷新未实现。[官方参考入口](https://learn.microsoft.com/en-us/azure/foundry/openai/reference) |
| `ollama` | 原生 /api/chat / 本地无鉴权 | `options.num_predict`；message；eval_count | 默认 localhost:11434；stream=false；本机模型需已安装，think 能力按模型发现，不能通用发送。[官方参考](https://docs.ollama.com/api/chat) |
| `vllm` | Chat / Bearer | `max_tokens`；choices | 自己填写完整服务地址/部署模型 ID；要求已配置 API key，chat template 缺失可导致失败。HTTP 只允许 loopback；不负责下载/启动模型。[官方说明](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html) |

本轮另检查了[火山方舟 Chat 文档](https://docs.volcengine.com/docs/ark/chat-api?lang=zh)，
但正文只返回 JavaScript 壳，尚未取得足够的当前接口参数证据；**未注册 Doubao 配置**，
不把社区文章当官方 API 规范。其他平台可按相同流程新增配置、来源与契约测试；
没有以“自定义 URL”将未知平台标为兼容。Bedrock 签名、Vertex OAuth、更多厂商原生协议、
多模态、流式、工具调用、自动路由不在当前实现范围。

## 配置与开启

普通运行不需要任何 API。服务端使用 `WORLDMIND_<配置名大写，下划线替换连字符>_MODEL`、
`_API_KEY`，可选 `_ENDPOINT`；Anthropic 可选 `_WORKSPACE_ID`。
密钥不从请求体或浏览器接收，也不通过目录接口返回。
JSON 模式默认不启用；程序调用 `Settings(json_mode=True)` 仅用于已经确认模型支持的配置，
Messages 不接受此开关。只要求文本 JSON并在本地严格校验，不能保证模型一定返回有效提议。

初次真实调用需另行授权后，由操作者：

1. 在持久磁盘初始化账本，配置小额请求总上限。
2. 配置模型、地区、账号、密钥，并确认供应商账单硬上限。
3. 设置服务端模型访问口令及显式启用开关。
4. 单次调用检查 usage、账户账单和错误结果，再扩大额度。

本轮没有执行这些真实调用步骤。端点覆写是受信任管理员配置，不对浏览器开放；
应只填官方或自有服务。所有请求不跟随重定向，TLS 校验保持开启。

## 资源与审计契约

- 默认同时最多 2 个模型请求，每会话 1 个在途请求；没有自动重试或供应商切换。
- 每请求 20 秒总超时，输出上限 1024 token；响应体最多 128 KiB，动作 JSON 最多 16 KiB、
  30 动作，角色上下文最多 32 KiB。截断、拒绝、工具调用、无效 JSON 均拒绝。
- SQLite 在发送前原子占用一次请求额度，成功、超时、取消和失败均不退款。
  重启 pending 标为 abandoned_on_restart；不会自动重发。请求额度不是货币预算，
  不能代替供应商支出上限。缺失 token/费用保持 null/未知。
- 缓存键包含协议、供应商、端点、模型、完整请求、角色上下文、版本及 workspace；
  缓存只存经过语法和语义校验的动作，24 小时到期，命中仍重新校验。
- 审计仅保存请求摘要、状态、白名单数值用量；不保存密钥、认证头、原始错误、
  任意模型解释或思考内容。回放只保存结构化提议，绝不重新请求 API。
- API 接纳时再次检查当前会话/目标/计划/认知代次，防止重置、取消、换目标后的迟到回复覆盖。
- 只支持单服务进程；模拟会话在内存，额度账本在持久磁盘。模型启用且账本丢失时启动拒绝。

## 测试证据解释

`tests/test_models.py` 按每个注册配置检查请求字段、鉴权、正常解析、语义拒绝、
HTTP 错误、缺失用量、缓存和额度；另测超时、取消、并发、重启、超大响应、
重定向和无效 JSON。测试使用 `httpx.MockTransport`，只证明适配代码与合成协议样本一致。
尚不能证明这些平台当前接受指定模型的请求、网络可达、账号额度有效或账单金额准确。
