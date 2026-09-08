#!/bin/zsh
cd "${0:A:h}"
./audio_service.py stop
result=$?
echo
if [[ $result -eq 0 ]]; then
    echo "监听已停止，本地识别结果已经写入。"
    open "$HOME/Library/Application Support/LanShotAudio"
else
    echo "停止失败，请保留此窗口中的错误信息。"
fi
read -k 1 "?按任意键关闭窗口..."
echo
exit $result
