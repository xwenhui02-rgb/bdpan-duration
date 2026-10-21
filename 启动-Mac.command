#!/bin/bash
# 双击这个文件就能用。不需要打开终端。
cd "$(dirname "$0")" || exit 1
[ -t 1 ] && clear

echo ""
echo "=============================================="
echo "  百度网盘视频时长清单"
echo "=============================================="
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo "这台 Mac 上没找到 Python 3。"
  echo ""
  echo "我可以帮你打开下载页，装完再回来双击本文件。"
  echo "（如果弹出「安装命令行开发者工具」的窗口，点「安装」等它装完就行）"
  echo ""
  open "https://www.python.org/downloads/macos/" 2>/dev/null
  echo "按任意键关闭这个窗口…"
  read -n 1 -s -r
  exit 1
fi

python3 bdpan-duration.py --wizard

echo ""
echo "----------------------------------------------"
echo "跑完的清单在 bdpan-report 文件夹里："
echo "  inventory.html  ← 双击这个看结果"
echo ""
echo "按任意键关闭这个窗口…"
read -n 1 -s -r
