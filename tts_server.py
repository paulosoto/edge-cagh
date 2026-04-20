# PauloSt Texto-Voz EDGE-TTS

from flask import Flask, request, send_file, abort, jsonify
import edge_tts
import asyncio
import os
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

CACHE_DIR = "./tts_cache"
os.makedirs(CACHE_DIR, exist_ok=True)

VOICE = "es-PE-AlexNeural"
RATE = "+2%"
VOLUME = "+5%"

# Cuántas síntesis realmente permites en paralelo
MAX_SYNTH_WORKERS = 6

# Pool para generar audios
executor = ThreadPoolExecutor(max_workers=MAX_SYNTH_WORKERS)

# Protección para el diccionario de trabajos en curso
jobs_lock = threading.Lock()

# key -> Future
in_progress = {}

def normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())

def cache_key(text: str) -> str:
    base = f"{VOICE}|{RATE}|{VOLUME}|{normalize_text(text)}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def mp3_path_for(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.mp3")

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
    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass

    asyncio.run(synth_to_mp3_async(text, temp_path))

    # Rename atómico
    os.replace(temp_path, final_path)

def ensure_audio(text: str) -> str:
    text = normalize_text(text)
    if not text:
        raise ValueError("Missing text")

    key = cache_key(text)
    final_path = mp3_path_for(key)

    # Si ya existe, salir rápido
    if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
        return final_path

    with jobs_lock:
        # Revisar otra vez dentro del lock
        if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
            return final_path

        future = in_progress.get(key)

        # Si nadie lo está generando, crear trabajo
        if future is None:
            future = executor.submit(synth_to_mp3_sync, text, final_path)
            in_progress[key] = future

    try:
        # Todos esperan el mismo future
        future.result()

        if not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
            raise RuntimeError("Audio was not generated correctly")

        return final_path

    finally:
        # Solo limpiar si el future ya terminó y sigue siendo el actual
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
    return jsonify({
        "ok": True,
        "voice": VOICE,
        "rate": RATE,
        "volume": VOLUME,
        "cache_dir": os.path.abspath(CACHE_DIR),
        "max_synth_workers": MAX_SYNTH_WORKERS,
        "jobs_in_progress": len(in_progress)
    })

if __name__ == "__main__":
    # Solo desarrollo local
    app.run(host="0.0.0.0", port=5055, threaded=True)
