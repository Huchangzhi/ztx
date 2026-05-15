import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
import uuid
from functools import wraps
from io import BytesIO
from pathlib import Path

import aiohttp
import certifi
import requests
import ssl
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from collections import defaultdict

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# ============================================================
# Password auth (multi-password support: comma-separated)
# ============================================================
ACCESS_PASSWORDS = [p.strip() for p in os.environ.get("ACCESS_PASSWORD", "admin123").split(",") if p.strip()]
PASSWORD_TO_TOKEN = {
    p: hashlib.sha256((p + "_dictation_secret").encode()).hexdigest()
    for p in ACCESS_PASSWORDS
}
ADMIN_TOKENS = set(PASSWORD_TO_TOKEN.values())

# Guest token - no password required, limited access
GUEST_TOKEN = hashlib.sha256(b"guest_dictation_secret").hexdigest()

# ============================================================
# Rate limiter (per browser client)
# ============================================================
LOGIN_FAILURES = defaultdict(list)
RATE_WINDOW = 60
RATE_MAX = 3


def check_rate_limit(client_id):
    now = time.time()
    cutoff = now - RATE_WINDOW
    # Remove entries older than the window
    LOGIN_FAILURES[client_id] = [t for t in LOGIN_FAILURES[client_id] if t > cutoff]
    return len(LOGIN_FAILURES[client_id]) < RATE_MAX


def record_failure(client_id):
    LOGIN_FAILURES[client_id].append(time.time())


def clear_rate_limit(client_id):
    LOGIN_FAILURES.pop(client_id, None)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("X-Auth-Token", "")
        if auth in ADMIN_TOKENS or auth == GUEST_TOKEN:
            return f(*args, **kwargs)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:] in ADMIN_TOKENS:
            return f(*args, **kwargs)
        # Also check password directly
        pwd = request.headers.get("X-Password", "")
        if pwd in ACCESS_PASSWORDS:
            return f(*args, **kwargs)
        return jsonify({"error": "unauthorized"}), 401

    return decorated


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("X-Auth-Token", "")
        if auth in ADMIN_TOKENS:
            return f(*args, **kwargs)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:] in ADMIN_TOKENS:
            return f(*args, **kwargs)
        pwd = request.headers.get("X-Password", "")
        if pwd in ACCESS_PASSWORDS:
            return f(*args, **kwargs)
        return jsonify({"error": "游客模式不支持此功能"}), 403

    return decorated


@app.route("/api/login", methods=["POST"])
def login():
    client_id = request.headers.get("X-Client-Id", "")
    if not client_id:
        return jsonify({"error": "missing client id", "ok": False}), 400
    if not check_rate_limit(client_id):
        return jsonify({"error": "登录尝试过于频繁，请稍后再试", "ok": False}), 429

    data = request.get_json(silent=True) or {}
    pwd = data.get("password", "")
    if pwd in PASSWORD_TO_TOKEN:
        clear_rate_limit(client_id)
        return jsonify({"token": PASSWORD_TO_TOKEN[pwd], "ok": True, "guest": False})
    record_failure(client_id)
    return jsonify({"error": "密码错误", "ok": False}), 401


@app.route("/api/guest-login", methods=["POST"])
def guest_login():
    client_id = request.headers.get("X-Client-Id", "")
    if not client_id:
        return jsonify({"error": "missing client id", "ok": False}), 400
    if not check_rate_limit(client_id):
        return jsonify({"error": "登录尝试过于频繁，请稍后再试", "ok": False}), 429
    clear_rate_limit(client_id)
    return jsonify({"token": GUEST_TOKEN, "ok": True, "guest": True})


# ============================================================
# Edge TTS - WebSocket proxy
# ============================================================
TRUSTED_CLIENT_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
WIN_EPOCH = 11644473600
S_TO_NS = 1e9
CHROMIUM_VERSION = "143.0.3650.75"
clock_skew = 0.0


def _generate_sec_ms_gec():
    global clock_skew
    ticks = time.time() + clock_skew + WIN_EPOCH
    ticks -= ticks % 300
    ticks *= S_TO_NS / 100
    str_to_hash = f"{ticks:.0f}{TRUSTED_CLIENT_TOKEN}"
    return hashlib.sha256(str_to_hash.encode("ascii")).hexdigest().upper()


def _connect_id():
    return uuid.uuid4().hex


def _muid():
    return secrets.token_hex(16).upper()


def _date_str():
    return time.strftime(
        "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()
    )


async def _edge_tts_stream(text, voice="zh-CN-YunxiNeural"):
    gec = _generate_sec_ms_gec()
    conn_id = _connect_id()
    muid = _muid()
    url = (
        f"wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1"
        f"?TrustedClientToken={TRUSTED_CLIENT_TOKEN}"
        f"&ConnectionId={conn_id}"
        f"&Sec-MS-GEC={gec}"
        f"&Sec-MS-GEC-Version=1-{CHROMIUM_VERSION}"
    )

    headers = {
        "Pragma": "no-cache",
        "Cache-Control": "no-cache",
        "Origin": "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
        ),
        "Cookie": f"muid={muid};",
    }

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    audio_data = bytearray()

    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(
                url, headers=headers, ssl=ssl_ctx, timeout=30
            ) as ws:
                timestamp = _date_str()

                # speech.config
                config = (
                    f"X-Timestamp:{timestamp}\r\n"
                    "Content-Type:application/json; charset=utf-8\r\n"
                    "Path:speech.config\r\n\r\n"
                    '{"context":{"synthesis":{"audio":{"metadataoptions":'
                    '{"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"},'
                    '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
                )
                await ws.send_str(config)

                # SSML
                escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                ssml = (
                    "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis'"
                    " xml:lang='en-US'>"
                    f"<voice name='{voice}'>"
                    "<prosody pitch='+0Hz' rate='+0%' volume='+0%'>"
                    f"{escaped}"
                    "</prosody>"
                    "</voice>"
                    "</speak>"
                )
                req_id = uuid.uuid4().hex
                ssml_msg = (
                    f"X-RequestId:{req_id}\r\n"
                    "Content-Type:application/ssml+xml\r\n"
                    f"X-Timestamp:{timestamp}Z\r\n"
                    "Path:ssml\r\n\r\n"
                    f"{ssml}"
                )
                await ws.send_str(ssml_msg)

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        if len(msg.data) > 2:
                            header_len = int.from_bytes(msg.data[:2], "big")
                            payload = msg.data[header_len + 2 :]
                            audio_data.extend(payload)
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        if "turn.end" in msg.data:
                            break
        except aiohttp.ClientResponseError as e:
            if e.status == 403:
                # Adjust clock skew
                global clock_skew
                server_date = (e.headers or {}).get("Date", "")
                if server_date:
                    try:
                        parsed = time.strptime(
                            server_date, "%a, %d %b %Y %H:%M:%S %Z"
                        )
                        server_ts = time.mktime(parsed) - time.timezone
                        clock_skew = server_ts - time.time()
                        logger.info(f"Clock skew adjusted: {clock_skew:.1f}s")
                        # Retry once
                        return await _edge_tts_stream(text, voice)
                    except Exception:
                        pass
            raise

    return bytes(audio_data)


@app.route("/api/tts", methods=["POST"])
@require_auth
def tts():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    voice = data.get("voice", "zh-CN-YunxiNeural")

    if not text:
        return jsonify({"error": "text is required"}), 400

    try:
        audio = asyncio.run(_edge_tts_stream(text, voice))
        if not audio:
            return jsonify({"error": "no audio generated"}), 500
        return send_file(
            BytesIO(audio),
            mimetype="audio/mpeg",
            as_attachment=False,
        )
    except Exception as e:
        logger.exception("TTS error")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Voice list proxy (Edge TTS has no CORS)
# ============================================================
@app.route("/api/voices", methods=["GET"])
@require_auth
def voices():
    try:
        resp = requests.get(
            f"https://speech.platform.bing.com/consumer/speech/synthesize/readaloud/voices/list?trustedclienttoken={TRUSTED_CLIENT_TOKEN}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    " (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
                ),
            },
            timeout=15,
        )
        return jsonify(resp.json())
    except Exception as e:
        logger.exception("Voice list error")
        return jsonify({"error": str(e)}), 500


# ============================================================
# OpenAI AI word recognition
# ============================================================
@app.route("/api/ai-words", methods=["POST"])
@require_admin
def ai_words():
    data = request.get_json(silent=True) or {}
    images = data.get("images", [])
    user_prompt = data.get("user_prompt", "")

    openai_base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o")

    if not openai_key:
        return jsonify({"error": "服务端未配置 OpenAI API Key"}), 400
    if not images:
        return jsonify({"error": "请上传至少一张图片"}), 400

    system_prompt = (
        "你是一个图片内容识别助手。用户上传了图片，你需要根据用户的要求识别其中的内容。"
        "如果用户没有特别说明，默认提取所有文字内容。"
        "请严格遵从用户的指示，不要自行假设用户需要什么。"
    )

    if user_prompt:
        base_user_prompt = user_prompt
    else:
        base_user_prompt = "请从上传的图片中识别出所有可能用于语文听写的内容。"
    logger.info(f"user_prompt from frontend: {user_prompt!r}")
    logger.info(f"final base_user_prompt: {base_user_prompt[:200]}...")
    base_user_prompt += (
        "\n\n请先思考，然后以JSON格式返回结果。"
        "\n你必须在思考之后，在回复的最后输出以下格式（不要有任何其他文字在JSON后面）："
        '\n{"words": ["句子或词语1", "句子或词语2", "句子或词语3", ...]}'
    )

    # Compress images: max side 1000px
    processed_images = []
    for img in images:
        url = img.get("url", img.get("base64", ""))
        if url.startswith("data:image"):
            try:
                import base64 as b64mod
                header, data = url.split(",", 1)
                raw = b64mod.b64decode(data)
                from PIL import Image
                import io
                pil = Image.open(io.BytesIO(raw))
                w, h = pil.size
                max_side = 1000
                if w > max_side or h > max_side:
                    if w > h:
                        h = int(h * max_side / w); w = max_side
                    else:
                        w = int(w * max_side / h); h = max_side
                    pil = pil.resize((w, h), Image.LANCZOS)
                buf = io.BytesIO()
                pil.save(buf, format="JPEG", quality=85)
                compressed = "data:image/jpeg;base64," + b64mod.b64encode(buf.getvalue()).decode()
                processed_images.append({"type": "image_url", "image_url": {"url": compressed}})
                continue
            except Exception:
                pass
        processed_images.append({"type": "image_url", "image_url": {"url": url}})

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [{"type": "text", "text": base_user_prompt}]
            + processed_images,
        },
    ]

    # Log request (strip image data URLs)
    log_messages = []
    for m in messages:
        if isinstance(m.get("content"), list):
            cleaned = []
            for c in m["content"]:
                if c.get("type") == "image_url":
                    url = c["image_url"]["url"]
                    cleaned.append({"type": "image_url", "image_url": {"url": url[:60] + "...[truncated]"}})
                else:
                    cleaned.append(c)
            log_messages.append({"role": m["role"], "content": cleaned})
        else:
            log_messages.append(m)
    logger.info(f"messages to OpenAI: {json.dumps(log_messages, ensure_ascii=False)[:1000]}")

    # Fix base URL
    base = openai_base.rstrip("/")
    if not base.endswith("/v1"):
        base = base + "/v1"

    try:
        verify_ssl = openai_base.startswith("https://")
        resp = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 4096,
            },
            timeout=300,
            verify=verify_ssl,
        )
        if not resp.ok:
            return jsonify({"error": f"OpenAI API 错误: {resp.status_code} {resp.text[:500]}"}), 502

        result = resp.json()
        choices = result.get("choices")
        if not choices or not isinstance(choices, list) or len(choices) == 0:
            err_body = result.get("error", result)
            return jsonify({"error": f"OpenAI 响应异常: {json.dumps(err_body, ensure_ascii=False)[:300]}"}), 502
        content = choices[0].get("message", {}).get("content", "")
        if not content:
            return jsonify({"error": "AI 返回内容为空", "raw": result}), 422

        # Smart JSON extraction: find the last {"words" and extract the first complete } block
        idx = content.rfind('{"words"')
        if idx == -1:
            idx = content.rfind('"words"')
            if idx == -1:
                return jsonify({"error": "AI 响应中未找到词语列表", "raw": content}), 422
            idx = content.rfind('{', 0, idx)

        # From idx, find the first \n\n or end of JSON block
        # First, try to find the complete JSON by looking for matching brace
        json_part = content[idx:]
        brace_count = 0
        end = -1
        in_string = False
        escape = False
        for i, c in enumerate(json_part):
            if escape:
                escape = False; continue
            if c == '\\' and in_string:
                escape = True; continue
            if c == '"' and not escape:
                in_string = not in_string; continue
            if in_string: continue
            if c == '{': brace_count += 1
            elif c == '}':
                brace_count -= 1
                if brace_count == 0:
                    end = i + 1
                    break

        if end == -1:
            # Fallback: just find the first } after {"words"
            end = json_part.find('}') + 1
            if end <= 0:
                return jsonify({"error": "JSON 解析失败", "raw": content}), 422

        json_str = json_part[:end]
        # Try to fix common issues: trailing commas
        json_str = json_str.replace(',\n]', '\n]').replace(',\n}', '\n}').replace(',]', ']').replace(',}', '}')
        parsed = json.loads(json_str)
        words = parsed.get("words", [])

        if not words:
            return jsonify({"error": "AI 未识别出词语", "raw": content}), 422

        return jsonify({"words": words, "raw": content, "ok": True})

    except requests.Timeout:
        return jsonify({"error": "OpenAI API 请求超时，请重试"}), 504
    except Exception as e:
        logger.exception("AI words error")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Serve frontend
# ============================================================
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory(app.static_folder, path)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting server on port {port}, password count: {len(ACCESS_PASSWORDS)}")
    app.run(host="0.0.0.0", port=port, debug=False)
