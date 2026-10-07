#Requires -Version 5.1
# Commands arrive as JSON data, never an interpolated PowerShell expression.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
try {
    $request = [Console]::ReadLine() | ConvertFrom-Json
    Set-Location -LiteralPath $request.directory
    $executable = [string]$request.command[0]
    $arguments = @($request.command | Select-Object -Skip 1)
    & $executable @arguments
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
exit 0
