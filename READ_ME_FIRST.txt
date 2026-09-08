LanShot P1 原生显示与多接收服务修订包
版本 lanshot-p1r2-20260908.1

先读 screenshot-sender/P1_DELIVERY_CN.txt。

正式显示：capture-exclusion-demo 原生窗口；Tk GUI 已删除。
多入口：内嵌接收、本地8788/8789双服务、明确授权的远程HTTPS。
模式：redundant 默认；monolith 为无本地HTTP的内嵌单体。

安全演练：在 screenshot-sender 中运行 python3 smoke_multi.py。
完整测试：python3 -m unittest discover -s tests -v。

本包是源码修订，不是用户Mac当前P0的已部署更新。
未执行Mac原生App编译/开窗、launchd安装、真实模型、牛客、真实远程服务器部署。
不要直接覆盖现有P0；先保留备份并在独立目录验证。

完整变更：audit/CHANGELOG_FILES.txt、audit/AUDIT_CHANGELOG.json和全量patch。
验证：screenshot-sender/VALIDATION_REPORT.json与validation目录。
服务器配置：screenshot-sender/deployment/README.txt。
