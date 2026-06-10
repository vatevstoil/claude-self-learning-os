# Install Claude Self-Learning OS into ~/.claude
$dest = "$env:USERPROFILE\.claude"
New-Item -ItemType Directory -Force -Path "$dest\scripts","$dest\skills","$dest\logs" | Out-Null
Copy-Item -Recurse -Force scripts\* "$dest\scripts\"
Copy-Item -Recurse -Force skills\* "$dest\skills\"
if (Test-Path .env) { Copy-Item -Force .env "$dest\.env"; Write-Host "Copied .env -> $dest\.env" }
Write-Host "Installed scripts + skills to $dest"
Write-Host "Next: create your Pinecone index (1024-dim, multilingual-e5-large),"
Write-Host "      add config\wiki-map.example.json -> your vault as _shared\wiki-map.json,"
Write-Host "      and schedule automation_dispatcher.py (Task Scheduler). See docs\SYSTEM_GUIDE.md."
