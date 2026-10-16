#!/usr/bin/env pwsh
<#
.SYNOPSIS
    启用仓库自带的 Git 钩子（提交前密钥扫描）。

.DESCRIPTION
    把 Git 的钩子目录指向仓库内的 .githooks/，只影响当前仓库，不改全局配置。
    等价于在仓库根目录执行：

        git config core.hooksPath .githooks

    临时停用：git config --unset core.hooksPath

.PARAMETER Uninstall
    取消钩子（恢复 Git 默认的 .git/hooks）。

.EXAMPLE
    scripts/install-git-hooks.ps1
    scripts/install-git-hooks.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repoRoot
try {
    if (-not (Test-Path (Join-Path $repoRoot '.git'))) {
        Write-Host '[X]  当前目录不是 Git 仓库，请先 git init。' -ForegroundColor Red
        exit 1
    }

    if ($Uninstall) {
        git config --local --unset core.hooksPath 2>$null
        Write-Host '[OK] 钩子已停用（core.hooksPath 已清除）' -ForegroundColor Green
        exit 0
    }

    $hook = Join-Path $repoRoot '.githooks/pre-commit'
    if (-not (Test-Path $hook)) {
        Write-Host "[X]  找不到 $hook" -ForegroundColor Red
        exit 1
    }

    git config --local core.hooksPath .githooks
    Write-Host "[OK] 已启用 .githooks/（core.hooksPath = $((git config --local core.hooksPath))）" -ForegroundColor Green
    Write-Host '     以后每次 commit 都会先扫描暂存区里的疑似密钥。'
    Write-Host '     跳过单次检查：git commit --no-verify'

    # 钩子内部优先用 gitleaks（与 CI 口径一致），没装则退回 grep 兜底
    if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
        Write-Host "     已检测到 gitleaks $((gitleaks version 2>$null))，将使用官方规则扫描。"
    }
    else {
        Write-Host '     未检测到 gitleaks，将使用内置 grep 兜底扫描。'
        Write-Host '     想要和 CI 完全一致的规则，可安装 gitleaks：'
        Write-Host '       winget install --id gitleaks.gitleaks' -ForegroundColor DarkGray
    }
}
finally {
    Pop-Location
}
