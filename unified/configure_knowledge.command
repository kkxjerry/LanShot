#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/runtime_python.zsh"
PYTHON="$(lanshot_python)"

print -n -- "百炼 Workspace ID："
read workspace_id
print -n -- "已发布的知识检索 Agent ID："
read agent_id

"$PYTHON" "$SCRIPT_DIR/knowledge_config.py" configure \
  --workspace-id "$workspace_id" \
  --agent-id "$agent_id"

print
read -k 1 "?配置已保存，按任意键关闭窗口..."
print
