# YouTube Click Uploader (Flask)

A click-to-run web UI that uploads the **next** video from your remote folder to YouTube with proper title, description, tags, privacy, scheduling, thumbnails, and playlist add—modeled after your Instagram runner.

## Features
- Multiple “accounts” (profiles) with their own state prefix and OAuth token
- Click **Authorize** once per account, then **Run** to upload the next video
- Pulls content from your URLs:
  - Video files: `vid.mp4`, `vid (1).mp4`, `vid (2).mp4`, ...
  - Titles: one per line (from `title_url`)
  - Descriptions: one per line (from `description_url`)
  - Tags: comma or space separated (from `tags_url`)
  - Thumbnails: `thumb (1).jpg`, `thumb (2).jpg`, ...
- Status JSON per account (e.g., `yt_main_status.json`)

## Quick Start
1. **Create a Google Cloud project** → enable **YouTube Data API v3**.
2. Configure **OAuth consent screen** (Internal or External).
3. Create **OAuth 2.0 Client ID** (type: *Web application*). Add authorized redirect URIs:
   - `http://127.0.0.1:5000/oauth2callback`
   - (Optionally) `http://localhost:5000/oauth2callback`
4. Download the client file as `secrets/client_secret.json`.
5. Create folders: `secrets/` and `tokens/` (or any paths you prefer).
6. Create `.env` in the project root:
   ```env
   FLASK_SECRET_KEY=super-secret-change-me
   ```
7. Install deps:
   ```bash
   pip install -r requirements.txt
   ```
8. Copy `accounts.json.sample` to `accounts.json` and edit your first account:
   - Set `client_secrets_file`, `token_file`
   - Set `video_base_url`, `title_url`, `description_url`, etc.
9. Run the app:
   ```bash
   python app.py
   ```
10. Open `http://127.0.0.1:5000`:
    - Click **Authorize** on your account and finish OAuth.
    - Click **Run** to upload the next video.

## Notes
- **Shorts**: YouTube treats videos as Shorts based on vertical aspect ratio and length (<60s). Use `type: "short"` to label it in your UI—upload logic is identical; ensure your video meets Shorts criteria.
- **Scheduling**: If `schedule_publish_at` is set (e.g., `2025-08-30T06:30:00Z`), we keep privacy `private` until publish time.
- **Comments**: The Data API doesn't provide a clean toggle to fully disable comments for all cases; it's mostly controlled by *Made for Kids* status or Studio settings. The app attempts a best-effort.
- **Playlists**: If you provide `playlist_id`, the video will be added after upload.
- **State files**: We record used videos and increment indices for titles/descriptions/tags, just like your Instagram runner.

## File Layout
```
app.py
yt_runner.py
templates/
  index.html
  account_form.html
accounts.json.sample
requirements.txt
README.md
```

## Troubleshooting
- **invalid_grant / redirect_uri_mismatch**: Ensure your OAuth client’s redirect URI matches `http://127.0.0.1:5000/oauth2callback`.
- **Upload 403 / quota**: YouTube API quotas are strict. Keep requests minimal, avoid unnecessary calls.
- **Thumbnails fail**: Make sure your image is JPG/PNG within YT limits (<2MB typical).

---

Made to mirror your IG app’s flow: simple, explicit, click-to-run, and stateful.
