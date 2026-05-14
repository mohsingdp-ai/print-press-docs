# create-worktree.ps1
# Creates a `feat/docling-integration` worktree as a sibling of this repo
# and moves DOCLING_INTEGRATION_PLAN.md into it as the first commit.
#
# Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File .\create-worktree.ps1

$ErrorActionPreference = "Stop"

$repo  = (Get-Location).Path
$wt    = Join-Path (Split-Path $repo -Parent) "print-press-equals-docs-docling"
$plan  = Join-Path $repo "DOCLING_INTEGRATION_PLAN.md"

if (-not (Test-Path $plan)) {
    Write-Error "DOCLING_INTEGRATION_PLAN.md not found in $repo. Aborting."
}

Write-Host "Creating worktree at: $wt"
git worktree add -b feat/docling-integration $wt

# Move the plan into the new worktree and commit it there.
Move-Item -Path $plan -Destination (Join-Path $wt "DOCLING_INTEGRATION_PLAN.md")

Push-Location $wt
try {
    git add DOCLING_INTEGRATION_PLAN.md
    git commit -m "Add Docling integration plan (no code yet)"
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Worktree ready:"
Write-Host "  $wt"
Write-Host ""
Write-Host "  cd $wt"
Write-Host "  git status"
Write-Host ""
Write-Host "To remove later:"
Write-Host "  git worktree remove $wt"
