param([int]$Port = 8776)
$ErrorActionPreference = 'Stop'
$labRoot = $PSScriptRoot
$labPython = if (Test-Path -LiteralPath 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { 'python' }
& $labPython (Join-Path $labRoot 'server.py') --port $Port
