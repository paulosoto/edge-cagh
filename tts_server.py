# PauloSt Texto-Voz EDGE-TTS
from flask import Flask, request, send_file, abort, jsonify
import edge_tts
import asyncio
import os
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

CACHE_DIR = os.environ.get("CACHE_DIR", "./tts_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

VOICE = os.environ.get("VOICE", "es-PE-AlexNeural")
RATE = os.environ.get("RATE", "+2%")
VOLUME = os.environ.get("VOLUME", "+5%")

# Cuántas síntesis realmente permites en paralelo
MAX_SYNTH_WORKERS = int(os.environ.get("MAX_SYNTH_WORKERS", "4"))

# Tiempo de vida del cache en segundos (5 min = 300)
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))

# Cada cuánto correr limpieza automática
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "60"))

# Pool para generar audios
executor = ThreadPoolExecutor(max_workers=MAX_SYNTH_WORKERS)

# Protección para trabajos y limpieza
jobs_lock = threading.Lock()
cleanup_lock = threading.Lock()

# key -> Future
in_progress = {}

# Última limpieza ejecutada
last_cleanup_ts = 0.0


def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def cache_key(text: str) -> str:
    base = f"{VOICE}|{RATE}|{VOLUME}|{normalize_text(text)}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def mp3_path_for(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.mp3")


def is_file_fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) <= 0:
        return False

    age = time.time() - os.path.getmtime(path)
    return age <= CACHE_TTL_SECONDS


def safe_remove(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup_expired_files(force: bool = False):
    global last_cleanup_ts

    now = time.time()

    if not force and (now - last_cleanup_ts) < CLEANUP_INTERVAL_SECONDS:
        return

    with cleanup_lock:
        now = time.time()
        if not force and (now - last_cleanup_ts) < CLEANUP_INTERVAL_SECONDS:
            return

        try:
            for name in os.listdir(CACHE_DIR):
                path = os.path.join(CACHE_DIR, name)

                if not os.path.isfile(path):
                    continue

                # Limpia temporales huérfanos
                if name.endswith(".tmp"):
                    age = now - os.path.getmtime(path)
                    if age > 120:
                        safe_remove(path)
                    continue

                # Limpia mp3 expirados
                if name.endswith(".mp3"):
                    age = now - os.path.getmtime(path)
                    if age > CACHE_TTL_SECONDS:
                        safe_remove(path)
        finally:
            last_cleanup_ts = now


async def synth_to_mp3_async(text: str, out_path: str):
    communicate = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate=RATE,
        volume=VOLUME
    )
    await communicate.save(out_path)


def synth_to_mp3_sync(text: str, final_path: str):
    temp_path = final_path + ".tmp"

    # Limpieza por si quedó algo viejo
    safe_remove(temp_path)

    asyncio.run(synth_to_mp3_async(text, temp_path))

    # Rename atómico
    os.replace(temp_path, final_path)


def ensure_audio(text: str) -> str:
    cleanup_expired_files()

    text = normalize_text(text)
    if not text:
        raise ValueError("Missing text")

    key = cache_key(text)
    final_path = mp3_path_for(key)

    # Si ya existe y sigue vigente, salir rápido
    if is_file_fresh(final_path):
        return final_path

    # Si existe pero expiró o está dañado, eliminarlo
    if os.path.exists(final_path) and not is_file_fresh(final_path):
        safe_remove(final_path)

    with jobs_lock:
        # Revisar otra vez dentro del lock
        if is_file_fresh(final_path):
            return final_path

        # Si existe expirado dentro del lock, eliminar
        if os.path.exists(final_path) and not is_file_fresh(final_path):
            safe_remove(final_path)

        future = in_progress.get(key)

        # Si nadie lo está generando, crear trabajo
        if future is None:
            future = executor.submit(synth_to_mp3_sync, text, final_path)
            in_progress[key] = future

    try:
        # Todos esperan el mismo future
        future.result(timeout=20)

        if not is_file_fresh(final_path):
            raise RuntimeError("Audio was not generated correctly")

        return final_path

    finally:
        with jobs_lock:
            current = in_progress.get(key)
            if current is future and future.done():
                in_progress.pop(key, None)


@app.get("/tts")
def tts():
    text = request.args.get("text", "")

    if not text.strip():
        abort(400, "Missing text")

    try:
        out_path = ensure_audio(text)
        return send_file(out_path, mimetype="audio/mpeg", as_attachment=False)
    except Exception as e:
        app.logger.exception("TTS generation failed")
        abort(500, f"TTS error: {str(e)}")


@app.get("/health")
def health():
    cleanup_expired_files()

    try:
        cached_files = [
            f for f in os.listdir(CACHE_DIR)
            if f.endswith(".mp3") and os.path.isfile(os.path.join(CACHE_DIR, f))
        ]
    except Exception:
        cached_files = []

    return jsonify({
        "ok": True,
        "voice": VOICE,
        "rate": RATE,
        "volume": VOLUME,
        "cache_dir": os.path.abspath(CACHE_DIR),
        "max_synth_workers": MAX_SYNTH_WORKERS,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "cleanup_interval_seconds": CLEANUP_INTERVAL_SECONDS,
        "jobs_in_progress": len(in_progress),
        "cached_files": len(cached_files)
    })


@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "message": "Edge TTS server running"
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5055"))
    app.run(host="0.0.0.0", port=port, threaded=True)