param(
    [string]$Root = "."
)

$ErrorActionPreference = "Stop"
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$Utf8Strict = [System.Text.UTF8Encoding]::new($false, $true)
$ExcludedDirs = @(
    ".git", ".runtime", ".venv", "logs", "vendor", "__pycache__",
    "node_modules", "build", ".gradle", "data", "scratch"
)
$Extensions = @(
    ".py", ".ps1", ".cmd", ".js", ".css", ".html", ".md", ".yml", ".yaml",
    ".json", ".txt", ".sh", ".env", ".example", ".gitignore",
    ".gitattributes", ".editorconfig"
)

$badFiles = New-Object System.Collections.Generic.List[string]

Get-ChildItem -LiteralPath $Root -Recurse -File | ForEach-Object {
    $file = $_
    foreach ($dir in $ExcludedDirs) {
        if ($file.FullName -like "*\$dir\*") {
            return
        }
    }

    $name = $file.Name
    $ext = $file.Extension
    if (($Extensions -notcontains $ext) -and ($Extensions -notcontains $name)) {
        return
    }

    try {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            $badFiles.Add("$($file.FullName) (UTF-8 BOM is not allowed)")
            return
        }
        [void]$Utf8Strict.GetString($bytes)
    }
    catch {
        $badFiles.Add($file.FullName)
    }
}

if ($badFiles.Count -gt 0) {
    Write-Host "Invalid UTF-8 files:"
    $badFiles | ForEach-Object { Write-Host $_ }
    exit 1
}

Write-Host "UTF-8 check passed."
