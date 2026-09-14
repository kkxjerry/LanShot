# LanShot

LanShot 是一个 macOS 本地面试辅助工具，包含截屏模式和双路语音模式。语音模式分别采集系统声音与麦克风，使用千问实时 ASR 转写；按 `F23` 后停止本轮识别，将两路文字和最近对话历史发送给百炼模型，并在不可捕获的悬浮窗中显示答案。

## 在另一台 Mac 安装

要求：

- macOS 13 或更高版本。
- Python 3.10 或更高版本，推荐 Python 3.12。
- Xcode Command Line Tools，需包含 `swiftc`。
- 可用的阿里云百炼 API Key。

如果系统只有 Python 3.9，使用 Homebrew 并行安装即可，不要替换 macOS 自带版本：

```sh
brew install python@3.12
```

安装器会自动查找 Apple Silicon 和 Intel Mac 上的 Homebrew Python 3.10 至 3.14。

执行：

```sh
git clone --branch develop https://github.com/kkxjerry/LanShot.git
cd LanShot
open install.command
```

`install.command` 会检查环境、选择正确的 Python、将 API Key 安全写入当前 Mac 的钥匙串、编译音频程序和悬浮窗，并启动语音模式到待机状态。API Key 不会写入仓库或配置文件。

首次点击“开始采集”时，需要在 macOS“隐私与安全性”中允许：

- `LanShot Voice Capture.app`：麦克风、录屏与系统录音。
- 当前 Python 或终端：输入监控，用于接收 `F23`。

没有 Apple Development 证书时，安装器会使用一次性本地签名。该版本可以使用，但以后重新编译音频程序时，macOS 可能要求重新授权录屏。

## 语音流程

1. 点击“开始采集”：开始双路录音和实时识别。
2. 点击“停止采集”：只停止并保存，不发送问题。
3. 按 `F23`：尽快停止识别，将系统声音、麦克风文字和最近历史发送给模型。
4. 主控制程序与悬浮窗保持运行，可以继续下一轮。

每轮录音、两路文字、问题和答案保存在：

```text
~/Library/Application Support/LanShot2/questions/
```

## 日常启动

- `unified/LanShot.command`：选择截屏、语音或停止模式。
- `unified/voice_mode.command`：直接进入语音待机模式。
- `unified/stop_all.command`：停止所有 LanShot 进程。

截屏模式还依赖本机截图服务配置；`install.command` 当前优先完成可移植的语音模式安装。
