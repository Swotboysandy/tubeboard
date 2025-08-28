#!/usr/bin/env python3
import os, json, random, requests, tempfile
from urllib.parse import quote
from datetime import datetime
from dotenv import load_dotenv

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

load_dotenv()

ACCOUNTS_FILE = "accounts.json"
STATUS_SUFFIX = "_status.json"

def status_path(prefix):
    return f"{prefix}{STATUS_SUFFIX}"

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

# ---------------- State helpers ----------------
def load_last_index(prefix, key):
    fn = f"{prefix}_{key}.json"
    if not os.path.isfile(fn):
        return 0
    return json.load(open(fn, "r", encoding="utf-8")).get("last_index", 0)

def save_last_index(prefix, key, idx):
    fn = f"{prefix}_{key}.json"
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"last_index": idx}, f, indent=2)

def load_used_list(prefix):
    fn = f"{prefix}_video_used.json"
    if not os.path.isfile(fn):
        return []
    return json.load(open(fn, "r", encoding="utf-8")).get("used", [])

def save_used_list(prefix, used):
    fn = f"{prefix}_video_used.json"
    with open(fn, "w", encoding="utf-8") as f:
        json.dump({"used": used}, f, indent=2)

# ---------------- Accounts ----------------
def load_accounts(path=ACCOUNTS_FILE):
    return json.load(open(path, "r", encoding="utf-8")) if os.path.isfile(path) else []

def save_accounts(accounts, path=ACCOUNTS_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)

# ---------------- Content fetchers ----------------
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
            if chunk:
                f.write(chunk)
    return path

# ---------------- Robust candidate picking ----------------
def _url_exists(url, timeout=12):
    """Fast existence check that survives hosts that don't support HEAD."""
    try:
        h = requests.head(url, timeout=timeout, allow_redirects=True)
        if h.status_code == 405:  # HEAD not allowed
            g = requests.get(url, timeout=timeout, stream=True)
            g.close()
            return g.status_code < 400
        return h.status_code < 400
    except Exception:
        return False

def _candidate_names_from_manifest(cfg):
    manifest = (cfg.get("manifest_url") or "").strip()
    if not manifest:
        return None
    try:
        names = [ln for ln in fetch_lines(manifest)
                 if ln.lower().endswith((".mp4", ".mov", ".m4v", ".webm"))]
        return names or None
    except Exception:
        return None

def next_video(cfg):
    """
    Pick the first *unused* existing video:
      1) If cfg['manifest_url'] is set, use that ordered list (one filename per line).
      2) Else, try 'vid.mp4' (optional) then 'vid (1).mp4'..'vid (max_index).mp4'.
    Skips gaps automatically; supports .mp4/.mov/.m4v/.webm; remembers what was used.
    Optional config:
      - include_plain_vid: "auto" (default), "never", "always"
      - max_index: int upper bound (default 2000)
    """
    used = set(load_used_list(cfg["state_prefix"]))
    base = cfg["video_base_url"].rstrip("/")

    # (A) Manifest-driven list (optional)
    manifest_names = _candidate_names_from_manifest(cfg)
    if manifest_names:
        candidates = manifest_names
    else:
        # (B) Generated names
        max_index = int(cfg.get("max_index", 2000))
        include_plain = (cfg.get("include_plain_vid", "auto")).lower()  # auto|never|always
        candidates = []
        if include_plain in ("auto", "always"):
            candidates.append("vid.mp4")  # harmless if missing; we’ll skip it
        candidates.extend([f"vid ({i}).mp4" for i in range(1, max_index + 1)])

    # Try given name or swap extension if missing
    exts = [".mp4", ".mov", ".m4v", ".webm"]

    # Shuffle the search lightly to avoid hammering the same gap if many parallel runs
    # (comment out if you prefer strict order)
    # random.shuffle(candidates)

    for name in candidates:
        if name in used:
            continue

        base_name, ext = os.path.splitext(name)
        try_names = [name] if ext else [base_name + e for e in exts]

        for n in try_names:
            url = f"{base}/{quote(n, safe='')}"
            if _url_exists(url):
                # Download & mark used
                local_path = _download_to_tmp(url, os.path.splitext(n)[1] or ".mp4")
                used.add(name)  # mark by the logical candidate to keep state consistent
                save_used_list(cfg["state_prefix"], list(used))
                return url, local_path

    return None, None

def maybe_thumbnail(cfg):
    base = (cfg.get("thumbnail_base_url") or "").strip()
    if not base:
        return None, None
    last = load_last_index(cfg["state_prefix"], "thumb_index")
    fn = f"thumb ({last + 1}).jpg"
    url = f"{base}/{quote(fn, safe='')}"
    try:
        path = _download_to_tmp(url, ".jpg")
        save_last_index(cfg["state_prefix"], "thumb_index", last + 1)
        return url, path
    except Exception:
        return None, None

# ---------------- OAuth helpers ----------------
def _normalized_token_file(token_path, state_prefix="yt"):
    """Ensure token path is a *file*; auto-fix common mistakes like pointing to 'tokens' directory."""
    token_path = (token_path or "").strip()
    # treat pure 'tokens' (directory) or empty as no file → default to tokens/<state_prefix>.json
    if token_path.lower().replace("\\", "/").rstrip("/\\") in ("", "tokens"):
        token_path = os.path.join("tokens", f"{state_prefix}.json")
    # if someone pointed to a directory, convert to file under it
    if os.path.isdir(token_path):
        token_path = os.path.join(token_path, f"{state_prefix}.json")
    # ensure parent exists
    parent = os.path.dirname(token_path) or "."
    os.makedirs(parent, exist_ok=True)
    return token_path

def get_auth_flow_for_account(acct, scopes, host_base):
    redirect_uri = host_base + "/oauth2callback"
    return Flow.from_client_secrets_file(
        acct["client_secrets_file"], scopes=scopes, redirect_uri=redirect_uri
    )

def store_credentials_for_account(acct, credentials):
    token_path = _normalized_token_file(acct.get("token_file"), acct.get("state_prefix", "yt"))
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
    # write back normalized path so the app uses the corrected one
    acct["token_file"] = token_path
    save_accounts([acct] + [a for a in load_accounts() if a.get("state_prefix") != acct.get("state_prefix")])

def _load_credentials(token_path):
    try:
        token_path = _normalized_token_file(token_path)
        if not os.path.isfile(token_path):
            return None
        with open(token_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # sanity check: minimally require client_id
        if not isinstance(data, dict) or "client_id" not in data:
            return None
        return Credentials.from_authorized_user_info(data)
    except Exception:
        return None

def has_valid_credentials(acct):
    return _load_credentials(acct.get("token_file")) is not None

def _yt(acct):
    creds = _load_credentials(acct.get("token_file"))
    if not creds:
        raise RuntimeError("No credentials for account; please click Authorize first.")
    return build("youtube", "v3", credentials=creds, static_discovery=False)

def get_channel_title(acct):
    yt = _yt(acct)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    return items[0]["snippet"]["title"] if items else None

# ---------------- Upload / Publish ----------------
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

    media = MediaFileUpload(local_path, chunksize=1024 * 1024 * 8, resumable=True, mimetype="video/*")
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
                body={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
            ).execute()
        except HttpError:
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

# ---------------- Single account runner ----------------
def run_account(cfg):
    prefix = cfg["state_prefix"]
    save_status(prefix, "running", "")
    try:
        _, local_video = next_video(cfg)
        if not local_video:
            raise RuntimeError("No videos left / or download failed")

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

        result = upload_video(local_video, meta, cfg)

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

# --------------- CLI ---------------
if __name__ == "__main__":
    for a in load_accounts():
        print(f"\n→ {a.get('name')}")
        print("  ", run_account(a))
    print("\nAll finished.")
