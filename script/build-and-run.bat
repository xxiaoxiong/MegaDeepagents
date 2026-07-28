@echo off
chcp 65001 >nul
title MegaDeepagents - Build & Run

setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo.
echo ============================================
echo   MegaDeepagents - Build & Run (Docker)
echo ============================================
echo.
echo [1/2] 正在构建 Docker 镜像...
docker compose build --no-cache
if errorlevel 1 (
    echo [ERROR] 镜像构建失败
    pause
    exit /b 1
)

echo.
echo [2/2] 镜像构建成功！正在启动服务...

echo     停止旧容器...
docker compose down >nul 2>&1

echo     启动新容器...
docker compose up -d
if errorlevel 1 (
    echo [ERROR] 容器启动失败
    pause
    exit /b 1
)

echo.
echo     等待服务就绪...
set /a retries=0
:wait_backend
curl -s -o nul -w "%%{http_code}" http://localhost:8081/health 2>nul | findstr "200" >nul
if errorlevel 1 (
    set /a retries+=1
    if !retries! geq 30 (
        echo [ERROR] 服务 30s 内未就绪，请查看日志:
        echo       docker compose logs runtime
        pause
        exit /b 1
    )
    timeout /t 1 /nobreak >nul
    goto wait_backend
)

echo.
echo ============================================
echo   构建并启动完成！
echo.
echo   Frontend : http://127.0.0.1:8081/chat
echo   Backend  : http://127.0.0.1:8081/api/health
echo   API Docs : http://127.0.0.1:8081/docs
echo.
echo   停止服务: docker compose down
echo   查看日志: docker compose logs runtime
echo ============================================
echo.
pause
