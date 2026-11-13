# Image "porteuse" du code gateway : jamais executee, seulement lue par
# alarm-gateway-sync (docker create + docker cp) sur les noeuds qui ont une
# gateway. Elle suit le meme circuit que backend/patroni : cd-build -> :sha-XXX,
# cd-promote-stable -> :stable, alarm-cd-pull -> noeuds. Cf scripts/alarm-gateway-sync.sh.
FROM busybox:1.36.1
COPY *.py requirements.txt /gateway/
# Messages vocaux des appels d'alarme (INV-132/133), generes par scripts/generate-gateway-voice.py
COPY voice/*.wav /gateway/voice/
# Firmware de l'Arduino du contact sec, programme par la passerelle (INV-198) : versionne en base64, decode ici
COPY firmware/dry_contact_r4/dry_contact_r4.ino.bin.b64 /tmp/
RUN mkdir -p /gateway/firmware/dry_contact_r4 \
 && base64 -d /tmp/dry_contact_r4.ino.bin.b64 > /gateway/firmware/dry_contact_r4/dry_contact_r4.ino.bin \
 && rm /tmp/dry_contact_r4.ino.bin.b64
CMD ["true"]
