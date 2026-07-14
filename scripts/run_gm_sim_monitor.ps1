param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("preopen", "intraday", "eod", "manual")]
    [string]$Phase,

    [Parameter(Mandatory = $true)]
    [string]$TradeDate,

    [string]$ConfigPath = "",
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $Root "config\local_gm_sim.json"
}
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $Root "reports\gm_sim_monitor"
}

$Day = $TradeDate.Replace("-", "")
$LogDir = Join-Path $OutputRoot $Day
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "HHmmss"
$LogPath = Join-Path $LogDir ("task_{0}_{1}.log" -f $Phase, $Stamp)

Set-Location $Root
$Python = "python"
$Args = @(
    "scripts\monitor_gm_sim.py",
    "--phase", $Phase,
    "--trade-date", $TradeDate,
    "--config", $ConfigPath,
    "--output-root", $OutputRoot
)

& $Python @Args *>&1 | Tee-Object -FilePath $LogPath
exit $LASTEXITCODE
