# 开发记录

## 2026-09-12

- 补齐语音提问闭环：停止本轮采集后读取最终 `interviewer.txt`，调用百炼 `kimi-k2.7-code`，实时在答案区显示请求状态和纯文本答案；问题与答案按日期保存在本机，空问题不会调用模型。
- 语音模式复用现有底层全局功能键监听器，将 `F23` 绑定为开始/停止采集；过滤自动重复，状态栏立即显示按键产生的启动或停止状态，退出语音模式时同步释放事件监听。
- 根据现场标注将语音悬浮窗调整为上方 35% 双栏识别区和下方 65% 答案区；增加“尚未采集 / 正在启动 / 正在采集 / 采集失败”状态、开始时间和持续计时。
- 统一语音模式改为先进入待机，不再一打开就录音；悬浮窗和菜单栏都提供明确的“开始采集 / 停止采集”，停止后保留界面以便再次开始。
- 悬浮窗只向应用支持目录写入本地控制命令，由独立控制进程执行启停；避免悬浮应用直接启动桌面目录脚本而触发额外的 macOS 文件访问授权。
- 语音模式接入原生 LanShot 悬浮窗，实时合并展示面试官与本机麦克风文字并自动跟随最新内容；窗口继续使用 `sharingType = .none`，实测系统截图中不出现悬浮内容。
- 菜单栏由文字改为模式图标：语音模式显示系统麦克风图标，截屏模式显示取景框图标；菜单可显隐、居中或退出语音字幕。
- 音频服务统一管理悬浮窗的启动、状态和退出，模式状态会单独报告 `voice_overlay_running`，避免采集停止后残留窗口。
- 定位并修复麦克风首帧写入 WAV 时触发 CoreAudio `SIGTRAP` 的崩溃：系统音频和麦克风现在都先转换为单声道、16kHz、Int16 PCM，再写入固定格式 WAV；ASR 写入优先于本地录音，避免录音异常阻断转写。
- 定位并修复采集约 17 秒后静默停止、进程仍假装运行的问题：ScreenCaptureKit 流现在注册最小屏幕帧消费者，避免未消费视频队列触发 `SCFrameStatusStopped`。
- 最终签名版本重新授权后完成真实持续验证：两路 WAV 连续增长超过一分钟，进程保持运行，系统音频实时识别出正在播放的中文歌词，且系统日志未再出现流停止事件。

## 2026-09-11

- 两路实时 ASR 从旧 `qwen3-asr-flash-realtime` Realtime 协议迁移为 `qwen-audio-3.0-asr-flash-streaming` Inference WebSocket 协议；严格等待 `task-started` 后发送二进制 PCM，并处理 `result-generated`、`task-finished` 和 `task-failed`。
- 使用本机钥匙串凭据和公开 16kHz PCM 测试音频完成真实云端验证：收到 `task-started`、1 条非空 Final 文字和 `task-finished`，未输出或写入明文凭据。
- 首次真实启动确认新应用身份需要单独授权屏幕与系统音频录制。启动脚本现具备失败回滚：双路音频未启动时会停止已拉起的截图服务，不再留下半启动状态。
- 修复重新编译后旧授权开关仍显示开启、当前二进制却被TCC拒绝的问题：原先临时签名的 designated requirement 直接绑定每次变化的 CDHash；构建脚本现写入基于固定 Bundle ID `com.lanshot2.audio-capture` 的稳定 designated requirement。升级到该签名后只需重新授权一次，后续同标识构建不再因二进制哈希变化自动成为新身份。
- 进一步读取TCC日志与数据库确认：对临时签名应用，系统设置反复开关仍保留旧CDHash要求，手写identifier requirement没有被ScreenCapture记录采用。构建现强制使用本机有效的Apple Development证书；缺少稳定证书时直接失败，不再生成看似可用、重编译后权限必坏的App。升级后需定向重置一次旧ScreenCapture记录。
- 首次双路实采发现系统文字为空而麦克风识别到扬声器内容。修复三处链路问题：多显示器时明确选择 `CGMainDisplayID`；系统声道空闲时每5秒补100毫秒静音帧维持ASR任务；系统原始音频改为直接写PCM WAV，避免实时AAC Writer失败留下不可读M4A。ASR错误日志同时保留截断后的服务端错误信息。
- 修复后真实系统音频验证通过：约37秒内 `interviewer.wav` 持续增长至14MB，`interviewer.txt` 连续输出与正在播放视频一致的文字，且无ASR错误。该轮麦克风文件仍停留在WAV头且无转写，需要在确认默认输入设备和实际说话后单独验收，不能据系统通道成功宣称双路全部通过。
- 继续实测发现运行数分钟后系统与麦克风文件同时停止增长、进程却仍存活；默认输入输出为蓝牙 `KKX`，独立 AVAudioEngine 麦克风没有收到帧。macOS 15及以上现改为同一个 ScreenCaptureKit 流分别消费 `.audio` 与 `.microphone`，共享生命周期并避免蓝牙路由切换导致独立引擎失活；macOS 13/14保留旧引擎回退。后台辅助程序同时关闭自动终止和突然终止，防止无窗口运行被系统判定为空闲。
- 签名切换后的首次 `.audio + .microphone` 启动被 RunningBoard 以 `Two equal instances have unequal identities` 直接终止，且旧控制器因残留 `capture.log=running` 误报健康。控制器现绕过 LaunchServices缓存，直接启动已签名Bundle内的可执行文件，独立进程组运行并写入 `runtime.log`；启动后复查PID，状态命令也会把“running但无PID”报告为失败。
- 直接执行二进制使TCC将父进程识别为隐私责任主体，遗留的本地Speech授权请求因此触发系统强制退出。最终方案恢复标准App启动，删除未使用的Speech框架、权限请求、Info声明和entitlement，并启用全新稳定身份 `com.lanshot.unified.voice-capture`，彻底隔离此前反复签名产生的LaunchServices/RunningBoard缓存。该身份后续禁止变更。
- 用户截图确认系统设置仍只展示旧文件名 `LanShot2AudioCapture.app`，导致旧TCC记录与新Bundle身份无法区分。最终App目录同步改名为 `LanShot Voice Capture.app`；迁移时注销并移走旧包，只保留一个可见名称与一个固定Bundle ID。
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
