# LanShot

LanShot 是一个 macOS 本地面试辅助工具，包含截屏模式和双路语音模式。语音模式分别采集系统声音与麦克风，使用千问实时 ASR 转写；按 `F23` 后停止本轮识别，将两路文字和最近对话历史发送给百炼模型，并在不可捕获的悬浮窗中显示答案。两个模式都可选择先检索阿里云百炼知识库，再将召回资料交给现有回答模型。

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
4. 本轮快照保存后立即开始下一轮采集，LLM 在后台并行生成答案。
5. 主控制程序与悬浮窗保持运行；只有执行“全部停止”才会退出。

菜单栏的“模式”子菜单可以在“截屏模式”和“面试模式”之间直接切换，不需要退出后重新运行启动脚本。

菜单栏的“会话管理...”提供：

- 新建面试会话。
- 继续所选历史会话，并恢复该会话的模型上下文。
- 查看当前及历史会话的提问数量、问题和答案。
- 刷新会话列表。
- 打开本地历史目录。

每个会话使用独立的模型上下文；新建会话不会删除旧会话、录音或答案。旧版本产生的问答会显示为“旧版历史”。

## 百炼知识库

先在百炼华北 2（北京）业务空间创建知识库，再创建并发布“知识检索服务”。取得 `workspace_id` 和 `agent_id` 后双击：

```text
unified/configure_knowledge.command
```

配置保存在 `~/Library/Application Support/LanShot/knowledge.json`，只包含非秘密的服务 ID。API Key 继续从 macOS 钥匙串读取。

- 语音模式直接使用两路 ASR 文字检索。
- 截屏模式先用 macOS Vision 在本地提取截图文字，再检索。
- 检索结果标记为不可信资料，不执行文档中出现的指令。
- 未配置、无召回、OCR 失败、超时或百炼错误时，自动回退到原有模型回答，不让 F22/F23 流程卡住。
- 使用 `python3 unified/knowledge_config.py status` 查看配置，使用 `disable` / `enable` 临时关闭和开启。

每轮检索的召回、分数和耗时保存为 `*-knowledge.json`，其中不包含 API Key。

每轮录音、两路文字、问题和答案保存在：

```text
~/Library/Application Support/LanShot2/questions/
```

## 日常启动

- `unified/LanShot.command`：选择截屏、语音或停止模式。
- `unified/configure_knowledge.command`：配置可选的百炼知识检索服务。
- `unified/test_knowledge.command`：用一个已知问题检查召回数量、来源和耗时。
- `unified/voice_mode.command`：直接进入语音待机模式。
- `unified/stop_all.command`：停止所有 LanShot 进程。
- 菜单栏麦克风图标中的“退出 LanShot”：保存当前记录并停止采集、悬浮窗、F23 和控制器。

截屏模式还依赖本机截图服务配置；`install.command` 当前优先完成可移植的语音模式安装。
