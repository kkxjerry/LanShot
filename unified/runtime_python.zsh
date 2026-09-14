#!/bin/zsh

lanshot_python() {
  local config_file="$HOME/Library/Application Support/LanShot/python_path"
  local -a candidates
  candidates=()
  [[ -n "${LANSHOT_PYTHON:-}" ]] && candidates+=("$LANSHOT_PYTHON")
  [[ -f "$config_file" ]] && candidates+=("$(<"$config_file")")
  candidates+=(
    "${commands[python3]:-}"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
      print -r -- "$candidate"
      return 0
    fi
  done

  print -u2 -- "LanShot 需要 Python 3.10 或更高版本。"
  print -u2 -- "请先安装 Python 3.12，然后重新运行 install.command。"
  return 1
}
