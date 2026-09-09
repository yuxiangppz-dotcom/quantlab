param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"
$ProjectPath = "/home/administrator/projects/quantlab"
$Command = "cd '$ProjectPath' && uv run quantlab ui --port $Port"

Write-Host "Starting QuantLab Daily at http://127.0.0.1:$Port"
& wsl.exe -d Ubuntu -- bash -lc $Command
exit $LASTEXITCODE
