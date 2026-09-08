#!/bin/zsh
cd "${0:A:h}"
./audio_service.py start
result=$?
echo
if [[ $result -eq 0 ]]; then
    output="$HOME/Library/Application Support/LanShotAudio"
    while [[ -f "$output/capture.pid" ]]; do
        pid=$(<"$output/capture.pid")
        kill -0 "$pid" 2>/dev/null || break
        printf '\033[2J\033[H'
        echo "千问正在实时识别面试官，最近内容："
        echo "--------------------------------------------------"
        tail -n 30 "$output/interviewer.txt" 2>/dev/null
        sleep 0.5
    done
    echo
    echo "监听已经停止。"
else
    echo "启动失败，请保留此窗口中的错误信息。"
fi
read -k 1 "?按任意键关闭窗口..."
echo
exit $result
