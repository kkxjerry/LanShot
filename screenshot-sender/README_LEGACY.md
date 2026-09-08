# LanShot

## 接收端

在接收截图的 Mac 上运行：

```bash
./receiver_service.py
```

手机或其他 Mac 打开 `http://接收端内网IP:8787`，点击“立即截图”。接收端只保存最新一张图片，新截图会直接覆盖旧图。

收到图片后自动使用阿里云百炼的 `kimi-k2.7-code` 分析，并在网页中显示答案：

```bash
export DASHSCOPE_API_KEY="你的百炼API Key"
export LANSHOT_PROMPT="你的截图分析提示词"
./receiver_service.py
```

未设置 `LANSHOT_PROMPT` 时会读取 `interview_prompt.txt`。接收端启动时读取一次，修改提示词文件后需要重启接收端。API Key 只保留在接收端环境变量中，不会发送给网页。

笔试模式由 `start_written.command` 使用独立的 `written_prompt.txt`。模型同时生成 `<speech>` 自然讲解和 `<code>` 标准代码，接收端只把 speech 内容发送给 TTS，并分别保存为 `latest.txt` 和 `latest_tts.txt`。语音稿按逻辑模块讲解，不逐行朗读代码。

笔试语音模式设置 `LANSHOT_VOICE_URL` 和 `LANSHOT_VOICE_TOKEN` 后，接收端会把最新答案推送到手机语音服务。百炼 Key 仅随单次 HTTPS 请求转交给自有服务器，不会写入服务器磁盘。

双击 `start_written.command` 启动笔试模式，双击 `stop_written.command` 停止。笔试模式使用 `F22` 截图、`F23` 上一句、`F24` 下一句，不启动悬浮窗。iPhone 访问地址中的令牌已在本分析包中脱敏。

双击 `start_assessment.command` 启动测评模式，双击 `stop_assessment.command` 停止。测评模式使用 `F22` 截图并在悬浮窗显示答案，`F23` 向上翻页，`F24` 向下翻页，`Command + F23` 隐藏或显示悬浮字幕，`Command + F24` 把悬浮窗移动到鼠标位置。

## 发送端

发送端只有一个 Python 脚本，无第三方依赖。测评模式监听全局 `F22` 截取 Mac 主屏幕并上传，`F23` 向上翻页，`F24` 向下翻页。程序同时继续响应网页截图指令，不再执行定时截图。

## 1. 测试截图权限

```bash
./sender_service.py capture --output ~/Desktop/lanshot-test.jpg
```

首次运行时，在“系统设置 → 隐私与安全性 → 屏幕与系统音频录制”中允许 Terminal 或 Python，然后重新运行命令。

## 2. 写入配置

```bash
./sender_service.py configure \
  --server-url http://接收端地址:8787
```

配置默认保存到 `~/.config/lanshot-sender/config.json`。首次使用快捷键时，需要允许 Terminal 或 Python 的“输入监控”权限。

需要遮挡的文字写入 `~/.config/lanshot-sender/blocked_words.txt`，每行一个词。发送端会先使用 macOS Vision 在本地识别，并把包含这些词的文字区域涂黑，然后才上传图片。

## 双路音频监听

在 Finder 中双击 `start_audio.command` 开始监听，双击 `stop_audio.command` 停止监听。程序不会自动启动，也不会在停止后继续监听。

```bash
./audio_service.py start
./audio_service.py status
./audio_service.py stop
```

线上会议的系统音频保存为 `interviewer.m4a`，同时发送给百炼的 `qwen3-asr-flash-realtime` 实时识别到 `interviewer.txt`；本机麦克风保存为 `me.wav`，停止采集后由 macOS 在本地识别到 `me.txt`，用于复盘。默认目录为 `~/Library/Application Support/LanShotAudio`。

首次启动需要允许 **LanShot Audio Capture** 的麦克风、语音识别以及屏幕与系统音频录制权限。它是无界面的后台辅助程序，不是完整 App。

## 3. 前台测试

```bash
./sender_service.py once
./sender_service.py run
```

## 4. 安装为后台服务

```bash
./sender_service.py install
```

停止并删除后台服务：

```bash
./sender_service.py uninstall
```

日志位于 `~/Library/Logs/LanShotSender.log`。服务仅适合可信内网，不要把接收端端口映射到公网。

## 接口约定

- `GET /api/v1/agent/next?timeout=25`：返回 `204` 或 `{"id":"UUID"}`。
- `POST /api/v1/tasks/<UUID>/image`：上传远程任务 JPEG。
- `POST /api/v1/tasks/<UUID>/failure`：报告截图失败。
- `POST /api/v1/images`：上传定时截图，携带 `X-LanShot-Capture-ID`。
