"""
==========================================================
  PAINEL DE CONTROLE DE ACESSO - RECONHECIMENTO FACIAL
  Projeto ENE0477 - Automacao Predial com IoT (UnB)
  Alvaro Marcal de Araujo
----------------------------------------------------------
  Versao com SENSOR DE PRESENCA (PIR) via MQTT:
  - A ESP32 DevKit publica "presenca" quando o PIR detecta
    movimento (topico unb/alvaro/controleacesso/presenca).
  - O painel assina esse topico e so dispara a captura +
    reconhecimento quando ha presenca recente, evitando o
    processamento continuo de imagens.
==========================================================
"""

import os
import hmac
import json
import pickle
import socket
import threading
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, Response
from werkzeug.utils import secure_filename

# ===================== CONFIGURACOES =====================
# Valores sensiveis vem do arquivo .env (veja .env.example), nunca do codigo.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

IP_CAMERA = os.getenv("IP_CAMERA", "")
URL_CAPTURA = f"http://{IP_CAMERA}/capture"
URL_STREAM = f"http://{IP_CAMERA}/stream"

TUYA_ACCESS_ID = os.getenv("TUYA_ACCESS_ID", "")
TUYA_ACCESS_KEY = os.getenv("TUYA_ACCESS_KEY", "")
TUYA_API_ENDPOINT = os.getenv("TUYA_API_ENDPOINT", "https://openapi.tuyaus.com")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID", "")

# MQTT (broker Mosquitto local)
# O painel roda no MESMO PC do Mosquitto, entao usa localhost.
# Assim nao depende do IP que o roteador da ao PC (que pode mudar).
MQTT_BROKER = os.getenv("MQTT_BROKER", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# Painel web
PAINEL_HOST = os.getenv("PAINEL_HOST", "0.0.0.0")
PAINEL_PORTA = int(os.getenv("PAINEL_PORTA", "5000"))
PAINEL_USUARIO = os.getenv("PAINEL_USUARIO", "admin")
PAINEL_SENHA = os.getenv("PAINEL_SENHA", "")   # vazio = painel sem login (nao recomendado)
TOPICO_PRESENCA = "unb/alvaro/controleacesso/presenca"
TOPICO_COMANDO  = "unb/alvaro/controleacesso/comando"

# Logica do PIR
USAR_PIR = True            # True: so reconhece apos presenca; False: captura continua
JANELA_PRESENCA = 5        # segundos que a presenca permanece "valida" — reduzido de 8 pra 5

# ----- Parametros de precisao do reconhecimento -----
TOLERANCE = 20.0           # limiar de distancia: < TOLERANCE = reconhecido
N_CAPTURAS = 3             # quantas fotos tira por tentativa (votacao)
VOTOS_MINIMOS = 2          # quantas precisam concordar na mesma pessoa p/ autorizar
NITIDEZ_MINIMA = 40.0      # variancia do Laplaciano: abaixo disso a imagem esta borrada
BRILHO_MINIMO = 40         # brilho medio (0-255): abaixo disso a imagem esta escura
BRILHO_MAXIMO = 230        # acima disso a imagem esta estourada/clara demais

BASE_DIR = Path(__file__).parent
FOTOS_PATH = BASE_DIR.parent / "fotos_autorizadas"
ENCODINGS_FILE = BASE_DIR.parent / "encodings.pkl"
LOGS_FILE = BASE_DIR / "logs_acesso.json"
UPLOAD_EXTENSOES = {".jpg", ".jpeg", ".png"}

# ===================== APP FLASK =====================
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = str(FOTOS_PATH)


@app.before_request
def exigir_login():
    """Login HTTP Basic em todas as rotas quando PAINEL_SENHA esta definida."""
    if not PAINEL_SENHA:
        return None
    auth = request.authorization
    if auth and auth.username is not None and auth.password is not None:
        usuario_ok = hmac.compare_digest(auth.username.encode(), PAINEL_USUARIO.encode())
        senha_ok = hmac.compare_digest(auth.password.encode(), PAINEL_SENHA.encode())
        if usuario_ok and senha_ok:
            return None
    return Response("Autenticacao necessaria", 401,
                    {"WWW-Authenticate": 'Basic realm="Controle de Acesso"'})


def pasta_pessoa(nome):
    """Retorna a pasta da pessoa dentro de FOTOS_PATH, ou None se o nome for invalido.
    Impede nomes como '..' ou com barras, que sairiam da pasta de fotos."""
    nome = (nome or "").strip()
    if not nome or nome in (".", "..") or "/" in nome or "\\" in nome:
        return None
    pasta = (FOTOS_PATH / nome).resolve()
    if pasta.parent != FOTOS_PATH.resolve():
        return None
    return pasta

estado = {
    "reconhecimento_ativo": False,
    "thread_reconhecimento": None,
    "ultimo_acesso": None,
    "tuya_conectado": False,
    "pir_conectado": False,     # MQTT do PIR conectado
    "ultima_presenca": 0,       # timestamp da ultima deteccao de presenca
}

cache_status = {
    "camera": {"online": False, "ultima_checagem": 0},
    "fechadura": {"online": False, "bateria": None, "ultima_checagem": 0},
    "mosquitto": {"online": False, "ultima_checagem": 0},
}

tuya_api = None
mqtt_client = None

# Visualizacao da camera (Opcao 2): o painel e o UNICO cliente da camera.
# O navegador puxa o ultimo frame daqui, sem acessar a ESP32-CAM diretamente.
camera_lock = threading.Lock()   # garante 1 acesso por vez a camera (sem instabilidade)
ultimo_frame_jpg = None          # ultimo frame JPEG (bytes) com sobreposicao
ultimo_frame_ts = 0              # timestamp do ultimo frame


# ===================== UTILITARIOS =====================
def garantir_estrutura():
    FOTOS_PATH.mkdir(exist_ok=True)
    if not LOGS_FILE.exists():
        LOGS_FILE.write_text("[]", encoding="utf-8")


def carregar_logs():
    try:
        return json.loads(LOGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def registrar_log(tipo, pessoa, detalhe=""):
    logs = carregar_logs()
    logs.insert(0, {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tipo": tipo,
        "pessoa": pessoa,
        "detalhe": detalhe,
    })
    logs = logs[:500]
    LOGS_FILE.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")


def listar_pessoas():
    pessoas = []
    if not FOTOS_PATH.exists():
        return pessoas
    for pasta in sorted(FOTOS_PATH.iterdir()):
        if pasta.is_dir():
            fotos = [f for f in pasta.iterdir()
                     if f.suffix.lower() in UPLOAD_EXTENSOES and not f.name.endswith("Identifier")]
            pessoas.append({
                "nome": pasta.name,
                "num_fotos": len(fotos),
                "fotos": [f.name for f in fotos],
            })
    return pessoas


def contar_encodings():
    if not ENCODINGS_FILE.exists():
        return 0, []
    try:
        with open(ENCODINGS_FILE, "rb") as f:
            data = pickle.load(f)
        nomes = list(set(data.get("names", [])))
        return len(data.get("encodings", [])), nomes
    except Exception:
        return 0, []


# ===================== MQTT (PIR) =====================
def iniciar_mqtt():
    """Assina o topico de presenca publicado pela ESP32 DevKit (PIR).
    Usa reconexao automatica: se o broker nao estiver no ar, tenta de novo
    em background. Se cair e voltar, reconecta sozinho sem reiniciar o painel."""
    global mqtt_client
    try:
        import paho.mqtt.client as mqtt

        def on_connect(client, userdata, flags, reason_code, properties=None):
            # Compativel com paho v1 e v2
            rc = reason_code if isinstance(reason_code, int) else reason_code.value
            if rc == 0:
                estado["pir_conectado"] = True
                client.subscribe(TOPICO_PRESENCA)
                print(f"[MQTT] Conectado - assinando {TOPICO_PRESENCA}")
            else:
                estado["pir_conectado"] = False
                print(f"[MQTT] Falha na conexao rc={rc}")

        def on_disconnect(client, userdata, flags=None, reason_code=None, properties=None):
            estado["pir_conectado"] = False
            print("[MQTT] Desconectado - tentando reconectar...")

        def on_message(client, userdata, msg):
            if msg.topic == TOPICO_PRESENCA:
                estado["ultima_presenca"] = time.time()
                print("[MQTT] Presenca detectada (PIR)")

        # Tenta paho v2 primeiro, cai pra v1 se nao tiver
        try:
            mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        except AttributeError:
            mqtt_client = mqtt.Client()

        mqtt_client.on_connect    = on_connect
        mqtt_client.on_disconnect = on_disconnect
        mqtt_client.on_message    = on_message

        # Reconexao automatica: tenta a cada 5s se cair
        mqtt_client.reconnect_delay_set(min_delay=3, max_delay=30)

        # Conecta sem bloquear (nao depende da ordem de inicializacao)
        mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        print(f"[MQTT] Conectando a {MQTT_BROKER}:{MQTT_PORT} (reconexao automatica ativa)")
        return True
    except Exception as e:
        print(f"[MQTT] Erro ao iniciar: {e}")
        estado["pir_conectado"] = False
        return False


def presenca_recente():
    """True se houve presenca detectada dentro da janela configurada."""
    return (time.time() - estado["ultima_presenca"]) <= JANELA_PRESENCA


# ===================== TUYA =====================
def conectar_tuya():
    global tuya_api
    try:
        from tuya_connector import TuyaOpenAPI
        tuya_api = TuyaOpenAPI(TUYA_API_ENDPOINT, TUYA_ACCESS_ID, TUYA_ACCESS_KEY)
        tuya_api.connect()
        estado["tuya_conectado"] = True
        return True
    except Exception as e:
        print(f"Erro ao conectar Tuya: {e}")
        estado["tuya_conectado"] = False
        return False


def abrir_fechadura():
    """Abre a fechadura via Tuya Cloud (ticket + door-operate)"""
    global tuya_api
    if tuya_api is None:
        if not conectar_tuya():
            return False, "Nao foi possivel conectar ao Tuya"
    try:
        ticket = tuya_api.post(f"/v1.0/smart-lock/devices/{TUYA_DEVICE_ID}/password-ticket")
        if not ticket.get("success"):
            return False, f"Erro no ticket: {ticket.get('msg', '?')}"
        ticket_id = ticket["result"]["ticket_id"]
        resp = tuya_api.post(
            f"/v1.0/smart-lock/devices/{TUYA_DEVICE_ID}/password-free/door-operate",
            {"ticket_id": ticket_id, "open": True},
        )
        if resp.get("success"):
            return True, "Fechadura aberta"
        return False, f"Erro ao abrir: {resp.get('msg', '?')}"
    except Exception as e:
        return False, str(e)


def fechar_fechadura():
    """Fecha a fechadura via Tuya Cloud"""
    global tuya_api
    if tuya_api is None:
        if not conectar_tuya():
            return False, "Nao foi possivel conectar ao Tuya"
    try:
        ticket = tuya_api.post(f"/v1.0/smart-lock/devices/{TUYA_DEVICE_ID}/password-ticket")
        if not ticket.get("success"):
            return False, f"Erro no ticket: {ticket.get('msg', '?')}"
        ticket_id = ticket["result"]["ticket_id"]
        resp = tuya_api.post(
            f"/v1.0/smart-lock/devices/{TUYA_DEVICE_ID}/password-free/door-operate",
            {"ticket_id": ticket_id, "open": False},
        )
        if resp.get("success"):
            return True, "Fechadura fechada"
        return False, f"Erro ao fechar: {resp.get('msg', '?')}"
    except Exception as e:
        return False, str(e)


def status_fechadura():
    global tuya_api
    if tuya_api is None:
        if not conectar_tuya():
            return {"online": False, "bateria": None}
    try:
        info = tuya_api.get(f"/v1.0/devices/{TUYA_DEVICE_ID}")
        if info.get("success"):
            r = info["result"]
            bateria = None
            for st in r.get("status", []):
                if st["code"] == "residual_electricity":
                    bateria = st["value"]
            return {"online": r.get("online", False), "bateria": bateria}
    except Exception as e:
        print(f"Erro status fechadura: {e}")
    return {"online": False, "bateria": None}


# ===================== MONITORAMENTO =====================
def checar_camera():
    try:
        r = requests.get(URL_CAPTURA, timeout=4)
        return r.status_code == 200
    except Exception:
        return False


def checar_mosquitto():
    try:
        s = socket.create_connection((MQTT_BROKER, MQTT_PORT), timeout=3)
        s.close()
        return True
    except Exception:
        return False


# ===================== RECONHECIMENTO (THREAD) =====================
def qualidade_ok(img):
    """Retorna (ok, motivo). Rejeita imagens borradas ou mal iluminadas."""
    import cv2
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    nitidez = cv2.Laplacian(cinza, cv2.CV_64F).var()
    brilho = float(cinza.mean())
    if nitidez < NITIDEZ_MINIMA:
        return False, f"imagem borrada (nitidez={nitidez:.0f})"
    if brilho < BRILHO_MINIMO:
        return False, f"imagem escura (brilho={brilho:.0f})"
    if brilho > BRILHO_MAXIMO:
        return False, f"imagem estourada (brilho={brilho:.0f})"
    return True, ""


def detectar_rosto(img):
    """Retorna True se ha pelo menos um rosto detectavel na imagem.
    Usa o detector Haar Cascade do OpenCV — leve e rapido.
    Se nao detectar rosto, a captura e descartada antes do DeepFace,
    evitando reconhecimentos falsos com frames vazios ou de fundo."""
    import cv2
    cinza = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    rostos = detector.detectMultiScale(cinza, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))
    return len(rostos) > 0


def distancia_media_por_pessoa(emb, enc_por_pessoa):
    """Calcula a distancia media do embedding para cada pessoa.
    Retorna (melhor_pessoa, menor_distancia_media)."""
    melhor_pessoa, melhor_dist = None, float("inf")
    for pessoa, encs in enc_por_pessoa.items():
        dists = [sum((a - b) ** 2 for a, b in zip(emb, e)) ** 0.5 for e in encs]
        media = sum(dists) / len(dists)
        if media < melhor_dist:
            melhor_dist = media
            melhor_pessoa = pessoa
    return melhor_pessoa, melhor_dist


def gerar_frame_overlay(img, texto, cor_bgr):
    """Desenha uma faixa com texto sobre a imagem e retorna JPEG (bytes).
    O desenho e feito no servidor (nao na camera), entao nao gera carga na ESP32."""
    import cv2
    out = img.copy()
    h, w = out.shape[:2]
    # faixa inferior semitransparente
    overlay = out.copy()
    cv2.rectangle(overlay, (0, h - 44), (w, h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, out, 0.45, 0, out)
    cv2.putText(out, texto, (12, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.7, cor_bgr, 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", out, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else None


def loop_reconhecimento():
    import cv2
    import numpy as np
    from deepface import DeepFace

    ultima_autorizacao = 0

    if not ENCODINGS_FILE.exists():
        registrar_log("erro", "-", "encodings.pkl nao encontrado")
        estado["reconhecimento_ativo"] = False
        return

    with open(ENCODINGS_FILE, "rb") as f:
        data = pickle.load(f)
        known_encodings = data["encodings"]
        known_names = data["names"]

    # Agrupa os encodings por pessoa (para a media por pessoa)
    enc_por_pessoa = {}
    for nome, enc in zip(known_names, known_encodings):
        enc_por_pessoa.setdefault(nome, []).append(enc)

    conectar_tuya()

    while estado["reconhecimento_ativo"]:
        time.sleep(1)

        # anti-spam: 10s entre aberturas
        if time.time() - ultima_autorizacao < 10:
            continue

        # ---- Gatilho por PIR ----
        if USAR_PIR and estado["pir_conectado"]:
            if not presenca_recente():
                continue
        else:
            time.sleep(3)  # modo continuo: espaca as capturas

        # ---- Votacao por multiplas capturas ----
        votos = {}          # pessoa -> nº de capturas que a reconheceram
        dists_pessoa = {}   # pessoa -> lista de distancias (para log)
        capturas_validas = 0
        ultima_img = None   # guarda o ultimo frame para o visualizador

        for n in range(N_CAPTURAS):
            # Captura (com lock: a camera atende um acesso por vez)
            try:
                with camera_lock:
                    resp = requests.get(URL_CAPTURA, timeout=15)
                if resp.status_code != 200:
                    continue
                img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
            except Exception:
                continue
            if img is None:
                continue
            ultima_img = img

            # Filtro de qualidade
            ok_q, motivo = qualidade_ok(img)
            if not ok_q:
                continue

            # Deteccao de rosto obrigatoria
            # Se nao houver rosto no frame, descarta ANTES de chamar o DeepFace.
            # Isso evita reconhecimentos falsos quando a pessoa saiu da frente da camera.
            if not detectar_rosto(img):
                print("[RECONHECIMENTO] Nenhum rosto detectado no frame — descartando")
                continue

            # Embedding
            try:
                cv2.imwrite(str(BASE_DIR / "temp_captura.jpg"), img)
                emb = DeepFace.represent(
                    str(BASE_DIR / "temp_captura.jpg"),
                    model_name="Facenet512",
                    enforce_detection=False,
                )[0]["embedding"]
            except Exception:
                continue

            capturas_validas += 1

            # Distancia media por pessoa
            pessoa, dist = distancia_media_por_pessoa(emb, enc_por_pessoa)
            dists_pessoa.setdefault(pessoa, []).append(dist)
            if dist < TOLERANCE:
                votos[pessoa] = votos.get(pessoa, 0) + 1

            time.sleep(0.3)  # pequeno intervalo entre capturas

        # ---- Decisao ----
        if capturas_validas == 0:
            registrar_log("erro", "-", "nenhuma captura com qualidade suficiente")
            continue

        # Pessoa mais votada
        if votos:
            pessoa_eleita = max(votos, key=votos.get)
            n_votos = votos[pessoa_eleita]
            media_dist = sum(dists_pessoa[pessoa_eleita]) / len(dists_pessoa[pessoa_eleita])
        else:
            pessoa_eleita, n_votos, media_dist = None, 0, None

        if pessoa_eleita and n_votos >= VOTOS_MINIMOS:
            ok, msg = abrir_fechadura()
            if ok:
                registrar_log("autorizado", pessoa_eleita,
                              f"votos={n_votos}/{capturas_validas} dist_media={media_dist:.2f}")
                estado["ultimo_acesso"] = {
                    "pessoa": pessoa_eleita,
                    "hora": datetime.now().strftime("%H:%M:%S"),
                }
                ultima_autorizacao = time.time()
                texto_overlay, cor_overlay = f"{pessoa_eleita} - AUTORIZADO", (0, 255, 0)
            else:
                registrar_log("erro", pessoa_eleita, msg)
                texto_overlay, cor_overlay = f"{pessoa_eleita} - ERRO FECHADURA", (0, 165, 255)
        else:
            detalhe = f"votos insuficientes ({n_votos}/{capturas_validas})"
            if media_dist is not None:
                detalhe += f" dist_media={media_dist:.2f}"
            registrar_log("negado", "desconhecido", detalhe)
            texto_overlay, cor_overlay = "DESCONHECIDO - NEGADO", (0, 0, 255)

        # Atualiza o frame do visualizador com o resultado sobreposto
        if ultima_img is not None:
            global ultimo_frame_jpg, ultimo_frame_ts
            frame = gerar_frame_overlay(ultima_img, texto_overlay, cor_overlay)
            if frame:
                ultimo_frame_jpg = frame
                ultimo_frame_ts = time.time()


# ===================== ROTAS =====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    agora = time.time()

    if agora - cache_status["camera"]["ultima_checagem"] > 8:
        cache_status["camera"]["online"] = checar_camera()
        cache_status["camera"]["ultima_checagem"] = agora

    if agora - cache_status["fechadura"]["ultima_checagem"] > 30:
        st = status_fechadura()
        cache_status["fechadura"]["online"] = st["online"]
        cache_status["fechadura"]["bateria"] = st["bateria"]
        cache_status["fechadura"]["ultima_checagem"] = agora

    if agora - cache_status["mosquitto"]["ultima_checagem"] > 15:
        cache_status["mosquitto"]["online"] = checar_mosquitto()
        cache_status["mosquitto"]["ultima_checagem"] = agora

    num_enc, nomes = contar_encodings()
    pessoas = listar_pessoas()
    logs = carregar_logs()

    hoje = datetime.now().strftime("%Y-%m-%d")
    acessos_hoje = sum(1 for l in logs if l["tipo"] == "autorizado" and l["timestamp"].startswith(hoje))
    negados_hoje = sum(1 for l in logs if l["tipo"] == "negado" and l["timestamp"].startswith(hoje))

    return jsonify({
        "camera": {"online": cache_status["camera"]["online"], "ip": IP_CAMERA},
        "fechadura": {
            "online": cache_status["fechadura"]["online"],
            "bateria": cache_status["fechadura"]["bateria"],
        },
        "mosquitto": {"online": cache_status["mosquitto"]["online"], "broker": MQTT_BROKER},
        "pir": {
            "conectado": estado["pir_conectado"],
            "presenca": presenca_recente(),
            "usar_pir": USAR_PIR,
        },
        "tuya_conectado": estado["tuya_conectado"],
        "reconhecimento_ativo": estado["reconhecimento_ativo"],
        "ultimo_acesso": estado["ultimo_acesso"],
        "total_pessoas": len(pessoas),
        "total_rostos": num_enc,
        "nomes_treinados": nomes,
        "acessos_hoje": acessos_hoje,
        "negados_hoje": negados_hoje,
        "pessoas": pessoas,
    })


@app.route("/api/logs")
def api_logs():
    return jsonify(carregar_logs())


@app.route("/api/upload", methods=["POST"])
def api_upload():
    nome = request.form.get("nome", "").strip()
    if not nome:
        return jsonify({"ok": False, "msg": "Nome obrigatorio"}), 400

    pasta = pasta_pessoa(nome)
    if pasta is None:
        return jsonify({"ok": False, "msg": "Nome invalido"}), 400
    pasta.mkdir(exist_ok=True)

    arquivos = request.files.getlist("fotos")
    salvos = 0
    for arq in arquivos:
        if arq.filename == "":
            continue
        nome_arquivo = secure_filename(arq.filename)
        ext = Path(nome_arquivo).suffix.lower()
        if nome_arquivo and ext in UPLOAD_EXTENSOES:
            arq.save(str(pasta / nome_arquivo))
            salvos += 1

    return jsonify({"ok": True, "msg": f"{salvos} foto(s) salva(s) para {nome}"})


@app.route("/api/remover_pessoa", methods=["POST"])
def api_remover_pessoa():
    nome = request.json.get("nome", "").strip()
    pasta = pasta_pessoa(nome)
    if pasta is not None and pasta.exists() and pasta.is_dir():
        import shutil
        shutil.rmtree(pasta)
        return jsonify({"ok": True, "msg": f"{nome} removido"})
    return jsonify({"ok": False, "msg": "Pessoa nao encontrada"}), 404


@app.route("/api/treinar", methods=["POST"])
def api_treinar():
    try:
        from deepface import DeepFace
        known_encodings, known_names = [], []
        detalhes = []

        for pessoa in os.listdir(FOTOS_PATH):
            pessoa_path = FOTOS_PATH / pessoa
            if not pessoa_path.is_dir():
                continue
            for foto in os.listdir(pessoa_path):
                if foto.endswith("Identifier") or foto.startswith("."):
                    continue
                if Path(foto).suffix.lower() not in UPLOAD_EXTENSOES:
                    continue
                try:
                    emb = DeepFace.represent(
                        str(pessoa_path / foto),
                        model_name="Facenet512",
                        enforce_detection=False,
                    )[0]["embedding"]
                    known_encodings.append(emb)
                    known_names.append(pessoa)
                    detalhes.append(f"OK: {pessoa}/{foto}")
                except Exception as e:
                    detalhes.append(f"ERRO: {pessoa}/{foto} - {e}")

        with open(ENCODINGS_FILE, "wb") as f:
            pickle.dump({"encodings": known_encodings, "names": known_names}, f)

        registrar_log("sistema", "-", f"Treinamento: {len(known_encodings)} rostos")
        return jsonify({
            "ok": True,
            "total": len(known_encodings),
            "pessoas": list(set(known_names)),
            "detalhes": detalhes,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/abrir", methods=["POST"])
def api_abrir():
    """Abertura manual da fechadura"""
    ok, msg = abrir_fechadura()
    registrar_log("manual" if ok else "erro", "Operador", f"ABRIR: {msg}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/fechar", methods=["POST"])
def api_fechar():
    """Fechamento manual da fechadura"""
    ok, msg = fechar_fechadura()
    registrar_log("manual" if ok else "erro", "Operador", f"FECHAR: {msg}")
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/reconhecimento", methods=["POST"])
def api_reconhecimento():
    acao = request.json.get("acao")
    if acao == "iniciar":
        if not estado["reconhecimento_ativo"]:
            estado["reconhecimento_ativo"] = True
            t = threading.Thread(target=loop_reconhecimento, daemon=True)
            estado["thread_reconhecimento"] = t
            t.start()
            registrar_log("sistema", "-", "Reconhecimento iniciado")
        return jsonify({"ok": True, "ativo": True})
    elif acao == "parar":
        estado["reconhecimento_ativo"] = False
        registrar_log("sistema", "-", "Reconhecimento parado")
        return jsonify({"ok": True, "ativo": False})
    return jsonify({"ok": False, "msg": "Acao invalida"}), 400


@app.route("/api/limpar_logs", methods=["POST"])
def api_limpar_logs():
    LOGS_FILE.write_text("[]", encoding="utf-8")
    return jsonify({"ok": True})


@app.route("/foto/<pessoa>/<arquivo>")
def servir_foto(pessoa, arquivo):
    pasta = pasta_pessoa(pessoa)
    if pasta is None:
        return ("", 404)
    return send_from_directory(str(pasta), arquivo)


@app.route("/camera_frame.jpg")
def camera_frame():
    """Serve o ultimo frame da camera para visualizacao.
    - Com reconhecimento ATIVO: reaproveita o frame ja capturado (carga zero na camera).
    - Sem reconhecimento: captura sob demanda (~1 fps), com lock, como cliente unico."""
    global ultimo_frame_jpg, ultimo_frame_ts
    import cv2
    import numpy as np

    # Reconhecimento ativo -> usa o frame que o proprio reconhecimento gerou
    if estado["reconhecimento_ativo"] and ultimo_frame_jpg is not None:
        return Response(ultimo_frame_jpg, mimetype="image/jpeg")

    # Caso contrario, captura sob demanda com throttle de ~1s
    agora = time.time()
    if ultimo_frame_jpg is None or (agora - ultimo_frame_ts) > 1.0:
        try:
            with camera_lock:
                resp = requests.get(URL_CAPTURA, timeout=8)
            if resp.status_code == 200:
                img = cv2.imdecode(np.frombuffer(resp.content, np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    frame = gerar_frame_overlay(img, "AO VIVO", (255, 255, 255))
                    if frame:
                        ultimo_frame_jpg = frame
                        ultimo_frame_ts = agora
        except Exception:
            pass

    if ultimo_frame_jpg is not None:
        return Response(ultimo_frame_jpg, mimetype="image/jpeg")
    return ("", 503)


@app.route("/stream_proxy")
def stream_proxy():
    return redirect(URL_STREAM)


# ===================== MAIN =====================
if __name__ == "__main__":
    garantir_estrutura()
    print("=" * 55)
    print("  PAINEL DE CONTROLE DE ACESSO (com PIR via MQTT)")
    print(f"  Acesse: http://localhost:{PAINEL_PORTA}")
    print("=" * 55)
    if not PAINEL_SENHA:
        print("  [AVISO] PAINEL_SENHA nao definida: o painel esta SEM login.")
        print("          Qualquer pessoa na rede pode abrir a fechadura.")
    conectar_tuya()
    iniciar_mqtt()   # assina o topico de presenca do PIR
    app.run(host=PAINEL_HOST, port=PAINEL_PORTA, debug=False, threaded=True)