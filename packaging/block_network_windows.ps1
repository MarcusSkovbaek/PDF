# Optional extra safety: adds Windows Firewall rules that block Folio PDF from
# all inbound and outbound network traffic. The application already blocks
# network access internally; this makes it enforced by the OS as well.
#
# Run in an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File packaging\block_network_windows.ps1 [-ExePath C:\path\FolioPDF.exe]
param(
    [string]$ExePath = (Join-Path $PSScriptRoot "..\dist\FolioPDF\FolioPDF.exe")
)

$ErrorActionPreference = "Stop"
$exe = (Resolve-Path $ExePath).Path

foreach ($dir in @("Outbound", "Inbound")) {
    $name = "Folio PDF - block $dir"
    Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $name -Direction $dir -Program $exe -Action Block -Profile Any | Out-Null
    Write-Host "Oprettet firewall-regel: $name"
}
Write-Host "Folio PDF er nu blokeret fra netvaerket af Windows Firewall."
