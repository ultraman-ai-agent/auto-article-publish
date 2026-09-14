#Requires -Version 5.1
<#
  为 xiaohongshu-mcp（基于 go-rod）添加 Windows Defender 排除项。

  背景：程序运行时会下载 Chromium 并释放辅助进程 leakless.exe 到
        %LOCALAPPDATA%\Temp\leakless-amd64-<hash>\，
        Defender 以启发式规则将其误报为 Trojan:Win32/Kepavll!rfn 并查杀。
        注意：拦截是 **leakless.exe 自身进程** 发起的，所以必须排除该目录；
        仅排除 xiaohongshu-*.exe 进程或项目目录**无效**。
        官方 Windows 指南同样要求排除该 leakless 目录。

  用法（需管理员）：右键“以管理员身份运行 PowerShell”后执行
        powershell -ExecutionPolicy Bypass -File .\scripts\allow_defender.ps1
  非管理员运行时会自动尝试提权。
#>

$ErrorActionPreference = 'Stop'

# Defender 官方已知的 leakless 目录名（hash 由 leakless 二进制内容决定，通常稳定）
$KnownLeaklessName = 'leakless-amd64-adb80298fa6a3af7ced8b1c9b5f18007'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-Admin)) {
    Write-Host "需要管理员权限，正在尝试以管理员身份重新启动..." -ForegroundColor Yellow
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath
        )
    } catch {
        Write-Host "提权失败：$_" -ForegroundColor Red
        exit 1
    }
    exit 0
}

if (-not (Get-Command Add-MpPreference -ErrorAction SilentlyContinue)) {
    Write-Host "未找到 Defender 模块（Add-MpPreference），系统可能未启用 Windows Defender。" -ForegroundColor Red
    exit 1
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$exeNames = @('xiaohongshu-mcp-windows-amd64.exe', 'xiaohongshu-login-windows-amd64.exe')
$leaklessRoot = Join-Path $env:LOCALAPPDATA 'Temp'

Write-Host "项目目录：$projectRoot" -ForegroundColor Cyan

function Add-ExclusionPath($path) {
    $pref = Get-MpPreference
    if (@($pref.ExclusionPath) -contains $path) {
        Write-Host "已存在路径排除：$path"
    } else {
        Add-MpPreference -ExclusionPath $path
        Write-Host "已添加路径排除：$path" -ForegroundColor Green
    }
}

# 1. 关键：排除 leakless 所在目录（自动发现，找不到则用官方已知路径）
$leaklessDirs = @(
    Get-ChildItem -LiteralPath $leaklessRoot -Filter 'leakless-amd64-*' -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName }
)
if ($leaklessDirs.Count -eq 0) {
    $leaklessDirs = @(Join-Path $leaklessRoot $KnownLeaklessName)
}
foreach ($dir in $leaklessDirs) {
    Add-ExclusionPath $dir
}

# 2. 项目目录排除（辅助）
Add-ExclusionPath $projectRoot

# 3. 进程排除（辅助，对 leakless.exe 无效，保留无害）
foreach ($name in $exeNames) {
    $pref = Get-MpPreference
    if (@($pref.ExclusionProcess) -contains $name) {
        Write-Host "已存在进程排除：$name"
    } else {
        Add-MpPreference -ExclusionProcess $name
        Write-Host "已添加进程排除：$name" -ForegroundColor Green
    }
}

# 4. 清理被拦后残留的 leakless 目录，让下次重新释放（此时已排除）
Get-ChildItem -LiteralPath $leaklessRoot -Filter 'leakless-amd64-*' -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        Remove-Item -LiteralPath $_.FullName -Recurse -Force
        Write-Host "已清理残留：$($_.FullName)" -ForegroundColor Green
    } catch {
        Write-Host "清理失败：$($_.FullName) - $_" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "当前路径排除项：" -ForegroundColor Cyan
$pref = Get-MpPreference
@($pref.ExclusionPath) | ForEach-Object { Write-Host "  $_" }
Write-Host "当前进程排除项：" -ForegroundColor Cyan
@($pref.ExclusionProcess) | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "完成。请到 Defender 的“保护历史记录”中对 leakless.exe 条目选择“允许/还原”，然后重启登录与 MCP 服务。" -ForegroundColor Green
Write-Host ""
Write-Host "提示：微软电脑管家（Microsoft PC Manager）也会调用 Defender 引擎拦截 leakless。" -ForegroundColor Yellow
Write-Host "若仍被拦，请在“微软电脑管家 → 设置/病毒防护”中添加信任/排除（leakless 目录与项目目录），或临时关闭其实时防护。" -ForegroundColor Yellow
