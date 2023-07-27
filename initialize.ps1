function New-TemporaryDirectory {
    $parent = [System.IO.Path]::GetTempPath()
    [string] $name = [System.Guid]::NewGuid()
    New-Item -ItemType Directory -Path (Join-Path $parent $name)
}

function Install-OpenSSH {
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
}

function Install-Git {
    $version = "2.36.1"
    $distroFilename = "Git-$version-64-bit.exe"
    $distroUrl = "https://github.com/git-for-windows/git/releases/download/v$version.windows.1/$distroFilename"

    $tmpDir = New-TemporaryDirectory
    $distroFilepath = Join-Path $tmpDir $distroFilename
    if (Test-Path "$distroFilename") {
        Copy-Item "$distroFilename" "$distroFilepath"
    } else {
        Invoke-WebRequest -Uri "$distroUrl" -OutFile "$distroFilepath"
    }
    Start-Process "$distroFilepath" -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS=`"icons,assoc,assoc_sh`" /o:PathOption=CmdTools /o:SSHOption=ExternalOpenSSH" -Wait
    Remove-Item -Recurse $tmpDir
}

function Setup-OpenSSH {
    # Append the Win32-OpenSSH install directory to the system path
    [Environment]::SetEnvironmentVariable("Path", $env:Path + ';' + ${Env:ProgramFiles} + '\OpenSSH', [System.EnvironmentVariableTarget]::Machine)

    # authorized keys
    $akPath = "$env:PROGRAMDATA\ssh\administrators_authorized_keys"
    if (!(Test-Path $akPath)) {
        New-Item $akPath | Out-Null
    }
    $akAcl = Get-Acl -Path $akPath
    $akAcl.SetSecurityDescriptorSddlForm("O:BAD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
    Set-Acl -Path $akPath -AclObject $akAcl

    # Confirm the Firewall rule is configured.
    if (!(Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue | Select-Object Name, Enabled)) {
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }

    # config
    $sshdConfigPath = "$env:PROGRAMDATA\ssh\sshd_config"
    Add-Content $sshdConfigPath -Value "PasswordAuthentication no"

    # Set default shell
    $registryPath = "HKLM:\SOFTWARE\OpenSSH"

    IF(!(Test-Path $registryPath)) {
        New-Item -Path $registryPath -Force | Out-Null
    }

    New-ItemProperty -Path $registryPath -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null

    # Start the sshd service
    Set-Service -Name sshd -StartupType 'Automatic'
    Start-Service sshd
}

function Prepare-Machine {
    Clear-Content "$env:PROGRAMDATA\ssh\administrators_authorized_keys"
    Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_ecdsa_key"
    Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_ec25519_key"
    Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_dsa_key"
    Remove-Item "$env:PROGRAMDATA\ssh\ssh_host_rsa_key"
}

Install-OpenSSH
Install-Git
Setup-OpenSSH
