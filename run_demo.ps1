$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running. Start Docker Desktop and retry."
}

docker container inspect aiops-redis *> $null
if ($LASTEXITCODE -eq 0) {
    $running = docker inspect --format '{{.State.Running}}' aiops-redis
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the aiops-redis container state."
    }
    if ($running -ne "true") {
        docker start aiops-redis *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not start the aiops-redis container."
        }
    }
}

$quotedRoot = "'" + $repoRoot.Replace("'", "''") + "'"
$apiCommand = "Set-Location -LiteralPath $quotedRoot; `$root = (Get-Location).Path; `$env:PYTHONPATH = `"`$root;`$root\src`"; uvicorn aiops.api.main:app --app-dir src --host 127.0.0.1 --port 8000"
$workerCommand = "Set-Location -LiteralPath $quotedRoot; `$root = (Get-Location).Path; `$env:PYTHONPATH = `"`$root;`$root\src`"; python -m aiops.worker"
$targetCommand = "Set-Location -LiteralPath $quotedRoot; `$root = (Get-Location).Path; `$env:PYTHONPATH = `"`$root;`$root\src`"; streamlit run demo_ui/target_app.py --server.address 127.0.0.1 --server.port 8501"
$dashboardCommand = "Set-Location -LiteralPath $quotedRoot; `$root = (Get-Location).Path; `$env:PYTHONPATH = `"`$root;`$root\src`"; `$env:GITHUB_REPO = 'AbdulSattarMajid/aiops-test-target'; streamlit run demo_ui/dashboard.py --server.address 127.0.0.1 --server.port 8502"

Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoExit", "-Command", $apiCommand
)
Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoExit", "-Command", $workerCommand
)
Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoExit", "-Command", $targetCommand
)
Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoExit", "-Command", $dashboardCommand
)
