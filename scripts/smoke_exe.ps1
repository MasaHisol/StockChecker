# CI 用: ビルドした exe を起動し、画面が応答するか確認する
$dir = New-Item -ItemType Directory -Force -Path "$env:RUNNER_TEMP\sc"
Copy-Item dist\StockChecker.exe $dir
$p = Start-Process -FilePath "$dir\StockChecker.exe" -WorkingDirectory $dir -PassThru -RedirectStandardOutput "$dir\out.txt" -RedirectStandardError "$dir\err.txt"
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 2
  try { $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5000/ ; if ($r.StatusCode -eq 200) { $ok = $true; break } } catch {}
}
Get-Content "$dir\err.txt" -ErrorAction SilentlyContinue | Select-Object -Last 20
Stop-Process -Id $p.Id -Force
if (-not $ok) { throw "exe did not respond" }
Write-Host "exe smoke test OK"
