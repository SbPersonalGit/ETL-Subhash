<#
.SYNOPSIS
Promotes Git changes through the Waygate branch chain.

.DESCRIPTION
The promotion order is:
  SF_to_SF -> Puneet_Dev
  Puneet_Dev -> BH-Waygate-Migration
  BH-Waygate-Migration -> Saurabh_dev
  BH-Waygate-Migration -> Anurag_dev

By default the script prints the plan without changing anything. Pass -Execute
to fetch, merge, and push. The script requires a clean worktree and stops on
the first conflict or failed command.

.EXAMPLE
pwsh -File scripts/sync_branches.ps1

.EXAMPLE
pwsh -File scripts/sync_branches.ps1 -Execute
#>

[CmdletBinding()]
param(
    [switch]$Execute,
    [string]$Remote = "origin"
)

$ErrorActionPreference = "Stop"

$promotions = @(
    @{ Source = "SF_to_SF"; Target = "Puneet_Dev" },
    @{ Source = "Puneet_Dev"; Target = "BH-Waygate-Migration" },
    @{ Source = "BH-Waygate-Migration"; Target = "Saurabh_dev" },
    @{ Source = "BH-Waygate-Migration"; Target = "Anurag_dev" }
)

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Test-GitRef {
    param([string]$Ref)

    & git show-ref --verify --quiet $Ref
    return $LASTEXITCODE -eq 0
}

function Switch-ToTargetBranch {
    param([string]$Branch)

    if (Test-GitRef "refs/heads/$Branch") {
        Invoke-Git switch $Branch
        return
    }
    Invoke-Git switch --track -c $Branch "$Remote/$Branch"
}

Invoke-Git rev-parse --is-inside-work-tree | Out-Null
$repositoryRoot = (& git rev-parse --show-toplevel).Trim()
$originalBranch = (& git branch --show-current).Trim()
$dirty = & git status --porcelain --untracked-files=normal
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the Git worktree."
}
if ($dirty -and $Execute) {
    throw "The worktree is not clean. Commit or stash changes before syncing branches."
}
if (-not $originalBranch) {
    throw "The repository is in detached HEAD state. Switch to a branch first."
}

Write-Host "Repository: $repositoryRoot"
Write-Host "Remote:     $Remote"
Write-Host "Branch promotion plan:"
foreach ($promotion in $promotions) {
    Write-Host "  $($promotion.Source) -> $($promotion.Target)"
}

if (-not $Execute) {
    Write-Host "Dry run only. Re-run with -Execute to fetch, merge, and push."
    exit 0
}

$mergeInProgress = $false
try {
    Invoke-Git fetch --prune $Remote

    $requiredBranches = $promotions.Source + $promotions.Target | Sort-Object -Unique
    foreach ($branch in $requiredBranches) {
        if (-not (Test-GitRef "refs/remotes/$Remote/$branch")) {
            throw "Remote branch '$Remote/$branch' does not exist. Check its exact spelling or create it first."
        }
    }

    foreach ($promotion in $promotions) {
        $source = $promotion.Source
        $target = $promotion.Target
        Write-Host ""
        Write-Host "Promoting $source -> $target"

        Switch-ToTargetBranch $target
        Invoke-Git pull --ff-only $Remote $target

        $mergeInProgress = $true
        & git merge --no-edit "$Remote/$source"
        if ($LASTEXITCODE -ne 0) {
            throw "Merge conflict while promoting '$source' into '$target'. The merge will be aborted."
        }
        $mergeInProgress = $false

        Invoke-Git push $Remote $target
    }

    Write-Host ""
    Write-Host "All branch promotions completed successfully."
}
finally {
    if ($mergeInProgress -and (Test-Path -LiteralPath (Join-Path $repositoryRoot ".git\MERGE_HEAD"))) {
        & git merge --abort
    }
    if ((& git branch --show-current).Trim() -ne $originalBranch) {
        & git switch $originalBranch
    }
}
