param([Parameter(ValueFromRemainingArguments=$true)][string[]]$FlowArgs)
$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($env:NANOKVM_PYTHON) { $python=$env:NANOKVM_PYTHON }
elseif (-not(Test-Path -LiteralPath $python)) { $python=(Get-Command python -ErrorAction Stop).Source }
Push-Location -LiteralPath $PSScriptRoot
try { & $python -m nanokvm_flow @FlowArgs; exit $LASTEXITCODE } finally { Pop-Location }
