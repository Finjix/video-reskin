# Windows PowerShell 5.1+; defaults to the skill-local runtime cache.
$ErrorActionPreference = 'Stop'
$skillRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$cacheRoot = if ($env:VIDEO_RESKIN_CACHE_DIR) { [IO.Path]::GetFullPath($env:VIDEO_RESKIN_CACHE_DIR) } else { Join-Path $skillRoot 'runtime-cache' }
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONUTF8 = '1'
$env:VIDEO_RESKIN_CACHE_DIR = $cacheRoot
$env:UV_CACHE_DIR = Join-Path $cacheRoot 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $cacheRoot 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $cacheRoot 'python-bin'
$uvDir = Join-Path $cacheRoot 'bootstrap\uv-0.13.4'
$uv = Join-Path $uvDir 'uv.exe'
$lockStream = $null
try {
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    [IO.Directory]::CreateDirectory((Join-Path $cacheRoot 'bootstrap')) | Out-Null
    $lockFile = Join-Path $cacheRoot 'bootstrap\install.lock'
    $deadline = [DateTime]::UtcNow.AddMinutes(15)
    while ($null -eq $lockStream) {
        try { $lockStream = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None') }
        catch [IO.IOException] { if ([DateTime]::UtcNow -gt $deadline) { throw 'Timed out waiting for runtime setup; retry the command.' }; Start-Sleep -Milliseconds 250 }
    }
    if (-not (Test-Path -LiteralPath $uv)) {
        if ($env:VIDEO_RESKIN_OFFLINE -eq '1') { throw 'uv is not cached; retry online.' }
        [Console]::Error.WriteLine('Downloading uv 0.13.4...')
        $archive = Join-Path $cacheRoot 'bootstrap\uv-0.13.4.zip.partial'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://releases.astral.sh/github/uv/releases/download/0.13.4/uv-x86_64-pc-windows-msvc.zip' -OutFile $archive
        $hasher = [Security.Cryptography.SHA256]::Create()
        $sourceStream = [IO.File]::OpenRead($archive)
        try { $digest = [BitConverter]::ToString($hasher.ComputeHash($sourceStream)).Replace('-', '').ToLowerInvariant() }
        finally { $sourceStream.Dispose(); $hasher.Dispose() }
        if ($digest -ne 'ccec7508a07d9933d7cb1261880633efcf1f30d6e6eddab21823f1dae6f23225') { throw 'uv checksum mismatch; retry the command.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $uvTemp = Join-Path $cacheRoot ('bootstrap\uv-extract-' + [Guid]::NewGuid().ToString('N'))
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $uvTemp)
        [IO.Directory]::CreateDirectory($uvDir) | Out-Null
        [IO.File]::Copy((Join-Path $uvTemp 'uv.exe'), $uv, $true)
        [IO.File]::Delete($archive)
    }
    $python = Join-Path $cacheRoot 'environment\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        [Console]::Error.WriteLine('Preparing private Python 3.13.15...')
        $managedPython = Join-Path $cacheRoot 'python\cpython-3.13.15-windows-x86_64-none\python.exe'
        if (-not (Test-Path -LiteralPath $managedPython)) {
            if ($env:VIDEO_RESKIN_OFFLINE -eq '1') { throw 'Python is not cached; retry online.' }
            & $uv python install 3.13.15 --no-bin --no-registry
        }
        # Use the exact installation even if uv cannot create an optional version junction.
        if (-not (Test-Path -LiteralPath $managedPython)) { throw 'Python download failed; retry the command.' }
        & $managedPython -I -c 'import sys; sys.exit(0 if sys.version_info[:3] == (3, 13, 15) and sys.maxsize > 2**32 else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'Downloaded Python failed verification; retry the command.' }
        & $uv venv --python $managedPython --no-python-downloads (Join-Path $cacheRoot 'environment')
        if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed; retry the command.' }
    }
    $lockStream.Dispose(); $lockStream = $null
    if ($args.Count -eq 1 -and $args[0] -eq '__bootstrap') { exit 0 }
    & $python -B (Join-Path $PSScriptRoot 'video_reskin.py') @args
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('Runtime error: ' + $_.Exception.Message)
    exit 3
} finally { if ($null -ne $lockStream) { $lockStream.Dispose() } }
