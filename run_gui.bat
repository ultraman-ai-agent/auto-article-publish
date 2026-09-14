@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo 已根据 .env.example 生成 .env，请到界面「配置」页填写密钥后保存。
)

rem 去除下载来源标记，避免 SmartScreen 对未签名 exe 的拦截（尽力而为）
powershell -NoProfile -Command "Get-ChildItem -LiteralPath '%~dp0' -Filter '*.exe' -ErrorAction SilentlyContinue | Unblock-File" >nul 2>nul

python -c "import customtkinter" >nul 2>nul
if errorlevel 1 (
  echo 正在安装界面依赖 customtkinter...
  python -m pip install "customtkinter>=5.2.0"
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "app.py"
) else (
  start "" python "app.py"
)
