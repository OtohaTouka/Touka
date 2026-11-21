# Touka 本機小幫手：一行指令安裝（ADR-0329）
#
# 只從 GitHub 官方頁面（github.com/OtohaTouka/Touka 的 ToukaLocal/README.md）複製那一行指令。
# 這個檔由那一行的 sha256 鎖住；小幫手的檔案與 Python 由下面的 sha256 鎖住。
# 只用 Windows PowerShell 5.1 認得的語法。檔案是 UTF-8 含 BOM，5.1 才讀得懂中文。

param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [Parameter(Mandatory = $true)][string]$WorkDir,
    [string]$ToukaFolder = '',
    [switch]$Autostart,
    [switch]$NoAutostart
)

if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
    Write-Host '沒有安裝：這台電腦的 PowerShell 在受限語言模式，請看小幫手 README 的疑難排解。'
    exit 1
}
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
Add-Type -AssemblyName System.IO.Compression.FileSystem

# 釘住的 Python（官方安裝工具用的同一個 zip；禁令 69：上架滿 3 天才釘）
$PythonVersion = '3.14.10'
$PythonUrl = 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.zip'
$PythonSha256 = 'CC28026A650970CC1570BB127312F1F9634EB67ABFE157AE6CA1ACBBC627D091'
$PythonReleased = '2026-10-25'
$PythonPinned = '2026-11-22'

$RawBase = 'https://raw.githubusercontent.com/OtohaTouka/Touka'
$MutexName = 'Local\ToukaLocalHelper'
$MaxPath = 259

# 用 scripts/local_helper_pins.py 產生；測試核對它和 ToukaLocal 的檔案一致
$HelperFiles = [ordered]@{
    '__main__.py' = 'CA7BD011C238E816FF1340D2F2F550D0B647CDFBF36C7D72981381FCD0758AE3'
    'touka_local/__init__.py' = '5E8C3771513AD31BFBD54C83C126478E6F416772713D87EA63053E3CED1AA2E2'
    'touka_local/app.py' = '3AF0E8D6C7B957D400352EE68FCE6C48601DB196ACA9CA70ECB19B4484E4CDC7'
    'touka_local/approval.py' = '59AA42D9C8EE3BDFC621A592836966AEFCD4FD0239F34C5373F90323A5EEE2A8'
    'touka_local/connection.py' = 'A6C0D3C91AAE997C0B24A0D4F934091ABD22999C1D591225675AB579811DD278'
    'touka_local/display.py' = 'E55F95AA007E89887051A36B5ED8A62A8AD37E24E2BB670D3808004A83125206'
    'touka_local/signing.py' = 'E2A1CDCE95ED5A68ACE57F115160F58C958638DB8C872F947BA64E47B5E9E980'
    'touka_local/floors.py' = '2FD11280F38A1195208BF1D88AAA4976C6945920780FCD6AF48139553C72944C'
    'touka_local/errors.py' = '78927B5C61155A1C4B8823BF4E197F4C7CFFDBF3E34FD00058AD1D28E31B10C0'
    'touka_local/fsops.py' = '006244B5D4533C3631849D44C56550375D54388B2707E93A3D1B0395CBB2BA2B'
    'touka_local/grants.py' = '3DACE72BCF817F7B9D403BE27ACE1FE8EAB8ED03607AA60D20092AE3059BBE60'
    'touka_local/open_request.py' = '6647C0FB6A788C725C1370F79AA10DD2722B3F7B610E3EA68EC91362A30C2A5B'
    'touka_local/operations.py' = '254AB0B8E93296A304F3DA39D9372DC1E46DBE9716D8F260422B63FC2F6747B9'
    'touka_local/paths.py' = '8CB408212E2E9BF7F23A99E2005EA639E142C9B62141F5624445A048E6C8713C'
    'touka_local/protocol.py' = 'E82BC2CB20EA6A793AA432D65D9821D42B4C944FF670C8DCD76171F86C00BEB0'
    'touka_local/rules.py' = '0EBAFF63B711DA1D59F1B3C4901D1F5F43D5B6B1266A38620A190057177D7EA6'
    'touka_local/service.py' = '01F5E4BE1A70FF145F6094C31FDE664A8F482362EF587519F0A9CD1EE3A7429F'
    'touka_local/settings.py' = '7CB908082DD8DAA1D128F9EEFFB15F8B5888CA3266D7B4E46E94A7B23779453A'
    'touka_local/view.py' = '4E6B194F0C4E4CD7A68DF65331F230DBBDC72603022E0B62D427E7CEE8674FDE'
    'touka_local/winapi.py' = '9A80CEB6D9235AB3927BEA5F9E57E867C58EE70F8A25CE8FE9D8149F20D5759A'
    'install.py' = 'C6A8D8D96C38799A93BE77A4D0AE22AAE41E1B841F156CB4390D13BF21F1F7C8'
}

$script:Staging = ''
$script:CleanWorkDir = ''

function Get-FullPath([string]$Path) {
    return (Get-Item -LiteralPath $Path -Force).FullName.TrimEnd('\')
}

function Test-SamePath([string]$A, [string]$B) {
    return [string]::Equals($A, $B, [StringComparison]::OrdinalIgnoreCase)
}

function Test-Link($Item) {
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Remove-Folder([string]$Path, [string]$Parent) {
    # 只刪這次建的、上一層對、本身不是連結的資料夾；.NET 的遞迴刪除不跟著連結走
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (Test-Link $item) { return }
    if (-not (Test-SamePath (Get-FullPath $item.Parent.FullName) $Parent)) { return }
    [IO.Directory]::Delete($item.FullName, $true)
}

function Remove-Leftovers {
    if ($script:Staging) {
        try { Remove-Folder $script:Staging $script:HelperDir } catch { Write-Host "暫存資料夾刪不掉，留在 $($script:Staging)" }
    }
    if ($script:CleanWorkDir) {
        try { Remove-Folder $script:CleanWorkDir $script:Temp } catch { Write-Host "暫存資料夾刪不掉，留在 $($script:CleanWorkDir)" }
    }
}

function Stop-Install([string]$Message) {
    Write-Host "沒有安裝：$Message"
    Remove-Leftovers
    exit 1
}

function Assert-WorkDir {
    # 一行指令建的暫存資料夾：上一層是 TEMP、名稱固定、不是連結、只有這個檔
    $script:Temp = Get-FullPath $env:TEMP
    $item = Get-Item -LiteralPath $WorkDir -Force
    if (-not $item.PSIsContainer) { Stop-Install '暫存資料夾不對' }
    if (Test-Link $item) { Stop-Install '暫存資料夾不能是連結' }
    if (-not (Test-SamePath (Get-FullPath $item.Parent.FullName) $script:Temp)) { Stop-Install '暫存資料夾不在 TEMP 裡' }
    if ($item.Name -cnotmatch '^touka-helper-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') { Stop-Install '暫存資料夾的名稱不對' }
    $full = Get-FullPath $item.FullName
    if (-not (Test-SamePath (Get-FullPath $PSCommandPath) (Get-FullPath (Join-Path $full 'install.ps1')))) { Stop-Install '安裝腳本不在暫存資料夾裡' }
    $names = @(Get-ChildItem -LiteralPath $full -Force | ForEach-Object { $_.Name })
    if ($names.Count -ne 1 -or $names[0] -ne 'install.ps1') { Stop-Install '暫存資料夾裡有別的檔案' }
    $script:CleanWorkDir = $full
}

function Test-HelperRunning {
    try {
        $mutex = [Threading.Mutex]::OpenExisting($MutexName)
        $mutex.Dispose()
        return $true
    } catch [Threading.WaitHandleCannotBeOpenedException] {
        return $false
    } catch [UnauthorizedAccessException] {
        return $true
    }
}

function Get-Verified([string]$Url, [string]$Target, [string]$Sha256) {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Target
    $actual = (Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash
    if ($actual -ne $Sha256) {
        Remove-Item -LiteralPath $Target -Force
        Stop-Install "下載的檔案和預期不符：$Url"
    }
}

if ($Commit -cnotmatch '^[0-9a-f]{40}$') { Stop-Install 'commit 的格式不對' }
Assert-WorkDir

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { Stop-Install '請用一般的 PowerShell 執行，不要用系統管理員' }
$arch = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
if ($arch -ne 'AMD64') { Stop-Install '小幫手目前只支援 64 位元的 x64 電腦' }
if (Test-HelperRunning) { Stop-Install '小幫手正在執行，請先關掉小幫手再貼一次' }

$script:HelperDir = Join-Path $env:LOCALAPPDATA 'ToukaLocal'
New-Item -ItemType Directory -Force -Path $script:HelperDir | Out-Null
$script:HelperDir = Get-FullPath $script:HelperDir
$script:Staging = Join-Path $script:HelperDir ('staging-' + [guid]::NewGuid())
$code = 1
# finally 在 Ctrl+C 與 exit 時也會跑，暫存不會留下
try {
New-Item -ItemType Directory -Path $script:Staging | Out-Null
try {
    Write-Host "下載 Python $PythonVersion（約 33 MB）……"
    $zip = Join-Path $script:Staging 'python.zip'
    Get-Verified $PythonUrl $zip $PythonSha256
    $stagedPython = Join-Path $script:Staging 'python'
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $longest = ($archive.Entries | ForEach-Object { $_.FullName.Length } | Measure-Object -Maximum).Maximum
    } finally {
        $archive.Dispose()
    }
    if (($stagedPython.Length + 1 + $longest) -gt $MaxPath) { Stop-Install '使用者資料夾的路徑太長，解不開 Python' }
    [IO.Compression.ZipFile]::ExtractToDirectory($zip, $stagedPython)
    Remove-Item -LiteralPath $zip -Force
    & (Join-Path $stagedPython 'python.exe') -I -S -c "import tkinter; r = tkinter.Tk(); r.withdraw(); print('Tk', r.tk.call('info', 'patchlevel')); r.destroy()"
    if ($LASTEXITCODE -ne 0) { Stop-Install 'Python 的自我檢查沒有通過（視窗元件載入不了）' }

    Write-Host '下載小幫手……'
    $source = Join-Path $script:Staging 'src'
    foreach ($relative in $HelperFiles.Keys) {
        $target = Join-Path $source ($relative -replace '/', '\')
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Get-Verified "$RawBase/$Commit/ToukaLocal/$relative" $target $HelperFiles[$relative]
    }

    $python = Join-Path $script:HelperDir "python-$PythonVersion"
    $old = "$python.old-" + [guid]::NewGuid()
    $hadOld = Test-Path -LiteralPath $python
    if ($hadOld) {
        try { Rename-Item -LiteralPath $python -NewName (Split-Path -Leaf $old) } catch { Stop-Install "舊的 Python 正在使用，換不掉：$python" }
    }
    try {
        Move-Item -LiteralPath $stagedPython -Destination $python
    } catch {
        if ($hadOld) { Rename-Item -LiteralPath $old -NewName (Split-Path -Leaf $python) }
        Stop-Install "Python 搬不進去：$python"
    }
    if ($hadOld) {
        try { Remove-Folder $old $script:HelperDir } catch { Write-Host "舊的 Python 刪不掉，留在 $old" }
    }

    $arguments = @('-I', '-S', (Join-Path $source 'install.py'))
    if ($ToukaFolder) {
        $folder = $ToukaFolder
        if ($folder.Length -gt 3) { $folder = $folder.TrimEnd('\') }
        $arguments += @('--touka-folder', $folder)
    }
    if ($Autostart) { $arguments += '--autostart' } elseif ($NoAutostart) { $arguments += '--no-autostart' }
    & (Join-Path $python 'python.exe') @arguments
    $code = $LASTEXITCODE
} catch {
    Stop-Install $_.Exception.Message
}
} finally {
    Remove-Leftovers
}
exit $code
