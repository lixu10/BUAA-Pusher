$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvDirectory = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvDirectory "Scripts\python.exe"
$requirementsFile = Join-Path $PSScriptRoot "requirements.txt"
$requirementsStamp = Join-Path $venvDirectory ".requirements.sha256"
$targetSitePackages = Join-Path $venvDirectory "Lib\site-packages"

$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
$pythonArguments = @()
if (-not $pythonCommand) {
    $pythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pythonCommand) {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    $pythonArguments = @("-3")
}
if (-not $pythonCommand) {
    throw "未找到 Python 3。请先安装 Python 3.11 或更高版本，并确保 python、python3 或 py 位于 PATH。"
}
$basePython = $pythonCommand.Source

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "[PUAA] 使用 $basePython 创建虚拟环境..."
    $isCondaPython = $env:CONDA_PREFIX -and $basePython.StartsWith(
        [System.IO.Path]::GetFullPath($env:CONDA_PREFIX),
        [System.StringComparison]::OrdinalIgnoreCase
    )
    if ($isCondaPython) {
        & $basePython @pythonArguments -m venv --without-pip --system-site-packages $venvDirectory
    } else {
        & $basePython @pythonArguments -m venv $venvDirectory
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "[PUAA] 标准 venv 不可用，切换到无内置 pip 模式..."
        & $basePython @pythonArguments -m venv --without-pip $venvDirectory
    }
    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "虚拟环境创建失败。请确认当前 Python 支持 venv。"
    }
}

$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".env.example") -Destination $envFile
}

$requirementsHash = (Get-FileHash -LiteralPath $requirementsFile -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $requirementsStamp) {
    (Get-Content -LiteralPath $requirementsStamp -Raw).Trim()
} else {
    ""
}

$runtimePython = $venvPython
$useExistingEnvironment = $false
& $venvPython -c "import fastapi, httpx, uvicorn, argon2, cryptography" *> $null
$venvHasDependencies = $LASTEXITCODE -eq 0

if ($venvHasDependencies) {
    $useExistingEnvironment = $true
    Write-Host "[PUAA] 虚拟环境中的依赖已就绪。"
} else {
    & $venvPython -m pip --version *> $null
    $venvHasPip = $LASTEXITCODE -eq 0
    if (-not $venvHasPip) {
        & $basePython @pythonArguments -c "import fastapi, httpx, uvicorn, argon2, cryptography" *> $null
        if ($LASTEXITCODE -eq 0) {
            $runtimePython = $basePython
            $useExistingEnvironment = $true
            Write-Host "[PUAA] venv 未提供 pip，使用当前 Python 环境中已安装的依赖。"
        }
    }
}

if ($requirementsHash -ne $installedHash -and -not $useExistingEnvironment) {
    Write-Host "[PUAA] 安装 Python 依赖..."
    if ($venvHasPip) {
        & $venvPython -m pip install --disable-pip-version-check -r $requirementsFile
    } else {
        Write-Host "[PUAA] 当前 Python 未能向 venv 写入 pip，改用隔离目录安装..."
        New-Item -ItemType Directory -Path $targetSitePackages -Force | Out-Null
        & $basePython @pythonArguments -m pip install --disable-pip-version-check --upgrade --target $targetSitePackages -r $requirementsFile
    }
    if ($LASTEXITCODE -ne 0) {
        throw "依赖安装失败。请检查网络或 pip 配置后重试。"
    }
    Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash -NoNewline
} elseif ($useExistingEnvironment -and $requirementsHash -ne $installedHash) {
    Set-Content -LiteralPath $requirementsStamp -Value $requirementsHash -NoNewline
}

Write-Host "[PUAA] 启动 http://127.0.0.1:8000"
& $runtimePython -m app
