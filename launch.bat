@echo off
echo Lancement du backend...
docker compose up --build -d

echo Installation de l'app...
C:\Users\Charles\Android\Sdk\platform-tools\adb.exe install -r android\app\build\outputs\apk\debug\app-debug.apk

echo Configuration du port...
C:\Users\Charles\Android\Sdk\platform-tools\adb.exe reverse tcp:8000 tcp:8000

echo Done. Backend sur http://localhost:8000
pause
