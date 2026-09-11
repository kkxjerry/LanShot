# 开发记录

## 2026-09-11

- 两路实时 ASR 从旧 `qwen3-asr-flash-realtime` Realtime 协议迁移为 `qwen-audio-3.0-asr-flash-streaming` Inference WebSocket 协议；严格等待 `task-started` 后发送二进制 PCM，并处理 `result-generated`、`task-finished` 和 `task-failed`。
- 使用本机钥匙串凭据和公开 16kHz PCM 测试音频完成真实云端验证：收到 `task-started`、1 条非空 Final 文字和 `task-finished`，未输出或写入明文凭据。
- 首次真实启动确认新应用身份需要单独授权屏幕与系统音频录制。启动脚本现具备失败回滚：双路音频未启动时会停止已拉起的截图服务，不再留下半启动状态。
- 从 LanShot `develop` 创建独立 LanShot2 组合启动入口。
- 截图、AI、悬浮窗和现有快捷键代码保持不变。
- 新增独立 `com.lanshot2.audio-capture` 原生辅助程序。
- 系统音频和麦克风分别使用独立百炼实时 ASR，分别实时写入文字文件。
- 使用独立运行目录，避免覆盖旧 LanShotAudio 数据。
- 不自动监听，必须由用户主动启动和停止。
- 原生音频辅助程序已完成编译、签名和无参数启动检查。
- LanShot2 专项测试 2 项通过；原有 Python 服务测试 200 项通过。
- 原有 Swift Package 测试共 81 项，其中 79 项通过；两个与本次新增目录无依赖关系的既有测试失败：`ReceiverHTTPHandlerTests.testHandlerCollectsRequestPartsAndWritesCompleteFramedResponse` 存在 NIO 线程误用并超时，`ReceiverRouterTests.testRootReturnsMinimalChineseControlPage` 的页面文本断言不匹配。本轮未改动这两处旧逻辑。
- 尚未主动启动真实录音，因此双路真实麦克风、系统音频和百炼转写需要在用户确认开始监听后现场验收。
