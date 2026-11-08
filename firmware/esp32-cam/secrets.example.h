// Copie este arquivo para secrets.h (mesma pasta) e preencha.
// secrets.h esta no .gitignore: nunca commite credenciais reais.
#pragma once
#include <WiFi.h>

const char* WIFI_SSID     = "NOME_DA_REDE";
const char* WIFI_PASSWORD = "SENHA_DA_REDE";

// IP fixo da ESP32-CAM (o mesmo que vai em IP_CAMERA no .env do servidor)
IPAddress local_IP(192, 168, 0, 50);
IPAddress gateway(192, 168, 0, 1);
IPAddress subnet(255, 255, 255, 0);
IPAddress primaryDNS(8, 8, 8, 8);
IPAddress secondaryDNS(8, 8, 4, 4);
