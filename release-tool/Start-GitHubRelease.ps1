#Requires -Version 5.1
<#
.SYNOPSIS
Creates or verifies a remote release tag and starts Windows and macOS workflows.
.DESCRIPTION
Use -InputJson for the UI's single-line JSON request on stdin. The token is never
passed as a command-line argument. Without -InputJson, prompts are interactive.
#>
[CmdletBinding()]
param(
    [switch]$InputJson,
    [string]$Repository = 'OneTrueAsian/vault-spend',
    [string]$Ref,
    [string]$Tag,
    [string]$WindowsWorkflow = 'release-windows.yml',
    [string]$MacWorkflow = 'build-macos.yml',
    [string]$TagInput = 'tag',
    [Security.SecureString]$Token
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{}
$secretPointer = [IntPtr]::Zero
$exitCode = 0

function Send-Event {
    param([string]$Kind, [string]$Message, [string]$Url = '')
    [Console]::WriteLine((@{ kind = $Kind; message = $Message; url = $Url } | ConvertTo-Json -Compress))
}

function Invoke-GitHub {
    param([string]$Path, [string]$Method = 'GET', $Body = $null, [switch]$AllowMissing)
    $apiUri = "https://api.github.com/repos/$Repository"
    if ($Path) { $apiUri += "/$Path" }
    $resource = if ($Path) { "$Repository/$Path" } else { "repository $Repository" }
    $request = @{
        Uri = $apiUri
        Headers = $headers; Method = $Method; TimeoutSec = 45; ErrorAction = 'Stop'
    }
    if ($null -ne $Body) {
        $request.ContentType = 'application/json'
        $request.Body = ConvertTo-Json -InputObject $Body -Depth 10 -Compress
    }
    try { Invoke-RestMethod @request } catch {
        $property = $_.Exception.PSObject.Properties['Response']
        $status = 0
        if ($property -and $property.Value) { $status = [int]$property.Value.StatusCode }
        if ($status -eq 404 -and $AllowMissing) { return $null }
        switch ($status) {
            401 { throw 'GitHub rejected the token. Check its value and expiration.' }
            403 { throw 'GitHub denied access. Check token permissions, organization authorization and rate limits.' }
            404 {
                if (-not $Path) {
                    throw "GitHub cannot access repository $Repository (HTTP 404). Check the repository name, token resource owner and selected repositories. Private repositories also return 404 when the token lacks access."
                }
                throw "GitHub could not find $resource (HTTP 404). Check the branch or workflow and token access."
            }
            422 { throw "GitHub rejected $resource. Check the tag, workflow dispatch inputs and selected ref." }
            default { throw "GitHub request failed (HTTP $status). Check connectivity or try again from the Actions page." }
        }
    }
}

function Get-CommitForTag {
    param($Object)
    for ($depth = 0; $depth -lt 8; $depth++) {
        if ($Object.type -eq 'commit') { return $Object.sha }
        if ($Object.type -ne 'tag') { throw 'Release tag does not point to a commit.' }
        $Object = (Invoke-GitHub "git/tags/$($Object.sha)").object
    }
    throw 'Release tag has too many nested annotated tags.'
}

try {
    if ($InputJson) {
        $payload = [Console]::ReadLine() | ConvertFrom-Json
        $Repository = [string]$payload.repository
        $Ref = [string]$payload.ref
        $Tag = [string]$payload.tag
        $WindowsWorkflow = [string]$payload.windows_workflow
        $MacWorkflow = [string]$payload.mac_workflow
        $TagInput = [string]$payload.tag_input
        $Token = ConvertTo-SecureString ([string]$payload.token) -AsPlainText -Force
        $payload = $null
    }
    if (-not $Token) { $Token = Read-Host 'GitHub token' -AsSecureString }
    $Repository = $Repository.Trim()
    if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') { throw 'Repository must use owner/repo format.' }
    $secretPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Token)
    $headers = @{
        Authorization = 'Bearer ' + [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
        Accept = 'application/vnd.github+json'
        'X-GitHub-Api-Version' = '2026-03-10'
        'User-Agent' = 'InternalUtils-Release-Tool'
    }
    Send-Event 'log' "Checking repository $Repository..."
    $repo = Invoke-GitHub ''
    if (-not $Ref) {
        if ($InputJson) { $Ref = $repo.default_branch } else {
            $answer = Read-Host "Remote branch [$($repo.default_branch)]"
            $Ref = if ($answer) { $answer.Trim() } else { $repo.default_branch }
        }
    }
    if (-not $Tag -and -not $InputJson) { $Tag = Read-Host 'Release tag' }
    $Tag = $Tag.Trim()
    if (-not $Tag -or $Tag.StartsWith('-') -or $Tag -match '[\s~^:?*\[\\\x00-\x20\x7f]' -or
        $Tag.Contains('..') -or $Tag.Contains('@{') -or $Tag -eq '@' -or
        $Tag.StartsWith('/') -or $Tag.EndsWith('/') -or $Tag.EndsWith('.') -or $Tag.Contains('//')) {
        throw 'Invalid Git release tag.'
    }
    foreach ($part in $Tag.Split('/')) {
        if ($part.StartsWith('.') -or $part.EndsWith('.lock')) { throw 'Invalid Git release tag component.' }
    }
    if ($TagInput -and $TagInput -notmatch '^[A-Za-z_][A-Za-z0-9_-]*$') { throw 'Invalid workflow input name.' }
    $files = @($WindowsWorkflow.Trim(), $MacWorkflow.Trim())
    if ($files[0] -eq $files[1]) { throw 'Windows and macOS workflows must be different files.' }
    foreach ($file in $files) {
        if ($file -notmatch '^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml$') { throw 'Enter workflow filenames such as build-macos.yml.' }
    }
    Send-Event 'log' "Checking remote branch $Ref..."
    $branch = Invoke-GitHub ('branches/' + [Uri]::EscapeDataString($Ref))
    $sha = $branch.commit.sha
    $encodedTag = [Uri]::EscapeDataString($Tag)
    $existingTag = Invoke-GitHub "git/ref/tags/$encodedTag" -AllowMissing
    if ($existingTag -and (Get-CommitForTag $existingTag.object) -ne $sha) {
        throw 'This tag already points to a different commit. Select its original branch/commit or a new tag; tags are never moved.'
    }

    # Verify both workflows before making any remote changes.
    $workflows = @()
    foreach ($file in $files) {
        $metadata = Invoke-GitHub "actions/workflows/$file"
        if ($metadata.state -ne 'active') { throw "Workflow $file is disabled." }
        $null = Invoke-GitHub "contents/.github/workflows/$file"
        $source = Invoke-GitHub "contents/.github/workflows/$file`?ref=$sha"
        $yamlText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($source.content))
        if ($yamlText -notmatch '(?m)^\s*workflow_dispatch\s*:') {
            throw "Workflow $file must declare workflow_dispatch in block YAML format."
        }
        $workflows += @{ file = $file; id = $metadata.id }
    }
    Send-Event 'log' "Target: $Repository / $Ref / $Tag / $sha"
    if ($existingTag) {
        Send-Event 'log' 'Using the existing tag at the same commit.'
    } else {
        $null = Invoke-GitHub 'git/refs' 'POST' @{ ref = "refs/tags/$Tag"; sha = $sha }
        Send-Event 'log' 'Created release tag. Checking for workflows already triggered by the tag...'
        Start-Sleep -Seconds 3
    }

    function Send-Run {
        param($Workflow, $RunId, $Url, $RequestedAt)
        [Console]::WriteLine((@{ kind = 'run'; workflow = $Workflow; run_id = $RunId;
            head_sha = $sha; tag = $Tag; url = $Url; requested_at = $RequestedAt } | ConvertTo-Json -Compress))
    }
    $failed = 0
    foreach ($workflow in $workflows) {
        try {
            $runs = Invoke-GitHub "actions/workflows/$($workflow.id)/runs?head_sha=$sha&per_page=100"
            $matching = @($runs.workflow_runs | Where-Object {
                $_.head_branch -eq $Tag -and $_.head_sha -eq $sha -and
                $_.event -in @('push', 'workflow_dispatch') -and
                ($_.status -ne 'completed' -or $_.conclusion -eq 'success')
            })
            if ($matching.Count) {
                Send-Event 'link' "$($workflow.file): already started or completed successfully" $matching[0].html_url
                Send-Run $workflow.file $matching[0].id $matching[0].html_url ''
                continue
            }
            $body = @{ ref = $Tag }
            if ($TagInput) { $body.inputs = @{ $TagInput = $Tag } }
            $requestedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
            $result = Invoke-GitHub "actions/workflows/$($workflow.id)/dispatches" 'POST' $body
            $runUrl = "https://github.com/$Repository/actions/workflows/$($workflow.file)"
            if ($result -and $result.PSObject.Properties['html_url']) { $runUrl = $result.html_url }
            Send-Event 'link' "$($workflow.file): build requested" $runUrl
            $runId = $null
            if ($result -and $result.PSObject.Properties['workflow_run_id']) { $runId = $result.workflow_run_id }
            Send-Run $workflow.file $runId $runUrl $requestedAt
        } catch {
            $failed++
            Send-Event 'error' "$($workflow.file): $($_.Exception.Message) Tag retained. Retry to start any missing workflow."
        }
    }
    Send-Event 'link' 'Release page (assets appear after the workflows publish them)' "https://github.com/$Repository/releases/tag/$encodedTag"
    if ($failed) { throw "$failed workflow request(s) failed. Other workflow requests may have succeeded." }
    Send-Event 'log' 'Workflow dispatch finished. Build completion is not yet confirmed.'
} catch {
    Send-Event 'error' $_.Exception.Message
    $exitCode = 1
} finally {
    $headers.Clear()
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    $Token = $null
}
exit $exitCode
