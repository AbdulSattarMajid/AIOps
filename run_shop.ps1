$ErrorActionPreference = "Stop"

$secretsPath = Join-Path $PSScriptRoot ".streamlit\secrets.toml"
if (-not (Test-Path -LiteralPath $secretsPath)) {
    throw "Missing .streamlit/secrets.toml."
}

$apiKey = $null
foreach ($line in Get-Content -LiteralPath $secretsPath) {
    $match = [regex]::Match(
        $line,
        '^\s*AIOPS_DEMO_API_KEY\s*=\s*"([^"]*)"\s*(?:#.*)?$'
    )
    if ($match.Success) {
        $apiKey = $match.Groups[1].Value
        break
    }
}

if ([string]::IsNullOrWhiteSpace($apiKey)) {
    throw "AIOPS_DEMO_API_KEY is missing from .streamlit/secrets.toml."
}

$env:AIOPS_API_KEY = $apiKey
$env:AIOPS_API_URL = "http://127.0.0.1:8000"
Remove-Variable apiKey

uvicorn demo_shop.app:app --host 127.0.0.1 --port 9000
