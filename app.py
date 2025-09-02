#!/usr/bin/env python3
import os, json, threading
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort, flash
from yt_runner import (
    load_accounts, save_accounts, load_status, run_account, get_auth_flow_for_account,
    store_credentials_for_account, has_valid_credentials, get_channel_title,
    # new imports
    peek_next_video_url, reset_used_list, scan_candidates, set_force_next,
    get_channel_info, get_channel_url, list_recent_uploads
)

load_dotenv()

app = Flask(__name__, static_url_path="/static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-please")

# Enable http on localhost during dev
if os.getenv("FLASK_DEBUG", "false").lower() in ("1","true","yes"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtubepartner",
]

_run_locks = {}

def _safe_idx(idx, accounts):
    return idx is not None and 0 <= idx < len(accounts)

def _redirect_base():
    return request.url_root.rstrip("/")

def background_run(acct, idx: int):
    try:
        _run_locks[idx] = True
        run_account(acct)
    finally:
        _run_locks[idx] = False

@app.route("/")
def index():
    accounts = load_accounts()
    for acct in accounts:
        st = load_status(acct["state_prefix"])
        acct["status"] = st
        acct["status_parsed"] = None
        if st.get("status") == "success":
            try:
                acct["status_parsed"] = json.loads(st.get("message") or "{}")
            except Exception:
                pass
        try:
            acct["authed"] = has_valid_credentials(acct)
        except Exception:
            acct["authed"] = False
        if acct["authed"]:
            try:
                info = get_channel_info(acct)
                acct["channel_title"] = info["title"] if info else "(Unknown Channel)"
                acct["channel_url"] = get_channel_url(info) if info else None
            except Exception:
                acct["channel_title"] = "(Unknown Channel)"
                acct["channel_url"] = None
        else:
            acct["channel_title"] = None
            acct["channel_url"] = None
    return render_template("index.html", accounts=accounts)

@app.route("/status")
def all_status():
    accounts = load_accounts()
    return jsonify([load_status(acct["state_prefix"]) for acct in accounts])

@app.route("/run/<int:idx>", methods=["POST"])
def run_now(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    if _run_locks.get(idx):
        return jsonify({"error": "Already running"}), 429
    if not has_valid_credentials(accounts[idx]):
        return jsonify({"error": "Account is not authorized yet"}), 400
    t = threading.Thread(target=background_run, args=(accounts[idx], idx), daemon=True)
    t.start()
    return jsonify({"status": "started"}), 202

# ---- NEW: preview/scan/force/used endpoints ----
@app.route("/preview/<int:idx>")
def preview_next(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    url = peek_next_video_url(accounts[idx])
    return jsonify({"next_video_url": url})

@app.route("/scan/<int:idx>")
def scan(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    limit = int(request.args.get("limit", 50))
    include_used = request.args.get("include_used", "true").lower() in ("1","true","yes")
    items = scan_candidates(accounts[idx], limit=limit, include_used=include_used)
    return jsonify(items)

@app.route("/force-next/<int:idx>", methods=["POST"])
def force_next(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing name"}), 400
    set_force_next(accounts[idx]["state_prefix"], name)
    return jsonify({"status": "ok", "forced": name})

@app.route("/used/<int:idx>")
def used_list(idx):
    from yt_runner import load_used_list
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    used = load_used_list(accounts[idx]["state_prefix"])
    return jsonify(used)

@app.route("/used/<int:idx>/clear", methods=["POST"])
def clear_used(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    reset_used_list(accounts[idx]["state_prefix"])
    return jsonify({"status":"ok"})

# ---- Channel info / latest (optional) ----
@app.route("/latest/<int:idx>")
def latest_uploads(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error": "Invalid account index"}), 400
    if not has_valid_credentials(accounts[idx]):
        return jsonify({"error": "Account is not authorized"}), 400
    items = list_recent_uploads(accounts[idx], max_results=int(request.args.get("n", 5)))
    return jsonify(items)

# ---- Account CRUD & OAuth ----
@app.route("/account/new", methods=["GET", "POST"])
@app.route("/account/<int:idx>/edit", methods=["GET", "POST"])
def account_form(idx=None):
    accounts = load_accounts()
    acct = accounts[idx] if _safe_idx(idx, accounts) else {}
    if request.method == "POST":
        state_prefix = request.form["state_prefix"].strip()
        token_file_raw = request.form["token_file"].strip()

        token_file = token_file_raw.replace("\\", "/").rstrip("/")
        if token_file.lower() in ("", "tokens"):
            token_file = f"tokens/{state_prefix}.json"
        if os.path.isdir(token_file):
            token_file = os.path.join(token_file, f"{state_prefix}.json")
        os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)

        data = {
            "name": request.form["name"].strip(),
            "state_prefix": state_prefix,
            "type": request.form["type"].strip(),
            "video_base_url": request.form["video_base_url"].strip(),
            "manifest_url": request.form.get("manifest_url", "").strip(),
            "title_url": request.form.get("title_url", "").strip(),
            "description_url": request.form.get("description_url", "").strip(),
            "tags_url": request.form.get("tags_url", "").strip(),

            # Thumbnail sources
            "thumbnail_url": request.form.get("thumbnail_url", "").strip(),
            "thumb_manifest_url": request.form.get("thumb_manifest_url", "").strip(),
            "thumbnail_base_url": request.form.get("thumbnail_base_url", "").strip(),

            "slides_per_post": int(request.form.get("slides_per_post", "1") or "1"),
            "client_secrets_file": request.form["client_secrets_file"].strip(),
            "token_file": token_file,
            "privacy_status": request.form.get("privacy_status", "private"),
            "category_id": request.form.get("category_id", "22"),
            "made_for_kids": request.form.get("made_for_kids", "false"),
            "self_declared_mfk": request.form.get("self_declared_mfk", "false"),
            "default_language": request.form.get("default_language", "").strip(),
            "playlist_id": request.form.get("playlist_id", "").strip(),
            "schedule_publish_at": request.form.get("schedule_publish_at", "").strip(),
            "include_plain_vid": request.form.get("include_plain_vid", "auto").strip(),
            "max_index": int(request.form.get("max_index", "2000") or "2000"),
        }

        if acct and idx is not None:
            accounts[idx] = data
        else:
            accounts.append(data)

        save_accounts(accounts)
        return redirect(url_for("index"))

    return render_template("account_form.html", account=acct)

@app.route("/account/<int:idx>/delete", methods=["POST"])
def account_delete(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return jsonify({"error":"Invalid account index"}), 400
    accounts.pop(idx)
    save_accounts(accounts)
    flash("Account deleted")
    return redirect(url_for("index"))

@app.route("/auth/<int:idx>/start")
def auth_start(idx):
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        abort(404)
    acct = accounts[idx]
    flow = get_auth_flow_for_account(acct, SCOPES, _redirect_base())
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    session["oauth_state"] = state
    session["oauth_idx"] = idx
    return redirect(auth_url)

@app.route("/oauth2callback")
def oauth2callback():
    idx = session.get("oauth_idx")
    accounts = load_accounts()
    if not _safe_idx(idx, accounts):
        return "Invalid session state", 400
    acct = accounts[idx]
    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_secrets_file(
            acct["client_secrets_file"],
            scopes=SCOPES,
            state=session.get("oauth_state")
        )
        flow.redirect_uri = _redirect_base() + "/oauth2callback"
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        store_credentials_for_account(acct, creds)
        accounts[idx] = acct
        save_accounts(accounts)
        flash("Authorization successful")
        return redirect(url_for("index"))
    except Exception as e:
        return f"Authorization failed: {e}", 400

if __name__ == "__main__":
    os.makedirs("tokens", exist_ok=True)
    os.makedirs("secrets", exist_ok=True)
    app.run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG","true").lower() in ("1","true","yes"))
