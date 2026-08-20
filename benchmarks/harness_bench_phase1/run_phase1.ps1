param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "dotclaw-qwen37plus", "nanobot-qwen37plus",
        "dotclaw-luna", "nanobot-luna"
    )]
    [string]$Harness,
    [Parameter(Mandatory = $true)]
    [string]$HarnessBenchRepo,
    [Parameter(Mandatory = $true)]
    [string]$AppConfig,
    [Parameter(Mandatory = $true)]
    [string]$HarnessConfig,
    [string]$Model,
    [string]$Endpoint,
    [string]$RubricModel,
    [string[]]$Tasks,
    [string]$TaskCsv
)

$ErrorActionPreference = "Stop"

$isLuna = $Harness.EndsWith("-luna")
$apiKeyVariable = if ($isLuna) { "JOJOCODE_API_KEY" } else { "QWEN_API_KEY" }
$apiKey = [Environment]::GetEnvironmentVariable($apiKeyVariable)
if (-not $apiKey) {
    throw "$apiKeyVariable 未设置"
}

if (-not $Model) {
    $Model = if ($isLuna) { "gpt-5.6-luna" } else { "qwen3.7-plus-2026-05-26" }
}
if (-not $Endpoint) {
    $Endpoint = if ($isLuna) {
        "https://max2.jojocode.com/v1"
    } else {
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    }
}
if (-not $RubricModel) {
    $RubricModel = if ($isLuna) { "gpt-5.6-sol" } else { $Model }
}

# 固定官方 Phase 1 的 26 题，不接受运行时静默删题。
$phase1Tasks = @(
    "001-file", "002-exec", "003-browser", "006-access-bilibili",
    "008-image-recognize", "013-image-edit", "020-archive-checksum",
    "021-batch-rename-transform", "022-local-rest-api-summary",
    "023-web-form-extraction", "077-archive-manifest-defense",
    "078-local-api-cursor-retry-ledger", "079-smallfile-batch-reject-ledger",
    "080-schema-roundtrip-conversion", "081-local-html-dom-form-extract",
    "007-session-memory", "014-task-decomposition", "057-interruption-resume",
    "058-multiday-project-state", "059-event-update-replan",
    "060-task-cancellation-cleanup", "061-periodic-status-rollup",
    "103-policy-update-replan-diff", "104-async-ops-window-rollup",
    "105-partial-batch-resume-ledger", "106-release-approval-gate-plan"
)

# 允许从中断点只重跑缺失或基础设施失败的题，避免重复消耗。
if ($TaskCsv) {
    $Tasks = @($TaskCsv.Split(",", [System.StringSplitOptions]::RemoveEmptyEntries))
}
if (-not $Tasks -or $Tasks.Count -eq 0) {
    $Tasks = $phase1Tasks
}
$unknownTasks = @($Tasks | Where-Object { $_ -notin $phase1Tasks })
if ($unknownTasks.Count -gt 0) {
    throw "包含非 Phase 1 题目: $($unknownTasks -join ', ')"
}

$env:PYTHONPATH = Join-Path $HarnessBenchRepo "src"
$env:HARNESSBENCH_APP_CONFIG = (Resolve-Path -LiteralPath $AppConfig).Path
$env:HARNESSBENCH_HARNESS_CONFIG = (Resolve-Path -LiteralPath $HarnessConfig).Path
$env:HARNESSBENCH_PUBLIC_URL_TEMPLATE = "{local_url}"
$env:RUBRIC_API_KEY = $apiKey
$env:RUBRIC_BASE_URL = $Endpoint
$env:RUBRIC_MODEL = $RubricModel
$env:HARNESSBENCH_RUBRIC_STREAM = "1"
$env:HARNESSBENCH_RUBRIC_MAX_TOKENS = "4096"

foreach ($task in $tasks) {
    Write-Host "FORMAL $Harness START $task"
    & .\.venv\Scripts\python.exe -m harnessbench.cli run-task --task $task --harness $Harness --mode live
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "$task 的 Harness-Bench 命令退出码为 $LASTEXITCODE；不得从正式结果中删除该题。"
    }
    Write-Host "FORMAL $Harness END $task"
}
