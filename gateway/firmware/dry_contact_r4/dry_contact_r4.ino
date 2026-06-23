// Lecteur de contact sec — Alarm 2.0 (Arduino UNO R4 Minima)
//
// Découple le contact-sec du modem SIM7600 : l'Arduino lit le contact et le
// pousse sur l'USB-série au mini-PC, qui le relaie au backend via la gateway
// (DRY_CONTACT_SOURCE=host). Le contact-sec reste donc fonctionnel même quand
// le modem drop du bus USB. Cf docs + gateway/dry_contact.py + HostDryContactMonitorThread.
//
// Câblage : contact NC entre PIN_CONTACT et GND, pull-up interne (pas de résistance).
//   fermé (repos) -> LOW  -> "DC:0" (normal)
//   ouvert/coupé  -> HIGH -> "DC:1" (alarme / sabotage)
// Protocole USB-série (115200) : ligne "DC:<0|1>" à chaque changement (après
// debounce) + heartbeat 1 Hz (preuve de vie ; la gateway alarme si le µC se tait).
//
// Flash (headless) : arduino-cli compile/upload --fqbn arduino:renesas_uno:minima
const uint8_t  PIN_CONTACT  = 2;
const uint16_t DEBOUNCE_MS  = 50;
const uint16_t HEARTBEAT_MS = 1000;

int last = -1, stable = -1;
unsigned long tChange = 0, tEmit = 0;

void emit(int s) {
  Serial.print("DC:");
  Serial.println(s);
  digitalWrite(LED_BUILTIN, s);   // LED allumée = ouvert/alarme
  tEmit = millis();
}

void setup() {
  pinMode(PIN_CONTACT, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(115200);
  delay(50);
  stable = last = digitalRead(PIN_CONTACT);
  emit(stable);
}

void loop() {
  int r = digitalRead(PIN_CONTACT);
  unsigned long now = millis();
  if (r != last) { last = r; tChange = now; }
  if (r != stable && now - tChange >= DEBOUNCE_MS) { stable = r; emit(stable); }
  if (now - tEmit >= HEARTBEAT_MS) emit(stable);
}
