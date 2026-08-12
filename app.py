import json
import subprocess
import threading
import time

from flask import Flask, request, jsonify
from flask_cors import CORS

# ============================================
# CONFIGURACION - EDITA ESTO
# ============================================
# Tu(s) dominio(s) de Hostinger. Sin esto el navegador bloquea las
# respuestas por CORS aunque el servidor si responda bien.
ALLOWED_ORIGINS = [
    "vakodesign.github.io",   # cambia esto por tu dominio real de Hostinger
]

# En Render, yt-dlp se instala via pip (esta en requirements.txt), asi
# que se llama como comando de sistema, no como ruta a un .exe.
YTDLP_CMD = "yt-dlp"

FREE_DAILY_LIMIT = 10
# ============================================

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})

# NOTA sobre el filesystem de Render (plan Free): se borra cada vez que
# el servicio duerme y despierta. Por eso los limites por IP y el
# "premium" viven en memoria (dict), no en archivos .json como en tu
# version original — igual se hubieran perdido con cada reinicio.
_limits = {}
_limits_lock = threading.Lock()
_premium_ips = set()  # agrega IPs aqui a mano, o cambia esto por una lista real

QUALITY_MAP = {
    "best": "bestvideo+bestaudio/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/bestvideo+bestaudio/best",
}

# Solo una consulta a yt-dlp a la vez, igual que tu download_lock original,
# para no mandarle rafagas de peticiones a YouTube que parezcan bot.
resolve_lock = threading.Lock()


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "desconocida"


def check_and_register(ip):
    """Igual logica que tu version original (limite diario por IP),
    pero guardada en memoria en vez de download_limits.json."""
    if ip in _premium_ips:
        return True, None

    today = time.strftime("%Y-%m-%d")
    with _limits_lock:
        entry = _limits.get(ip, {})
        if entry.get("date") != today:
            entry = {"date": today, "count": 0}
        if entry["count"] >= FREE_DAILY_LIMIT:
            _limits[ip] = entry
            return False, 0
        entry["count"] += 1
        _limits[ip] = entry
        return True, FREE_DAILY_LIMIT - entry["count"]


@app.route("/health")
def health():
    # Ruta liviana para el ping de UptimeRobot: no toca yt-dlp.
    return jsonify({"ok": True})


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    """Reemplaza a tu /api/download. Ya NO descarga el video en el
    servidor: solo le pregunta a yt-dlp por la metadata (-j) y devuelve
    la(s) URL(s) directas. El navegador del usuario se encarga de bajar
    el archivo y, si hace falta, convertirlo/unirlo con ffmpeg.wasm."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip().strip('"')
    mode = data.get("mode", "video")  # "video" | "audio"
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"ok": False, "error": "Falta la URL"}), 400

    ip = get_client_ip()
    allowed, remaining = check_and_register(ip)
    if not allowed:
        return jsonify({
            "ok": False,
            "error": f"Llegaste al limite de {FREE_DAILY_LIMIT} descargas gratis por hoy. Suscribete para descargas ilimitadas.",
            "limit_reached": True,
        }), 429

    fmt = "bestaudio/best" if mode == "audio" else QUALITY_MAP.get(quality, QUALITY_MAP["best"])

    cmd = [
        YTDLP_CMD,
        "-f", fmt,
        "--no-playlist",
        "--sleep-requests", "1",
        # Le pedimos a yt-dlp que se identifique como la app de Android
        # de YouTube en vez de un navegador. Esto a veces evita el
        # bloqueo "Sign in to confirm you're not a bot" sin necesidad
        # de usar cookies de ninguna cuenta. No es 100% garantizado:
        # YouTube cambia esto seguido, puede dejar de funcionar.
        "--extractor-args", "youtube:player_client=android",
        "-j",
        url,
    ]

    with resolve_lock:
        try:
            # 90s de margen: resolver formato con yt-dlp a veces tarda
            # mas de lo que uno esperaria, sobre todo si YouTube esta
            # lento respondiendo o hay reintentos internos.
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            return jsonify({"ok": False, "error": "yt-dlp tardo demasiado en responder"}), 504

    if result.returncode != 0:
        return jsonify({"ok": False, "error": result.stderr[-1500:] or "Error de yt-dlp"}), 500

    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "No se pudo interpretar la respuesta de yt-dlp"}), 500

    response = {
        "ok": True,
        "title": info.get("title"),
        "ext": info.get("ext"),
        "duration": info.get("duration"),
    }

    # Caso simple: un solo stream ya trae video+audio juntos (o es audio
    # solo). El navegador puede bajarlo directo, sin ffmpeg.wasm.
    if info.get("url"):
        response["direct_url"] = info["url"]
        response["needs_mux"] = False
    # Caso YouTube tipico en calidades altas: video y audio vienen en
    # streams separados. El navegador va a necesitar unirlos con
    # ffmpeg.wasm (o convertir, si el modo es audio a otro formato).
    elif info.get("requested_formats"):
        response["streams"] = [
            {
                "url": f.get("url"),
                "ext": f.get("ext"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            }
            for f in info["requested_formats"]
        ]
        response["needs_mux"] = True
    else:
        return jsonify({"ok": False, "error": "yt-dlp no devolvio una URL usable"}), 500

    return jsonify(response)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
