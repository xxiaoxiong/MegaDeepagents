@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MegaDeepagents - 本地开发启动

REM ============================================================
REM  MegaDeepagents 本地开发启动脚本
REM  - 后端：容器内 uvicorn (端口 8081)
REM  - 前端：本地 Vite 开发服务器 (端口 5173, 热更新)
REM  - 双击即可，关闭窗口即停止所有服务
REM ============================================================

cd /d "%~dp0\.."

echo.
echo [1/4] 检查 Docker 容器状态...
docker compose ps runtime 2>nul | findstr "running" >nul
if errorlevel 1 (
    echo     后端容器未运行，启动中...
    docker compose up -d runtime
    if errorlevel 1 (
        echo [ERROR] 后端容器启动失败
        pause
        exit /b 1
    )
) else (
    echo     后端容器已运行
)

echo.
echo [2/4] 检查后端健康状态 (端口 8081)...
set /a retries=0
:wait_backend
curl -s -o nul -w "%%{http_code}" http://localhost:8081/health 2>nul | findstr "200" >nul
if errorlevel 1 (
    set /a retries+=1
    if !retries! geq 30 (
        echo [ERROR] 后端 30s 内未就绪，请检查 docker compose logs
        pause
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
    goto wait_backend
)
echo     后端就绪

echo.
echo [3/4] 检查前端依赖...
if not exist "frontend\node_modules" (
    echo     安装前端依赖...
    pushd frontend
    call npm install
    popd
    if errorlevel 1 (
        echo [ERROR] 前端依赖安装失败
        pause
        exit /b 1
    )
) else (
    echo     前端依赖已存在
)

echo.
echo [4/4] 启动前端 Vite 开发服务器 (端口 5173, 热更新)...
echo.
echo ============================================================
echo   后端 API:  http://localhost:8081
echo   前端 UI:   http://localhost:5173
echo   关闭此窗口即停止前端开发服务器
echo ============================================================
echo.

pushd frontend
call npm run dev
popd

echo.
echo [INFO] 前端开发服务器已停止
echo [INFO] 后端容器仍在运行，可执行 docker compose down 停止
pause
