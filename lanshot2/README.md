# LanShot2 音频模块

更新时间：2026-09-16。

本目录现在是合并版 LanShot 的语音模块。统一入口位于 `../unified/LanShot.command`；截屏模式和语音模式不会同时运行。

最终原生应用名称为 `LanShot Voice Capture.app`，Bundle ID 固定为 `com.lanshot.unified.voice-capture`。旧的 `LanShot2AudioCapture.app` 不再使用。

- 系统音频实时写入 `interviewer.txt`，原始音频写入 `interviewer.wav`。
- 麦克风实时写入 `me.txt`，原始音频写入 `me.wav`。
- 菜单栏显示麦克风图标；悬浮窗上方 35% 分栏显示“系统声音”和“麦克风”，下方 65% 显示答案。
- 统一语音模式只打开待机界面，点击“开始采集”后才录音；界面持续显示开始时间和已采集时长。
- “开始采集”和“停止采集”只由界面按钮控制；`F22` 立即结束当前识别并发送问题，`F23` / `F24` 将答案向上/向下翻半页。
- 悬浮字幕沿用 LanShot 的 `sharingType = .none` 窗口，不进入 macOS 系统截图。
- 两路使用相互独立的百炼 `qwen-audio-3.0-asr-flash-streaming` 连接，不混流。
- 语音问题默认使用百炼 `glm-5.3` 低推理强度回答，不再等待 Gemini 超时后回退。
- 可选的百炼知识检索在回答前执行；空召回、超时或错误不会阻断原回答。尚未包含 Skill、声纹识别或 TTS。

运行数据位于：

```text
~/Library/Application Support/LanShot2/audio/
```

每轮完整归档位于 `~/Library/Application Support/LanShot2/questions/日期/`，持续对话历史位于 `~/Library/Application Support/LanShot2/conversation_history.jsonl`；开始下一轮不会删除这些历史。

面试会话元数据保存在 `sessions.jsonl`，当前会话保存在 `current_session.json`。模型只携带当前会话最近六轮问答，避免不同面试之间互相污染；菜单栏“会话管理...”可以新建、查看和继续会话，旧历史不会清除。

启动、状态和停止：

```sh
./lanshot2/start_lanshot2.command
./lanshot2/status_lanshot2.command
./lanshot2/stop_lanshot2.command
```

程序不会自行开始监听。统一入口打开后保持“尚未采集”；只有点击“开始采集”才会录音。点击“停止采集”只停止并保存。按 `F22` 会尽快停止当前识别并固化两路快照，随后立即恢复下一轮采集，同时在后台把两份文字发送给大模型并将回答写入 `answer.txt`。`F23` / `F24` 控制答案向上/向下滚动半页。主控制进程不会退出，悬浮窗异常关闭时会自动恢复；菜单栏中的“退出 LanShot”或“全部停止”会保存当前记录并完整退出。开始或停止下一轮不会清空屏幕上的上一份答案、每轮归档或持续对话历史。API Key 从环境变量或 macOS 钥匙串读取，不写入项目和日志。

运行期间可通过菜单栏“模式”直接切换到截屏模式；截屏模式菜单也可直接切回面试模式。面试回答与 RAG 检索共用 macOS 钥匙串的 `com.lanshot.bailian / DASHSCOPE_API_KEY`。

知识库配置使用 `../unified/configure_knowledge.command`。最新检索状态写入 `knowledge_status.json`，每轮完整召回写入对应的 `*-knowledge.json`，均不包含 API Key。
