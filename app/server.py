"""本地 HTTP 服务：静态界面 + JSON API。仅绑定 127.0.0.1，不对外网开放。"""
import json
import logging
import os
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import llm
from .brain import Brain
from .store import Store

APP_NAME = "RoleChat"
VERSION = "1.1.0"
MAX_BODY = 20 * 1024 * 1024  # 语音上传（WAV）需要比纯 JSON 更大的限额

logger = logging.getLogger("rolechat")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

DEFAULT_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 8765, "open_browser": True},
    "model": {
        "provider": "mock",
        "base_url": "",
        "model": "",
        "api_key": "",
        "context_chars": 6000,
        "reply_max_chars": 1200,
        "max_tokens": 4096,
        "temperature": 0.7,
        "timeout_sec": 120,
    },
    "memory": {
        "max_memories_in_prompt": 5,
        "min_score": 0.15,
        "auto_extract_every": 4,
        "dedup_threshold": 0.55,
    },
    "voice": {
        "tts_voice": "female",   # 女声(sid=7)；男声 male(sid=86)
        "auto_tts": False,
        "speed": 1.0,
        "server": "http://127.0.0.1:8790",  # 本地语音服务（sherpa-onnx，由 启动RoleChat.bat 拉起）
    },
}

VOICE_SERVER_DOWN_HINT = (
    "语音服务未启动：请用项目根目录的「启动RoleChat.bat」启动"
    "（它会一并拉起本地语音识别/合成服务）"
)


def _proxy_http(method, url, body=None, headers=None, timeout=120):
    """把请求转发给本地语音服务；返回 (状态码, 字节, 响应头)。"""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = _deep_merge(cfg, json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("配置文件读取失败，使用默认配置: %s", e)
    return cfg


def save_config(path, cfg):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _mask_config(cfg):
    """给界面看的配置：密钥只提示是否已设置，不回传。"""
    out = json.loads(json.dumps(cfg))
    key = out.get("model", {}).get("api_key", "")
    out["model"]["api_key_set"] = bool(key)
    out["model"]["api_key_hint"] = ("…" + key[-4:]) if len(key) > 4 else ("已设置" if key else "")
    out["model"].pop("api_key", None)
    return out


def _make_handler(store, cfg, cfg_path, static_root):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RoleChat/" + VERSION

        def log_message(self, fmt, *args):  # 关闭默认 stderr 访问日志
            pass

        # ---------- 基础工具 ----------
        def _send(self, status, obj, content_type="application/json; charset=utf-8"):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8") \
                if not isinstance(obj, bytes) else obj
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionAbortedError, BrokenPipeError):
                pass
            logger.info("%s %s -> %s (%db)", self.command, self.path.split("?")[0], status, len(body))

        def _read_json(self):
            raw = self._read_bytes()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        def _read_bytes(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                raise ValueError("请求体为空或超过 %d 字节" % MAX_BODY)
            return self.rfile.read(length)

        def _brain(self):
            return Brain(store, cfg)

        # ---------- 路由 ----------
        def do_GET(self):
            try:
                self._route_get()
            except Exception as e:  # noqa: BLE001
                logger.exception("GET %s 失败", self.path)
                self._send(500, {"error": str(e)[:200]})

        def do_POST(self):
            try:
                self._route_post()
            except llm.LLMError as e:
                self._send(502, {"error": "模型调用失败：%s" % e})
            except Exception as e:  # noqa: BLE001
                logger.exception("POST %s 失败", self.path)
                self._send(500, {"error": str(e)[:200]})

        def do_PUT(self):
            try:
                self._route_put()
            except Exception as e:  # noqa: BLE001
                logger.exception("PUT %s 失败", self.path)
                self._send(500, {"error": str(e)[:200]})

        def do_DELETE(self):
            try:
                self._route_delete()
            except Exception as e:  # noqa: BLE001
                logger.exception("DELETE %s 失败", self.path)
                self._send(500, {"error": str(e)[:200]})

        def _route_get(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
                return
            if path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            qs = parse_qs(parsed.query)
            if path == "/api/health":
                m = cfg.get("model", {})
                self._send(200, {"ok": True, "app": APP_NAME, "version": VERSION,
                                 "provider": m.get("provider"), "model": m.get("model") or "",
                                 "data_dir": store.root})
            elif path == "/api/voice":  # 语音服务能力探测（给界面做优雅降级）
                vurl = cfg.get("voice", {}).get("server", "http://127.0.0.1:8790")
                try:
                    _, body, _ = _proxy_http("GET", vurl + "/capabilities", timeout=5)
                    self._send(200, json.loads(body.decode("utf-8")))
                except Exception as e:  # noqa: BLE001
                    self._send(200, {"available": False, "voices": [],
                                     "error": VOICE_SERVER_DOWN_HINT + "（" + str(e)[:80] + "）"})
            elif path == "/api/characters":
                self._send(200, {"characters": store.list_characters()})
            elif path == "/api/sessions":
                self._send(200, {"sessions": store.list_sessions(qs.get("character_id", [None])[0])})
            elif path.startswith("/api/sessions/"):
                sid = path.split("/")[3]
                sess = store.get_session(sid)
                char = store.get_character(sess["character_id"])
                self._send(200, {"session": sess, "character": char,
                                 "messages": store.list_messages(sid)})
            elif path == "/api/memories":
                scope = qs.get("scope", [None])[0]
                self._send(200, {"memories": store.list_memories(scope=scope)})
            elif path == "/api/config":
                self._send(200, {"config": _mask_config(cfg), "providers": llm.PROVIDERS})
            else:
                self._send(404, {"error": "not found"})

        def _route_post(self):
            path = urlparse(self.path).path
            if path == "/api/probe":  # 无请求体
                self._send(200, llm.probe(cfg.get("model", {})))
                return
            if path == "/api/voice/tts":  # JSON -> 转发 -> WAV 字节
                vcfg = cfg.get("voice", {})
                vurl = vcfg.get("server", "http://127.0.0.1:8790")
                data = self._read_json()
                payload = {
                    "text": str(data.get("text") or "")[:1200],
                    "sid": data.get("voice") or vcfg.get("tts_voice") or "zf_xiaoxiao",
                    "speed": float(data.get("speed") or vcfg.get("speed") or 1.0),
                }
                if not payload["text"].strip():
                    self._send(400, {"error": "text 不能为空"})
                    return
                try:
                    _, body, hdrs = _proxy_http(
                        "POST", vurl + "/tts",
                        json.dumps(payload).encode("utf-8"),
                        {"Content-Type": "application/json"})
                except Exception as e:  # noqa: BLE001
                    self._send(503, {"error": VOICE_SERVER_DOWN_HINT})
                    logger.warning("语音合成服务不可达: %s", e)
                    return
                self._send(200, body, hdrs.get("Content-Type", "audio/wav"))
                return
            if path == "/api/voice/asr":  # 原始 WAV 字节 -> 转发 -> {"text": ...}
                vurl = cfg.get("voice", {}).get("server", "http://127.0.0.1:8790")
                raw = self._read_bytes()
                if len(raw) < 100:
                    self._send(400, {"error": "音频数据为空或过短"})
                    return
                try:
                    _, body, _ = _proxy_http("POST", vurl + "/asr", raw,
                                             {"Content-Type": "audio/wav"}, timeout=120)
                except Exception as e:  # noqa: BLE001
                    self._send(503, {"error": VOICE_SERVER_DOWN_HINT})
                    logger.warning("语音识别服务不可达: %s", e)
                    return
                self._send(200, json.loads(body.decode("utf-8")))
                return
            data = self._read_json()
            if path == "/api/chat":
                sid = data.get("session_id")
                text = str(data.get("message") or "").strip()
                if not sid or not text:
                    self._send(400, {"error": "缺少 session_id 或 message"})
                    return
                result = self._brain().chat(sid, text[:8000])
                result["model"] = {"provider": cfg["model"].get("provider"),
                                   "model": cfg["model"].get("model") or ""}
                self._send(200, result)
            elif path == "/api/characters":
                char = store.upsert_character(data)
                self._send(200, {"character": char})
            elif path == "/api/sessions":
                cid = data.get("character_id")
                if not cid:
                    self._send(400, {"error": "缺少 character_id"})
                    return
                sess = store.create_session(cid, str(data.get("title") or ""))
                self._send(200, {"session": sess})
            elif path == "/api/memories":
                content = str(data.get("content") or "").strip()
                if not content:
                    self._send(400, {"error": "content 不能为空"})
                    return
                mem = store.add_memory(content,
                                       scope=data.get("scope") if data.get("scope") in ("user", "character") else "user",
                                       source_session=data.get("session_id"),
                                       confidence="manual")
                self._send(200, {"memory": mem})
            elif path == "/api/memories/extract":
                sid = data.get("session_id")
                if not sid:
                    self._send(400, {"error": "缺少 session_id"})
                    return
                events = self._brain().extract_now(sid)
                self._send(200, {"memory_events": events,
                                 "memories": store.list_memories()})
            elif path == "/api/config":
                self._update_config(data)
            else:
                self._send(404, {"error": "not found"})

        def _route_put(self):
            parts = urlparse(self.path).path.split("/")
            if len(parts) == 4 and parts[1] == "api" and parts[2] == "characters":
                data = self._read_json()
                data["id"] = parts[3]
                self._send(200, {"character": store.upsert_character(data)})
            elif len(parts) == 4 and parts[1] == "api" and parts[2] == "memories":
                data = self._read_json()
                self._send(200, {"memory": store.update_memory(parts[3], data)})
            else:
                self._send(404, {"error": "not found"})

        def _route_delete(self):
            parts = urlparse(self.path).path.split("/")
            if len(parts) == 4 and parts[2] == "characters":
                store.delete_character(parts[3])
                self._send(200, {"ok": True})
            elif len(parts) == 4 and parts[2] == "sessions":
                store.delete_session(parts[3])
                self._send(200, {"ok": True})
            elif len(parts) == 4 and parts[2] == "memories":
                store.delete_memory(parts[3])
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        # ---------- 配置更新 ----------
        def _update_config(self, data):
            new_model = data.get("model")
            new_memory = data.get("memory")
            if new_model:
                if new_model.get("provider") and new_model["provider"] not in llm.PROVIDERS:
                    self._send(400, {"error": "未知 provider"})
                    return
                merged_model = _deep_merge(cfg.get("model", {}), new_model)
                # 密钥：空字符串=保持不变；api_key_clear=true=清除
                if not str(new_model.get("api_key") or "").strip():
                    merged_model["api_key"] = cfg.get("model", {}).get("api_key", "")
                if new_model.get("api_key_clear"):
                    merged_model["api_key"] = ""
                for k in ("context_chars", "reply_max_chars", "max_tokens", "timeout_sec"):
                    try:
                        merged_model[k] = int(merged_model.get(k) or DEFAULT_CONFIG["model"][k])
                    except (TypeError, ValueError):
                        merged_model[k] = DEFAULT_CONFIG["model"][k]
                merged_model.pop("api_key_clear", None)
                merged_model.pop("api_key_set", None)
                merged_model.pop("api_key_hint", None)
                cfg["model"] = merged_model
            if new_memory:
                cfg["memory"] = _deep_merge(cfg.get("memory", {}), new_memory)
            new_voice = data.get("voice")
            if new_voice:
                merged_voice = _deep_merge(cfg.get("voice", {}), new_voice)
                merged_voice["speed"] = min(2.0, max(0.5, float(merged_voice.get("speed") or 1.0)))
                cfg["voice"] = merged_voice
            save_config(cfg_path, cfg)
            logger.info("配置已更新 (provider=%s)", cfg["model"].get("provider"))
            self._send(200, {"config": _mask_config(cfg)})

        # ---------- 静态文件 ----------
        def _serve_static(self, rel):
            rel = rel.replace("\\", "/")
            if ".." in rel:
                self._send(400, {"error": "bad path"})
                return
            path = os.path.normpath(os.path.join(static_root, rel))
            if not path.startswith(os.path.abspath(static_root)):
                self._send(400, {"error": "bad path"})
                return
            if not os.path.isfile(path):
                self._send(404, {"error": "not found"})
                return
            ext = os.path.splitext(path)[1].lower()
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            logger.info("GET /static/%s -> 200 (%db)", rel, len(body))

    return Handler


def make_server(store, cfg, static_root, cfg_path, host="127.0.0.1", port=0):
    handler = _make_handler(store, cfg, cfg_path, static_root)
    return ThreadingHTTPServer((host, port), handler)


def _setup_logging(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(os.path.join(log_dir, "app.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.addHandler(fh)
    logger.addHandler(ch)


def main(static_root, data_root, config_path, no_browser=False, port=None):
    cfg = load_config(config_path)
    store = Store(data_root)
    _setup_logging(os.path.join(data_root, "logs"))
    host = cfg["server"].get("host", "127.0.0.1")
    want = int(port or cfg["server"].get("port") or 8765)
    httpd = None
    actual = None
    for p in range(want, want + 21):
        try:
            httpd = make_server(store, cfg, static_root, config_path, host=host, port=p)
            actual = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("端口 %d~%d 均被占用，无法启动" % (want, want + 20))
    url = "http://%s:%d/" % (host, actual)
    logger.info("%s v%s 已启动：%s （数据目录：%s）", APP_NAME, VERSION, url, data_root)
    print("=" * 56)
    print("  %s v%s  本地角色聊天" % (APP_NAME, VERSION))
    print("  界面地址: %s" % url)
    print("  数据目录: %s" % data_root)
    print("  模型: %s%s" % (cfg["model"].get("provider"),
                            " · " + cfg["model"].get("model") if cfg["model"].get("model") else ""))
    print("  关闭此窗口（或按 Ctrl+C）即退出。")
    print("=" * 56)
    if cfg["server"].get("open_browser", True) and not no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        logger.info("服务已停止")
