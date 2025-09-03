#!/usr/bin/env python3
import os, json, tempfile, requests
from urllib.parse import quote
from datetime import datetime
from typing import Optional, Tuple, List, Dict

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request as GARequest

ACCOUNTS_FILE = "accounts.json"
STATUS_SUFFIX = "_status.json"

# =========================
# Status I/O
# =========================
def status_path(prefix): return f"{prefix}{STATUS_SUFFIX}"

def save_status(prefix, status, message=""):
    data = {
        "last_run": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "status": status,
        "message": (message or "")[:2000],
    }
    os.makedirs(os.path.dirname(status_path(prefix)) or ".", exist_ok=True)
    with open(status_path(prefix), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_status(prefix):
    p = status_path(prefix)
    return json.load(open(p, "r", encoding="utf-8")) if os.path.isfile(p) else {
        "last_run": None, "status": "never", "message": ""
    }

# =========================
# Small state helpers
# =========================
def _state_file(prefix, key): return f"{prefix}_{key}.json"

def load_last_index(prefix, key):
    fn = _state_file(prefix, key)
    if not os.path.isfile(fn): return 0
    return json.load(open(fn, "r", encoding="utf-8")).get("last_index", 0)

def save_last_index(prefix, key, idx):
    fn = _state_file(prefix, key)
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"last_index": idx}, f, indent=2)

def load_used_list(prefix) -> List[str]:
    fn = f"{prefix}_video_used.json"
    if not os.path.isfile(fn): return []
    return json.load(open(fn, "r", encoding="utf-8")).get("used", [])

def save_used_list(prefix, used: List[str]):
    fn = f"{prefix}_video_used.json"
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"used": used}, f, indent=2)

def reset_used_list(prefix):
    save_used_list(prefix, [])

# "force next" override
def _force_next_file(prefix): return f"{prefix}_force_next.json"

def get_force_next(prefix) -> Optional[str]:
    fn = _force_next_file(prefix)
    if not os.path.isfile(fn): return None
    data = json.load(open(fn, "r", encoding="utf-8"))
    return data.get("name")

def set_force_next(prefix, name: Optional[str]):
    fn = _force_next_file(prefix)
    if not name:
        if os.path.isfile(fn):
            os.remove(fn)
        return
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"name": name}, f, indent=2)

# =========================
# Accounts file
# =========================
def load_accounts(path=ACCOUNTS_FILE):
    return json.load(open(path, "r", encoding="utf-8")) if os.path.isfile(path) else []

def save_accounts(accounts, path=ACCOUNTS_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)

# =========================
# Content fetchers
# =========================
def fetch_lines(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return [l.strip() for l in r.text.splitlines() if l.strip()]

def next_title(cfg):
    if not cfg.get("title_url"):
        return f"Untitled {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
    lines = fetch_lines(cfg["title_url"])
    idx = load_last_index(cfg["state_prefix"], "title")
    title = lines[idx % len(lines)]
    save_last_index(cfg["state_prefix"], "title", idx + 1)
    return title

def next_description(cfg):
    if not cfg.get("description_url"):
        return ""
    lines = fetch_lines(cfg["description_url"])
    idx = load_last_index(cfg["state_prefix"], "description")
    desc = lines[idx % len(lines)]
    save_last_index(cfg["state_prefix"], "description", idx + 1)
    return desc

def next_tags(cfg):
    if not cfg.get("tags_url"):
        return []
    lines = fetch_lines(cfg["tags_url"])
    idx = load_last_index(cfg["state_prefix"], "tags")
    tags_line = lines[idx % len(lines)]
    save_last_index(cfg["state_prefix"], "tags", idx + 1)
    return [t.strip().lstrip("#") for t in tags_line.replace(",", " ").split() if t.strip()][:500]

def _download_to_tmp(url, suffix):
    r = requests.get(url, timeout=90, stream=True)
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        for chunk in r.iter_content(1024 * 512):
            if chunk: f.write(chunk)
    return path

# =========================
# Candidate picking / scanning
# =========================
def _url_exists(url, timeout=8):
    try:
        h = requests.head(url, timeout=timeout, allow_redirects=True)
        if h.status_code == 405:
            g = requests.get(url, timeout=timeout, stream=True)
            g.close()
            return g.status_code < 400
        return h.status_code < 400
    except Exception:
        return False

def _candidate_names_from_manifest(cfg) -> Optional[List[str]]:
    manifest = (cfg.get("manifest_url") or "").strip()
    if not manifest: return None
    try:
        names = [ln for ln in fetch_lines(manifest)
                 if ln.lower().endswith((".mp4", ".mov", ".m4v", ".webm"))]
        return names or None
    except Exception:
        return None

def _gen_candidates(cfg) -> List[str]:
    manifest_names = _candidate_names_from_manifest(cfg)
    if manifest_names:
        return manifest_names
    max_index = int(cfg.get("max_index", 2000))
    include_plain = (cfg.get("include_plain_vid", "auto")).lower()
    candidates = []
    if include_plain in ("auto", "always"):
        candidates.append("vid.mp4")
    candidates.extend([f"vid ({i}).mp4" for i in range(1, max_index + 1)])
    return candidates

def _exts(): return [".mp4", ".mov", ".m4v", ".webm"]

def peek_next_video_url(cfg) -> Optional[str]:
    used = set(load_used_list(cfg["state_prefix"]))
    base = cfg["video_base_url"].rstrip("/")
    force_name = get_force_next(cfg["state_prefix"])
    if force_name:
        base_name, ext = os.path.splitext(force_name)
        names = [force_name] if ext else [base_name + e for e in _exts()]
        for n in names:
            url = f"{base}/{quote(n, safe='')}"
            if _url_exists(url):
                return url
    for name in _gen_candidates(cfg):
        if name in used: continue
        base_name, ext = os.path.splitext(name)
        try_names = [name] if ext else [base_name + e for e in _exts()]
        for n in try_names:
            url = f"{base}/{quote(n, safe='')}"
            if _url_exists(url): return url
    return None

def scan_candidates(cfg, limit=100, include_used=True) -> List[Dict]:
    base = cfg["video_base_url"].rstrip("/")
    used = set(load_used_list(cfg["state_prefix"]))
    force_name = get_force_next(cfg["state_prefix"])
    results = []
    count_found = 0
    for name in _gen_candidates(cfg):
        base_name, ext = os.path.splitext(name)
        try_names = [name] if ext else [base_name + e for e in _exts()]
        exists_any = False
        final_url = None
        for n in try_names:
            url = f"{base}/{quote(n, safe='')}"
            if _url_exists(url):
                exists_any = True
                final_url = url
                break
        info = {
            "name": name,
            "url": final_url,
            "exists": bool(exists_any),
            "used": name in used,
            "is_force": (name == force_name)
        }
        if info["exists"]:
            if include_used or not info["used"]:
                results.append(info)
                count_found += 1
                if count_found >= limit:
                    break
    return results

def next_video(cfg) -> Tuple[Optional[str], Optional[str]]:
    used = set(load_used_list(cfg["state_prefix"]))
    base = cfg["video_base_url"].rstrip("/")
    # forced first
    force_name = get_force_next(cfg["state_prefix"])
    if force_name and force_name not in used:
        base_name, ext = os.path.splitext(force_name)
        try_names = [force_name] if ext else [base_name + e for e in _exts()]
        for n in try_names:
            url = f"{base}/{quote(n, safe='')}"
            if _url_exists(url):
                local_path = _download_to_tmp(url, os.path.splitext(n)[1] or ".mp4")
                used.add(force_name)
                save_used_list(cfg["state_prefix"], list(used))
                set_force_next(cfg["state_prefix"], None)
                return url, local_path
    # auto-pick
    for name in _gen_candidates(cfg):
        if name in used: continue
        base_name, ext = os.path.splitext(name)
        try_names = [name] if ext else [base_name + e for e in _exts()]
        for n in try_names:
            url = f"{base}/{quote(n, safe='')}"
            if _url_exists(url):
                local_path = _download_to_tmp(url, os.path.splitext(n)[1] or ".mp4")
                used.add(name)
                save_used_list(cfg["state_prefix"], list(used))
                return url, local_path
    return None, None

# =========================
# OAuth & credentials
# =========================
def _normalized_token_file(token_path, state_prefix="yt"):
    token_path = (token_path or "").strip()
    if token_path.lower().replace("\\", "/").rstrip("/\\") in ("", "tokens"):
        token_path = os.path.join("tokens", f"{state_prefix}.json")
    if os.path.isdir(token_path):
        token_path = os.path.join(token_file, f"{state_prefix}.json")
    parent = os.path.dirname(token_path) or "."
    os.makedirs(parent, exist_ok=True)
    return token_path

def _write_token_file(token_path, credentials):
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    data = {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or []),
        "expiry": getattr(credentials, "expiry", None).isoformat() if getattr(credentials, "expiry", None) else None,
    }
    with open(token_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def get_auth_flow_for_account(acct, scopes, redirect_base) -> Flow:
    redirect_uri = redirect_base.rstrip("/") + "/oauth2callback"
    return Flow.from_client_secrets_file(
        acct["client_secrets_file"], scopes=scopes, redirect_uri=redirect_uri
    )

def store_credentials_for_account(acct, credentials):
    token_path = _normalized_token_file(acct.get("token_file"), acct.get("state_prefix", "yt"))
    _write_token_file(token_path, credentials)
    acct["token_file"] = token_path

    accounts = load_accounts()
    updated = False
    for i, a in enumerate(accounts):
        if a.get("state_prefix") == acct.get("state_prefix"):
            accounts[i] = {**a, **acct}
            updated = True
            break
    if not updated:
        accounts.append(acct)
    save_accounts(accounts)

def _load_credentials(token_path) -> Optional[Credentials]:
    try:
        token_path = _normalized_token_file(token_path)
        if not os.path.isfile(token_path): return None
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "client_id" not in data: return None
        creds = Credentials.from_authorized_user_info(data)
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(GARequest())
                _write_token_file(token_path, creds)
            except Exception:
                pass
        return creds
    except Exception:
        return None

REQUIRED_SCOPES = {
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtubepartner",
}

def _read_client_id_from_secrets(client_secrets_file: str) -> str | None:
    try:
        with open(client_secrets_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        container = data.get("installed") or data.get("web") or {}
        return container.get("client_id")
    except Exception:
        return None

def has_valid_credentials(acct):
    creds = _load_credentials(acct.get("token_file"))
    if not creds: return False
    if not REQUIRED_SCOPES.issubset(set(getattr(creds, "scopes", []) or [])):
        return False
    token_client_id = getattr(creds, "client_id", None)
    secrets_client_id = _read_client_id_from_secrets(acct.get("client_secrets_file") or "")
    if not token_client_id or not secrets_client_id or token_client_id != secrets_client_id:
        return False
    return True

def _yt(acct):
    creds = _load_credentials(acct.get("token_file"))
    if not creds:
        raise RuntimeError("No credentials for account; please click Authorize first.")
    return build("youtube", "v3", credentials=creds, static_discovery=False)

# =========================
# Channel info (dashboard)
# =========================
def get_channel_title(acct):
    yt = _yt(acct)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    return items[0]["snippet"]["title"] if items else None

def get_channel_info(acct):
    yt = _yt(acct)
    resp = yt.channels().list(part="snippet,contentDetails", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return None
    it = items[0]
    info = {
        "id": it["id"],
        "title": it["snippet"]["title"],
        "custom_url": it["snippet"].get("customUrl"),
        "uploads_playlist_id": it["contentDetails"]["relatedPlaylists"]["uploads"],
    }
    return info

def get_channel_url(info):
    if not info:
        return None
    if info.get("custom_url"):
        handle = info["custom_url"]
        if not handle.startswith("@"):
            handle = "@" + handle
        return f"https://www.youtube.com/{handle}"
    return f"https://www.youtube.com/channel/{info['id']}"

def list_recent_uploads(acct, max_results=5):
    info = get_channel_info(acct)
    if not info:
        return []
    yt = _yt(acct)
    resp = yt.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=info["uploads_playlist_id"],
        maxResults=max_results
    ).execute()
    out = []
    for it in resp.get("items", []):
        vid = it["contentDetails"]["videoId"]
        out.append({
            "video_id": vid,
            "title": it["snippet"]["title"],
            "publishedAt": it["contentDetails"].get("videoPublishedAt") or it["snippet"].get("publishedAt"),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": (it["snippet"].get("thumbnails", {}).get("medium") or
                      it["snippet"].get("thumbnails", {}).get("default") or {}).get("url")
        })
    return out

# =========================
# Upload / Thumbnail
# =========================
def upload_video(local_path, meta, acct):
    yt = _yt(acct)
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "categoryId": meta.get("category_id", "22"),
            "defaultLanguage": meta.get("default_language") or None,
            "tags": meta.get("tags", []),
        },
        "status": {
            "privacyStatus": meta.get("privacy_status", "private"),
            "selfDeclaredMadeForKids": str(meta.get("self_declared_mfk", "false")).lower() == "true",
        },
    }
    if str(meta.get("made_for_kids", "false")).lower() == "true":
        body["status"]["madeForKids"] = True

    publish_at = (meta.get("schedule_publish_at") or "").strip()
    if publish_at:
        body["status"]["publishAt"] = publish_at
        if body["status"]["privacyStatus"] not in ("private", "unlisted"):
            body["status"]["privacyStatus"] = "private"

    media = MediaFileUpload(local_path, chunksize=8 * 1024 * 1024, resumable=True, mimetype="video/*")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            _, response = request.next_chunk()
    except HttpError as e:
        raise RuntimeError(f"YouTube upload error: {e}")

    video_id = response["id"]

    playlist_id = (meta.get("playlist_id") or "").strip()
    if playlist_id:
        try:
            yt.playlistItems().insert(
                part="snippet",
                body={"snippet": {"playlistId": playlist_id,
                                  "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
        except HttpError:
            # playlist add is best-effort
            pass

    return {"video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}"}

def set_thumbnail(video_id, thumb_path, acct):
    yt = _yt(acct)
    try:
        media = MediaFileUpload(thumb_path, mimetype="image/jpeg")
        yt.thumbnails().set(videoId=video_id, media_body=media).execute()
        return True
    except HttpError:
        return False

def maybe_thumbnail(cfg) -> Tuple[Optional[str], Optional[str]]:
    one = (cfg.get("thumbnail_url") or "").strip()
    if one:
        try:
            path = _download_to_tmp(one, ".jpg")
            return one, path
        except Exception:
            return None, None
    tman = (cfg.get("thumb_manifest_url") or "").strip()
    if tman:
        try:
            lines = fetch_lines(tman)
            idx = load_last_index(cfg["state_prefix"], "thumb_index")
            url = lines[idx % len(lines)]
            path = _download_to_tmp(url, ".jpg")
            save_last_index(cfg["state_prefix"], "thumb_index", idx + 1)
            return url, path
        except Exception:
            return None, None
    base = (cfg.get("thumbnail_base_url") or "").strip()
    if not base: return None, None
    last = load_last_index(cfg["state_prefix"], "thumb_index")
    fn = f"thumb ({last + 1}).jpg"
    url = f"{base}/{quote(fn, safe='')}"
    try:
        path = _download_to_tmp(url, ".jpg")
        save_last_index(cfg["state_prefix"], "thumb_index", last + 1)
        return url, path
    except Exception:
        return None, None

# =========================
# Single account runner (EXPORTED)
# =========================
def run_account(cfg):
    """
    Picks a video (honors force-next), uploads it with title/desc/tags,
    optionally sets thumbnail, updates status, returns upload result dict.
    """
    prefix = cfg["state_prefix"]
    save_status(prefix, "running", "")
    try:
        # Pick + download video
        _, local_video = next_video(cfg)
        if not local_video:
            raise RuntimeError("No videos left / download failed")

        # Compose metadata
        meta = {
            "title": next_title(cfg),
            "description": next_description(cfg),
            "tags": next_tags(cfg),
            "privacy_status": cfg.get("privacy_status", "private"),
            "category_id": cfg.get("category_id", "22"),
            "default_language": cfg.get("default_language", ""),
            "playlist_id": cfg.get("playlist_id", ""),
            "schedule_publish_at": cfg.get("schedule_publish_at", ""),
            "self_declared_mfk": cfg.get("self_declared_mfk", "false"),
            "made_for_kids": cfg.get("made_for_kids", "false"),
        }

        # Upload
        result = upload_video(local_video, meta, cfg)

        # Try thumbnail sources
        _, thumb_local = maybe_thumbnail(cfg)
        if thumb_local:
            try:
                set_thumbnail(result["video_id"], thumb_local, cfg)
            except Exception:
                pass

        save_status(prefix, "success", json.dumps(result))
        return result

    except Exception as e:
        save_status(prefix, "error", str(e))
        return None
