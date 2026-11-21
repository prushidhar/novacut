"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import collections
import json
import os
import re
import threading
import time

import httpx

from . import config, ctx

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-flash-lite-latest"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.0-flash-001"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.RLock()
_saved = {"at": None, "data": {}}
_key_index = 0
_key_cooldowns: dict[str, float] = {}  # key -> cooldown end timestamp


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


def current() -> dict:
    """Provider, models, address and key: what the dashboard saved, or else what .env says."""
    d = _from_dashboard()
    p = str(d.get("provider") or config.AI_PROVIDER or "").strip().lower()
    return {"provider": ALIASES.get(p, p),
            "models": list(d["models"]) if d.get("provider") else list(config.AI_MODELS),
            "base_url": (str(d.get("base_url") or "") if d.get("provider") else config.AI_BASE_URL).rstrip("/"),
            "key": str(d["key"]) if d.get("provider") and "key" in d else config.ai_key()}


def provider() -> str:
    return current()["provider"]


def models() -> list:
    return current()["models"]


def get_keys() -> list[str]:
    """All keys configured for the current provider (supports comma/newline-separated key pools)."""
    c = current()
    raw = str(c.get("key") or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[\s,;\n\r]+", raw) if p.strip()]
    return [p for p in parts if len(p) >= 10] or ([raw] if raw else [])


def key() -> str:
    """Returns the current active, non-cooldown key from the pool."""
    ks = get_keys()
    if not ks:
        return ""
    now = time.time()
    with _lock:
        global _key_index
        for offset in range(len(ks)):
            idx = (_key_index + offset) % len(ks)
            k = ks[idx]
            if _key_cooldowns.get(k, 0) <= now:
                _key_index = idx
                return k
        # If all keys on cooldown, pick the one expiring soonest
        _key_index = min(range(len(ks)), key=lambda i: _key_cooldowns.get(ks[i], 0))
        return ks[_key_index]


def rotate_key(failed_key=None, reason="", cooldown=1800.0) -> str:
    """Marks failed_key as rate-limited/exhausted and advances to the next key."""
    ks = get_keys()
    if not ks:
        return ""
    now = time.time()
    with _lock:
        global _key_index
        fk = failed_key or (ks[_key_index] if 0 <= _key_index < len(ks) else ks[0])
        _key_cooldowns[fk] = now + cooldown
        for offset in range(1, len(ks) + 1):
            idx = (_key_index + offset) % len(ks)
            k = ks[idx]
            if _key_cooldowns.get(k, 0) <= now:
                _key_index = idx
                print(f"[AI] Quota hit on key (...{fk[-4:]}). Rotated to next key (...{k[-4:]}).", flush=True)
                return k
        _key_index = (_key_index + 1) % len(ks)
        return ks[_key_index]


def cur_key() -> str:
    return key()


def label() -> str:
    return PROVIDERS.get(provider(), ("",))[0]


def base_url() -> str:
    c = current()
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def ready() -> bool:
    c = current()
    return bool(c["provider"] in PROVIDERS and c["models"] and base_url() and (c["key"] or c["provider"] in NO_KEY))


def public() -> dict:
    """What the settings page may see."""
    c = current()
    ks = get_keys()
    now = time.time()
    active = key()
    pool_info = []
    for i, k_str in enumerate(ks):
        cd = _key_cooldowns.get(k_str, 0)
        remaining = max(0, int(cd - now))
        is_active = (k_str == active)
        hint = f"...{k_str[-4:]}" if len(k_str) >= 8 else "key"
        pool_info.append({
            "index": i + 1,
            "hint": hint,
            "active": is_active,
            "on_cooldown": (remaining > 0),
            "cooldown_sec": remaining
        })
    k_hint = ""
    if len(ks) > 1:
        k_hint = f"{len(ks)} keys in pool (active: ...{active[-4:] if len(active) >= 4 else '?'})"
    elif ks:
        k_hint = ks[0][-4:] if len(ks[0]) >= 12 else ""
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(ks),
            "key_count": len(ks), "key_hint": k_hint, "pool": pool_info, "ready": ready(), "label": label(),
            "saved_here": bool(_from_dashboard().get("provider")),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None) -> dict:
    """Save the choice made in the dashboard. Supports 1, 10, or unlimited keys (comma/newline separated)."""
    p = ALIASES.get(str(provider_id or "").strip().lower(), str(provider_id or "").strip().lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current()
    raw_k = old["key"] if new_key is None and old["provider"] == p else str(new_key or "").strip()[:8000]
    parsed = [part.strip() for part in re.split(r"[\s,;\n\r]+", raw_k) if part.strip() and len(part.strip()) >= 10]
    k = ", ".join(parsed) if parsed else raw_k
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service.")
    with _lock:
        global _key_index, _key_cooldowns
        _key_index = 0
        _key_cooldowns.clear()
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"provider": p, "models": ms, "base_url": address, "key": k}), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the key is only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None
    clear_problem()
    return public()


def list_models(provider_id=None, address=None, with_key=None) -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current()
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k = str(with_key or "").strip() or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test() -> dict:
    """One tiny request with the settings in use, to prove the key, the address and at least one model works."""
    ms = models()
    if not ready():
        raise ValueError("Save a service, a model and a key first.")
    last_err = None
    for m in ms:
        try:
            out, sent, wrote = call(m, 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}})
            clear_problem()
            ks = get_keys()
            k_info = f" ({len(ks)} keys active in pool)" if len(ks) > 1 else ""
            return {"ok": True, "model": m, "tokens": sent + wrote, "answer": str(out)[:80], "key_hint": k_info}
        except Exception as e:
            last_err = e
            continue
    clear_problem()
    if last_err:
        raise last_err
    raise ValueError("No models configured to test.")


class AIError(RuntimeError):
    pass


class RateLimitError(RuntimeError):
    pass


class _RateLimiter:
    """Tracks per-user AI request rates and token usage to prevent runaway costs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.tokens: dict[str, collections.deque] = collections.defaultdict(collections.deque)

    def check(self, uid: str | None):
        if not uid:
            return
        now = time.time()
        with self.lock:
            if config.AI_RATE_LIMIT:
                q = self.requests[uid]
                while q and q[0] <= now - 60:
                    q.popleft()
                if len(q) >= config.AI_RATE_LIMIT:
                    wait_sec = max(1, int(60 - (now - q[0])))
                    raise RateLimitError(f"AI request rate limit reached ({config.AI_RATE_LIMIT}/min). Please wait {wait_sec}s.")

            if config.AI_TOKEN_LIMIT:
                tq = self.tokens[uid]
                while tq and tq[0][0] <= now - 3600:
                    tq.popleft()
                used = sum(t[1] for t in tq)
                if used >= config.AI_TOKEN_LIMIT:
                    wait_sec = max(1, int(3600 - (now - tq[0][0])))
                    raise RateLimitError(f"AI token budget reached for this hour ({used:,} tokens used). Please wait {wait_sec // 60}m.")

    def record_request(self, uid: str | None):
        if not uid:
            return
        now = time.time()
        with self.lock:
            self.requests[uid].append(now)

    def record_tokens(self, uid: str | None, tokens: int):
        if not uid or not tokens:
            return
        now = time.time()
        with self.lock:
            self.tokens[uid].append((now, tokens))


limiter = _RateLimiter()


problem = {"text": "", "at": 0.0}       # why the AI last failed, for the dashboard; cleared when it answers again


def explain(e) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    if isinstance(e, RateLimitError) or "rate limit reached" in str(e).lower() or "token budget reached" in str(e).lower():
        return str(e)
    text, low = str(e), str(e).lower()
    name = (models() or ["?"])[0]
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{label()} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label()} refused the key. Paste a working key under Settings, AI."
    if "503" in text or "temporar" in low or "high demand" in low or "unavailable" in low:
        return f"{label()} is experiencing high demand right now. Try a lighter model like gemini-flash-lite-latest or try again shortly."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{label()} says the key has hit its limit or has no credit left. Check the account with {label()}, or try again later."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low:
        return f"NovaCut couldn't reach {label()}. Check the internet connection" + (" and that Ollama is running." if provider() == "ollama" else ".")
    return ""


def note_problem(e):
    import time
    why = explain(e)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def _fail(r, model, k=None):
    text = r.text[:300].replace(k or key() or "\0", "<key>")
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise AIError("The AI did not answer with JSON.")
        out = json.loads(m.group(0))
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict):
    """Ask one model. Returns (the parsed JSON answer, tokens sent, tokens written back).
    Automatically rotates through the key pool if a key hits rate limits or quota exhaustion."""
    u = ctx.uid_or_none()
    limiter.check(u)
    how = PROVIDERS[provider()][2]
    fn = {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[how]
    ks = get_keys()
    attempts = max(1, len(ks))
    last_err = None
    for attempt in range(attempts):
        cur_k = key()
        try:
            res = fn(model, prompt, schema, cur_k)
            limiter.record_request(u)
            limiter.record_tokens(u, res[1] + res[2])
            return res
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(term in msg for term in ("429", "quota", "rate limit", "exhausted", "perday", "free_tier_requests", "api_key_invalid", "api key not valid", "401", "403", "unauthorized")) and len(ks) > 1 and attempt < attempts - 1:
                rotate_key(cur_k, reason="quota_or_auth_error", cooldown=1800.0)
                continue
            raise
    if last_err:
        raise last_err


def _gemini(model, prompt, schema, active_k=None):
    k = active_k or key()
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{base_url()}/{model}:generateContent", json=body, headers={"x-goog-api-key": k}, timeout=120)
    if r.status_code != 200:
        _fail(r, model, k)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai(model, prompt, schema, active_k=None):
    k = active_k or key()
    headers = {"Authorization": f"Bearer {k}"} if k else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
    extras = [{"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}]
    r = None
    for extra in extras:
        r = httpx.post(f"{base_url()}/chat/completions", json={**body, **extra}, headers=headers, timeout=180)
        if r.status_code not in (400, 422):
            break
    if r.status_code != 200:
        _fail(r, model, k)
    data = r.json()
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(model, prompt, schema, active_k=None):
    k = active_k or key()
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    r = httpx.post(f"{base_url()}/messages", json=body, timeout=180,
                   headers={"x-api-key": k, "anthropic-version": "2023-06-01"})
    if r.status_code != 200:
        _fail(r, model, k)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
