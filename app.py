import os
import json
import time
import hashlib
import binascii
import asyncio
from threading import RLock
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, request, jsonify, make_response, Response, render_template
from flask_cors import CORS
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson

import requests
import aiohttp
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import orjson
    def _jsonify(data, status=200):
        return Response(orjson.dumps(data), status=status, mimetype='application/json')
except ImportError:
    def _jsonify(data, status=200):
        return Response(json.dumps(data, separators=(',', ':'), ensure_ascii=False),
                        status=status, mimetype='application/json')

import like_pb2
import like_count_pb2
import uid_generator_pb2

app = Flask(__name__, template_folder="site")
CORS(app)

DEV_NAME     = "SIFAT"
BRAND_NAME   = "FXS_LIKE_API"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CLIENT_VERSION = "1.132.1"
OB_VERSION     = "OB55"

JWT_API = "https://fx-sifat-jwt-api.vercel.app/token"

JWT_WORKERS   = 60
LIKE_CONCUR   = 150

SERVER_ACCOUNT_FILES = {
    "BD":  "account_bd.txt",
    "IND": "account_ind.txt",
    "BR":  "account_br.txt",
    "US":  "account_us.txt",
    "SAC": "account_sac.txt",
    "NA":  "account_na.txt",
}

CONFIG_RO_PATH = os.path.join(BASE_DIR, "apikey.json")
CONFIG_RW_PATH = os.path.join("/tmp", "apikey.json")
config_lock = RLock()

def _active_config_path_for_read() -> str:
    return CONFIG_RW_PATH if os.path.exists(CONFIG_RW_PATH) else CONFIG_RO_PATH

def _read_config() -> dict:
    path = _active_config_path_for_read()
    if not os.path.exists(path):
        raise FileNotFoundError("apikey.json not found")
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "API_KEY" not in cfg or not isinstance(cfg["API_KEY"], str) or not cfg["API_KEY"].strip():
        raise ValueError("apikey.json must include a non-empty API_KEY")
    return cfg

def get_api_key() -> str:
    with config_lock:
        return _read_config()["API_KEY"]

def is_valid_key(api_key: str) -> bool:
    try:
        return api_key == get_api_key()
    except Exception:
        return False

def _account_file_path(server_name: str) -> str:
    fname = SERVER_ACCOUNT_FILES.get(server_name.upper())
    if not fname:
        return ""
    return os.path.join(BASE_DIR, fname)


def _load_accounts_for_server(server_name: str) -> list:
    path = _account_file_path(server_name)
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or ":" not in ln:
                continue
            uid, pw = ln.split(":", 1)
            uid, pw = uid.strip(), pw.strip()
            if uid and pw:
                out.append((uid, pw))
    return out

def _fetch_single_jwt(uid: str, pw: str, timeout: int = 12):
    url = f"{JWT_API}?uid={uid}&password={pw}"
    try:
        r = requests.get(url, timeout=timeout, verify=False)
        if r.status_code != 200:
            return uid, None
        try:
            j = r.json()
        except ValueError:
            text = r.text.strip()
            if text and len(text) > 50:
                return uid, text
            return uid, None
        token = None
        if isinstance(j, dict):
            token = j.get("token") or j.get("jwt") or j.get("access_token")
            if not token and isinstance(j.get("data"), dict):
                token = j["data"].get("token") or j["data"].get("jwt")
        elif isinstance(j, str):
            token = j
        return uid, token
    except requests.RequestException:
        return uid, None


def generate_jwts_live(accounts: list, max_workers: int = JWT_WORKERS):
    if not accounts:
        return []
    tokens = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_single_jwt, uid, pw): uid for uid, pw in accounts}
        for fut in as_completed(futures):
            uid, token = fut.result()
            if token:
                tokens.append(token)
    return tokens

def encrypt_message(plaintext):
    k = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(k, AES.MODE_CBC, iv)
    padded = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded)).decode("utf-8")


def create_protobuf_message(user_id, region):
    m = like_pb2.like()
    m.uid = int(user_id)
    m.region = region
    return m.SerializeToString()


def create_protobuf(uid):
    m = uid_generator_pb2.uid_generator()
    m.krishna_ = int(uid)
    m.teamXdarks = 1
    return m.SerializeToString()


def enc(uid):
    return encrypt_message(create_protobuf(uid))


async def send_request(encrypted_uid, token, url, session_):
    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    try:
        async with session_.post(url, data=edata, headers=headers) as resp:
            return resp.status
    except Exception:
        return 0


async def _burst_all_tokens(uid, server_name, url, tokens, concurrency=LIKE_CONCUR):
    msg = create_protobuf_message(uid, server_name)
    enc_uid = encrypt_message(msg)
    if not tokens:
        return []

    connector = aiohttp.TCPConnector(limit=concurrency, ssl=False)
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(connector=connector) as sess:
        async def worker(tok):
            async with sem:
                return await send_request(enc_uid, tok, url, sess)
        results = await asyncio.gather(*[worker(t) for t in tokens])
        return results


def send_likes_from_all_tokens(uid, server_name, url, tokens):
    if not tokens:
        return 0
    results = asyncio.run(_burst_all_tokens(uid, server_name, url, tokens,
                                            concurrency=LIKE_CONCUR))
    success = sum(1 for s in results if s == 200)
    return success


def _like_url_for(server_name: str) -> str:
    s = server_name.upper()
    if s == "IND":
        return "https://client.ind.freefiremobile.com/LikeProfile"
    elif s in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/LikeProfile"
    else:
        return "https://clientbp.ppmainecoonghj.com/LikeProfile"


def _show_url_for(server_name: str) -> str:
    s = server_name.upper()
    if s == "IND":
        return "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif s in {"BR", "US", "SAC", "NA"}:
        return "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    else:
        return "https://clientbp.ppmainecoonghj.com/GetPlayerPersonalShow"


def make_request(encrypted, server_name, token):
    url = _show_url_for(server_name)
    edata = bytes.fromhex(encrypted)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }
    resp = requests.post(url, data=edata, headers=headers, verify=False, timeout=30)
    binary = bytes.fromhex(resp.content.hex())
    try:
        obj = like_count_pb2.Info()
        obj.ParseFromString(binary)
        return obj
    except Exception:
        return None


def _parse_account_info(pb_obj):
    try:
        if pb_obj is None:
            return None
        js = json.loads(MessageToJson(pb_obj))
        ai = js.get("AccountInfo", {})
        uid = int(ai.get("UID", 0))
        likes = int(ai.get("Likes", 0))
        name = str(ai.get("PlayerNickname", ""))
        if uid <= 0:
            return None
        return {"uid": uid, "likes": likes, "name": name}
    except Exception:
        return None

@app.get("/")
def index():
    return render_template(
        "index.html",
        brand=BRAND_NAME,
        dev=DEV_NAME,
    )


@app.get("/health")
def route_health():
    return _jsonify({
        "status": "ok",
        "service": BRAND_NAME,
        "dev": DEV_NAME,
        "jwt_api": JWT_API,
        "servers": list(SERVER_ACCOUNT_FILES.keys()),
    })

@app.get("/like")
def handle_like():
    try:
        uid = request.args.get("uid")
        server_name = request.args.get("server_name", "").upper()
        api_key = request.args.get("key", "").strip()

        if not api_key or not is_valid_key(api_key):
            return jsonify({"error": "Invalid or missing API key 🔑"}), 403
        if not uid or not server_name:
            return jsonify({"error": "UID and server_name are required"}), 400
        if server_name not in SERVER_ACCOUNT_FILES:
            return jsonify({
                "error": f"Unsupported server_name '{server_name}'",
                "allowed": list(SERVER_ACCOUNT_FILES.keys())
            }), 400

        accounts = _load_accounts_for_server(server_name)
        if not accounts:
            fname = SERVER_ACCOUNT_FILES[server_name]
            return jsonify({
                "error": f"No guest accounts found for server {server_name}",
                "hint": f"Add uid:pass lines into {fname}"
            }), 500

        print(f"[*] LIVE JWT for {len(accounts)} accounts ({server_name}) ...")
        tokens = generate_jwts_live(accounts, max_workers=JWT_WORKERS)
        print(f"[+] Got {len(tokens)} live tokens")

        if not tokens:
            return jsonify({
                "error": "Failed to generate any JWT tokens.",
                "server": server_name,
                "accounts_loaded": len(accounts)
            }), 500

        token = tokens[0]
        encrypted = enc(uid)

        before = _parse_account_info(make_request(encrypted, server_name, token))
        if before is None:
            return jsonify({
                "LikesGivenByAPI": 0,
                "LikesafterCommand": 0,
                "LikesbeforeCommand": 0,
                "PlayerNickname": "Unknown",
                "UID": int(uid) if str(uid).isdigit() else uid,
                "GiftCount": 0,
                "accounts_loaded": len(accounts),
                "tokens_generated": len(tokens),
                "server_name": server_name,
                "status": 0
            })

        url = _like_url_for(server_name)
        send_likes_from_all_tokens(uid, server_name, url, tokens)

        after = _parse_account_info(make_request(encrypted, server_name, token)) or {
            "likes": before["likes"], "uid": before["uid"], "name": before["name"]
        }
        like_given = max(0, int(after["likes"]) - int(before["likes"]))
        gift_count = like_given * 2 if like_given > 0 else 0
        status_value = 1 if like_given > 0 else 2

        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": int(after["likes"]),
            "LikesbeforeCommand": int(before["likes"]),
            "PlayerNickname": str(after["name"]),
            "UID": int(after["uid"]),
            "GiftCount": gift_count,
            "server_name": server_name,
            "accounts_loaded": len(accounts),
            "tokens_generated": len(tokens),
            "status": status_value
        })
    except Exception as e:
        return jsonify({"error": "config_or_runtime_error", "detail": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
