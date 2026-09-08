# LanShot P1 修订版：原生显示与多接收路径

版本：lanshot-p1r2-20260908.1。

这是对已上传 P1 包的修订源码，不是用户 Mac 当前目录的部署或已合并提交。原始 P1 ZIP、原 P0 目录和用户运行数据没有改动。

正式显示端恢复为 `../capture-exclusion-demo` 的原生窗口。Tk 窗口及 GUI 命令已删除；诊断保留为命令行工具。

接收链路支持内嵌、本地双 HTTP 服务和显式启用的远程 HTTPS 服务。它们不是并行群发：本地实例共用任务库及执行锁，独立远程实例按持久任务归属受控切换。

先做安全演练：

```sh
python3 smoke_p1.py
python3 smoke_multi.py
python3 -m unittest discover -s tests -v
```

这些演练不截取桌面、不调用付费模型、不启动 launchd、不修改用户运行目录。

在 macOS 的独立解压目录准备新版配置：

```sh
sh ../capture-exclusion-demo/build.sh
sh build_native_redactor.command
python3 manage_services.py configure --mode redundant
python3 manage_services.py start --dry-run
```

配置默认禁用，默认本地端口 8788/8789。旧设置存在时拒绝覆盖。安装或迁移前，必须先按 `P1_DELIVERY_CN.txt` 检查旧进程、权限、数据备份和路径。不要直接覆盖当前 P0 工作区。

完成本机准备后，显式启动原生显示：

```sh
sh start_assessment.command
python3 diagnostics.py status
python3 diagnostics.py routes
```

完整改动、删除清单、使用方式和未验证边界见 `P1_DELIVERY_CN.txt`。远程部署见 `deployment/README.txt`。逐文件清单见 `../audit/CHANGELOG_FILES.txt`，完整差异见 `../audit/`。测试结果见 `VALIDATION_REPORT.json` 和 `validation/`。
