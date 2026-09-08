远程接收服务部署说明（模板未安装，未连接真实服务器）

一、先区分本地冗余与远程备份

本地 embedded、local-primary、local-backup 使用同一台机器、同一份 receiver_tasks.sqlite3 和 analysis-owner.lock。
远程服务有自己的磁盘与数据库，不能把 Mac 上的 SQLite/WAL/锁文件复制到网络共享盘来冒充统一事务。
发送端记录每个任务实际交给了哪个 cluster_id。请求可能已经到达某个集群以后，不会自动改投另一个独立集群。

二、服务器准备

使用独立的系统账户和私有状态目录，安装 Python 3.10 或更新版本以及 TLS 证书，复制整个 screenshot-sender 源码目录。
本次实测 Python 是 3.13.5；未逐一验证其他 Python 小版本。
准备与本机完全相同的 prompt 文件和 profile；内容摘要不匹配时发送端拒绝提交，避免同一任务切换成另一套提示词。

服务器环境必须提供 DASHSCOPE_API_KEY 和 LANSHOT_LOCAL_TOKEN。
这里 LANSHOT_LOCAL_TOKEN 是“该远程服务的入站认证令牌”，不是要求你把 Mac 本地服务的令牌复用过去。
Mac 使用单独账户 LANSHOT_REMOTE_TOKEN 保存同一份远程认证值；不要填写在 URL、路由 JSON、命令行参数或 plist 中。

以前台方式启动，替换实际路径和主机名：

python3 deployment/remote_receiver.py --public-host receiver.example.com --port 9443 --profile default --prompt-file /etc/lanshot/prompt.txt --state-dir /var/lib/lanshot --tls-cert /etc/lanshot/tls/fullchain.pem --tls-key /etc/lanshot/tls/privkey.pem

模型 Key 和令牌由环境或受限的服务配置注入。此命令不包含它们的值。
也可以采用 lanshot-remote.service.example 模板，由管理员创建账户、目录、证书权限、0600 的环境文件，再安装启用 systemd 单元。模板本身不会执行安装。
证书必须对客户端可信且主机名匹配；生产环境不要关闭 TLS 验证。测试套件使用测试进程专用的信任上下文，并没有全局关闭 TLS 校验。
防火墙和访问范围需由服务器管理员配置；示例不等于经过公网渗透测试或企业安全认证。

三、本机添加远程后备

先停止本版本的托管服务：
python3 manage_services.py stop

在 macOS 钥匙串中，以服务 com.lanshot.p1、账户 LANSHOT_REMOTE_TOKEN 保存远程服务的认证令牌。
然后执行：
python3 manage_services.py add-remote --url https://receiver.example.com:9443 --name remote --token-env LANSHOT_REMOTE_TOKEN --allow-remote-images

建议从经过认证的 /api/health 获取远程 cluster_id，并通过 --expected-cluster <UUID> 固定身份。
使用私有 CA 或直接连接带 IP SAN 的证书时，通过 `--ca-file /绝对路径/ca.crt` 为该端点固定信任根；
不要关闭 TLS 校验，也不要把 CA 私钥复制到客户端。
添加路由只改配置，不上传图片，也不代表服务已经部署。远程默认关闭；命令中的 --allow-remote-images 是显式授权这个目的地接收截图。

重新检查配置和队列，再启动：
python3 manage_services.py start --dry-run
sh start_assessment.command
python3 diagnostics.py routes

四、旧任务与路由修改

任务的目标是路由策略摘要，不只是一个 URL。修改端点、远程授权、profile 或 prompt 会形成新策略。
旧未完成任务不会被悄悄发送到新策略；需要保留旧配置进行恢复/对账或显式处理任务。不要通过删数据库、改任务 ID 来绕过去重或结果未知状态。
请求发出前所有本地入口都不可用，或明确连接建立失败时，新任务可尝试远程。
发出后超时、5xx、确认内容不匹配等结果不确定时，仅向原集群查询/重试；不因独立远程返回 404 就认定原模型没有执行。

五、边界

本机磁盘或 sender 整体损坏时，远程服务不能替代已经停止的桌面采集器；尚未上传的图片也不会凭空恢复。
本实现没有跨机器全局 exactly-once 保证，也没有实现自动跨集群迁移已进入模型处理的任务。
本次实际 HTTPS 验证在本地测试端口运行，并不是你的真实服务器部署验证。
