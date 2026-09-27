Param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,
    [string]$Region = "europe-west2",
    [string]$Service = "dietary-insight-partner-api",
    [int]$MaxInstances = 3,
    [int]$DailyAiCallCap = 500
)

$ErrorActionPreference = "Stop"

function Invoke-GCloud {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud command failed with exit code $LASTEXITCODE"
    }
}

function Get-SecretValue {
    param(
        [string]$Name
    )

    $envValue = (Get-Item -Path "Env:$Name" -ErrorAction SilentlyContinue).Value
    if ($envValue) {
        return $envValue
    }

    return Read-Host -Prompt "Paste value for $Name"
}

Set-Location (Join-Path $PSScriptRoot "..")

Invoke-GCloud { gcloud config set project $ProjectId | Out-Null }

Write-Host "==> Enabling APIs"
Invoke-GCloud { gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com | Out-Null }

$runtimeSaName = "partner-api-runtime"
$runtimeSa = "$runtimeSaName@$ProjectId.iam.gserviceaccount.com"

Write-Host "==> Runtime service account (least privilege)"
$saExists = $true
try {
    Invoke-GCloud { gcloud iam service-accounts describe $runtimeSa | Out-Null }
} catch {
    $saExists = $false
}
if (-not $saExists) {
    Invoke-GCloud { gcloud iam service-accounts create $runtimeSaName --display-name "Partner API runtime" | Out-Null }
}

Write-Host "==> Secrets (stored in $Region)"
$secretNames = @("MONGO_URI", "GEMINI_API_KEY")
foreach ($secretName in $secretNames) {
    $secretExists = $true
    try {
        Invoke-GCloud { gcloud secrets describe $secretName | Out-Null }
    } catch {
        $secretExists = $false
    }

    if (-not $secretExists) {
        $value = Get-SecretValue -Name $secretName
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Secret $secretName cannot be empty."
        }

        $tmpFile = [System.IO.Path]::GetTempFileName()
        Set-Content -Path $tmpFile -Value $value -NoNewline
        Invoke-GCloud { gcloud secrets create $secretName --replication-policy user-managed --locations $Region --data-file $tmpFile | Out-Null }
        Remove-Item $tmpFile -Force
    }

    Invoke-GCloud { gcloud secrets add-iam-policy-binding $secretName --member "serviceAccount:$runtimeSa" --role "roles/secretmanager.secretAccessor" | Out-Null }
}

Write-Host "==> Building and deploying $Service"
Invoke-GCloud { gcloud run deploy $Service `
  --source . `
  --region $Region `
  --service-account $runtimeSa `
  --allow-unauthenticated `
  --min-instances 0 `
  --max-instances $MaxInstances `
  --concurrency 40 `
  --cpu 1 `
  --memory 512Mi `
  --timeout 90 `
  --set-env-vars "ENV=production,DAILY_AI_CALL_CAP=$DailyAiCallCap" `
    --set-secrets "MONGO_URI=MONGO_URI:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest" | Out-Null }

$url = (& gcloud run services describe $Service --region $Region --format "value(status.url)").Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($url)) {
        throw "Deployment finished without a valid service URL."
}
Write-Host ""
Write-Host "Deployed: $url"
Write-Host "Health check: curl $url/healthz"
