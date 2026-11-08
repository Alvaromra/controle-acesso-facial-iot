/*
==========================================================
  ESP32 DevKit - Sensor de Presenca (PIR)
  Projeto ENE0477 - Controle de Acesso Facial (UnB)
  Alvaro Marcal de Araujo
==========================================================

  Funcao:
   Le o sensor PIR (GPIO13) e publica "presenca" via MQTT
   quando detecta movimento -> dispara a captura no painel.

  Ligacao do PIR HC-SR501:
   VCC -> 5V (VIN)
   OUT -> GPIO13
   GND -> GND

  Rede: IPs fixos para PC/broker, ESP32-CAM e esta placa,
  definidos em secrets.h (copie de secrets.example.h).

  Topico MQTT:
   Publica presenca: unb/alvaro/controleacesso/presenca   ("1")
*/

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>

// ===== Wi-Fi, IP fixo e broker MQTT =====
// Copie secrets.example.h para secrets.h e preencha com os dados da sua rede.
// O secrets.h esta no .gitignore e nunca deve ser commitado.
#include "secrets.h"
const char* TOPICO_PRESENCA = "unb/alvaro/controleacesso/presenca";

// ===== Pino do PIR =====
#define PIR_PIN   13

// ===== Parametros de operacao =====
const unsigned long TEMPO_AQUECIMENTO  = 30000;  // PIR estabiliza em ~30s
const unsigned long INTERVALO_PRESENCA = 5000;   // nao repete "presenca" em menos de 5s

WiFiClient espClient;
PubSubClient mqtt(espClient);

bool pirArmado = false;
int  ultimoEstadoPir = LOW;
unsigned long ultimaPublicacao = 0;
unsigned long inicioBoot = 0;

// ---------- Conexoes ----------
void conectarWiFi() {
  if (!WiFi.config(local_IP, gateway, subnet, primaryDNS, secondaryDNS)) {
    Serial.println("Falha ao configurar IP fixo");
  }
  Serial.print("Conectando ao Wi-Fi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Wi-Fi conectado! IP: ");
  Serial.println(WiFi.localIP());
}

void conectarMQTT() {
  while (!mqtt.connected()) {
    Serial.print("Conectando ao MQTT...");
    String clientId = "ESP32-DevKit-PIR-" + String(random(0xffff), HEX);
    if (mqtt.connect(clientId.c_str())) {
      Serial.println(" conectado!");
    } else {
      Serial.print(" falhou, rc="); Serial.print(mqtt.state());
      Serial.println(" - nova tentativa em 3s");
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);

  pinMode(PIR_PIN, INPUT);

  Serial.println("\n=== ESP32 DevKit - Sensor de Presenca (PIR) ===\n");

  conectarWiFi();
  mqtt.setServer(MQTT_BROKER, MQTT_PORT);
  mqtt.setKeepAlive(60);
  conectarMQTT();

  inicioBoot = millis();
  Serial.println("Aguardando aquecimento do PIR (30s)...");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    conectarWiFi();
  }
  if (!mqtt.connected()) {
    conectarMQTT();
  }
  mqtt.loop();

  // Aquecimento do PIR
  if (!pirArmado) {
    if (millis() - inicioBoot >= TEMPO_AQUECIMENTO) {
      pirArmado = true;
      Serial.println("PIR pronto. Monitorando presenca...\n");
    }
    return;
  }

  // Deteccao por borda de subida
  int estadoPir = digitalRead(PIR_PIN);
  if (estadoPir == HIGH && ultimoEstadoPir == LOW) {
    unsigned long agora = millis();
    if (agora - ultimaPublicacao >= INTERVALO_PRESENCA) {
      Serial.println(">>> PRESENCA DETECTADA - publicando MQTT");
      mqtt.publish(TOPICO_PRESENCA, "1");
      ultimaPublicacao = agora;
    }
  }
  ultimoEstadoPir = estadoPir;

  delay(50);
} 