# LanShot 安装与新电脑部署指南

本文档介绍如何在另一台全新的 Mac 电脑上从零安装、配置并运行 LanShot。

---

## 一、环境要求

在开始安装前，请确认这台 Mac 满足以下条件：

| 项目 | 要求说明 |
| :--- | :--- |
| **操作系统** | **macOS 13.0 (Ventura) 或更高版本**（推荐 macOS 14 Sonoma / macOS 15 Sequoia）<br>*支持 Apple Silicon (M1/M2/M3/M4) 与 Intel 芯片* |
| **开发工具** | **Xcode Command Line Tools**（需包含 `swiftc` 编译器） |
| **Python** | **Python 3.10 至 3.14**（推荐 Python 3.12） |
| **API Key** | **阿里云百炼 API Key**（用于通义千问双路实时 ASR 语音识别） |

---

## 二、前置依赖准备

打开终端（Terminal），依次执行以下两步：

### 1. 安装 Xcode 命令行工具
如果新电脑尚未安装开发工具，在终端运行：
```bash
xcode-select --install
```
*在弹出的系统窗口中点击“安装”，等待下载安装完成。*

### 2. 检查或安装 Python 3.10+
检查当前系统的 Python 版本：
```bash
python3 --version
```
如果系统 Python 低于 3.10，可通过 Homebrew 安装（**不要**覆盖或替换系统默认的 Python）：
```bash
# 如果没有 Homebrew，先安装 Homebrew：
# /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install python@3.12
```

---

## 三、一键极简安装（推荐）

LanShot 内置了自动配置与编译脚本 `install.command`，它会自动检测 Python 路径、安全保存 API Key、编译原生组件并完成待机启动。

### 1. 克隆代码仓库
```bash
git clone --branch develop https://github.com/kkxjerry/LanShot.git
cd LanShot
```

### 2. 运行一键安装器
```bash
open install.command
```
或直接在 Finder 中双击 `install.command`。

### 3. 按提示操作：
1. **输入百炼 API Key**：安装器会提示输入阿里云百炼 API Key，并安全存入 macOS 钥匙串（Keychain），**绝不会**明文写入代码或配置文件；
2. **自动编译**：脚本会自动编译防录屏悬浮窗（`CaptureExclusionDemo.app`）与原生双路音频采集器（`LanShot Voice Capture.app`）；
3. **完成并待机**：出现“安装完成”提示后，系统菜单栏将出现 LanShot 麦克风图标，悬浮窗将进入待机状态。

---

## 四、关键系统权限配置（必读）

macOS 对录屏和麦克风有严格的隐私保护机制，**首次使用必须手动授权**，否则无法捕获对方声音或监听快捷键。

打开系统设置：**`系统设置 (System Settings)` ➔ `隐私与安全性 (Privacy & Security)`**，检查并允许以下权限：

### 1. 屏幕录制与系统音频 (Screen & System Audio Recording)
* **路径**：`隐私与安全性` ➔ `屏幕与系统音频录制`
* **勾选应用**：
  * `LanShot Voice Capture`
  * （如果在终端运行，也勾选当前终端，如 `Terminal` 或 `iTerm2`）

### 2. 麦克风 (Microphone)
* **路径**：`隐私与安全性` ➔ `麦克风`
* **勾选应用**：`LanShot Voice Capture`

### 3. 输入监控 / 辅助功能 (Input Monitoring / Accessibility)
* **路径**：`隐私与安全性` ➔ `输入监控` 及 `辅助功能`
* **勾选应用**：当前使用的终端（`Terminal` / `iTerm2`）或当前运行的 Python。
* **作用**：允许接收全局快捷键（`F22` 提问、`F23`/`F24` 翻页）。

---

## 五、进阶技巧：Apple Development 代码签名（防权限丢失）

* **为什么需要**：macOS 对无签名或临时签名的二进制应用，每次重启电脑或重新编译后，可能会重置 TCC 隐私权限，要求反复授权。
* **一劳永逸的解决方法**：
  1. 在新 Mac 上打开 **Xcode**；
  2. 点击顶部菜单 **`Xcode` ➔ `Settings...` (或 `Preferences...`) ➔ `Accounts`**；
  3. 点击左下角 `+` 号，登录你的**普通免费 Apple ID**；
  4. 登录后，Xcode 会自动在钥匙串中生成一张免费的个人开发证书（`Apple Development: your_name (...)`）；
  5. 之后无论运行 `install.command` 还是重新编译，脚本都会自动读取该证书签名，**系统权限永久保留，永不丢失**。

---

## 六、日常使用与快捷操作

日常使用时，无需重新运行安装器，直接双击运行以下脚本即可：

| 脚本路径 | 作用 |
| :--- | :--- |
| **`unified/LanShot.command`** | **主控入口**：可在菜单中选择“语音模式”、“截屏模式”或一键停止 |
| **`unified/voice_mode.command`** | **快捷直达**：直接启动语音面试待机模式 |
| **`unified/stop_all.command`** | **一键退出**：安全保存当前未提交记录，并彻底退出所有进程 |

### 快捷键速查表
* **`F22`**：停止当前识别，将双路时序对话流提交给大模型生成回答。
* **`F23` / `F24`**：悬浮窗长答案向上 / 向下翻页滚动。
* **`Cmd + Shift + R`**：**触发反问建议**（在菜单栏“会话管理...”下方也可直接点击），基于整场面试上下文实时提炼 P0~Pn 优先级反问候选卡片。

---

## 七、常见问题排查 (FAQ)

### Q1: 运行提示 `缺少 Xcode Command Line Tools`？
* 执行 `xcode-select --install`。如果已安装但仍报错，执行 `sudo xcode-select --reset` 重置路径。

### Q2: 听不到/识别不到面试官的声音？
* 检查 `系统设置 -> 隐私与安全性 -> 屏幕与系统音频录制`，确保 `LanShot Voice Capture` 已勾选；
* 如果刚授予权限，建议双击 `unified/stop_all.command` 后重新运行 `unified/voice_mode.command` 重启服务。

### Q3: 快捷键 `F22`、`F23`、`F24` 按了没反应？
* 检查 Mac 键盘设置：外接键盘或 Mac 自带键盘是否需要配合 `Fn` 键（即按 `Fn + F22`）；
* 检查 `系统设置 -> 隐私与安全性 -> 输入监控`，确保执行命令的终端（Terminal）已被允许。

### Q4: 怎么更换或更新 API Key？
* 在终端执行：
  ```bash
  security add-generic-password -U -s com.lanshot.bailian -a DASHSCOPE_API_KEY -w "你的新Key"
  ```
