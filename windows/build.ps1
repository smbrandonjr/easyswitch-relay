<#
  Builds EasySwitch Relay for Windows.

  Run from anywhere:
      powershell -ExecutionPolicy Bypass -File windows\build.ps1

  Needs Python 3.10-3.14 on THIS machine only (python.org installer is fine).
  The result runs on any 64-bit Windows 10/11 PC without Python.

  Output:
      dist\EasySwitchRelay-windows-x64\        the app plus install/uninstall scripts
      dist\EasySwitchRelay-windows-x64.zip     the same, zipped, to copy to other PCs
#>
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Find-Python {
    $candidates = @(
        @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.14'), @('py', '-3.11'), @('py', '-3.10'),
        @('python')
    )
    foreach ($c in $candidates) {
        $exe = $c[0]
        $pre = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -eq 0 -and "$v" -match '^3\.(1[0-4])$') { return , $c }
    }
    throw "Python 3.10-3.14 was not found. Install it from https://www.python.org/downloads/windows/ (only needed for building), then run this again."
}

$py = Find-Python
$pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
Write-Host "Using Python: $(& $pyExe @pyArgs -c 'import sys; print(sys.version)')"

# Fresh, isolated build environment every time.
if (Test-Path .build-venv) { Remove-Item -Recurse -Force .build-venv }
& $pyExe @pyArgs -m venv .build-venv
$vpy = Join-Path $Root '.build-venv\Scripts\python.exe'

# Hash-locked, binary-only install: every file must match a sha256 in
# requirements-build.txt, and no package may run build code during install.
& $vpy -m pip install --disable-pip-version-check --require-hashes --only-binary ':all:' -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed (a hash mismatch here means a download was altered)." }

& $vpy -m PyInstaller --noconfirm --clean --distpath build\dist --workpath build\work easyswitch-relay.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

# Assemble the distributable folder.
$out = Join-Path $Root 'dist\EasySwitchRelay-windows-x64'
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force -Path $out | Out-Null
Copy-Item -Recurse -Path 'build\dist\easyswitch-relay\*' -Destination $out
Copy-Item 'windows\install.ps1', 'windows\uninstall.ps1', 'README.md' -Destination $out

$zip = "$out.zip"
if (Test-Path $zip) { Remove-Item -Force $zip }
Compress-Archive -Path "$out\*" -DestinationPath $zip
$hash = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
Set-Content -Path "$zip.sha256" -Value "$hash  $(Split-Path -Leaf $zip)"

Write-Host ""
Write-Host "Built:  $zip"
Write-Host "SHA256: $hash"
Write-Host ""
Write-Host "Install on this PC:  powershell -ExecutionPolicy Bypass -File `"$out\install.ps1`""
