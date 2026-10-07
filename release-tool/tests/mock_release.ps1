# Offline integration fixture: no GitHub requests and no real credentials.
param([ValidateSet('new', 'same', 'different', 'missing', 'partial', 'automatic', 'annotated', 'repository404', 'failed', 'bothfail', 'error422', 'push', 'recovery')][string]$Scenario = 'new')
$global:releaseMockScenario = $Scenario
$global:releaseMockCalls = [Collections.Generic.List[object]]::new()
function Invoke-RestMethod {
    param($Uri, $Headers, $Method, $TimeoutSec, $ErrorAction, $ContentType, $Body)
    if ($Headers.Authorization -ne 'Bearer offline-secret') { throw 'Unexpected authentication' }
    $global:releaseMockCalls.Add(@{ uri = $Uri; method = $Method; body = $Body })
    $path = ([Uri]$Uri).AbsolutePath
    if ($path -eq '/repos/owner/repo') {
        if ($global:releaseMockScenario -eq 'repository404') {
            $ex = [Exception]::new('missing')
            $ex | Add-Member NoteProperty Response ([pscustomobject]@{ StatusCode = 404 })
            throw $ex
        }
        return [pscustomobject]@{ default_branch = 'main' }
    }
    if ($path -eq '/repos/owner/repo/') { throw 'Repository URL must not have a trailing slash' }
    if ($path.Contains('/branches/') -and $global:releaseMockScenario -eq 'recovery') { return [pscustomobject]@{ commit = @{ sha = 'def456' } } }
    if ($path.Contains('/branches/')) { return [pscustomobject]@{ commit = @{ sha = 'abc123' } } }
    if ($path.Contains('/git/ref/tags/')) {
        if ($global:releaseMockScenario -eq 'new') {
            $ex = [Exception]::new('missing')
            $ex | Add-Member NoteProperty Response ([pscustomobject]@{ StatusCode = 404 })
            throw $ex
        }
        $type = if ($global:releaseMockScenario -eq 'annotated') { 'tag' } else { 'commit' }
        $sha = if ($global:releaseMockScenario -eq 'different') { 'other-commit' } else { 'abc123' }
        return [pscustomobject]@{ object = [pscustomobject]@{ type = $type; sha = $sha } }
    }
    if ($path.Contains('/git/tags/')) { return [pscustomobject]@{ object = [pscustomobject]@{ type = 'commit'; sha = 'abc123' } } }
    if ($path.EndsWith('/git/refs')) { return [pscustomobject]@{ ref = 'refs/tags/v1.2.3' } }
    if ($path.EndsWith('/runs')) {
        if ($global:releaseMockScenario -eq 'automatic' -and $path.Contains('/22/')) {
            return [pscustomobject]@{ workflow_runs = @([pscustomobject]@{
                id = 22; head_branch = 'v1.2.3'; head_sha = 'abc123'; event = 'push'; status = 'in_progress'; conclusion = $null
                html_url = 'https://github.com/owner/repo/actions/runs/22'
            }) }
        }
        if ($global:releaseMockScenario -eq 'failed') {
            return [pscustomobject]@{ workflow_runs = @([pscustomobject]@{ id = 10; head_branch = 'v1.2.3'; head_sha = 'abc123'; event = 'workflow_dispatch'; status = 'completed'; conclusion = 'failure' }) }
        }
        return [pscustomobject]@{ workflow_runs = @() }
    }
    if ($path.EndsWith('/dispatches')) {
        if ($global:releaseMockScenario -eq 'bothfail') { throw 'Simulated network failure' }
        if ($global:releaseMockScenario -eq 'error422') {
            $ex = [Exception]::new('rejected')
            $ex | Add-Member NoteProperty Response ([pscustomobject]@{ StatusCode = 422 })
            $record = [Management.Automation.ErrorRecord]::new($ex, 'api', 'InvalidArgument', $null)
            $record.ErrorDetails = [Management.Automation.ErrorDetails]::new('{"message":"Unexpected input offline-secret"}')
            throw $record
        }
        if ($global:releaseMockScenario -eq 'partial' -and $path.Contains('/22/')) { throw 'Simulated network failure' }
        return [pscustomobject]@{ workflow_run_id = $(if ($path.Contains('/22/')) { 200 } else { 100 }); html_url = 'https://github.com/owner/repo/actions/runs/100' }
    }
    if ($path.Contains('/contents/')) {
        $text = if ($global:releaseMockScenario -eq 'missing' -and $path.Contains('build-macos')) { "on:`n  push:" } else { "run-name: Recover Windows `${{ inputs.tag }}`non:`n  workflow_dispatch:`n    inputs:`n      tag:`n      recovery:`n      source_sha:" }
        if ($global:releaseMockScenario -eq 'push') { $text += "`n  push:`n    tags:`n      - v*" }
        return [pscustomobject]@{ content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text)) }
    }
    if ($path.Contains('/actions/workflows/')) {
        return [pscustomobject]@{ id = $(if ($path.Contains('build-macos')) { 22 } else { 11 }); state = 'active' }
    }
    throw "Unexpected mock request $path"
}
function Start-Sleep { param($Seconds) }
try {
    & (Join-Path $PSScriptRoot '../Start-GitHubRelease.ps1') -InputJson
    $childExitCode = $LASTEXITCODE
} finally {
    [Console]::WriteLine((@{ kind = 'test_calls'; calls = @($global:releaseMockCalls.ToArray()) } | ConvertTo-Json -Depth 10 -Compress))
    Remove-Item Function:Invoke-RestMethod
    Remove-Item Function:Start-Sleep
    Remove-Variable releaseMockCalls,releaseMockScenario -Scope Global
}
exit $childExitCode
