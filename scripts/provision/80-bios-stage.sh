#!/usr/bin/env bash
# 80-bios-stage.sh <chemin/9020MA19.exe> — (root, sur la machine) prepare la mise a jour du BIOS Dell
# vers la version du standard : verifie taille et SHA-256 du fichier contre infra/onsite/standard.env
# (catalogue Dell), puis le depose a la racine de la partition EFI (/boot/efi/9020MA19.exe).
# RIEN n'est flashe ici : sur place, F12 > BIOS Flash Update > choisir le fichier, sans jamais couper
# le courant (docs/STANDARD_MACHINE.md §2). Machine deja a la bonne version : rien a faire.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
need_root; this_node
f=${1:-}
[ -f "$f" ] || usage "<chemin du fichier $STD_BIOS_FILE telecharge depuis le catalogue Dell>"
cur=$(cat "$R/sys/class/dmi/id/bios_version" 2>/dev/null || echo inconnu)
if [ "$cur" = "$STD_BIOS_VERSION" ]; then say "$HOST : BIOS deja en $cur, rien a faire"; exit 0; fi
[ "$(stat -c %s "$f")" = "$STD_BIOS_SIZE" ] || die "taille de $f differente du catalogue Dell ($STD_BIOS_SIZE octets) : ne pas flasher"
[ "$(sha256sum < "$f" | cut -d' ' -f1)" = "$STD_BIOS_SHA256" ] || die "SHA-256 de $f different du standard : ne pas flasher"
[ -d "$R/boot/efi/EFI" ] || die "partition EFI non montee sur /boot/efi"
install -m644 "$f" "$R/boot/efi/$STD_BIOS_FILE"
[ "$(sha256sum < "$R/boot/efi/$STD_BIOS_FILE" | cut -d' ' -f1)" = "$STD_BIOS_SHA256" ] || die "copie corrompue sur /boot/efi"
say "$HOST : BIOS $cur -> $STD_BIOS_VERSION pret sur /boot/efi/$STD_BIOS_FILE (SHA-256 verifie)"
say "sur place : F12 au demarrage > BIOS Flash Update > $STD_BIOS_FILE ; ne jamais couper le courant pendant le flash"
say "apres : reglages BIOS a revoir (docs/STANDARD_MACHINE.md §3), rm /boot/efi/$STD_BIOS_FILE, sudo alarm-node-check"
