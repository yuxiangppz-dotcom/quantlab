$ErrorActionPreference = "Stop"
$Launcher = Join-Path $PSScriptRoot "start_quantlab_ui.ps1"
$Desktop = [Environment]::GetFolderPath("Desktop")
$PowerShellPath = Join-Path ([Environment]::GetFolderPath("System")) "WindowsPowerShell\v1.0\powershell.exe"
$ShortcutShell = New-Object -ComObject WScript.Shell

foreach ($Item in @(
    @{ Name = "QuantLab"; Action = "start"; Icon = 21 },
    @{ Name = "Stop QuantLab"; Action = "stop"; Icon = 27 }
)) {
    $ShortcutPath = Join-Path $Desktop ($Item.Name + ".lnk")
    if (Test-Path -LiteralPath $ShortcutPath) {
        $Existing = $ShortcutShell.CreateShortcut($ShortcutPath)
        if ($Existing.Arguments -notlike "*$Launcher*") {
            throw "An unrelated shortcut already exists at $ShortcutPath; it was preserved."
        }
    }
    $Shortcut = $ShortcutShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $PowerShellPath
    $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`" -Action $($Item.Action) -Quiet"
    $Shortcut.WorkingDirectory = $Desktop
    $Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,$($Item.Icon)"
    $Shortcut.Description = "QuantLab local workbench: $($Item.Action)"
    $Shortcut.Save()
    Write-Output $ShortcutPath
}
