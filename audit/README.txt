审计说明

P1_to_R2.patch：上传旧P1→本次修订的文本源码/测试/文档差异。不是用户Mac当前P0可直接应用的补丁。
native_overlay.patch：原上传原生源码→本次显示适配。
uploaded_snapshot_to_original_P1.patch：只供历史审计。它描述更早上传快照→旧P1，敏感硬编码历史值会替换为REDACTED。本次匹配替换计数：0。不要应用这个历史patch来覆盖当前版本。

CHANGELOG_FILES.txt和AUDIT_CHANGELOG.json包含所有当前源码、测试、文档和验证文件的变更及摘要。
审计自身生成文件及SHA256SUMS不纳入清单，避免递归；根目录SHA256SUMS覆盖实际发布文件（除它自身）。
发布包不包含容器.git、Python缓存、用户截图、运行数据库、凭据、证书私钥或预编译的原生App。
原始输入ZIP与用户数据不变。历史patch出现已删除Tk代码仅作删除证据，不是运行时模块。
