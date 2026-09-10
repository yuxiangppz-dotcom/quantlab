param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8501,
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "start",
    [switch]$NoBrowser,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$ProjectPath = "/home/administrator/projects/quantlab"
$Command = "cd '$ProjectPath' && .venv/bin/python -m quantlab.local_ui $Action --port $Port"

try {
    $ResultText = & wsl.exe -d Ubuntu -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) { throw "QuantLab could not complete $Action. $ResultText" }
    $Result = $ResultText | ConvertFrom-Json
    if ($Result.status -eq "error") { throw $Result.message }
    if ($Action -eq "start" -and -not $NoBrowser) {
        Start-Process $Result.url
    }
    if (-not $Quiet) { Write-Output $ResultText }
    exit 0
} catch {
    if ($Quiet) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show(
            "QuantLab failed. Please check data/runtime/ui/$Port.log.`n$($_.Exception.Message)",
            "QuantLab"
        ) | Out-Null
    } else {
        Write-Error $_.Exception.Message
    }
    exit 1
}
