# LanShot P1R2 中文说明

版本：`lanshot-p1r2-20260908.1`

LanShot 是一套运行在 macOS 上的截图、可靠投递、模型分析和原生悬浮显示工具。
当前默认采用冗余模式：一个内嵌接收入口、两个本地 HTTP 接收入口、一个发送端、
一个显示桥接和一个 AppKit 原生悬浮窗。

## 完整链路

```text
F22 / 原生菜单 / CLI 截图请求
        |
        v
Sender 获取全局截图锁
        |
        +--> macOS screencapture 截图
        +--> 可选 native_redactor 本地脱敏
        |
        v
发送端 SQLite 持久队列
任务 ID + 图片摘要 + 来源序号 + 模式 + 路由策略
        |
        v
MultiReceiver 按优先级选择一个入口
embedded -> local-primary:8788 -> local-backup:8789 -> 显式授权的 remote HTTPS
        |
        v
接收端 SQLite 持久接收并返回 durable ACK
        |
        v
analysis-owner.lock 保证本地只有一个模型执行者
        |
        v
百炼模型分析并持久保存结果
        |
        v
DisplayBridge 按原任务归属读取结果
        |
        v
latest_state.json -> CaptureExclusionDemo.app
        |
        v
原生悬浮窗应用文本并写 display_ack.json
```

## 链路分解

### 1. 触发截图

支持三种入口：

- 按 `F22` 全局热键。
- 点击菜单栏 LanShot 中的“截图并分析”。
- 执行 `diagnostics.py capture`。

每次请求生成唯一任务 ID。热键重复触发或服务重启时，系统依靠任务 ID 去重，
不会把同一任务当成多次新截图。

### 2. 截图与本地脱敏

Sender 使用 macOS `/usr/sbin/screencapture` 获取 JPEG。截图阶段持有独立锁，
网络上传不占用截图锁，因此接收端暂时离线不会长期阻塞下一次截图。

如果 `blocked_words.txt` 包含规则，截图必须先经过 `native_redactor`；脱敏失败时任务停止，
不会为了可用性而绕过脱敏。截图成功但未能写入队列时，临时文件会保留供人工核查。

### 3. 持久发送队列

图片内容和任务元数据在同一个 SQLite 事务中落盘。持久字段包括：

- 任务 ID、图片 SHA-256、创建时间和过期时间。
- 来源标识、来源序号和运行模式。
- 固定的接收策略、目标集群和尝试次数。
- 当前状态、重试时间、租约和错误码。

只有接收端返回与任务 ID、摘要、模式和集群全部匹配的持久确认后，发送端才释放图片负载。

### 4. 多接收路由

默认优先级如下：

1. `embedded`：Sender 进程内的接收运行时，不经过 localhost HTTP。
2. `local-primary`：本机 `127.0.0.1:8788`。
3. `local-backup`：本机 `127.0.0.1:8789`。
4. `remote`：可选远程 HTTPS，默认禁用。

系统不会把一张截图同时群发给多个模型端。任务一旦可能已经提交，就会固定到原集群；
超时、错误确认或响应丢失时，只在同一集群内核对，避免切换到独立服务器造成重复模型调用。

### 5. 接收与模型处理

本机三个入口共享同一个接收数据库和集群 ID。主接收、备用接收和内嵌接收都可以接收任务，
但只有获得 `analysis-owner.lock` 的运行时能够调用模型。

接收端先持久保存图片和任务，再返回 durable ACK。模型调用完成后写入结果；如果进程在模型调用期间
异常退出，任务进入 `uncertain`，不会自动再次调用模型。再次处理需要明确确认可能重复计费。

### 6. 显示桥接

`display_bridge.py` 不截图、不上传图片，也不调用模型。它负责：

- 依据发送端当前任务和持久路由找到正确结果。
- 防止旧任务晚到或晚完成后覆盖新任务。
- 在新任务失败时明确标注“上一任务结果不是本次答案”。
- 把当前状态写入显示目录中的 `latest_state.json`。
- 校验原生悬浮窗写回的 `display_ack.json`。

显示回执只证明指定版本已经被原生窗口应用，不表示用户一定看过，也不证明第三方采集行为。

### 7. 原生悬浮窗

`CaptureExclusionDemo.app` 使用 AppKit 实现，读取统一显示目录，不依赖 Tk。

| 操作 | 功能 |
| --- | --- |
| `F22` | 截图并分析 |
| `F23` | 悬浮答案向上翻页 |
| `F24` | 悬浮答案向下翻页 |
| `Command + F23` | 隐藏或显示悬浮窗 |
| `Command + F24` | 把悬浮窗移动到鼠标位置 |

菜单栏 LanShot 菜单还提供手动截图、显示设置和退出显示。

## 主要功能

- 截图、上传和模型分析解耦，网络故障不会丢失已经持久化的截图。
- 任务 ID 和图片摘要双重去重，避免重复提交和重复分析。
- 有界队列、图片容量、任务有效期、重试次数和退避时间。
- 上传租约和模型执行锁，旧工作者不能覆盖新状态。
- 内嵌、本地主接收、本地备用接收和可选远程 HTTPS 路由。
- 当前任务、上一成功任务、模型状态和显示回执分离。
- 原生 AppKit 悬浮窗、菜单操作、翻页、移动和显示切换。
- LaunchAgent 托管、异常退出节流、重启预算和显式停止。
- 白名单诊断导出，不包含截图、答案、提示词、密钥或原始日志正文。
- `written` 模式保留语音发布和上一句、下一句控制。

## 运行模式

| 模式 | 进程结构 | 适用场景 |
| --- | --- | --- |
| `redundant` | 主接收 + 备用接收 + Sender + DisplayBridge，Sender 内还有 embedded | 默认模式，提供本机入口冗余 |
| `monolith` | Sender + DisplayBridge，接收逻辑内嵌 | 结构更简单，不依赖本机 HTTP 接收进程 |

本地多入口仍共享同一台 Mac 和同一块磁盘，不等于多个独立故障域。

## 核心文件

| 文件 | 职责 |
| --- | --- |
| `sender_service.py` | 热键、截图、脱敏、发送队列和上传工作线程 |
| `multi_receiver.py` | 多入口选择、路由固定、熔断和结果核对 |
| `receiver_service.py` | HTTP 接收接口、持久确认和结果查询 |
| `receiver_runtime.py` | 内嵌/HTTP 共用的模型运行时和执行锁 |
| `reliability.py` | SQLite 状态机、事务、租约、重试和文件锁 |
| `display_bridge.py` | 生成原生显示投影并消费显示回执 |
| `manage_services.py` | 配置和管理 LaunchAgent 角色 |
| `diagnostics.py` | 状态、路由、队列、重试、取消和诊断导出 |
| `../capture-exclusion-demo/` | AppKit 原生悬浮窗源码与构建脚本 |

## 当前机器的启动与检查

当前实际运行配置位于：

```sh
SETTINGS="$HOME/Library/Application Support/LanShotP1R2Live/settings.json"
```

启动并打开原生悬浮窗：

```sh
python3 manage_services.py start \
  --settings "$SETTINGS" \
  --expected-profile default \
  --open-display
```

查看整体状态和任务路由：

```sh
python3 diagnostics.py --settings "$SETTINGS" status
python3 diagnostics.py --settings "$SETTINGS" routes
python3 diagnostics.py --settings "$SETTINGS" queue
```

停止全部 R2 托管角色：

```sh
python3 manage_services.py stop --settings "$SETTINGS"
```

`diagnostics.py capture` 和 `F22` 都会进行真实截图并可能产生模型费用；
`capture-test` 只检查本地截图，不上传也不调用模型。

## 权限

macOS 需要给实际运行的 `python3.12` 开启：

- 隐私与安全性 -> 输入监控：用于 F22、F23、F24 全局热键。
- 隐私与安全性 -> 录屏与系统录音：用于实际截图。

修改权限后通常需要重启托管进程。只给 Terminal 授权，不一定能覆盖由 LaunchAgent 直接启动的 Python。

## 状态含义

| 状态 | 含义 |
| --- | --- |
| `pending` | 已持久入队，等待上传 |
| `inflight` | 正在向固定接收入口提交 |
| `queued` | 接收端已持久接收，等待模型 |
| `processing` | 模型处理中 |
| `complete` | 分析完成 |
| `delivered` | 发送端收到持久确认，等待分析状态 |
| `paused` | 达到重试限制或遇到需要人工处理的错误 |
| `uncertain` | 模型可能执行过，不能自动重复调用 |
| `failed` | 本次截图或处理失败 |
| `expired` | 任务超过有效期 |
| `cancelled` | 任务被明确取消 |

## 隐私与安全边界

- 默认接收路径全部在本机，远程路由默认 `allow_remote=false`。
- 启用远程服务后，截图会通过 HTTPS 离开 Mac，必须使用独立令牌、TLS 和受限服务账户。
- API Key 从 Keychain 或服务环境读取，不写入配置、plist 或仓库。
- 当前结果和截图保存在本机私有目录中，但不是加密数据库，也不是取证级安全擦除。
- 原生悬浮窗用于本地显示和减少应用内部耦合，不承诺绕过监控，也不保证不会被第三方采集。

远程部署的详细要求见 `deployment/README.txt`。

## 已验证情况

截至 2026-09-08，本机已经验证：

- 199 项 Python 自动化测试通过。
- 原生 AppKit 悬浮窗编译、链接和临时签名通过。
- 主接收 `8788`、备用接收 `8789`、Sender、DisplayBridge 和原生悬浮窗同时运行。
- 真实 F22 截图、持久入队、embedded 接收、模型分析、结果显示和显示回执完整成功。

尚未验证公网远程服务器、睡眠唤醒、所有显示器组合及所有第三方应用环境。
