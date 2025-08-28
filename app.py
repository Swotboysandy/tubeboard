#!/usr/bin/env python3
import os, json, threading
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, abort
from yt_runner import (
    load_accounts, save_accounts, load_status, run_account, get_auth_flow_for_account,
    store_credentials_for_account, has_valid_credentials, get_channel_title,
)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-please")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtubepartner",
]

def background_run(acct):
    run_account(acct)

@app.route("/")
def index():
    accounts = load_accounts()
    for acct in accounts:
        acct["status"] = load_status(acct["state_prefix"])
        try:
            acct["authed"] = has_valid_credentials(acct)
        except Exception:
            acct["authed"] = False
        if acct["authed"]:
            try:
                acct["channel_title"] = get_channel_title(acct) or "(Unknown Channel)"
            except Exception:
                acct["channel_title"] = "(Unknown Channel)"
        else:
            acct["channel_title"] = None
    return render_template("index.html", accounts=accounts)

@app.route("/status")
def all_status():
    accounts = load_accounts()
    return jsonify([load_status(acct["state_prefix"]) for acct in accounts])

@app.route("/run/<int:idx>", methods=["POST"])
def run_now(idx):
    accounts = load_accounts()
    if not (0 <= idx < len(accounts)):
        return jsonify({"error": "Invalid account index"}), 400
    if not has_valid_credentials(accounts[idx]):
        return jsonify({"error": "Account is not authorized yet"}), 400
    threading.Thread(target=background_run, args=(accounts[idx],), daemon=True).start()
    return jsonify({"status": "started"}), 202

@app.route("/account/new", methods=["GET", "POST"])
@app.route("/account/<int:idx>/edit", methods=["GET", "POST"])
def account_form(idx=None):
    accounts = load_accounts()
    acct = accounts[idx] if idx is not None and 0 <= idx < len(accounts) else {}
    if request.method == "POST":
        state_prefix = request.form["state_prefix"].strip()
        token_file_raw = request.form["token_file"].strip()

        # Normalize token path: ensure it's a *file*, never a folder
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
            "title_url": request.form.get("title_url", "").strip(),
            "description_url": request.form.get("description_url", "").strip(),
            "tags_url": request.form.get("tags_url", "").strip(),
            "thumbnail_base_url": request.form.get("thumbnail_base_url", "").strip(),
            "slides_per_post": int(request.form.get("slides_per_post", "1")),
            "client_secrets_file": request.form["client_secrets_file"].strip(),
            "token_file": token_file,
            "privacy_status": request.form.get("privacy_status", "private"),
            "category_id": request.form.get("category_id", "22"),
            "made_for_kids": request.form.get("made_for_kids", "false"),
            "self_declared_mfk": request.form.get("self_declared_mfk", "false"),
            "default_language": request.form.get("default_language", "").strip(),
            "playlist_id": request.form.get("playlist_id", "").strip(),
            "schedule_publish_at": request.form.get("schedule_publish_at", "").strip(),
            "enable_comments": request.form.get("enable_comments", "true"),
        }

        if acct and idx is not None:
            accounts[idx] = data
        else:
            accounts.append(data)

        save_accounts(accounts)
        return redirect(url_for("index"))

    return render_template("account_form.html", account=acct)

@app.route("/auth/<int:idx>/start")
def auth_start(idx):
    accounts = load_accounts()
    if not (0 <= idx < len(accounts)):
        abort(404)
    acct = accounts[idx]
    flow = get_auth_flow_for_account(acct, SCOPES, request.host_url.rstrip("/"))
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
    if idx is None or not (0 <= idx < len(accounts)):
        return "Invalid session state", 400
    acct = accounts[idx]
    flow = get_auth_flow_for_account(acct, SCOPES, request.host_url.rstrip("/"))
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    store_credentials_for_account(acct, creds)  # also normalizes acct['token_file']
    # re-save accounts so the normalized token_file persists
    accounts[idx] = acct
    save_accounts(accounts)
    return redirect(url_for("index"))

if __name__ == "__main__":
    # Ensure base folders exist (safe no-ops if present)
    os.makedirs("tokens", exist_ok=True)
    os.makedirs("secrets", exist_ok=True)
    app.run(debug=True)