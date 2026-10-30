#!/usr/bin/env python
"""RoleChat 本地语音服务（Luke Liu）：全离线，只绑定 127.0.0.1。

- 识别(ASR)：SenseVoice int8（中/英/日/韩/粤），模型在 tools/voice/asr/
- 合成(TTS)：Kokoro v1.1 多语言 int8，中文男/女声，模型在 tools/voice/tts/
- 接口：
    GET  /capabilities                     -> {"available":true,"voices":[...]}
    POST /tts  {"text","sid","speed"}      -> audio/wav 字节
    POST /asr  (audio/wav 16k mono 字节)   -> {"text":"..."}

依赖：工作区 .venv 里的 sherpa-onnx（由 启动RoleChat.bat 用 venv Python 启动本脚本）。
"""
import glob
import io
import json
import logging
import os
import struct
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASR_DIR = os.path.join(ROOT, "tools", "voice", "asr")
TTS_DIR = os.path.join(ROOT, "tools", "voice", "tts")
PORT = int(os.environ.get("ROLECHAT_VOICE_PORT", "8790"))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              logging.FileHandler(os.path.join(ROOT, "data", "logs", "voice.log"),
                                                  encoding="utf-8", delay=True)])
log = logging.getLogger("voice")

try:
    import numpy as np
    import sherpa_onnx
except ImportError as e:  # 允许“模型缺失时优雅降级”，但缺库直接报清楚
    print("缺少依赖：请在项目 venv 中 `pip install sherpa-onnx`（%s）" % e)
    sys.exit(1)


def _find(patterns, base):
    for p in patterns:
        hits = sorted(glob.glob(os.path.join(base, p)))
        if hits:
            return hits[0]
    return None


# ---------------- ASR：SenseVoice ----------------
def build_asr():
    model = _find(["model.int8.onnx", "model.onnx"], ASR_DIR)
    tokens = _find(["tokens.txt"], ASR_DIR)
    if not model or not tokens:
        log.warning("ASR 模型缺失（tools/voice/asr），语音识别不可用")
        return None
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model, tokens=tokens, use_itn=True, language="zh")


# ---------------- TTS：Kokoro ----------------
# 音色编号由实测确定（tools/_probe_api2.py 全量扫描 103 个音色的基频 F0）：
#   sid=7  女声 F0≈220Hz（合成速度也最快）   sid=86 男声 F0≈121Hz
VOICE_CATALOG = [
    {"id": "female", "name": "女声", "gender": "female", "sid": 7},
    {"id": "male", "name": "男声", "gender": "male", "sid": 86},
]


def build_tts():
    model = _find(["model.int8.onnx", "model.onnx"], TTS_DIR)
    voices = _find(["voices-v1.1.bin", "voices*.bin"], TTS_DIR)
    tokens = _find(["tokens.txt"], TTS_DIR)
    if not model or not voices or not tokens:
        log.warning("TTS 模型缺失（tools/voice/tts），语音合成不可用")
        return None
    kw = {}
    espeak = _find(["espeak-ng-data"], TTS_DIR)
    if espeak and os.path.isdir(espeak):
        kw["data_dir"] = espeak
    dict_dir = _find(["dict"], TTS_DIR)
    if dict_dir and os.path.isdir(dict_dir):
        kw["dict_dir"] = dict_dir
    for lex in ("lexicon-zh.txt", "lexicon.txt"):
        p = os.path.join(TTS_DIR, lex)
        if os.path.exists(p):
            kw["lexicon"] = p
            break
    rule_fsts = ",".join(
        os.path.join(TTS_DIR, f) for f in ("phone-zh.fst", "date-zh.fst", "number-zh.fst")
        if os.path.exists(os.path.join(TTS_DIR, f)))
    cfg = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=model, voices=voices, tokens=tokens, **kw),
            num_threads=4),
        rule_fsts=rule_fsts,
        max_num_sentences=1,
        silence_scale=0.2)
    return sherpa_onnx.OfflineTts(cfg)


ASR = build_asr()
TTS = build_tts()
NUM_SPEAKERS = int(getattr(TTS, "num_speakers", 0) or 0) if TTS else 0
LOCK = threading.Lock()  # onnx 推理串行化，避免线程安全问题


def wav_bytes_from_float(samples, sample_rate):
    """float32 [-1,1] -> 16bit PCM WAV 字节。"""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def float_from_wav_bytes(raw):
    """WAV 字节 -> (采样率, float32)，只支持 PCM16 单/双声道。"""
    with wave.open(io.BytesIO(raw), "rb") as w:
        rate = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        if width != 2:
            raise ValueError("只支持 16bit PCM WAV")
        frames = w.readframes(w.getnframes())
    data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return rate, data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if urlparse_path(self.path) == "/capabilities":
            voices = [v for v in VOICE_CATALOG if NUM_SPEAKERS > v["sid"]]
            self._json({"available": bool(ASR or TTS), "asr": bool(ASR),
                        "tts": bool(TTS), "voices": voices})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse_path(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 20 * 1024 * 1024:
            self._json({"error": "请求体为空或过大"}, 400)
            return
        raw = self.rfile.read(length)
        if path == "/tts":
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._json({"error": "请求体必须是 JSON"}, 400)
                return
            text = str(data.get("text") or "").strip()[:1200]
            sid_name = str(data.get("sid") or "female")
            speed = float(data.get("speed") or 1.0)
            if not text:
                self._json({"error": "text 不能为空"}, 400)
                return
            if not TTS:
                self._json({"error": "TTS 模型未加载（检查 tools/voice/tts）"}, 503)
                return
            sid = next((v["sid"] for v in VOICE_CATALOG if v["id"] == sid_name),
                       VOICE_CATALOG[0]["sid"])
            try:
                with LOCK:
                    audio = TTS.generate(text, sid=sid, speed=speed)
                self._send(200, wav_bytes_from_float(audio.samples, audio.sample_rate), "audio/wav")
                log.info("TTS ok: sid=%s speed=%.1f chars=%d dur=%.1fs",
                         sid_name, speed, len(text), len(audio.samples) / audio.sample_rate)
            except Exception as e:  # noqa: BLE001
                log.exception("TTS 失败")
                self._json({"error": "合成失败：%s" % e}, 500)
        elif path == "/asr":
            if not ASR:
                self._json({"error": "ASR 模型未加载（检查 tools/voice/asr）"}, 503)
                return
            try:
                rate, samples = float_from_wav_bytes(raw)
            except Exception as e:  # noqa: BLE001
                self._json({"error": "音频格式错误：%s" % e}, 400)
                return
            try:
                with LOCK:
                    stream = ASR.create_stream()
                    stream.accept_waveform(rate, samples)
                    ASR.decode_stream(stream)
                text = (stream.result.text or "").strip()
                log.info("ASR ok: %d 秒音频 -> %s", len(samples) / rate, text[:80])
                self._json({"text": text})
            except Exception as e:  # noqa: BLE001
                log.exception("ASR 失败")
                self._json({"error": "识别失败：%s" % e}, 500)
        else:
            self._json({"error": "not found"}, 404)


def urlparse_path(p):
    return p.split("?")[0]


def main():
    os.makedirs(os.path.join(ROOT, "data", "logs"), exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("=" * 52)
    print("  RoleChat 本地语音服务 (Luke Liu)")
    print("  地址: http://127.0.0.1:%d" % PORT)
    print("  ASR: %s | TTS: %s | 音色: %s" % (
        "SenseVoice 已加载" if ASR else "未加载",
        "Kokoro 已加载" if TTS else "未加载",
        ", ".join("%s(sid=%d)" % (v["id"], v["sid"]) for v in VOICE_CATALOG
                  if NUM_SPEAKERS > v["sid"]) or "-"))
    print("  关闭此窗口（或 Ctrl+C）即退出。")
    print("=" * 52)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
