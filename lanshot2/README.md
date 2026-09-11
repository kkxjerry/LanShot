# LanShot2

更新时间：2026-09-11。

LanShot2 保留现有 LanShot 的截图、AI 分析、悬浮窗和 F22/F23/F24 行为，只新增需要用户主动启动的双路音频采集：

- 系统音频实时写入 `interviewer.txt`，原始音频写入 `interviewer.m4a`。
- 麦克风实时写入 `me.txt`，原始音频写入 `me.wav`。
- 两路使用相互独立的百炼实时 ASR 连接，不混流。
- 暂不包含 RAG、Skill、声纹识别、语音问题提交或 TTS。

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

程序不会自行开始监听。只有用户执行启动命令后才会申请权限并采集；执行停止命令后结束采集并写完文件。API Key 从环境变量或 macOS 钥匙串读取，不写入项目和日志。
