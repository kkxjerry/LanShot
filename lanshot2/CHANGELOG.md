# 开发记录

## 2026-09-11

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
