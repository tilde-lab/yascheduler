# How to prepare a Windows node

Install OpenSSH:

```powershell

function New-TemporaryDirectory {
    $parent = [System.IO.Path]::GetTempPath()
    [string] $name = [System.Guid]::NewGuid()
    New-Item -ItemType Directory -Path (Join-Path $parent $name)
}

$distroFilename = "OpenSSH-Win64-v8.9.1.0.msi"
$distroUrl = "https://github.com/PowerShell/Win32-OpenSSH/releases/download/v8.9.1.0p1-Beta/$distroFilename"

$tmpDir = New-TemporaryDirectory
$distroFilepath = Join-Path $tmpDir $distroFilename
if (Test-Path "$distroFilename") {
    Copy-Item "$distroFilename" "$distroFilepath"
} else {
    Invoke-WebRequest -Uri "$distroUrl" -OutFile "$distroFilepath"
}
Start-Process "$env:windir\System32\msiexec.exe" -ArgumentList "/i `"$distroFilepath`" /qn" -Wait
Remove-Item -Recurse $tmpDir
```

Add OpenSSH to `$env:PATH`:

```powershell
# Append the Win32-OpenSSH install directory to the system path
[Environment]::SetEnvironmentVariable(
    "Path",
    $env:Path + ';' + ${Env:ProgramFiles} + '\OpenSSH',
    [System.EnvironmentVariableTarget]::Machine
)
```

Set ACLs for authorized keys file:

```powershell
$akPath = "$env:PROGRAMDATA\ssh\administrators_authorized_keys"
if (!(Test-Path $akPath)) {
    New-Item $akPath | Out-Null
}
$akAcl = Get-Acl -Path $akPath
$akAcl.SetSecurityDescriptorSddlForm("O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
Set-Acl -Path $akPath -AclObject $akAcl
```

Open SSH port:

```powershell
$rule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" `
    -ErrorAction SilentlyContinue | Select-Object Name, Enabled
if (!$rule) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
      -DisplayName 'OpenSSH Server (sshd)' `
      -Enabled True `
      -Direction Inbound `
      -Action Allow `
      -Protocol TCP `
      -LocalPort 22 | Out-Null
}
```

Disable password authentication:

```powershell
$sshdConfigPath = "$env:PROGRAMDATA\ssh\sshd_config"
Add-Content $sshdConfigPath -Value "PasswordAuthentication no"
```

Set default shell for SSH:

```powershell
$registryPath = "HKLM:\SOFTWARE\OpenSSH"

if (!(Test-Path $registryPath)) {
    New-Item -Path $registryPath -Force | Out-Null
}

New-ItemProperty -Path $registryPath -Name DefaultShell `
    -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -PropertyType String -Force | Out-Null
```

Enable and start OpenSSH service:

```powershell
Set-Service -Name sshd -StartupType 'Automatic'
Start-Service sshd
```

Add public key to `$env:PROGRAMDATA\ssh\administrators_authorized_keys`
file.

If you're preparing image, don't forget to delete all SSH keys before capture:

```powershell
Clear-Content "$env:PROGRAMDATA\ssh\administrators_authorized_keys"
Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_ecdsa_key"
Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_ec25519_key"
Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_dsa_key"
Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_rsa_key"
```

Add authorized keys on first boot.
