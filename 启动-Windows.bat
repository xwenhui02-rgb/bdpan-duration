@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 百度网盘视频时长清单

echo.
echo ==============================================
echo   百度网盘视频时长清单
echo ==============================================
echo.

set PY=
where py >nul 2>&1 && set PY=py
if "%PY%"=="" (
    where python >nul 2>&1 && set PY=python
)

if "%PY%"=="" (
    echo 没有找到 Python。
    echo.
    echo 请先装 Python，再来双击这个文件：
    echo   https://www.python.org/downloads/
    echo.
    echo 安装界面最下面的 Add python.exe to PATH 一定要勾上。
    echo.
    pause
    exit /b 1
)

%PY% bdpan-duration.py --wizard

echo.
echo ----------------------------------------------
echo 跑完的清单在 bdpan-report 文件夹里：
echo   inventory.html  ^<- 双击这个看结果
echo.
pause
