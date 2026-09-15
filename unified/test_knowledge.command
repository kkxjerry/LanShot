#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/runtime_python.zsh"
PYTHON="$(lanshot_python)"

print -n -- "输入一个肯定存在于知识库的问题："
read query
"$PYTHON" "$SCRIPT_DIR/knowledge_config.py" probe --query "$query"

print
read -k 1 "?测试完成，按任意键关闭窗口..."
print
