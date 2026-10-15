# Image "porteuse" du code gateway : jamais executee, seulement lue par
# alarm-gateway-sync (docker create + docker cp) sur les noeuds qui ont une
# gateway. Elle suit le meme circuit que backend/patroni : cd-build -> :sha-XXX,
# cd-promote-stable -> :stable, alarm-cd-pull -> noeuds. Cf scripts/alarm-gateway-sync.sh.
FROM busybox:1.36.1
COPY *.py requirements.txt /gateway/
# Messages vocaux des appels d'alarme (INV-132/133), generes par scripts/generate-gateway-voice.py
COPY voice/*.wav /gateway/voice/
CMD ["true"]
