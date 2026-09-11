# 开发记录

## 2026-09-11

- 两路实时 ASR 从旧 `qwen3-asr-flash-realtime` Realtime 协议迁移为 `qwen-audio-3.0-asr-flash-streaming` Inference WebSocket 协议；严格等待 `task-started` 后发送二进制 PCM，并处理 `result-generated`、`task-finished` 和 `task-failed`。
- 使用本机钥匙串凭据和公开 16kHz PCM 测试音频完成真实云端验证：收到 `task-started`、1 条非空 Final 文字和 `task-finished`，未输出或写入明文凭据。
- 首次真实启动确认新应用身份需要单独授权屏幕与系统音频录制。启动脚本现具备失败回滚：双路音频未启动时会停止已拉起的截图服务，不再留下半启动状态。
- 修复重新编译后旧授权开关仍显示开启、当前二进制却被TCC拒绝的问题：原先临时签名的 designated requirement 直接绑定每次变化的 CDHash；构建脚本现写入基于固定 Bundle ID `com.lanshot2.audio-capture` 的稳定 designated requirement。升级到该签名后只需重新授权一次，后续同标识构建不再因二进制哈希变化自动成为新身份。
- 进一步读取TCC日志与数据库确认：对临时签名应用，系统设置反复开关仍保留旧CDHash要求，手写identifier requirement没有被ScreenCapture记录采用。构建现强制使用本机有效的Apple Development证书；缺少稳定证书时直接失败，不再生成看似可用、重编译后权限必坏的App。升级后需定向重置一次旧ScreenCapture记录。
- 首次双路实采发现系统文字为空而麦克风识别到扬声器内容。修复三处链路问题：多显示器时明确选择 `CGMainDisplayID`；系统声道空闲时每5秒补100毫秒静音帧维持ASR任务；系统原始音频改为直接写PCM WAV，避免实时AAC Writer失败留下不可读M4A。ASR错误日志同时保留截断后的服务端错误信息。
- 修复后真实系统音频验证通过：约37秒内 `interviewer.wav` 持续增长至14MB，`interviewer.txt` 连续输出与正在播放视频一致的文字，且无ASR错误。该轮麦克风文件仍停留在WAV头且无转写，需要在确认默认输入设备和实际说话后单独验收，不能据系统通道成功宣称双路全部通过。
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
