# Compile and run Skeletons.cs with 32-bit PowerShell on a machine with ArcMap 10.8:  build.ps1 ROOT
# ROOT holds Skeletons.cs and the base layer files from make_base.py; the layer files land in ROOT\gen.
param([string]$root)
$gac = 'C:\Windows\Microsoft.NET\assembly\GAC_MSIL'
$refs = @()
foreach ($n in @('Carto', 'Display', 'Geodatabase', 'Geometry', 'System', 'DataSourcesFile', 'Version')) {
  $refs += "/r:" + (Join-Path $gac "ESRI.ArcGIS.$n\v4.0_10.8.0.0__8fc3cc631e44ad86\ESRI.ArcGIS.$n.dll")
}
$stdole = Get-ChildItem 'C:\Windows\assembly\GAC\stdole' -Recurse -Filter stdole.dll | Select-Object -First 1
$out = Join-Path $root 'Skeletons.exe'
& 'C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe' /nologo /platform:x86 "/r:$($stdole.FullName)" "/out:$out" @refs (Join-Path $root 'Skeletons.cs') 2>&1 | ForEach-Object { Write-Output $_ }
if ($LASTEXITCODE -eq 0) { & $out $root 2>&1 | ForEach-Object { Write-Output $_ } }
