#requires -Version 5.1
#requires -RunAsAdministrator
<#
.SYNOPSIS
    Active la nested virtualization sur une VM Hyper-V (sur le host Windows)
    pour permettre /dev/kvm dans la VM Linux invitee.

.DESCRIPTION
    A executer depuis le host Windows, en PowerShell admin, **VM arretee**.
    Active :
    - Set-VMProcessor ... -ExposeVirtualizationExtensions $true
      (la VM peut alors faire tourner KVM/QEMU en interne)
    - Set-VMNetworkAdapter ... -MacAddressSpoofing On
      (necessaire pour que les bridges Docker dans la VM voient l'externe)

    Idempotent : ne casse rien si deja actif.

.PARAMETER VMName
    Nom de la VM Hyper-V cible. Liste : `Get-VM`.

.EXAMPLE
    .\enable-hyperv-nested-virt.ps1 -VMName "alarm-android-runner"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$VMName
)

$ErrorActionPreference = "Stop"

# 1. Verifier que la VM existe
$vm = Get-VM -Name $VMName -ErrorAction SilentlyContinue
if (-not $vm) {
    Write-Host "VMs disponibles :" -ForegroundColor Yellow
    Get-VM | Select-Object Name, State | Format-Table
    throw "VM '$VMName' introuvable."
}

# 2. VM doit etre arretee pour modifier les processor settings
if ($vm.State -ne "Off") {
    throw "VM '$VMName' est dans l'etat '$($vm.State)'. Stop-VM -Name '$VMName' avant de relancer."
}

# 3. Activer nested virt
Write-Host "==> Set-VMProcessor -VMName '$VMName' -ExposeVirtualizationExtensions `$true" -ForegroundColor Cyan
Set-VMProcessor -VMName $VMName -ExposeVirtualizationExtensions $true
$proc = Get-VMProcessor -VMName $VMName
Write-Host "    ExposeVirtualizationExtensions = $($proc.ExposeVirtualizationExtensions)" -ForegroundColor Green

# 4. Activer MAC address spoofing (necessaire pour Docker bridge dans la VM)
$adapters = Get-VMNetworkAdapter -VMName $VMName
foreach ($a in $adapters) {
    Write-Host "==> Set-VMNetworkAdapter (adapter '$($a.Name)') -MacAddressSpoofing On" -ForegroundColor Cyan
    Set-VMNetworkAdapter -VMNetworkAdapter $a -MacAddressSpoofing On
    $a2 = Get-VMNetworkAdapter -VMName $VMName -Name $a.Name
    Write-Host "    MacAddressSpoofing = $($a2.MacAddressSpoofing)" -ForegroundColor Green
}

# 5. Verifier la VM CPU compat (doit avoir VT-x/EPT exposable)
$hostCpu = Get-CimInstance -ClassName Win32_Processor
$ept = $hostCpu.SecondLevelAddressTranslationExtensions
$vmx = $hostCpu.VirtualizationFirmwareEnabled
Write-Host ""
Write-Host "Host CPU :" -ForegroundColor DarkGray
Write-Host "  VT-x firmware enabled : $vmx" -ForegroundColor DarkGray
Write-Host "  EPT (Second Level Address Translation) : $ept" -ForegroundColor DarkGray
if (-not $ept) {
    Write-Warning "EPT pas detecte sur l'host CPU. Nested virt ne fonctionnera probablement pas."
}

Write-Host ""
Write-Host "Nested virt active sur '$VMName'." -ForegroundColor Green
Write-Host "Demarre la VM (Start-VM -Name '$VMName' ou via le Hyper-V Manager)" -ForegroundColor Green
Write-Host "puis dans la VM verifie : ls -l /dev/kvm"
