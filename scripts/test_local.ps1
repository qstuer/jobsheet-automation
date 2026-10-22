param(
    [string]$EnvironmentPath = 'tmp/local-test-env',
    [switch]$Setup
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskEnvironment = [System.IO.Path]::GetFullPath((Join-Path $taskRoot $EnvironmentPath))
if (-not $taskEnvironment.StartsWith(($taskRoot + [System.IO.Path]::DirectorySeparatorChar), [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The isolated test environment must stay inside this checkout.'
}
$taskPython = Join-Path $taskEnvironment 'Scripts/python.exe'
if ($Setup) {
    if (-not (Test-Path -LiteralPath $taskPython)) {
        $taskLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($taskLauncher) {
            & $taskLauncher.Source -3.12 -m venv $taskEnvironment
        } else {
            $taskBootstrapPython = Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312/python.exe'
            if (-not (Test-Path -LiteralPath $taskBootstrapPython)) {
                throw 'Python 3.12 is required to create the isolated test environment.'
            }
            & $taskBootstrapPython -m venv $taskEnvironment
        }
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 virtual environment creation failed.' }
    }
    & $taskPython -m pip install --disable-pip-version-check -r (Join-Path $taskRoot 'requirements.txt') -r (Join-Path $taskRoot 'requirements-ocr-trace.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Pinned test dependency installation failed.' }
}
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Run this script with -Setup once to create the isolated test environment.'
}
$taskOldPath = $env:PYTHONPATH
$taskOldBytecode = $env:PYTHONDONTWRITEBYTECODE
$taskSavedEnvironment = @{}
foreach ($taskName in @('ASANA_TOKEN', 'NVIDIA_API_KEY', 'DEEPSEEK_API_KEY',
        'JOBSHEET_DEVICE_SEARCH_AGENT', 'JOBSHEET_DATE_CORROBORATION',
        'JOBSHEET_MATCHING_RULES', 'JOBSHEET_EVIDENCE_LIVE_RULES',
        'JOBSHEET_BACKTEST_RECIPIENT_KEY', 'JOBSHEET_ORIGINAL_UPLOAD_DATE')) {
    $taskSavedEnvironment[$taskName] = [Environment]::GetEnvironmentVariable($taskName)
    [Environment]::SetEnvironmentVariable($taskName, $null)
}
Push-Location $taskRoot
try {
    $env:PYTHONPATH = ''
    $env:PYTHONDONTWRITEBYTECODE = '1'
    & $taskPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Test dependency validation failed.' }
    & $taskPython -m unittest discover -s tests -p 'test_*.py' -q
    if ($LASTEXITCODE -ne 0) { throw 'Local unit tests failed.' }
} finally {
    $env:PYTHONPATH = $taskOldPath
    $env:PYTHONDONTWRITEBYTECODE = $taskOldBytecode
    foreach ($taskName in $taskSavedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($taskName, $taskSavedEnvironment[$taskName])
    }
    Pop-Location
}
