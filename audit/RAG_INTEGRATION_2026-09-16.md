# RAG 接入留痕（2026-09-16）

## 目标

在不改变 LanShot 截屏/面试双模式、快捷键和回答模型的前提下，接入阿里云百炼已发布的知识检索服务。

## 实现边界

- 使用官方 `POST /api/v1/indices/knowledge/search`，Endpoint 只能由验证后的 Workspace ID 推导，配置不允许注入自定义 URL。
- API Key 仍使用 `com.lanshot.bailian / DASHSCOPE_API_KEY` 钥匙串项，知识库配置和日志不记录凭据。
- 面试模式使用已封口的系统声音和麦克风文字作为查询。
- 截屏模式使用 macOS Vision 本地 OCR 生成查询；图像仍只进入原有的截图模型流程。
- 检索不是回答成功的必要条件。任何检索或 OCR 错误仅记录类型，随后继续无 RAG 的原模型请求。
- 召回内容标记为不可信事实资料，防止文档中的指令改变原提示词或输出格式。

## 本地记录

- 公共配置：`~/Library/Application Support/LanShot/knowledge.json`。
- 面试模式：`~/Library/Application Support/LanShot2/audio/knowledge_status.json` 及每轮 `*-knowledge.json`。
- 截屏模式：接收端 `latest_knowledge.json` 及七天历史中的 `*.knowledge.json`。
- 记录内容包含查询、召回切片、来源、分数、请求 ID 和耗时，不包含 API Key。

## 已验证与待验证

- 已用现有钥匙串凭据验证用户提供的 Workspace/Agent 组合：HTTP 200，服务可达。
- 当时通用测试查询召回为 0，因用户正在准备数据，不将空召回视为实际效果验收。
- 代码单元测试覆盖配置校验、有召回、空召回、网络失败降级、双模式注入和本地留痕。
- Python 回归共 231 项通过；原生 OCR 在实际截图上完成编译与中英文提取验证。
- Swift 旧套件共 81 项，79 项通过；仍是与本次 RAG 无关的两个已知失败：NIO `EmbeddedEventLoop` 线程误用超时，以及旧控制页文本断言不匹配。
- 数据完成导入后，仍需用已知答案问题验证召回率、分数、端到端延迟和回答忠实度。
