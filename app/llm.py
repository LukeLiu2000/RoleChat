"""模型接入层：mock / ollama / openai 兼容 / anthropic 兼容。

- 全部使用标准库 urllib，无第三方依赖（便于打包 exe）。
- 不做流式输出（当前限制，见 README）。
- api_key 只出现在请求头中，绝不写日志。
- anthropic 说明：按官方现行规范——不发送 temperature 等采样参数（新版模型会 400）；
  默认附带 refusal fallback（服务端拒绝回退）；stop_reason=refusal 视为失败并提示用户。
"""
import json
import re
import urllib.error
import urllib.request

PROVIDERS = ["mock", "ollama", "openai", "anthropic"]

_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _norm_base(url):
    return (url or "").strip().rstrip("/")


def _http(method, url, body, headers, timeout):
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise LLMError("HTTP %s: %s" % (e.code, detail), status=e.code) from None
    except urllib.error.URLError as e:
        raise LLMError("无法连接 %s：%s" % (url, e.reason)) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise LLMError("返回内容不是合法 JSON（%s）" % url) from None


def _timeout(cfg):
    try:
        return max(5, int(cfg.get("timeout_sec") or 120))
    except (TypeError, ValueError):
        return 120


# ---------------------------------------------------------------- 对话
def chat(messages, cfg):
    """messages: [{role: system|user|assistant, content}] -> 回复文本"""
    provider = cfg.get("provider") or "mock"
    if not messages:
        raise LLMError("消息为空")
    if provider == "mock":
        return _mock_chat(messages)
    if provider == "ollama":
        return _ollama_chat(messages, cfg, _timeout(cfg))
    if provider == "openai":
        return _openai_chat(messages, cfg, _timeout(cfg))
    if provider == "anthropic":
        return _anthropic_chat(messages, cfg, _timeout(cfg))
    raise LLMError("未知 provider: %s（可选：%s）" % (provider, "/".join(PROVIDERS)))


def _mock_chat(messages):
    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    last_user = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user = m["content"]
            break
    name = "角色"
    m = re.search(r"名称[：:]\s*(\S+)", system)
    if m:
        name = m.group(1)
    mem_lines = len(re.findall(r"^-\s*\[(?:用户|角色)记忆#", system, re.M))
    preview = last_user[:60] + ("…" if len(last_user) > 60 else "")
    return (
        "【模拟模型·离线】%s 收到了你的消息：“%s”。\n"
        "当前是离线模拟模式：记忆检索、摘要、存储等应用功能都在真实运行，"
        "但这句话由本地规则生成，不是大模型输出。\n"
        "本轮引用长期记忆 %d 条。要获得真实回复，请在“设置”中配置本地模型（如 Ollama）或云端 API。"
    ) % (name, preview, mem_lines)


def _ollama_chat(messages, cfg, timeout):
    base = _norm_base(cfg.get("base_url") or "http://localhost:11434")
    body = {
        "model": cfg.get("model") or "qwen2.5:7b",
        "messages": [dict(role=m["role"], content=m["content"]) for m in messages],
        "stream": False,
        # 闲置 30 分钟内不卸载模型：避免 Ollama 默认 5 分钟卸载后，
        # 下一条消息要等几十秒重新加载显存（用户会以为应用卡死）。
        "keep_alive": "30m",
    }
    opts = {}
    if cfg.get("temperature") not in (None, ""):
        opts["temperature"] = float(cfg["temperature"])
    if cfg.get("context_tokens"):
        opts["num_ctx"] = int(cfg["context_tokens"])
    if opts:
        body["options"] = opts
    data = _http("POST", base + "/api/chat", body,
                 {"Content-Type": "application/json"}, timeout)
    try:
        return (data["message"]["content"] or "").strip()
    except (KeyError, TypeError, AttributeError):
        raise LLMError("Ollama 返回格式异常") from None


def _openai_base(cfg):
    base = _norm_base(cfg.get("base_url") or "http://localhost:1234")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _openai_chat(messages, cfg, timeout):
    body = {
        "model": cfg.get("model") or "local-model",
        "messages": [dict(role=m["role"], content=m["content"]) for m in messages],
        "stream": False,
        "max_tokens": int(cfg.get("max_tokens") or 4096),
    }
    if cfg.get("temperature") not in (None, ""):
        body["temperature"] = float(cfg["temperature"])
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    data = _http("POST", _openai_base(cfg) + "/chat/completions", body, headers, timeout)
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMError("OpenAI 兼容接口返回格式异常") from None


def _anthropic_headers(cfg, beta=None):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg.get("api_key") or "",
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    if beta:
        headers["anthropic-beta"] = beta
    return headers


def _anthropic_chat(messages, cfg, timeout):
    base = _norm_base(cfg.get("base_url") or "https://api.anthropic.com")
    if not cfg.get("api_key"):
        raise LLMError("anthropic provider 需要填写 API Key（设置 → 模型服务）")
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    msgs = [{"role": m["role"], "content": m["content"]}
            for m in messages if m["role"] in ("user", "assistant")]
    # API 要求 user/assistant 交替：合并相邻同角色消息
    merged = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(dict(m))
    if not merged or merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "（开始对话）"})
    body = {
        "model": cfg.get("model") or "claude-opus-5-5",
        "max_tokens": int(cfg.get("max_tokens") or 4096),
        "messages": merged,
    }
    if system:
        body["system"] = system
    # 注意：现行 Claude 模型不接受 temperature/top_p/top_k，不发送。
    # 官方建议默认启用服务端拒绝回退（refusal fallback）。
    body_fb = dict(body, fallbacks="default")
    try:
        data = _http("POST", base + "/v1/messages", body_fb,
                     _anthropic_headers(cfg, _ANTHROPIC_FALLBACK_BETA), timeout)
    except LLMError as e:
        if e.status == 400:
            # 某些 Anthropic 兼容网关不识别 fallback 字段：去掉后原样重试一次
            data = _http("POST", base + "/v1/messages", body,
                         _anthropic_headers(cfg), timeout)
        else:
            raise
    stop = data.get("stop_reason")
    if stop == "refusal":
        raise LLMError("模型安全分类器拒绝了本次请求（stop_reason=refusal），请换种说法重试")
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "text").strip()
    if not text:
        raise LLMError("模型返回为空（stop_reason=%s）" % stop)
    return text


# ---------------------------------------------------------------- 任务型调用：摘要 / 记忆提取
def summarize(text, cfg):
    """把较早的对话压缩成摘要。"""
    provider = cfg.get("provider") or "mock"
    if provider == "mock":
        lines = [ln for ln in text.splitlines() if ln.strip()]
        heads = " / ".join(ln[:40] for ln in lines[:2])
        return "（离线摘要）此前共 %d 条消息，开头内容：%s…" % (len(lines), heads or "（无）")
    system = ("你是对话摘要器。把输入的早期对话压缩为不超过300字的第三人称摘要，"
              "保留人名、称谓、事实、偏好和未完成的话题；不要添加猜测；直接输出摘要正文。")
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": text[:12000]}]
    if provider != "anthropic":  # anthropic 新版模型不接受采样参数
        cfg = dict(cfg, temperature=0.2)
    return chat(msgs, cfg)


_EXTRACT_SYSTEM = (
    "你是长期记忆提取器，工作在角色扮演聊天应用中。从对话中提取“值得长期记住”的内容，"
    "严格输出 JSON 数组（可以为空数组 []），每个元素形如 "
    '{"content":"...","scope":"user" 或 "character"}。\n'
    "规则：\n"
    "1. 只提取明确说出的事实与偏好（用户的名字、喜好、职业、正在做的事、住址等）。\n"
    "2. scope=user 表示关于真实用户的信息；scope=character 表示用户明确要求角色记住的剧情/设定。\n"
    "3. 剧情扮演中的虚构事件、角色自己说的话，一律不得变成 scope=user 的记忆。\n"
    "4. 拿不准、可能只是客套或猜测的内容，不要输出。\n"
    "5. 不要输出任何 JSON 以外的文字。"
)


def extract(dialog_text, cfg):
    """返回 [{content, scope}]；解析失败抛 LLMError。mock 返回 []。"""
    provider = cfg.get("provider") or "mock"
    if provider == "mock":
        return []
    raw = chat([{"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": dialog_text[:12000]}], cfg)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise LLMError("提取结果不是 JSON 数组")
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        raise LLMError("提取结果 JSON 解析失败") from None
    out = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and str(it.get("content", "")).strip():
                out.append({"content": str(it["content"]).strip()[:300],
                            "scope": it.get("scope") if it.get("scope") in ("user", "character") else "user"})
    return out


# ---------------------------------------------------------------- 连接探测
def probe(cfg):
    """检查模型服务是否可用，返回 {connected, message, models}。"""
    provider = cfg.get("provider") or "mock"
    try:
        if provider == "mock":
            return {"connected": True, "message": "离线模拟模式（不调用任何模型）", "models": []}
        if provider == "ollama":
            base = _norm_base(cfg.get("base_url") or "http://localhost:11434")
            data = _http("GET", base + "/api/tags", None, {}, 5)
            models = [m.get("name", "") for m in data.get("models", [])]
            msg = "Ollama 可用，共 %d 个模型" % len(models)
            if cfg.get("model") and cfg["model"] not in models:
                msg += "（注意：配置的 “%s” 不在列表中）" % cfg["model"]
            return {"connected": True, "message": msg, "models": models}
        if provider == "openai":
            headers = {"Content-Type": "application/json"}
            if cfg.get("api_key"):
                headers["Authorization"] = "Bearer " + cfg["api_key"]
            data = _http("GET", _openai_base(cfg) + "/models", None, headers, 5)
            models = [m.get("id", "") for m in data.get("data", [])]
            return {"connected": True, "message": "OpenAI 兼容接口可用，共 %d 个模型" % len(models),
                    "models": models}
        if provider == "anthropic":
            base = _norm_base(cfg.get("base_url") or "https://api.anthropic.com")
            if not cfg.get("api_key"):
                return {"connected": False, "message": "未填写 API Key", "models": []}
            data = _http("GET", base + "/v1/models", None,
                         _anthropic_headers(cfg), 8)
            models = [m.get("id", "") for m in data.get("data", [])]
            return {"connected": True, "message": "Anthropic 接口可用，共 %d 个模型" % len(models),
                    "models": models}
        return {"connected": False, "message": "未知 provider: %s" % provider, "models": []}
    except LLMError as e:
        return {"connected": False, "message": str(e)[:200], "models": []}
