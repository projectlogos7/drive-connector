# Nous Sync installer for Windows (PowerShell).
#   irm <server>/install.ps1 | iex
#   Or: .\install.ps1 -Code ABCD-EFGH -Server https://drive.nous.example [-Name "Edit Suite PC"]
param([string]$Code = "", [string]$Server = "", [string]$Name = "", [string]$Source = "")
$ErrorActionPreference = "Stop"
$py = (Get-Command py -ErrorAction SilentlyContinue) ?? (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "Python 3.11+ is required (https://www.python.org/downloads/windows/)" }
$app = Join-Path $env:USERPROFILE ".nous-sync\app"
New-Item -ItemType Directory -Force -Path $app | Out-Null
& $py.Source -m venv "$app\venv"
& "$app\venv\Scripts\pip.exe" install --quiet --upgrade pip
if (-not $Source) {
  if ($Server) { $Source = "nous-sync[content] @ $($Server.TrimEnd('/'))/agent/nous-sync.zip" }
  else { $Source = "nous-sync[content]" }
} elseif ($Source -notmatch '@|\[' ) {
  $Source = "$Source[content]"
}
& "$app\venv\Scripts\pip.exe" install --quiet $Source
$exe = "$app\venv\Scripts\nous-sync.exe"
if ($Code -and $Server) {
  if ($Name) { & $exe pair $Code --server $Server --name $Name } else { & $exe pair $Code --server $Server }
}
& $exe install
Write-Host "Done. Add folders with: $exe add D:\Footage"
