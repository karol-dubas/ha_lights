# Stop Settings app to release registry locks
Get-Process SystemSettings -ErrorAction SilentlyContinue | Stop-Process -Force

# Define base CloudStore path
$basePath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\CloudStore\Store"

# Find and remove all registry keys containing 'bluelightreduction'
Get-ChildItem -Path $basePath -Recurse -ErrorAction SilentlyContinue | 
Where-Object { $_.Name -match "bluelightreduction" } | 
Remove-Item -Recurse -Force

# Restart Explorer to apply changes
Stop-Process -Name explorer -Force
Start-Process explorer