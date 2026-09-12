# LanShot2 音频模块

更新时间：2026-09-11。

本目录现在是合并版 LanShot 的语音模块。统一入口位于 `../unified/LanShot.command`；截屏模式和语音模式不会同时运行。

最终原生应用名称为 `LanShot Voice Capture.app`，Bundle ID 固定为 `com.lanshot.unified.voice-capture`。旧的 `LanShot2AudioCapture.app` 不再使用。

- 系统音频实时写入 `interviewer.txt`，原始音频写入 `interviewer.wav`。
- 麦克风实时写入 `me.txt`，原始音频写入 `me.wav`。
- 菜单栏显示麦克风图标；悬浮窗上方 35% 分栏显示“系统声音”和“麦克风”，下方 65% 显示答案。
- 统一语音模式只打开待机界面，点击“开始采集”后才录音；界面持续显示开始时间和已采集时长。
- 语音模式下 `F23` 与“开始采集 / 结束本轮并提问”按钮作用相同；第二次按下只结束本轮，主控制进程与悬浮窗继续运行。
- 悬浮字幕沿用 LanShot 的 `sharingType = .none` 窗口，不进入 macOS 系统截图。
- 两路使用相互独立的百炼 `qwen-audio-3.0-asr-flash-streaming` 连接，不混流。
- 语音问题使用百炼 `kimi-k2.7-code` 回答；暂不包含 RAG、Skill、声纹识别或 TTS。

运行数据位于：

```text
~/Library/Application Support/LanShot2/audio/
```

启动、状态和停止：

```sh
./lanshot2/start_lanshot2.command
./lanshot2/status_lanshot2.command
./lanshot2/stop_lanshot2.command
```

程序不会自行开始监听。统一入口打开后保持“尚未采集”；点击“开始采集”或按 `F23` 才会录音。再次按 `F23` 或点击“结束本轮并提问”会保存本轮两路 WAV 和文字，将 `interviewer.txt` 提交给大模型，并把回答写入 `answer.txt`。主控制进程不会退出，可直接开始下一轮。API Key 从环境变量或 macOS 钥匙串读取，不写入项目和日志。
