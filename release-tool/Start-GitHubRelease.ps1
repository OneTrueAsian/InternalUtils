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
    [string]$Repository = '',
    [string]$Ref,
    [string]$Tag,
    [string]$WindowsWorkflow = 'release-windows.yml',
    [string]$MacWorkflow = 'build-macos.yml',
    [string]$TagInput = 'tag',
    [ValidateSet("both", "windows_recovery", "windows_sharded", "windows_serial_draft", "macos_draft")][string]$BuildMode = "both",
    [Security.SecureString]$Token
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$headers = @{}
$secretPointer = [IntPtr]::Zero
$exitCode = 0
$IncludeMacos = $false
$SourceShaInput = ''
$ReleaseIdInput = 0

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
        $detail = ''
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try {
                $apiError = $_.ErrorDetails.Message | ConvertFrom-Json
                $detail = [string]$apiError.message
                if ($apiError.PSObject.Properties['errors']) {
                    $detail += ' ' + ($apiError.errors | ConvertTo-Json -Depth 5 -Compress)
                }
                $secret = $headers.Authorization.Substring(7)
                $detail = $detail.Replace($secret, '[redacted]') -replace '[\r\n]', ' '
                if ($detail.Length -gt 1000) { $detail = $detail.Substring(0, 1000) }
                $detail = " GitHub: $detail"
            } catch { $detail = '' }
        }
        switch ($status) {
            401 { throw "GitHub rejected the token. Check its value and expiration.$detail" }
            403 { throw "GitHub denied access. Check token permissions, organization authorization and rate limits.$detail" }
            404 {
                if (-not $Path) {
                    throw "GitHub cannot access repository $Repository (HTTP 404). Check the repository name, token resource owner and selected repositories. Private repositories also return 404 when the token lacks access.$detail"
                }
                throw "GitHub could not find $resource (HTTP 404). Check the branch or workflow and token access.$detail"
            }
            422 { throw "GitHub rejected $resource. Check the tag, workflow dispatch inputs and selected ref.$detail" }
            default { throw "GitHub request failed (HTTP $status). Check connectivity or try again from the Actions page.$detail" }
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

# The macOS workflow must join the existing draft by release id, keep it a draft and run the release check (Vault Spend's build-macos.yml after VS-20).
# A workflow that creates or publishes its own release is never dispatched against a draft.
function Test-MacosDraftContract {
    param([string]$Yaml)
    return ($Yaml -match '(?m)^\s+releaseDraft:\s*true\s*$') -and ($Yaml -notmatch '(?m)^\s+releaseDraft:\s*false\s*$') -and
        ($Yaml -match '(?m)^\s+releaseId:\s*\S') -and ($Yaml -match 'assert-macos-release\.mjs')
}

function Get-TagReleases {
    param([string]$Name)
    $found = @()
    for ($page = 1; $page -le 100; $page++) {
        $batch = @(Invoke-GitHub "releases?per_page=100&page=$page")
        $found += @($batch | Where-Object { $_.tag_name -eq $Name })
        if ($batch.Count -lt 100) { return $found }
    }
    throw 'The release listing exceeded the verification limit; inspect the draft manually. Nothing was dispatched.'
}

# Adds macOS to the finished Windows draft: only after tag, release id, Windows' signed set and the workflow contract all check out, and never over existing macOS files.
function Invoke-MacosDraftPhase {
    if ($SourceShaInput -notmatch '^[0-9a-f]{40}$') { throw 'macOS needs the exact source commit of the finished Windows draft.' }
    if (-not $ReleaseIdInput -or $ReleaseIdInput -le 0) { throw 'macOS needs the id of the Windows draft it will join.' }
    if ($Tag -notmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') { throw 'The macOS draft step requires a production vX.Y.Z tag.' }
    if (-not $TagInput -or $TagInput -notmatch '^[A-Za-z_][A-Za-z0-9_-]*$') { throw 'The macOS draft step requires the workflow tag input name.' }
    $file = $MacWorkflow.Trim()
    if ($file -notmatch '^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml$') { throw 'Enter workflow filenames such as build-macos.yml.' }
    $ref = Invoke-GitHub "git/ref/tags/$([Uri]::EscapeDataString($Tag))" -AllowMissing
    if (-not $ref) { throw "Tag $Tag does not exist. macOS never creates a tag." }
    if ((Get-CommitForTag $ref.object) -ne $SourceShaInput) { throw "Tag $Tag no longer points at the Windows draft's source commit. Tags are never moved; nothing was dispatched." }
    $matching = @(Get-TagReleases $Tag)
    if ($matching.Count -ne 1) { throw "Expected exactly one release for $Tag, found $($matching.Count). The tool will not choose between them. Nothing was dispatched." }
    $release = $matching[0]
    if ($release.draft -ne $true) { throw "The $Tag release is already published, not a draft. macOS is not added by this mode." }
    if ($release.prerelease -ne $false) { throw "The $Tag draft is marked prerelease; this mode adds macOS to production drafts only." }
    if ([int64]$release.id -ne [int64]$ReleaseIdInput) { throw "The $Tag release is a different release (id $($release.id)) from the Windows draft (id $ReleaseIdInput); not substituting it." }
    $assets = @($release.assets)
    $names = @($assets | ForEach-Object { [string]$_.name })
    if (($names | Select-Object -Unique).Count -ne $names.Count) { throw 'The draft has duplicate file names. Nothing was dispatched.' }
    function Test-Nonempty { param([string]$Name) $asset = $assets | Where-Object { $_.name -eq $Name } | Select-Object -First 1; return ($asset -and $asset.size -gt 0) }
    $installers = @($names | Where-Object { $_ -match '\.(exe|msi)$' })
    $missing = @()
    if (-not ($installers | Where-Object { $_ -match '\.exe$' }) -or -not ($installers | Where-Object { $_ -match '\.msi$' })) { $missing += 'Windows EXE and MSI installers' }
    foreach ($installer in $installers) { foreach ($suffix in @('', '.manifest.json', '.signature.json')) { if (-not (Test-Nonempty ($installer + $suffix))) { $missing += ($installer + $suffix) } } }
    if ($missing.Count) { throw "Windows' signed set is not complete in the draft; missing: $($missing -join ', '). Finish the Windows draft, including its signing approval, first. Nothing was dispatched." }
    $macNames = @($names | Where-Object { $_ -match '\.dmg$|\.dmg\.manifest\.json$|\.dmg\.signature\.json$|\.app\.tar\.gz$' })
    if ($macNames.Count) {
        $dmgs = @($names | Where-Object { $_ -match '\.dmg$' })
        if ($dmgs.Count -eq 1 -and (Test-Nonempty $dmgs[0]) -and (Test-Nonempty "$($dmgs[0]).manifest.json") -and (Test-Nonempty "$($dmgs[0]).signature.json")) {
            Send-Event 'log' "macOS signed files are already in the $Tag draft; macOS packaging is not requested again and nothing is replaced."
            return
        }
        throw "macOS files are only partly in the $Tag draft ($($macNames -join ', ')). The tool never deletes or replaces files: inspect the macOS run, remove the partial macOS files yourself if you decide to, then run this mode again. Nothing was dispatched."
    }
    $metadata = Invoke-GitHub "actions/workflows/$file"
    if ($metadata.state -ne 'active') { throw "Workflow $file is disabled." }
    $source = Invoke-GitHub "contents/.github/workflows/$file`?ref=$SourceShaInput"
    $yamlText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($source.content))
    if ($yamlText -notmatch '(?m)^\s*workflow_dispatch\s*:' -or $yamlText -match '(?m)^  push\s*:') { throw "Workflow $file must be manual-only workflow_dispatch." }
    if (-not (Test-MacosDraftContract $yamlText)) { throw "Workflow $file at the release commit does not follow the draft policy (join the existing draft by release id, keep it a draft, run the release check); it is not dispatched." }
    $runs = Invoke-GitHub "actions/workflows/$($metadata.id)/runs?head_sha=$SourceShaInput&per_page=100"
    $same = @($runs.workflow_runs | Where-Object { $_.head_branch -eq $Tag -and $_.head_sha -eq $SourceShaInput -and $_.event -eq 'workflow_dispatch' })
    $baseline = @($runs.workflow_runs | ForEach-Object { $_.id })
    $active = @($same | Where-Object { $_.status -ne 'completed' })
    if ($active.Count) {
        $adopt = $active | Sort-Object { [int64]$_.id } -Descending | Select-Object -First 1
        Send-Event 'link' "${file}: a run is already active for this tag and commit; tracking it instead of starting another" $adopt.html_url
        [Console]::WriteLine((@{ kind = 'run'; workflow = $file; run_id = $adopt.id; head_sha = $SourceShaInput; tag = $Tag; run_ref = $Tag; run_title = ''; source_sha = $SourceShaInput;
            url = $adopt.html_url; requested_at = ''; previous_run_ids = @($baseline) } | ConvertTo-Json -Compress))
        return
    }
    if (@($same | Where-Object { $_.conclusion -eq 'success' }).Count) { throw "${file}: a macOS run for this tag and commit already completed successfully, but the draft holds no macOS files. It is not run again automatically: open that run to see what happened." }
    if ($same.Count) { Send-Event 'log' "WARNING: ${file} previously failed for $Tag at $SourceShaInput. Starting it again builds the SAME commit and refuses to replace any file already in the draft." }
    $requestedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $result = Invoke-GitHub "actions/workflows/$($metadata.id)/dispatches" 'POST' @{ ref = $Tag; inputs = @{ $TagInput = $Tag } }
    $runUrl = "https://github.com/$Repository/actions/workflows/$file"
    if ($result -and $result.PSObject.Properties['html_url']) { $runUrl = $result.html_url }
    $runId = $null
    if ($result -and $result.PSObject.Properties['workflow_run_id']) { $runId = $result.workflow_run_id }
    Send-Event 'link' "${file}: macOS build requested for the existing draft" $runUrl
    [Console]::WriteLine((@{ kind = 'run'; workflow = $file; run_id = $runId; head_sha = $SourceShaInput; tag = $Tag; run_ref = $Tag; run_title = ''; source_sha = $SourceShaInput;
        url = $runUrl; requested_at = $requestedAt; previous_run_ids = @($baseline) } | ConvertTo-Json -Compress))
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
        if ($payload.PSObject.Properties["build_mode"]) { $BuildMode = [string]$payload.build_mode }
        if ($payload.PSObject.Properties["include_macos"]) { $IncludeMacos = [bool]$payload.include_macos }
        if ($payload.PSObject.Properties["source_sha"] -and $payload.source_sha) { $SourceShaInput = [string]$payload.source_sha }
        if ($payload.PSObject.Properties["release_id"] -and $payload.release_id) { $ReleaseIdInput = [int64]$payload.release_id }
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
    if ($BuildMode -notin @("both", "windows_recovery", "windows_sharded", "windows_serial_draft", "macos_draft")) { throw "Invalid build mode." }
    $recovery = $BuildMode -eq "windows_recovery"
    $draftMode = $BuildMode -in @("windows_sharded", "windows_serial_draft")
    if ($IncludeMacos -and -not $draftMode) { throw 'macOS can only follow a Windows draft mode.' }
    $windowsOnly = $recovery -or ($draftMode -and -not $IncludeMacos)
    if ($draftMode -and ($Tag -notmatch '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or $Tag -match '^v1\.3\.9[0-9]$' -or $TagInput -ne 'tag')) {
        throw 'Draft mode requires a production vX.Y.Z tag and the tag input; v1.3.90 to v1.3.99 are reserved tests.'
    }
    if ($Repository -ieq 'OneTrueAsian/vault-spend' -and $Tag -match '^v0*1\.0*3\.0*9[0-9]$') { throw "$Tag is a reserved test version (v1.3.90 to v1.3.99) for $Repository and cannot be released by this tool." }
    if ($BuildMode -eq 'macos_draft') {
        # The second phase of a Windows + macOS draft: no branch, no tag creation, no Windows dispatch. It either dispatches macOS once or stops.
        Invoke-MacosDraftPhase
        exit 0
    }
    if ($recovery -and -not $TagInput) { throw "Recovery requires a tag input name." }
    $files = @(if ($windowsOnly) { $WindowsWorkflow.Trim() } else { $WindowsWorkflow.Trim(); $MacWorkflow.Trim() })
    if ($files.Count -eq 2 -and $files[0] -eq $files[1]) { throw 'Windows and macOS workflows must be different files.' }
    foreach ($file in $files) {
        if ($file -notmatch '^[A-Za-z0-9_-][A-Za-z0-9_.-]*\.ya?ml$') { throw 'Enter workflow filenames such as build-macos.yml.' }
    }
    Send-Event 'log' "Checking remote branch $Ref..."
    $branch = Invoke-GitHub ('branches/' + [Uri]::EscapeDataString($Ref))
    $sha = $branch.commit.sha
    if ($draftMode -and $sha -notmatch '^[0-9a-f]{40}$') { throw 'Draft mode requires an exact 40-character source commit.' }
    $encodedTag = [Uri]::EscapeDataString($Tag)
    $existingTag = Invoke-GitHub "git/ref/tags/$encodedTag" -AllowMissing
    $sourceSha = $sha
    $runRef = $Tag
    $runTitle = ''
    if ($recovery) {
        if (-not $existingTag) { throw 'Windows recovery requires an existing tag; no tag will be created.' }
        $sourceSha = Get-CommitForTag $existingTag.object
        $runRef = $Ref
        $runTitle = "Recover Windows $Tag"
    }
    if (-not $recovery -and $existingTag -and (Get-CommitForTag $existingTag.object) -ne $sha) {
        throw 'This tag already points to a different commit. Select its original branch/commit or a new tag; tags are never moved.'
    }

    # Verify both workflows before making any remote changes.
    $workflows = @()
    $windowsCreatesDraft = $false
    foreach ($file in $files) {
        $metadata = Invoke-GitHub "actions/workflows/$file"
        if ($metadata.state -ne 'active') { throw "Workflow $file is disabled." }
        $null = Invoke-GitHub "contents/.github/workflows/$file"
        $source = Invoke-GitHub "contents/.github/workflows/$file`?ref=$sha"
        $yamlText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($source.content))
        if ($yamlText -notmatch '(?m)^\s*workflow_dispatch\s*:') {
            throw "Workflow $file must declare workflow_dispatch in block YAML format."
        }
        if ($yamlText -match '(?m)^  push\s*:') {
            throw "Workflow $file declares a push trigger. Use manual-only workflow_dispatch for release publishing to prevent duplicate builds. Update both the default and release branches, then use a new tag."
        }
        if ($recovery -and ($yamlText -notmatch '(?m)^\s+recovery\s*:' -or $yamlText -notmatch '(?m)^\s+source_sha\s*:')) {
            throw "Workflow $file must support recovery and source_sha inputs on the workflow branch."
        }
        if ($recovery -and $yamlText -notmatch '(?m)^run-name:[^\r\n]*Recover Windows[^\r\n]*inputs\.tag') {
            throw "Recovery workflow $file must name runs Recover Windows <tag> using inputs.tag."
        }
        if ($file -eq $WindowsWorkflow.Trim()) { $windowsCreatesDraft = $yamlText -match '(?m)^\s+releaseDraft:\s*true\s*$' }
        $isMacFile = $file -eq $MacWorkflow.Trim() -and -not $windowsOnly
        if ($draftMode -and -not $isMacFile) {
            foreach ($inputName in @('tag', 'sharded', 'source_sha', 'diagnostic_only', 'test_release', 'recovery')) {
                if ($yamlText -notmatch ("(?m)^      " + $inputName + ":\s*$")) { throw "Workflow $file does not support the draft release input $inputName." }
            }
            if (-not $windowsCreatesDraft -or $yamlText -match '(?m)^\s+releaseDraft:\s*false\s*$') { throw 'Draft mode requires a draft-only packaging workflow; no tag was created.' }
        }
        if ($IncludeMacos -and $isMacFile -and -not (Test-MacosDraftContract $yamlText)) {
            throw "Workflow $file does not follow the draft policy (join the existing draft by release id, keep it a draft, run the release check). It would not be safe to dispatch after Windows; no tag was created."
        }
        if (-not $windowsOnly -and -not $IncludeMacos -and $isMacFile -and $windowsCreatesDraft) {
            throw 'Windows now prepares a draft. Do not dispatch macOS alongside it: macOS must wait until Windows has finished and signed. Choose Windows + macOS: sharded draft (or serial draft), which adds macOS to the same draft afterwards, or a Windows-only draft mode.'
        }
        # With macOS following, its workflow is only validated here; it is dispatched later, by the macOS phase.
        if (-not ($IncludeMacos -and $isMacFile)) { $workflows += @{ file = $file; id = $metadata.id } }
    }
    Send-Event 'log' "Target: $Repository / $Ref / $Tag / $sha"
    if ($draftMode) {
        $macosNote = if ($IncludeMacos) { "macOS will be added to the same draft only after Windows has finished and signed; it is not dispatched now." } else { "macOS is not requested." }
        Send-Event 'log' "Windows draft mode: $BuildMode, exact source $sourceSha. $macosNote Approve release-signing on GitHub when prompted; publication remains a separate owner action."
    }
    if ($recovery) {
        Send-Event "log" "Windows-only recovery: workflow branch $Ref; existing tag $Tag at $sourceSha. macOS is not requested; the tag is preserved."
    } elseif ($existingTag) {
        Send-Event 'log' 'Using the existing tag at the same commit.'
    } else {
        $null = Invoke-GitHub 'git/refs' 'POST' @{ ref = "refs/tags/$Tag"; sha = $sha }
        Send-Event 'log' 'Created release tag. Checking for existing workflow runs...'
    }

    function Send-Run {
        param($Workflow, $RunId, $Url, $RequestedAt)
        [Console]::WriteLine((@{ kind = 'run'; workflow = $Workflow; run_id = $RunId;
            head_sha = $sha; tag = $Tag; run_ref = $runRef; run_title = $runTitle; source_sha = $sourceSha; url = $Url; requested_at = $RequestedAt; previous_run_ids = @($runs.workflow_runs | ForEach-Object { $_.id }) } | ConvertTo-Json -Compress))
    }
    $failed = 0
    $tracked = 0
    foreach ($workflow in $workflows) {
        try {
            $runs = Invoke-GitHub "actions/workflows/$($workflow.id)/runs?head_sha=$sha&per_page=100"
            $matching = @($runs.workflow_runs | Where-Object {
                $_.head_branch -eq $runRef -and $_.head_sha -eq $sha -and
                (-not $recovery -or ($_.PSObject.Properties["display_title"] -and $_.display_title -eq $runTitle)) -and
                $_.event -in @('push', 'workflow_dispatch') -and
                ($_.status -ne 'completed' -or $_.conclusion -eq 'success')
            })
            if ($draftMode -and @($matching | Where-Object { $_.status -ne 'completed' }).Count) {
                throw 'A workflow run is already active for this tag and commit. Inspect it before requesting another draft build.'
            }
            if ($matching.Count -and -not $draftMode) {
                Send-Event 'link' "$($workflow.file): already started or completed successfully" $matching[0].html_url
                $tracked++
                Send-Run $workflow.file $matching[0].id $matching[0].html_url ''
                continue
            }
            $previousFailures = @($runs.workflow_runs | Where-Object {
                $_.head_branch -eq $runRef -and $_.head_sha -eq $sha -and
                (-not $recovery -or ($_.PSObject.Properties["display_title"] -and $_.display_title -eq $runTitle)) -and
                $_.status -eq 'completed' -and $_.conclusion -ne 'success'
            })
            if ($previousFailures.Count) {
                Send-Event 'log' "WARNING: $($workflow.file) previously failed for $Tag at $sha. Retrying builds the SAME commit; application code is unchanged. For code fixes use a new version/tag; Windows recovery can repair legacy test driver paths."
            }
            $body = @{ ref = $runRef }
            if ($TagInput) { $body.inputs = @{ $TagInput = $Tag } }
            if ($recovery) { $body.inputs.recovery = $true; $body.inputs.source_sha = $sourceSha }
            if ($draftMode) {
                $body.inputs.sharded = $BuildMode -eq 'windows_sharded'
                $body.inputs.source_sha = $sourceSha
                $body.inputs.recovery = $false
                $body.inputs.diagnostic_only = $false
                $body.inputs.test_release = $false
            }
            $requestedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
            $result = Invoke-GitHub "actions/workflows/$($workflow.id)/dispatches" 'POST' $body
            $runUrl = "https://github.com/$Repository/actions/workflows/$($workflow.file)"
            if ($result -and $result.PSObject.Properties['html_url']) { $runUrl = $result.html_url }
            Send-Event 'link' "$($workflow.file): build requested" $runUrl
            $runId = $null
            if ($result -and $result.PSObject.Properties['workflow_run_id']) { $runId = $result.workflow_run_id }
            $tracked++
            Send-Run $workflow.file $runId $runUrl $requestedAt
        } catch {
            $failed++
            Send-Event 'error' "$($workflow.file): $($_.Exception.Message) Tag retained. Retry to start any missing workflow."
        }
    }
    if ($tracked -gt 0) { Send-Event 'link' $(if ($draftMode) { 'Draft release: review after all checks and signing finish (not published)' } else { 'Release page (assets appear after the workflows finish)' }) "https://github.com/$Repository/releases/tag/$encodedTag" }
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
