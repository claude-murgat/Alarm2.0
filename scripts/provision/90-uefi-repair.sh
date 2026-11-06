#!/usr/bin/env bash
# 90-uefi-repair.sh — (root, sur la machine) repare le demarrage UEFI (incident alarm-2 du 2026-11-06) :
# paquets inacheves termines, GRUB reinstalle (shim signe + chemin de secours EFI/BOOT/BOOTX64.EFI,
# utile si la NVRAM est videe par un flash BIOS), update-grub, entrees « Windows Boot Manager »
# orphelines retirees (leur partition n'existe plus). Ne redemarre pas : le faire quand le cluster le
# permet, puis sudo alarm-node-check. Ne regle pas le BIOS : Boot List Option = UEFI se fait sur place.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
[ -d "$R/sys/firmware/efi" ] || die "machine demarree en legacy, pas en UEFI : F2 > Boot Sequence > Boot List Option = UEFI d'abord (docs/STANDARD_MACHINE.md §3)"
a=$(dpkg --audit)
if [ -n "$a" ]; then say "paquets inacheves : dpkg --configure -a"; DEBIAN_FRONTEND=noninteractive dpkg --configure -a; fi
grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=debian --force-extra-removable --recheck
update-grub
parts=$(blkid -s PARTUUID -o value | tr 'A-F' 'a-f')
efibootmgr -v | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\)\*\{0,1\} Windows Boot Manager.*HD([0-9]*,GPT,\([0-9A-Fa-f-]*\),.*/\1 \2/p' |
while read -r num guid; do
  if grep -qx "$(tr 'A-F' 'a-f' <<<"$guid")" <<<"$parts"; then
    say "Boot$num Windows Boot Manager : sa partition existe, conservee"
  else
    efibootmgr -q -b "$num" -B
    say "Boot$num Windows Boot Manager orpheline retiree"
  fi
done
efibootmgr | sed -n 1,4p
say "FAIT. Redemarrer quand le cluster le permet, puis : sudo alarm-node-check"
