# LanShot P1 更新包

本目录已将 P1 独立更新包合并到 `develop` 管理的源码中，但尚未部署或启动服务。
合并时以本机 P0 为共同基线做了核对，保留了音频源码、笔试提示词和音频启停脚本；
P1 的 SQLite 队列、持久确认和 LaunchAgent 管理实现取代了 P0 中对应的临时可靠性实现。

先阅读 `MERGE_NOTES_2026-09-08.md` 和 `P1_DELIVERY_CN.txt`。旧使用说明留在
`README_LEGACY.md`，不要按其中的旧 install/启动方式部署 P1。

安全演练（不截图、不调用真实模型、不启动 launchd）：

```sh
python3 smoke_p1.py
python3 -m unittest discover -s tests -v
```

macOS 独立安装准备（不修改旧 P0 目录）：

```sh
python3 manage_services.py configure
python3 manage_services.py start --dry-run
```

配置使用独立的 LanShotP1 状态目录、8788 端口，且初始禁用。
在完成代码核对、停止旧监听服务、确认权限后，再显式运行 `python3 manage_services.py start`。

完整变更、测试证据、风险边界和迁移说明见 `MERGE_NOTES_2026-09-08.md`、
`P1_DELIVERY_CN.txt`、`VALIDATION_REPORT.json`、`COMPATIBILITY_MATRIX.json`。
