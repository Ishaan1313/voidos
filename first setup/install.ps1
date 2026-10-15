<#
══════════════════════════════════════════════════════════════════════
 Void OS installer — Windows 10 / 11          Linux & macOS: install.sh

   Double-click install.cmd                (easiest), or in PowerShell:
   .\install.ps1                           interactive
   .\install.ps1 -Yes                      accept all defaults
   .\install.ps1 -Help                     every option

 Safe to re-run: updates the Python packages, keeps your .env and data.
 Needs administrator rights: started normally, it asks Windows for them
 (one UAC prompt) and carries on in an elevated window. Void OS installs
 for the account that approves the prompt.
══════════════════════════════════════════════════════════════════════
#>
[CmdletBinding()]
param(
  [switch]$Yes,
  [int]$Port = 0,
  [string]$FileRoot = '',
  [string]$With = '',
  [string]$Without = '',
  [switch]$NoAutostart,
  [switch]$NoStart,
  [switch]$Uninstall,
  [switch]$Help,
  # internal: the elevated half (firewall, Defender, Developer Mode, NFS)
  [switch]$AdminTasks,
  [string]$AdminList = '',
  # internal: set when this run is the elevated re-launch of the installer
  [switch]$Elevated,
  [string]$OriginalUser = ''
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Windows PowerShell's progress bar makes downloads ~10x slower
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
# Windows PowerShell 5.1 defaults to TLS 1.0, which python.org and GitHub refuse
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$SetupDir = Split-Path -Parent $MyInvocation.MyCommand.Path     # <app>\first setup
$Root    = Split-Path -Parent $SetupDir                          # <app>
$Log     = Join-Path $Root 'install.log'
$Venv    = Join-Path $Root '.venv'
$VPy     = Join-Path $Venv 'Scripts\python.exe'
$Helper  = Join-Path $SetupDir 'helper.py'
$TmpDir  = Join-Path $Root 'tmp'          # never %TEMP% — Defender scans it hardest (see .env.example)
$PyWant  = '3.12'
$PyFull  = '3.12.10'                      # python.org fallback when winget isn't available
$MinPy   = '3.10'
$AppName = 'Void OS'

function Show-Help {
@"
Void OS installer for Windows

  .\install.ps1 [options]       (or double-click install.cmd)

  -Yes                 Don't ask questions; use defaults (or the options below)
  -Port N              Web port (default 7777)
  -FileRoot DIR        Folder shown in the Files tab (default %USERPROFILE%\voidos-files)
  -With LIST           Also install/configure these features (comma-separated)
  -Without LIST        Skip these features
  -NoAutostart         Don't start Void OS when you sign in
  -NoStart             Don't start Void OS at the end
  -Uninstall           Remove shortcuts, autostart and the Python environment
                       (keeps .env, your files and all Void OS data)

  Features: tunnel    Cloudflare Tunnel - reach Void OS from anywhere   (winget)
            sync      rclone - continuous Dropbox/Google Drive sync     (winget)
            docker    Docker Desktop - App Store & Containers           (winget, large)
            defender  Exclude Void OS's tmp folder from Defender scans  (admin)
            firewall  Allow other devices on your network to connect    (admin)
            devmode   Developer Mode - needed to mount SMB shares       (admin)
            nfs       Windows NFS client - mount NFS shares             (admin, Pro+)
"@ | Write-Host
}
if ($Help) { Show-Help; exit 0 }

# ── look ───────────────────────────────────────────────────────────────
# NB: PowerShell variable names are case-insensitive and dynamically scoped,
# so these get long, distinctive names — a local $p or $e inside any function
# would otherwise silently replace a colour code or the escape character.
$VT = $false
try { $VT = [bool]$Host.UI.SupportsVirtualTerminal } catch { }
if ($env:WT_SESSION) { $VT = $true }
if ($env:NO_COLOR) { $VT = $false }
$Esc = [char]27
function C([int]$n) { if ($VT) { "$Esc[38;5;${n}m" } else { '' } }
$cReset  = if ($VT) { "$Esc[0m" } else { '' }
$cBold = if ($VT) { "$Esc[1m" } else { '' }
$cDim = if ($VT) { "$Esc[2m" } else { '' }
$cPurple = C 141; $cPurple2 = C 99; $cGreen = C 114; $cYellow = C 221; $cRed = C 203; $cCyan = C 117
$Glyph = @{ ok = [char]0x2713; no = [char]0x2717; dot = [char]0x2022 }
$Frames = if ($VT) { @([char]0x280B,[char]0x2819,[char]0x2839,[char]0x2838,[char]0x283C,[char]0x2834,[char]0x2826,[char]0x2827,[char]0x2807,[char]0x280F) } else { @('|','/','-','\') }
$script:Warnings = New-Object System.Collections.Generic.List[string]
$script:StepN = 0; $StepsTotal = 7

function Out-Line([string]$text, [ConsoleColor]$fallback = 'Gray') {
  if ($VT) { Write-Host $text } else { Write-Host ($text -replace "$Esc\[[0-9;]*m", '') -ForegroundColor $fallback }
}
function Log([string]$t) { try { Add-Content -LiteralPath $Log -Value $t -Encoding UTF8 } catch { } }
function Step([string]$t) {
  $script:StepN++
  Write-Host ''
  Out-Line "$cPurple$cBold[$($script:StepN)/$StepsTotal]$cReset $cBold$t$cReset" 'Magenta'
  Log "=== [$($script:StepN)/$StepsTotal] $t"
}
function Ok([string]$t)   { Out-Line "  $cGreen$($Glyph.ok)$cReset $t" 'Green'; Log "  ok: $t" }
function Info([string]$t) { Out-Line "  $cDim$($Glyph.dot)$cReset $t" 'Gray'; Log "  info: $t" }
function Warn([string]$t) { Out-Line "  $cYellow!$cReset $t" 'Yellow'; Log "  WARN: $t"; $script:Warnings.Add($t) }
function Fail([string]$t) {
  Write-Host ''
  Out-Line "  $cRed$($Glyph.no)$cReset $cBold$t$cReset" 'Red'
  Log "  FAIL: $t"
  Out-Line "  ${cDim}Full details: $Log$cReset" 'DarkGray'
  Pause-IfDoubleClicked; exit 1
}
function Pause-IfDoubleClicked { if (($env:VOIDOS_LAUNCHED_BY_CMD -eq '1' -or $Elevated) -and -not $Yes) { Write-Host ''; Read-Host '  Press Enter to close' | Out-Null } }

function Show-Banner {
  Write-Host ''
  $art = @(
    '██╗   ██╗ ██████╗ ██╗██████╗      ██████╗ ███████╗',
    '██║   ██║██╔═══██╗██║██╔══██╗    ██╔═══██╗██╔════╝',
    '██║   ██║██║   ██║██║██║  ██║    ██║   ██║███████╗',
    '╚██╗ ██╔╝██║   ██║██║██║  ██║    ██║   ██║╚════██║',
    ' ╚████╔╝ ╚██████╔╝██║██████╔╝    ╚██████╔╝███████║',
    '  ╚═══╝   ╚═════╝ ╚═╝╚═════╝      ╚═════╝ ╚══════╝'
  )
  $shades = @(93, 99, 105, 141, 147, 183)
  $fallback = @('DarkMagenta','DarkMagenta','Magenta','Magenta','Magenta','White')
  for ($i = 0; $i -lt $art.Count; $i++) {
    if ($VT) { Write-Host ("  " + (C $shades[$i]) + $art[$i] + $cReset) } else { Write-Host ("  " + $art[$i]) -ForegroundColor $fallback[$i] }
  }
  Out-Line "  ${cDim}Self-hosted home server  $($Glyph.dot)  installer for Windows$cReset" 'DarkGray'
}

# Windows command-line quoting for one argument (Start-Process doesn't quote,
# so a path with a space would otherwise split into two arguments).
function Quote-Arg([string]$a) {
  if ($a -eq '') { return '""' }
  if ($a -notmatch '[\s"]') { return $a }
  $q = $a -replace '(\\*)"', '$1$1\"'
  $q = $q -replace '(\\+)$', '$1$1'
  return '"' + $q + '"'
}

# Runs a program with a spinner; its output goes to install.log, and the
# last lines are shown only if it fails. Returns the exit code.
function Invoke-Step([string]$Message, [string]$File, [string[]]$Arguments = @()) {
  New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
  $id = [Guid]::NewGuid().ToString('N').Substring(0, 8)
  $outFile = Join-Path $TmpDir "installer-$id.out"; $errFile = Join-Path $TmpDir "installer-$id.err"
  $argLine = ($Arguments | ForEach-Object { Quote-Arg $_ }) -join ' '
  Log "`n> $File $argLine"
  try {
    $proc = Start-Process -FilePath $File -ArgumentList $argLine -NoNewWindow -PassThru `
         -RedirectStandardOutput $outFile -RedirectStandardError $errFile -WorkingDirectory $Root
  } catch {
    Out-Line "  $cRed$($Glyph.no)$cReset $Message ${cDim}(couldn't start $File)$cReset" 'Red'; Log "  $_"; return 9009
  }
  $null = $proc.Handle   # Windows PowerShell quirk: without this, ExitCode can come back empty
  $i = 0
  while (-not $proc.HasExited) {
    Write-Host -NoNewline ("`r  " + $cPurple + $Frames[$i % $Frames.Count] + $cReset + ' ' + $Message)
    Start-Sleep -Milliseconds 100; $i++
  }
  $proc.WaitForExit()
  $rc = $proc.ExitCode; if ($null -eq $rc) { $rc = 0 }
  $w = $Message.Length + 6
  try { $cw = [Console]::WindowWidth; if ($cw -gt 1) { $w = [Math]::Min($cw - 1, $w) } } catch { }
  Write-Host -NoNewline ("`r" + (' ' * $w) + "`r")
  foreach ($f in @($outFile, $errFile)) { if (Test-Path $f) { Get-Content -LiteralPath $f -ErrorAction SilentlyContinue | Add-Content -LiteralPath $Log -Encoding UTF8; Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue } }
  if ($rc -eq 0) { Ok $Message } else {
    Out-Line "  $cRed$($Glyph.no)$cReset $Message ${cDim}(exit $rc)$cReset" 'Red'
    Get-Content -LiteralPath $Log -Tail 12 | ForEach-Object { Out-Line "      $cDim$_$cReset" 'DarkGray' }
  }
  return $rc
}

function Ask-YesNo([string]$q, [bool]$default) {
  if ($Yes) { return $default }
  $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
  $a = Read-Host "  ${cCyan}?$cReset $q $cDim$hint$cReset"
  if ([string]::IsNullOrWhiteSpace($a)) { return $default }
  return $a.Trim() -match '^[Yy]'
}
function Ask-Value([string]$q, [string]$default) {
  if ($Yes) { return $default }
  $a = Read-Host "  ${cCyan}?$cReset $q $cDim[$default]$cReset"
  if ([string]::IsNullOrWhiteSpace($a)) { return $default }
  return $a.Trim().Trim('"')
}

function Test-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Has-Command([string]$n) { return [bool](Get-Command $n -ErrorAction SilentlyContinue) }
# Runs a native program and returns its stdout. Under ErrorActionPreference
# 'Stop', Windows PowerShell turns ANY stderr line from a native program (even
# a harmless warning) into a script-ending error — so relax it locally and
# send stderr to the log instead.
function Invoke-Native([string]$exe, [string[]]$arguments) {
  $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  try {
    $out = & $exe @arguments 2>&1 | ForEach-Object {
      if ($_ -is [System.Management.Automation.ErrorRecord]) { Log "  stderr: $_"; $null } else { $_ }
    }
    return (($out | Where-Object { $null -ne $_ }) | Out-String).Trim()
  } finally { $ErrorActionPreference = $old }
}
function Refresh-Path {
  $m = [Environment]::GetEnvironmentVariable('Path', 'Machine'); $u = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = ($m, $u | Where-Object { $_ }) -join ';'
}

# ══════════════════════════════════════════════════════════════════════
# Administrator rights are required: relaunch this script elevated (one UAC prompt)
# ══════════════════════════════════════════════════════════════════════
if (-not $AdminTasks -and -not (Test-Admin)) {
  $selfPath = $MyInvocation.MyCommand.Path
  $relaunch = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Quote-Arg $selfPath), '-Elevated',
                '-OriginalUser', (Quote-Arg $env:USERNAME))
  foreach ($k in $PSBoundParameters.Keys) {
    $v = $PSBoundParameters[$k]
    if ($v -is [System.Management.Automation.SwitchParameter]) { if ($v.IsPresent) { $relaunch += "-$k" } }
    else { $relaunch += "-$k"; $relaunch += (Quote-Arg ([string]$v)) }
  }
  Write-Host ''
  Write-Host '  Void OS needs administrator rights to install.'
  Write-Host '  Windows will ask you to allow it; the installer continues in a new window.'
  Write-Host ''
  try {
    $proc = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList ($relaunch -join ' ')
    exit $proc.ExitCode
  } catch {
    Write-Host '  Administrator access was declined, or isn''t available for this account.' -ForegroundColor Red
    Write-Host '  Right-click install.cmd and choose "Run as administrator", then try again.' -ForegroundColor Red
    Write-Host ''
    Pause-IfDoubleClicked
    exit 1
  }
}

# ══════════════════════════════════════════════════════════════════════
# Elevated half — runs in a second, administrator PowerShell window
# ══════════════════════════════════════════════════════════════════════
if ($AdminTasks) {
  Show-Banner
  Out-Line "  ${cBold}Administrator steps$cReset" 'White'
  $tasks = $AdminList.Split(',') | Where-Object { $_ }
  foreach ($t in $tasks) {
    try {
      switch ($t) {
        'defender' {
          Add-MpPreference -ExclusionPath $TmpDir
          Ok "Windows Defender won't scan $TmpDir (keeps uploads and zips fast)"
        }
        'firewall' {
          Get-NetFirewallRule -DisplayName $AppName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
          New-NetFirewallRule -DisplayName $AppName -Description 'Void OS web interface' -Direction Inbound -Protocol TCP `
            -LocalPort $Port -Action Allow -Profile Private,Domain | Out-Null
          Ok "Firewall: devices on private networks can reach port $Port"
        }
        'firewall-remove' {
          Get-NetFirewallRule -DisplayName $AppName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
          Ok 'Removed the firewall rule'
        }
        'devmode' {
          $k = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock'
          if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
          Set-ItemProperty -Path $k -Name AllowDevelopmentWithoutDevLicense -Value 1 -Type DWord
          Ok 'Developer Mode on (lets Void OS link SMB shares into Files)'
        }
        'nfs' {
          $f = Get-WindowsOptionalFeature -Online -FeatureName ServicesForNFS-ClientOnly -ErrorAction SilentlyContinue
          if ($null -eq $f) { Warn 'NFS client is only available on Windows Pro, Enterprise and Education' }
          else {
            Enable-WindowsOptionalFeature -Online -FeatureName ServicesForNFS-ClientOnly, ClientForNFS-Infrastructure -All -NoRestart | Out-Null
            Ok 'NFS client installed (a restart may be needed)'
          }
        }
      }
    } catch { Warn "$t failed: $($_.Exception.Message)" }
  }
  Start-Sleep -Seconds 2
  exit 0
}

# ══════════════════════════════════════════════════════════════════════
Show-Banner
Log ''; Log ("---- install.ps1 " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " ----")
if (-not (Test-Path (Join-Path $Root 'voidos.py'))) { Fail "voidos.py isn't in the folder above this script. Keep install.cmd inside the Void OS 'first setup' folder." }

$StartupDir  = [Environment]::GetFolderPath('Startup')
$StartMenu   = Join-Path ([Environment]::GetFolderPath('Programs')) $AppName
$DesktopUrl  = Join-Path ([Environment]::GetFolderPath('Desktop')) "$AppName.url"
$StartupLnk  = Join-Path $StartupDir "$AppName.lnk"

function Stop-RunningVoidOS {
  # A running Void OS keeps its packages' .pyd files locked, which makes pip
  # fail; stop it (only the copy that lives in this folder) before updating.
  $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*voidos.py*' -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) })
  foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  return $procs.Count
}

# ── uninstall ──────────────────────────────────────────────────────────
if ($Uninstall) {
  Out-Line "  ${cBold}Uninstalling Void OS$cReset ${cDim}(your files, .env and Void OS data are kept)$cReset" 'White'
  Write-Host ''
  $n = Stop-RunningVoidOS; if ($n) { Ok "Stopped Void OS" }
  foreach ($f in @($StartupLnk, $DesktopUrl)) { if (Test-Path $f) { Remove-Item -LiteralPath $f -Force; Ok "Removed $(Split-Path $f -Leaf)" } }
  if (Test-Path $StartMenu) { Remove-Item -LiteralPath $StartMenu -Recurse -Force; Ok 'Removed Start menu entries' }
  if (Get-NetFirewallRule -DisplayName $AppName -ErrorAction SilentlyContinue) {
    try { Start-Process powershell -Verb RunAs -Wait -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File ' + (Quote-Arg $MyInvocation.MyCommand.Path) + ' -AdminTasks -AdminList firewall-remove') }
    catch { Warn 'Firewall rule left in place (administrator prompt declined)' }
  }
  if (Test-Path $Venv) { Remove-Item -LiteralPath $Venv -Recurse -Force; Ok 'Removed the Python environment (.venv)' }
  Write-Host ''
  Info "Void OS is uninstalled. To remove it completely, delete: $Root"
  Pause-IfDoubleClicked; exit 0
}

# ── 1. System ──────────────────────────────────────────────────────────
Step 'Checking this system'
$os = Get-CimInstance Win32_OperatingSystem
$arch = $env:PROCESSOR_ARCHITECTURE; if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
Info "System    $cBold$($os.Caption)$cReset ${cDim}(build $($os.BuildNumber), $arch)$cReset"
Info "Folder    $Root"
Info "Runs as   $cBold$env:USERNAME$cReset$(if (Test-Admin) { " ${cDim}(administrator)$cReset" })"
if ($OriginalUser -and $OriginalUser -ne $env:USERNAME) {
  Warn "You approved the prompt as $env:USERNAME, so Void OS is installed for that account, not for $OriginalUser."
}
if ([int]$os.BuildNumber -lt 17763) { Warn 'Windows 10 version 1809 or newer is recommended' }
if ($arch -eq 'x86') { Fail '32-bit Windows is not supported (Void OS needs 64-bit Python packages).' }
$memGb = [Math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
if ($memGb -lt 2) { Warn "Only $memGb GB of RAM — Void OS runs, but App Store apps may struggle" }
$drive = (Get-Item $Root).PSDrive
if ($drive -and $drive.Free -lt 1GB) { Warn 'Less than 1 GB free where Void OS is installed' }
try { Invoke-WebRequest -Uri 'https://pypi.org/simple/flask/' -UseBasicParsing -TimeoutSec 10 | Out-Null; Ok 'Internet connection' }
catch { Warn "Couldn't reach pypi.org — downloads may fail (check the connection or a proxy)" }
$HasWinget = Has-Command 'winget'
if ($HasWinget) { Info 'Packages  winget' } else { Warn 'winget not found — optional tools will need installing by hand (get "App Installer" from the Microsoft Store)' }

# ── features ───────────────────────────────────────────────────────────
$FeatureNames = @('tunnel','sync','docker','defender','firewall','devmode','nfs')
$Feat = @{
  tunnel = (Has-Command 'cloudflared'); sync = (Has-Command 'rclone'); docker = (Has-Command 'docker')
  defender = $true; firewall = $true; devmode = $false; nfs = $false
}
foreach ($pair in @(@($With, $true), @($Without, $false))) {
  foreach ($f in ($pair[0].Split(',') | Where-Object { $_ })) {
    $f = $f.Trim().ToLower()
    if ($FeatureNames -notcontains $f) { Fail "Unknown feature '$f' — choose from: $($FeatureNames -join ', ')" }
    $Feat[$f] = $pair[1]
  }
}
if (-not $Yes) {
  Write-Host ''
  Out-Line "  ${cBold}Optional features$cReset ${cDim}— Void OS works without any of these; you can re-run this later.$cReset" 'White'
  $Feat.tunnel   = Ask-YesNo 'Cloudflare Tunnel — reach Void OS from anywhere?' $Feat.tunnel
  $Feat.sync     = Ask-YesNo 'rclone — continuous Dropbox/Google Drive sync?' $Feat.sync
  $Feat.docker   = Ask-YesNo 'Docker Desktop — App Store & Containers? (large download, needs WSL 2)' $Feat.docker
  $Feat.defender = Ask-YesNo "Exclude Void OS's tmp folder from Defender scans? (faster uploads; admin)" $Feat.defender
  $Feat.firewall = Ask-YesNo 'Let other devices on your home network connect? (admin)' $Feat.firewall
  $Feat.devmode  = Ask-YesNo 'Turn on Developer Mode, needed to mount SMB network shares? (admin)' $Feat.devmode
  $Feat.nfs      = Ask-YesNo 'Install the Windows NFS client to mount NFS shares? (admin, Pro+)' $Feat.nfs
}

# All questions are asked here, up front, so the rest runs unattended.
if (-not (Test-Path (Join-Path $Root '.env')) -and -not $Yes) {
  Write-Host ''
  Out-Line "  ${cBold}Settings$cReset ${cDim}— change any time in .env$cReset" 'White'
  if ($Port -le 0) {
    while ($true) {
      $v = Ask-Value 'Web port' '7777'; $n = 0
      if ([int]::TryParse($v, [ref]$n) -and $n -ge 1 -and $n -le 65535) { $Port = $n; break }
      Out-Line "  $cYellow'$v' isn't a port number (1-65535)$cReset" 'Yellow'
    }
  }
  if (-not $FileRoot) { $FileRoot = Ask-Value 'Folder for your files (shown in the Files tab)' (Join-Path $env:USERPROFILE 'voidos-files') }
  Out-Line "  ${cDim}That's everything — the rest runs on its own.$cReset" 'DarkGray'
}

# ── 2. Python ──────────────────────────────────────────────────────────
Step "Python $MinPy+"
function Test-Python([string]$exe, [string[]]$pre = @()) {
  # Returns the interpreter's full path if it's 3.10+, else $null. Skips the
  # Microsoft Store "python.exe" alias, which only opens the Store.
  if ($exe -like '*\WindowsApps\*') { return $null }
  try {
    $out = Invoke-Native $exe ($pre + @('-c', "import sys;print(sys.executable if sys.version_info>=(3,10) else '')"))
    if ($LASTEXITCODE -eq 0 -and $out) { return $out.Split("`n")[-1].Trim() }
  } catch { }
  return $null
}
function Find-Python {
  if (Has-Command 'py') {
    foreach ($v in @('3.13','3.12','3.11','3.10')) { $p = Test-Python 'py' @("-$v"); if ($p) { return $p } }
  }
  foreach ($c in @(Get-Command python, python3 -All -ErrorAction SilentlyContinue)) { $p = Test-Python $c.Source; if ($p) { return $p } }
  $dirs = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}") | Where-Object { $_ -and (Test-Path $_) }
  foreach ($d in $dirs) {
    foreach ($exe in @(Get-ChildItem -Path $d -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending |
                       ForEach-Object { Join-Path $_.FullName 'python.exe' })) {
      if (Test-Path $exe) { $p = Test-Python $exe; if ($p) { return $p } }
    }
  }
  return $null
}
$Py = Find-Python
if (-not $Py) {
  Info "Python $MinPy+ not found — installing Python $PyWant for your account"
  if ($HasWinget) {
    $null = Invoke-Step "Installing Python $PyWant (winget)" 'winget' @('install','--id',"Python.Python.$PyWant",'-e','--scope','user','--silent',
              '--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
    Refresh-Path; $Py = Find-Python
  }
  if (-not $Py) {
    $pyArch = if ($arch -eq 'ARM64') { 'arm64' } else { 'amd64' }
    $url = "https://www.python.org/ftp/python/$PyFull/python-$PyFull-$pyArch.exe"
    New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null
    $inst = Join-Path $TmpDir "python-$PyFull-$pyArch.exe"
    try { Invoke-WebRequest -Uri $url -OutFile $inst -UseBasicParsing; Ok "Downloaded Python $PyFull" }
    catch { Fail "Couldn't download Python from python.org. Install Python $MinPy+ from https://python.org yourself, then re-run." }
    $null = Invoke-Step "Installing Python $PyFull" $inst @('/quiet','InstallAllUsers=0','PrependPath=1','Include_launcher=1','Include_test=0')
    Remove-Item -LiteralPath $inst -Force -ErrorAction SilentlyContinue
    Refresh-Path; $Py = Find-Python
  }
  if (-not $Py) { Fail "Python installed but couldn't be found. Open a new window and re-run install.cmd." }
}
$PyVer = Invoke-Native $Py @('-c', "import sys;print('%d.%d.%d'%sys.version_info[:3])")
Ok "Python $PyVer ${cDim}($Py)$cReset"

# ── 3. Python packages ─────────────────────────────────────────────────
Step 'Void OS packages'
$stopped = Stop-RunningVoidOS
if ($stopped) { Info 'Stopped the running Void OS so its files can be updated (it restarts at the end)' }
if ((Test-Path $VPy) -and -not (Test-Python $VPy)) { Info 'Existing .venv uses an old Python — rebuilding it'; Remove-Item -LiteralPath $Venv -Recurse -Force }
if (-not (Test-Path $VPy)) {
  if ((Invoke-Step 'Creating a private Python environment (.venv)' $Py @('-m','venv',$Venv)) -ne 0) { Fail "Couldn't create .venv" }
} else { Ok 'Python environment (.venv) already exists' }
if ((Invoke-Step 'Updating pip' $VPy @('-m','pip','install','--disable-pip-version-check','-q','--upgrade','pip','setuptools','wheel')) -ne 0) {
  Warn "Couldn't update pip — continuing with the bundled one"
}
if ((Invoke-Step 'Installing requirements (Flask, Docker SDK, pywinpty...)' $VPy @('-m','pip','install','--disable-pip-version-check','-q','-r',(Join-Path $Root 'requirements.txt'))) -ne 0) {
  Fail "Installing Python packages failed — see $Log"
}
if ((Invoke-Step 'Checking the packages load' $VPy @('-c','import flask, psutil, docker, yaml, flask_sock, dotenv, winpty, sounddevice')) -ne 0) {
  Fail "Packages installed but can't be imported — see $Log"
}
$null = Invoke-Native $VPy @($Helper, 'mark-requirements')   # Void OS skips reinstalling these at startup

# ── 4. Optional tools ──────────────────────────────────────────────────
Step 'Optional tools'
function Install-Winget([string]$label, [string]$id, [string]$cmd) {
  if (Has-Command $cmd) { Ok "$label already installed"; return }
  if (-not $HasWinget) { Warn "${label}: install it by hand (winget isn't available)"; return }
  $rc = Invoke-Step "Installing $label" 'winget' @('install','--id',$id,'-e','--silent','--accept-package-agreements','--accept-source-agreements','--disable-interactivity')
  if ($rc -ne 0 -and $rc -ne -1978335189) { Warn "$label install failed (winget exit $rc)" }   # -1978335189 = already installed
  Refresh-Path
}
$anyTool = $false
if ($Feat.tunnel) { $anyTool = $true; Install-Winget 'cloudflared' 'Cloudflare.cloudflared' 'cloudflared' }
if ($Feat.sync)   { $anyTool = $true; Install-Winget 'rclone' 'Rclone.Rclone' 'rclone' }
if ($Feat.docker) { $anyTool = $true
  Install-Winget 'Docker Desktop' 'Docker.DockerDesktop' 'docker'
  if (-not (Has-Command 'docker')) { Info 'Docker Desktop may need a restart and WSL 2 (it will guide you) — then start it once' }
}
if (-not $anyTool) { Info 'No optional tools selected' }

# ── 5. Configuration ───────────────────────────────────────────────────
Step 'Configuration'
function H([string[]]$a) { return Invoke-Native $VPy (@($Helper) + $a) }
$created = H @('env-init')
if ($created -eq 'created') { Ok 'Created .env from .env.example (every setting, with its default)' }
else {
  Ok 'Keeping your existing .env'
  $added = H @('env-sync'); if ($added -and [int]$added -gt 0) { Ok "Added $added new setting(s) to .env, commented out with their defaults" }
}
if ((H @('ensure-secret')) -eq 'generated') { Ok 'Generated a new secret key' } else { Ok 'Secret key already set' }
$cur = H @('env-get','VOID_PORT'); if (-not $cur) { $cur = '7777' }
$ThePort = if ($Port -gt 0) { $Port } else { [int]$cur }
if ($created -eq 'created' -or $Port -gt 0) {
  if ($Port -le 0) { $ThePort = [int](Ask-Value 'Web port' "$ThePort") }
  while ($true) {
    $null = Invoke-Native $VPy @($Helper, 'port-free', "$ThePort"); if ($LASTEXITCODE -eq 0) { break }
    Warn "Port $ThePort is already in use by another program"
    if ($Yes) { $ThePort++ } else { $ThePort = [int](Ask-Value 'Choose another port' "$($ThePort + 1)") }
  }
  $null = H @('env-set','VOID_PORT',"$ThePort")
}
Ok "Port $cBold$ThePort$cReset"
$rootCur = H @('env-get','VOID_FILE_ROOT')
if ($FileRoot) { $TheRoot = $FileRoot }
elseif ($created -eq 'created' -or -not $rootCur) { $TheRoot = Ask-Value 'Folder for your files (shown in the Files tab)' (Join-Path $env:USERPROFILE 'voidos-files') }
else { $TheRoot = $rootCur }
$TheRoot = [Environment]::ExpandEnvironmentVariables($TheRoot)
if (-not (Test-Path $TheRoot)) {
  try { New-Item -ItemType Directory -Force -Path $TheRoot | Out-Null; Ok "Created $TheRoot" } catch { Fail "Couldn't create $TheRoot" }
}
$null = H @('env-set','VOID_FILE_ROOT',$TheRoot)
Ok "Files folder $cBold$TheRoot$cReset"
if ($Feat.tunnel -and $created -eq 'created') { $null = H @('env-set','VOID_CLOUDFLARE_TUNNEL','true'); Ok 'Cloudflare Tunnel switched on' }
New-Item -ItemType Directory -Force -Path $TmpDir | Out-Null

# Administrator-only tweaks: one UAC prompt for all of them
$admin = @()
foreach ($k in @('defender','firewall','devmode','nfs')) { if ($Feat[$k]) { $admin += $k } }
if ($admin.Count) {
  Info "Asking Windows for administrator rights for: $($admin -join ', ')"
  try {
    $argLine = '-NoProfile -ExecutionPolicy Bypass -File ' + (Quote-Arg $MyInvocation.MyCommand.Path) + " -AdminTasks -Port $ThePort -AdminList " + ($admin -join ',')
    if (Test-Admin) { & powershell -NoProfile -ExecutionPolicy Bypass -File $MyInvocation.MyCommand.Path -AdminTasks -Port $ThePort -AdminList ($admin -join ',') }
    else { Start-Process powershell -Verb RunAs -Wait -ArgumentList $argLine }
    Ok 'Administrator steps finished'
  } catch { Warn "Administrator steps skipped (the prompt was declined): $($admin -join ', ')" }
}

# ── 6. Shortcuts & start on sign-in ───────────────────────────────────
Step 'Shortcuts'
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$path, [string]$target, [string]$arguments, [int]$style, [string]$desc) {
  $s = $shell.CreateShortcut($path)
  $s.TargetPath = $target; $s.Arguments = $arguments; $s.WorkingDirectory = $Root
  $s.WindowStyle = $style; $s.Description = $desc
  $ico = Join-Path $Root 'static\favicon.ico'; if (Test-Path $ico) { $s.IconLocation = $ico }
  $s.Save()
}
function New-UrlShortcut([string]$path, [string]$url) {
  $ico = Join-Path $Root 'static\favicon.ico'
  $body = "[InternetShortcut]`r`nURL=$url`r`n"
  if (Test-Path $ico) { $body += "IconFile=$ico`r`nIconIndex=0`r`n" }
  [IO.File]::WriteAllText($path, $body)
}
$Url = "http://localhost:$ThePort"
New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
New-Shortcut (Join-Path $StartMenu "Start $AppName.lnk") $VPy (Quote-Arg (Join-Path $Root 'voidos.py')) 7 'Start the Void OS server'
New-UrlShortcut (Join-Path $StartMenu "Open $AppName.url") $Url
New-Shortcut (Join-Path $StartMenu "$AppName folder.lnk") $Root '' 1 'Void OS installation folder'
Ok 'Start menu: Start Void OS, Open Void OS, Void OS folder'
New-UrlShortcut $DesktopUrl $Url
Ok "Desktop shortcut: $AppName"
if ($NoAutostart) { Info 'Start on sign-in skipped (-NoAutostart)'; if (Test-Path $StartupLnk) { Remove-Item -LiteralPath $StartupLnk -Force } }
else {
  # A Startup-folder shortcut (no admin needed), started minimized. The
  # in-app Restart button relaunches it by itself (VOID_RESTART_MODE=respawn).
  New-Shortcut $StartupLnk $VPy (Quote-Arg (Join-Path $Root 'voidos.py')) 7 'Start Void OS when you sign in'
  Ok 'Starts automatically when you sign in to Windows'
}

# ── 7. Start ───────────────────────────────────────────────────────────
Step "Starting $AppName"
$Started = $false
if ($NoStart) { Info 'Skipped (-NoStart)' }
else {
  Start-Process -FilePath $VPy -ArgumentList (Quote-Arg (Join-Path $Root 'voidos.py')) -WorkingDirectory $Root -WindowStyle Minimized
  $rc = Invoke-Step 'Waiting for Void OS to answer' $VPy @($Helper,'wait-health',"$ThePort",'60')
  if ($rc -eq 0) { $Started = $true } else { Warn 'Void OS didn''t come up — open "Start Void OS" from the Start menu to see what it says' }
}

# ── done ───────────────────────────────────────────────────────────────
$ips = (H @('lan-ips')).Split(' ') | Where-Object { $_ }
$rule = '  ' + ([string][char]0x2500) * 74
Write-Host ''; Out-Line "$cPurple2$rule$cReset" 'DarkMagenta'
if ($Started) { Out-Line "  $cGreen$cBold$($Glyph.ok) Void OS is running$cReset" 'Green' } else { Out-Line "  $cPurple${cBold}Void OS is installed$cReset" 'Magenta' }
Out-Line "$cPurple2$rule$cReset" 'DarkMagenta'
Out-Line '  Open it in a browser:' 'White'
Out-Line "     $cCyan$cBold$Url$cReset" 'Cyan'
foreach ($ip in $ips) { Out-Line "     ${cCyan}http://${ip}:$ThePort$cReset   ${cDim}from other devices on your network$cReset" 'Cyan' }
Write-Host ''
Out-Line "  ${cBold}First visit:$cReset create your account — the first one becomes the Super User." 'White'
Write-Host ''
Out-Line "  ${cDim}Start    $cReset Start menu > Void OS > Start Void OS" 'Gray'
Out-Line "  ${cDim}Settings $cReset$Root\.env ${cDim}(see .env.example for every option)$cReset" 'Gray'
Out-Line "  ${cDim}Update   $cReset Settings > Updates in Void OS (or replace the files and run install.cmd again)" 'Gray'
Out-Line "  ${cDim}Remove   $cReset first setup\install.cmd -Uninstall" 'Gray'
if ($script:Warnings.Count) {
  Write-Host ''; Out-Line "  $cYellow${cBold}Worth checking:$cReset" 'Yellow'
  foreach ($w in $script:Warnings) { Out-Line "  $cYellow!$cReset $w" 'Yellow' }
}
Out-Line "$cPurple2$rule$cReset" 'DarkMagenta'
Write-Host ''
if ($Started -and -not $Yes) { Start-Process $Url }
Pause-IfDoubleClicked
