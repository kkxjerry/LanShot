# LanShot 多端部署记录

日期：2026-09-08

## 部署结果

本次从 `develop` 部署两个相互独立的远程接收集群。远程路由位于本机三个入口之后，
不会并行发送截图，也不会在结果不确定时跨集群重复调用模型。

| 名称 | SSH 别名 | 客户端入口 | 集群 ID | 状态 |
| --- | --- | --- | --- | --- |
| 火山云 | `vv` | `https://115.191.9.22/lanshot-vv` | `f92064bd-39e2-457a-9cde-16c6adbbcd23` | ready |
| A40 | `a40`，经 `a40-jump` | `https://10.147.20.54:19443` | `04b37562-befd-4b03-b559-4a8a294484b1` | ready |

## 服务结构

火山云：

```text
Mac -> 115.191.9.22:443 -> Nginx /lanshot-vv/
    -> 127.0.0.1:19444 -> lanshot-remote.service
```

A40：

```text
Mac -> 10.147.20.54:19443 -> a40-jump 受限 SSH 反向端口
    -> A40 127.0.0.1:9443 -> lanshot-remote.service
```

两台接收服务器均使用：

- Ubuntu 22.04、Python 3.10 和 systemd。
- 独立 `lanshot` 系统用户。
- `/opt/lanshot/screenshot-sender` 最小运行源码。
- `/var/lib/lanshot` 私有状态目录。
- `/etc/lanshot/receiver.env` root-only 模型 Key 和入站令牌。
- `/etc/lanshot/prompt.txt` 与本机一致的 `default` 提示词。
- loopback Python 监听、TLS、Bearer Token 和持久 SQLite 状态。

## TLS 与认证

火山云公网 IP 使用部署专用 CA 签发的 IP SAN 证书。A40 内网证书由同一 CA 签发，
CA 私钥只保存在火山云 root 权限目录中。客户端 CA 公钥为：

`deployment/instances/remote-vv-ca.crt`

SHA-256 指纹：

`0F:8A:1E:AD:18:60:FE:E7:3E:F5:DF:D0:C5:22:5F:9E:94:5C:2E:1B:72:59:40:56:60:DC:C7:18:CF:A9:0F:46`

两台服务器使用不同的入站令牌。本机只在 Keychain 服务 `com.lanshot.p1` 中保存：

- `LANSHOT_REMOTE_VV_TOKEN`
- `LANSHOT_REMOTE_A40_TOKEN`

仓库、路由 JSON、systemd 命令行和本文均不包含令牌值或模型 Key。

## 本机路由

活动配置：

`~/Library/Application Support/LanShotP1R2Live/settings.json`

实际优先级：

```text
embedded
-> local-primary 127.0.0.1:8788
-> local-backup 127.0.0.1:8789
-> remote-vv
-> remote-a40
```

`allow_remote=true` 已由用户的多端部署请求显式授权。已完成的旧任务仍固定在原 embedded 集群，
不会因策略变化被重新发送。

## 验证结果

- 两台服务器 `lanshot-remote.service` 均为 `active`。
- A40 的 `lanshot-tunnel.service` 为 `active`，jump 仅监听内网 `10.147.20.54:19443`。
- 两个入口均通过应用自身的自定义 CA 校验、Bearer Token、构建版本、profile、cluster ID、
  storage 和 worker 健康检查。
- 两个上传入口均拒绝测试用非法负载，远程任务计数保持为空。
- 未发送真实截图到远程服务器，也未因部署验证产生远程模型调用。
- A40 时钟比 Mac 快约 7 分钟；未修改服务器系统时间，证书改由火山云 CA 签发。

## 运维命令

火山云：

```sh
ssh vv 'systemctl status lanshot-remote.service nginx'
```

A40：

```sh
ssh a40 'systemctl status lanshot-remote.service lanshot-tunnel.service'
```

本机查看路由和状态：

```sh
SETTINGS="$HOME/Library/Application Support/LanShotP1R2Live/settings.json"
python3 diagnostics.py --settings "$SETTINGS" status
python3 diagnostics.py --settings "$SETTINGS" routes
```

## 边界

- A40 入口依赖 Mac 能访问 `10.147.20.54` 私网地址。
- 火山云入口和 A40 入口分别拥有独立数据库，不提供跨集群全局 exactly-once。
- 已经可能送达某个集群的任务不会自动改投另一集群。
- 远程服务器不能替代 Mac 截图，也不能恢复尚未上传的本地图片。
