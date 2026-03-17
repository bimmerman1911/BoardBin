from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    render_template_string,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "boards.sqlite3"
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
CANVAS_WIDTH = 4000
CANVAS_HEIGHT = 2400

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE


def load_env_settings(env_path: Path) -> dict[str, str]:
    settings: dict[str, str] = {}
    if not env_path.exists():
        return settings

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip().strip("\"'")
    return settings


def parse_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


ENV_SETTINGS = load_env_settings(BASE_DIR / ".env")
HOST = os.environ.get("HOST", ENV_SETTINGS.get("HOST", "0.0.0.0"))
PORT = int(os.environ.get("PORT", ENV_SETTINGS.get("PORT", "8000")))
DEBUG = parse_bool(os.environ.get("DEBUG", ENV_SETTINGS.get("DEBUG", "0")))
MAX_CONCURRENT_USERS = int(os.environ.get("MAX_CONCURRENT_USERS", ENV_SETTINGS.get("MAX_CONCURRENT_USERS", "25")))
PRESENCE_TTL_SECONDS = int(os.environ.get("PRESENCE_TTL_SECONDS", ENV_SETTINGS.get("PRESENCE_TTL_SECONDS", "45")))


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS boards (
            id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            board_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER NOT NULL,
            is_image INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_files_board_id ON files(board_id);

        CREATE TABLE IF NOT EXISTS board_sessions (
            board_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (board_id, session_id),
            FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_board_sessions_board_id ON board_sessions(board_id);
        CREATE INDEX IF NOT EXISTS idx_board_sessions_expires_at ON board_sessions(expires_at);

        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            message_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(board_id) REFERENCES boards(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_chat_messages_board_id_id ON chat_messages(board_id, id);
        """
    )
    conn.commit()
    conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expiry_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=PRESENCE_TTL_SECONDS)).isoformat()


def prune_expired_sessions(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM board_sessions WHERE expires_at < ?", (now_iso(),))


def active_user_count(db: sqlite3.Connection, board_id: str) -> int:
    row = db.execute("SELECT COUNT(*) AS total FROM board_sessions WHERE board_id = ?", (board_id,)).fetchone()
    return int(row["total"] if row else 0)


init_db()


def default_state(board_id: str) -> dict[str, Any]:
    return {
        "boardId": board_id,
        "canvas": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
        "viewport": {"x": 0, "y": 0, "zoom": 1},
        "strokes": [],
        "texts": [],
        "assets": [],
    }


def get_board(board_id: str) -> sqlite3.Row | None:
    db = get_db()
    return db.execute("SELECT * FROM boards WHERE id = ?", (board_id,)).fetchone()


def create_board(board_id: str | None = None) -> str:
    db = get_db()
    board_id = board_id or str(uuid.uuid4())
    ts = now_iso()
    state = json.dumps(default_state(board_id))
    db.execute(
        "INSERT INTO boards(id, state_json, version, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
        (board_id, state, ts, ts),
    )
    db.commit()
    return board_id


def ensure_board(board_id: str) -> sqlite3.Row:
    board = get_board(board_id)
    if board is None:
        abort(404, description="Board not found")
    return board


def board_file_url(file_id: str) -> str:
    return url_for("download_file", file_id=file_id)


@app.errorhandler(413)
def file_too_large(_: Exception) -> tuple[Response, int]:
    return jsonify({"error": "File too large. Maximum size is 100 MB."}), 413


@app.get("/")
def root() -> Response:
    board_id = create_board()
    return redirect(f"/{board_id}")


@app.get("/<board_id>")
def board_page(board_id: str) -> str:
    ensure_board(board_id)
    return render_template_string(PAGE_TEMPLATE, board_id=board_id, max_file_size_mb=MAX_FILE_SIZE // (1024 * 1024))


@app.get("/api/board/<board_id>")
def get_board_state(board_id: str) -> Response:
    board = ensure_board(board_id)
    db = get_db()
    prune_expired_sessions(db)
    db.commit()
    state = json.loads(board["state_json"])
    return jsonify(
        {
            "boardId": board_id,
            "version": board["version"],
            "updatedAt": board["updated_at"],
            "activeUsers": active_user_count(db, board_id),
            "state": state,
        }
    )


@app.post("/api/board/<board_id>/presence")
def board_presence(board_id: str) -> Response:
    ensure_board(board_id)
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("sessionId", "")).strip()
    if not session_id:
        return jsonify({"error": "Missing sessionId."}), 400

    db = get_db()
    prune_expired_sessions(db)
    existing = db.execute(
        "SELECT 1 FROM board_sessions WHERE board_id = ? AND session_id = ?",
        (board_id, session_id),
    ).fetchone()
    count = active_user_count(db, board_id)

    if existing is None and count >= MAX_CONCURRENT_USERS:
        return jsonify({"error": f"Board is full ({MAX_CONCURRENT_USERS} concurrent users).", "maxUsers": MAX_CONCURRENT_USERS}), 429

    ts = now_iso()
    db.execute(
        """
        INSERT INTO board_sessions(board_id, session_id, last_seen_at, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(board_id, session_id)
        DO UPDATE SET last_seen_at = excluded.last_seen_at, expires_at = excluded.expires_at
        """,
        (board_id, session_id, ts, expiry_iso()),
    )
    db.commit()
    return jsonify({"ok": True, "activeUsers": active_user_count(db, board_id), "maxUsers": MAX_CONCURRENT_USERS})


@app.post("/api/board/<board_id>/presence/leave")
def board_presence_leave(board_id: str) -> Response:
    ensure_board(board_id)
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("sessionId", "")).strip()
    if session_id:
        db = get_db()
        db.execute("DELETE FROM board_sessions WHERE board_id = ? AND session_id = ?", (board_id, session_id))
        db.commit()
    return jsonify({"ok": True})


@app.post("/api/board/<board_id>/save")
def save_board_state(board_id: str) -> Response:
    board = ensure_board(board_id)
    payload = request.get_json(silent=True)
    if not payload or "state" not in payload:
        return jsonify({"error": "Missing state payload."}), 400

    state = payload["state"]
    if not isinstance(state, dict):
        return jsonify({"error": "State must be a JSON object."}), 400

    state["boardId"] = board_id
    current_version = int(board["version"])
    client_version = int(payload.get("version", current_version))
    new_version = max(current_version, client_version) + 1
    ts = now_iso()
    db = get_db()
    db.execute(
        "UPDATE boards SET state_json = ?, version = ?, updated_at = ? WHERE id = ?",
        (json.dumps(state), new_version, ts, board_id),
    )
    db.commit()
    return jsonify({"ok": True, "version": new_version, "updatedAt": ts})


@app.post("/api/board/<board_id>/upload")
def upload_to_board(board_id: str) -> Response:
    ensure_board(board_id)
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    incoming = request.files["file"]
    if incoming.filename == "":
        return jsonify({"error": "No file selected."}), 400

    original_name = secure_filename(incoming.filename) or "file"
    ext = Path(original_name).suffix
    file_id = str(uuid.uuid4())
    stored_name = f"{file_id}{ext}"
    destination = UPLOAD_DIR / stored_name
    incoming.save(destination)
    size = destination.stat().st_size
    if size > MAX_FILE_SIZE:
        destination.unlink(missing_ok=True)
        return jsonify({"error": "File too large. Maximum size is 100 MB."}), 413

    mime_type = incoming.mimetype or mimetypes.guess_type(original_name)[0] or "application/octet-stream"
    is_image = int(mime_type.startswith("image/"))

    db = get_db()
    db.execute(
        """
        INSERT INTO files(id, board_id, original_name, stored_name, mime_type, size_bytes, is_image, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file_id, board_id, original_name, stored_name, mime_type, size, is_image, now_iso()),
    )
    db.commit()

    return jsonify(
        {
            "ok": True,
            "file": {
                "id": file_id,
                "name": original_name,
                "mimeType": mime_type,
                "sizeBytes": size,
                "isImage": bool(is_image),
                "downloadUrl": board_file_url(file_id),
                "previewUrl": board_file_url(file_id) if is_image else None,
            },
        }
    )


@app.post("/api/board/<board_id>/clear")
def clear_board(board_id: str) -> Response:
    ensure_board(board_id)
    db = get_db()
    file_rows = db.execute("SELECT stored_name FROM files WHERE board_id = ?", (board_id,)).fetchall()

    for row in file_rows:
        (UPLOAD_DIR / row["stored_name"]).unlink(missing_ok=True)

    ts = now_iso()
    reset_state = json.dumps(default_state(board_id))
    db.execute("DELETE FROM files WHERE board_id = ?", (board_id,))
    db.execute("DELETE FROM chat_messages WHERE board_id = ?", (board_id,))
    db.execute(
        "UPDATE boards SET state_json = ?, version = version + 1, updated_at = ? WHERE id = ?",
        (reset_state, ts, board_id),
    )
    db.commit()

    row = db.execute("SELECT version FROM boards WHERE id = ?", (board_id,)).fetchone()
    return jsonify({"ok": True, "version": row["version"], "updatedAt": ts})


@app.get("/api/board/<board_id>/chat")
def get_chat_messages(board_id: str) -> Response:
    ensure_board(board_id)
    after_id = request.args.get("after", "0")
    try:
        after = max(0, int(after_id))
    except ValueError:
        return jsonify({"error": "Invalid 'after' value."}), 400

    db = get_db()
    rows = db.execute(
        """
        SELECT id, author_name, message_text, created_at
        FROM chat_messages
        WHERE board_id = ? AND id > ?
        ORDER BY id ASC
        LIMIT 120
        """,
        (board_id, after),
    ).fetchall()
    messages = [
        {
            "id": int(row["id"]),
            "author": row["author_name"],
            "message": row["message_text"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]
    latest_id = messages[-1]["id"] if messages else after
    return jsonify({"messages": messages, "latestId": latest_id})


@app.post("/api/board/<board_id>/chat")
def post_chat_message(board_id: str) -> Response:
    ensure_board(board_id)
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("sessionId", "")).strip()
    author = str(payload.get("author", "")).strip()
    message = str(payload.get("message", "")).strip()

    if not session_id:
        return jsonify({"error": "Missing sessionId."}), 400
    if not author:
        return jsonify({"error": "Missing author."}), 400
    if not message:
        return jsonify({"error": "Message cannot be empty."}), 400
    if len(author) > 32:
        return jsonify({"error": "Author is too long."}), 400
    if len(message) > 500:
        return jsonify({"error": "Message is too long (max 500 chars)."}), 400

    db = get_db()
    ts = now_iso()
    row = db.execute(
        """
        INSERT INTO chat_messages(board_id, session_id, author_name, message_text, created_at)
        VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (board_id, session_id, author, message, ts),
    ).fetchone()
    db.commit()
    return jsonify({"ok": True, "message": {"id": int(row["id"]), "author": author, "message": message, "createdAt": ts}})


@app.get("/files/<file_id>")
def download_file(file_id: str) -> Response:
    db = get_db()
    row = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        abort(404)
    return send_from_directory(
        UPLOAD_DIR,
        row["stored_name"],
        mimetype=row["mime_type"] or "application/octet-stream",
        as_attachment=not bool(row["is_image"]),
        download_name=row["original_name"],
    )


PAGE_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Boardbin</title>
  <style>
    :root {
      --bg: #0b1020;
      --panel: rgba(14, 20, 37, 0.78);
      --panel-strong: rgba(17, 24, 39, 0.92);
      --border: rgba(255,255,255,0.12);
      --text: #f5f7fb;
      --muted: #9aa6bf;
      --accent: #7c9cff;
      --accent-2: #8b5cf6;
      --success: #11c47f;
      --shadow: 0 16px 50px rgba(0,0,0,.35);
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0;
      height: 100%;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(124,156,255,.22), transparent 35%),
        radial-gradient(circle at top right, rgba(139,92,246,.18), transparent 32%),
        linear-gradient(180deg, #0b1020 0%, #0f172a 100%);
      overflow: hidden;
    }
    .shell {
      position: fixed;
      inset: 0;
      display: grid;
      grid-template-columns: 88px 1fr;
      gap: 14px;
      padding: 14px;
    }
    .toolbar {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 28px;
      backdrop-filter: blur(16px);
      box-shadow: var(--shadow);
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 16px 10px;
      gap: 10px;
      z-index: 20;
    }
    .tool-btn, .swatch, .mini-btn {
      border: 1px solid transparent;
      background: rgba(255,255,255,.05);
      color: var(--text);
      width: 52px;
      height: 52px;
      border-radius: 18px;
      display: grid;
      place-items: center;
      cursor: pointer;
      transition: .18s ease;
      user-select: none;
    }
    .tool-btn:hover, .swatch:hover, .mini-btn:hover { transform: translateY(-1px); background: rgba(255,255,255,.08); }
    .tool-btn.active, .swatch.active { border-color: rgba(255,255,255,.35); background: rgba(124,156,255,.18); }
    .divider { width: 48px; height: 1px; background: rgba(255,255,255,.08); margin: 4px 0; }
    .swatch { width: 34px; height: 34px; border-radius: 999px; }
    .main {
      min-width: 0;
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 12px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 22px;
      border-radius: 24px;
      background: var(--panel);
      border: 1px solid var(--border);
      backdrop-filter: blur(16px);
      box-shadow: var(--shadow);
      z-index: 10;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand-logo {
      width: 40px;
      height: 40px;
      border-radius: 14px;
      background: linear-gradient(135deg, rgba(124,156,255,.28), rgba(139,92,246,.36));
      border: 1px solid rgba(255,255,255,.16);
      box-shadow: inset 0 1px 0 rgba(255,255,255,.18), 0 8px 20px rgba(0,0,0,.2);
      display: grid;
      place-items: center;
      flex: 0 0 auto;
    }
    .brand-stack { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
    .brand h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -.02em; }
    .brand p { margin: 0; color: var(--muted); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .status-wrap { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,.04);
      border: 1px solid rgba(255,255,255,.06);
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--success); box-shadow: 0 0 0 6px rgba(17,196,127,.12); }
    .board-wrap {
      position: relative;
      border-radius: 28px;
      overflow: hidden;
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
      background: linear-gradient(180deg, #10172c 0%, #0f172a 100%);
      backdrop-filter: blur(4px);
      touch-action: none;
      cursor: crosshair;
    }
    .board-wrap[data-tool="select"], .board-wrap[data-tool="text"] { cursor: default; }
    .board-wrap[data-tool="pan"] { cursor: grab; }
    .board-wrap.is-panning { cursor: grabbing; }
    .grid-layer {
      position: absolute;
      inset: 0;
      background:
        linear-gradient(90deg, rgba(255,255,255,.05) 1px, transparent 1px),
        linear-gradient(0deg, rgba(255,255,255,.05) 1px, transparent 1px),
        linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
      background-size: 40px 40px, 40px 40px, auto;
      opacity: .9;
      pointer-events: none;
    }
    .viewport {
      position: absolute;
      inset: 0;
      overflow: hidden;
    }
    .board {
      position: absolute;
      left: 0;
      top: 0;
      width: 12000px;
      height: 12000px;
      transform-origin: 0 0;
      will-change: transform;
    }
    .layer { position: absolute; inset: 0; }
    #strokesSvg { overflow: visible; }
    .drop-hint {
      position: absolute;
      inset: 24px;
      border: 2px dashed rgba(124,156,255,.44);
      border-radius: 24px;
      display: none;
      align-items: center;
      justify-content: center;
      text-align: center;
      background: rgba(124,156,255,.10);
      z-index: 40;
      font-size: 18px;
      font-weight: 700;
      pointer-events: none;
    }
    .drop-hint.visible { display: flex; }
    .text-box {
      position: absolute;
      min-width: 160px;
      min-height: 56px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(10,14,24,.78);
      border: 1px solid rgba(255,255,255,.12);
      color: var(--text);
      box-shadow: 0 12px 30px rgba(0,0,0,.25);
      outline: none;
      cursor: move;
      font-size: 18px;
      line-height: 1.45;
      white-space: pre-wrap;
    }
    .text-box:focus { border-color: rgba(124,156,255,.6); box-shadow: 0 0 0 4px rgba(124,156,255,.18), 0 12px 30px rgba(0,0,0,.25); }
    .asset {
      position: absolute;
      border-radius: 22px;
      background: rgba(8,11,19,.84);
      border: 1px solid rgba(255,255,255,.12);
      box-shadow: 0 18px 40px rgba(0,0,0,.28);
      overflow: hidden;
      backdrop-filter: blur(12px);
      cursor: move;
      min-width: 120px;
      min-height: 80px;
    }
    .asset.selected, .text-box.selected { outline: 2px solid rgba(124,156,255,.55); outline-offset: 2px; }
    .asset img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
      pointer-events: none;
      background: rgba(255,255,255,.02);
    }
    .file-card {
      width: 240px;
      min-height: 110px;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 16px;
    }
    .file-icon {
      width: 54px;
      height: 54px;
      border-radius: 16px;
      background: linear-gradient(135deg, rgba(124,156,255,.26), rgba(139,92,246,.26));
      display: grid;
      place-items: center;
      font-size: 24px;
      flex: 0 0 auto;
    }
    .file-meta { min-width: 0; }
    .file-name { font-weight: 700; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .file-sub { margin-top: 6px; color: var(--muted); font-size: 12px; }
    .file-actions { margin-top: 10px; display: flex; gap: 8px; }
    .link-btn {
      display: inline-flex;
      padding: 8px 10px;
      border-radius: 12px;
      color: white;
      text-decoration: none;
      font-size: 12px;
      background: rgba(124,156,255,.18);
      border: 1px solid rgba(124,156,255,.26);
    }
    .resize-handle {
      position: absolute;
      right: 8px;
      bottom: 8px;
      width: 16px;
      height: 16px;
      border-radius: 6px;
      background: rgba(255,255,255,.9);
      box-shadow: 0 2px 8px rgba(0,0,0,.25);
      cursor: nwse-resize;
    }
    .zoom-chip {
      min-width: 78px;
      justify-content: center;
      font-variant-numeric: tabular-nums;
    }
    .chatbox {
      position: absolute;
      right: 18px;
      bottom: 18px;
      width: min(360px, calc(100% - 36px));
      max-height: min(320px, calc(100% - 36px));
      border-radius: 18px;
      background: rgba(8,11,19,.8);
      border: 1px solid rgba(255,255,255,.1);
      color: var(--muted);
      font-size: 13px;
      backdrop-filter: blur(14px);
      z-index: 12;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .chat-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 10px 12px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      color: var(--text);
      font-weight: 600;
    }
    .chat-header small { color: var(--muted); font-weight: 500; }
    .chat-log {
      padding: 10px 12px;
      overflow-y: auto;
      min-height: 120px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .chat-empty { color: var(--muted); font-size: 12px; }
    .chat-item { display: grid; gap: 2px; }
    .chat-meta { color: #c9d3e6; font-size: 12px; }
    .chat-text { color: var(--text); white-space: pre-wrap; word-break: break-word; }
    .chat-form {
      display: flex;
      gap: 8px;
      padding: 10px;
      border-top: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.02);
    }
    .chat-input {
      flex: 1;
      min-width: 0;
      border-radius: 12px;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(7,10,17,.9);
      color: var(--text);
      font-size: 13px;
      padding: 8px 10px;
      outline: none;
    }
    .chat-input:focus { border-color: rgba(124,156,255,.6); }
    .chat-send {
      border: 1px solid rgba(124,156,255,.35);
      background: rgba(124,156,255,.25);
      color: var(--text);
      border-radius: 12px;
      font-size: 13px;
      padding: 8px 12px;
      cursor: pointer;
    }
    @media (max-width: 920px) {
      .shell {
        grid-template-columns: 1fr;
        grid-template-rows: auto auto 1fr;
        padding: 10px;
      }
      .toolbar {
        flex-direction: row;
        overflow-x: auto;
        justify-content: flex-start;
        border-radius: 22px;
        padding: 12px;
      }
      .divider { width: 1px; height: 40px; }
      .tool-btn, .mini-btn { width: 44px; height: 44px; border-radius: 14px; }
      .swatch { width: 28px; height: 28px; }
      .topbar { padding: 14px 16px; border-radius: 20px; align-items: flex-start; flex-direction: column; }
      .brand p { white-space: normal; }
      .chatbox { width: min(300px, calc(100% - 24px)); bottom: 12px; right: 12px; }
      .status-wrap { justify-content: flex-start; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="toolbar" aria-label="Toolbar">
      <button class="tool-btn active" data-tool="pen" title="Pen">✒️</button>
      <button class="tool-btn" data-tool="marker" title="Marker">🖍️</button>
      <button class="tool-btn" data-tool="eraser" title="Eraser">🩹</button>
      <button class="tool-btn" data-tool="text" title="Add text">T</button>
      <button class="tool-btn" data-tool="select" title="Move items">🖱️</button>
      <button class="tool-btn" data-tool="pan" title="Pan board">✋</button>
      <div class="divider"></div>
      <button class="mini-btn" id="undoBtn" title="Undo">↶</button>
      <button class="mini-btn" id="clearBtn" title="Clear board">⌫</button>
      <button class="mini-btn" id="zoomOutBtn" title="Zoom out">－</button>
      <button class="mini-btn" id="zoomInBtn" title="Zoom in">＋</button>
      <div class="divider"></div>
      <button class="swatch active" data-color="#f8fafc" style="background:#f8fafc"></button>
      <button class="swatch" data-color="#7c9cff" style="background:#7c9cff"></button>
      <button class="swatch" data-color="#11c47f" style="background:#11c47f"></button>
      <button class="swatch" data-color="#ffd166" style="background:#ffd166"></button>
      <button class="swatch" data-color="#ff6b6b" style="background:#ff6b6b"></button>
      <button class="swatch" data-color="#c084fc" style="background:#c084fc"></button>
      <div class="divider"></div>
      <button class="mini-btn" id="centerBtn" title="Center view">◎</button>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="brand">
          <div class="brand-logo" aria-hidden="true">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <rect x="4" y="4" width="16" height="16" rx="4" stroke="white" stroke-opacity="0.85" stroke-width="1.4"/>
              <path d="M8.2 8.7H12.9C14.9 8.7 16.3 9.8 16.3 11.7C16.3 13.6 14.9 14.8 12.9 14.8H8.2V8.7Z" fill="white" fill-opacity="0.9"/>
              <path d="M8.2 15.5H13.2C15.2 15.5 16.6 16.4 16.6 18.1C16.6 19.9 15.2 20.8 13.2 20.8H8.2V15.5Z" fill="white" fill-opacity="0.5"/>
            </svg>
          </div>
          <div class="brand-stack">
            <h1>Boardbin</h1>
            <p>Board URL: <span id="boardUrl"></span></p>
          </div>
        </div>
        <div class="status-wrap">
          <div class="status"><span class="dot"></span><span id="statusText">Loading…</span></div>
          <div class="status" id="presenceText">👥 0 online</div>
          <div class="status zoom-chip" id="zoomChip">100%</div>
        </div>
      </header>

      <section class="board-wrap" id="boardWrap" data-tool="pen">
        <div class="grid-layer" id="gridLayer"></div>
        <div class="viewport" id="viewport">
          <div class="drop-hint" id="dropHint">Drop files anywhere on the board<br><span style="font-weight:500;font-size:14px;opacity:.85">Images become resizable cards. Other files become downloadable tiles.</span></div>
          <div class="board" id="board">
            <svg class="layer" id="strokesSvg"></svg>
            <div class="layer" id="objectsLayer"></div>
          </div>
        </div>
        <div class="chatbox" aria-label="Board chat">
          <div class="chat-header">
            <span>Chat</span>
            <small id="chatIdentity"></small>
          </div>
          <div class="chat-log" id="chatLog">
            <div class="chat-empty">Chat lives for this board and resets when the board is cleared.</div>
          </div>
          <form class="chat-form" id="chatForm">
            <input class="chat-input" id="chatInput" maxlength="500" placeholder="Message everyone on this board" autocomplete="off" />
            <button class="chat-send" type="submit">Send</button>
          </form>
        </div>
      </section>
    </main>
  </div>

<script>
(() => {
  const BOARD_ID = {{ board_id|tojson }};
  const SAVE_DEBOUNCE_MS = 700;
  const POLL_MS = 4000;
  const CHAT_POLL_MS = 2000;
  const PRESENCE_HEARTBEAT_MS = 15000;
  const WORLD_WIDTH = 12000;
  const WORLD_HEIGHT = 12000;
  const DEFAULT_VIEW = { x: WORLD_WIDTH / 2, y: WORLD_HEIGHT / 2, zoom: 1 };

  const boardWrap = document.getElementById('boardWrap');
  const viewport = document.getElementById('viewport');
  const board = document.getElementById('board');
  const svg = document.getElementById('strokesSvg');
  const objectsLayer = document.getElementById('objectsLayer');
  const statusText = document.getElementById('statusText');
  const dropHint = document.getElementById('dropHint');
  const boardUrl = document.getElementById('boardUrl');
  const zoomChip = document.getElementById('zoomChip');
  const presenceText = document.getElementById('presenceText');
  const gridLayer = document.getElementById('gridLayer');
  const chatLog = document.getElementById('chatLog');
  const chatForm = document.getElementById('chatForm');
  const chatInput = document.getElementById('chatInput');
  const chatIdentity = document.getElementById('chatIdentity');
  boardUrl.textContent = window.location.href;

  const state = {
    boardId: BOARD_ID,
    canvas: { width: WORLD_WIDTH, height: WORLD_HEIGHT },
    viewport: { ...DEFAULT_VIEW },
    strokes: [],
    texts: [],
    assets: [],
  };

  let version = 1;
  let selectedTool = 'pen';
  let selectedColor = '#f8fafc';
  let drawing = false;
  let currentStroke = null;
  let erasing = false;
  let eraserPoints = [];
  let saveTimer = null;
  let isDirty = false;
  let isSaving = false;
  let lastAppliedVersion = 0;
  let interaction = null;
  let panKeyDown = false;
  let lastChatId = 0;
  let isSendingChat = false;

  const userName = (() => {
    const key = `boardbin-chat-name-${BOARD_ID}`;
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const adjectives = ['Blue', 'Swift', 'Calm', 'Bright', 'Brave', 'Sunny', 'Quick', 'Merry', 'Clever', 'Cosmic'];
    const animals = ['Otter', 'Fox', 'Panda', 'Lynx', 'Hawk', 'Koala', 'Dolphin', 'Raven', 'Tiger', 'Falcon'];
    const name = `${adjectives[Math.floor(Math.random() * adjectives.length)]}${animals[Math.floor(Math.random() * animals.length)]}${Math.floor(Math.random() * 90 + 10)}`;
    window.sessionStorage.setItem(key, name);
    return name;
  })();
  chatIdentity.textContent = `You are ${userName}`;
  const sessionId = (() => {
    const key = `boardbin-session-${BOARD_ID}`;
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const created = uid();
    window.sessionStorage.setItem(key, created);
    return created;
  })();

  function setStatus(text) {
    statusText.textContent = text;
  }

  function setPresence(users, maxUsers = null) {
    if (!Number.isFinite(users)) return;
    presenceText.textContent = maxUsers ? `👥 ${users} online · max ${maxUsers}` : `👥 ${users} online`;
  }


  function formatTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function appendChatMessage(item) {
    const row = document.createElement('div');
    row.className = 'chat-item';
    row.dataset.id = item.id;
    row.innerHTML = `<div class="chat-meta">${item.author} · ${formatTime(item.createdAt)}</div><div class="chat-text"></div>`;
    row.querySelector('.chat-text').textContent = item.message;
    chatLog.appendChild(row);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  function clearChatIfReset() {
    if (chatLog.querySelector('.chat-item')) {
      chatLog.innerHTML = '<div class="chat-empty">Chat lives for this board and resets when the board is cleared.</div>';
    }
  }

  async function fetchChatMessages() {
    try {
      const res = await fetch(`/api/board/${BOARD_ID}/chat?after=${lastChatId}`);
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || 'Failed to load chat');
      if (Array.isArray(payload.messages) && payload.messages.length) {
        if (chatLog.querySelector('.chat-empty')) {
          chatLog.innerHTML = '';
        }
        payload.messages.forEach(msg => appendChatMessage(msg));
      }
      if (Number.isFinite(payload.latestId)) {
        lastChatId = Math.max(lastChatId, payload.latestId);
      }
    } catch (err) {
      console.error('Chat poll failed', err);
    }
  }

  async function sendChatMessage(text) {
    if (isSendingChat) return;
    isSendingChat = true;
    try {
      const res = await fetch(`/api/board/${BOARD_ID}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId, author: userName, message: text }),
      });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || 'Failed to send message');
      chatInput.value = '';
      await fetchChatMessages();
    } catch (err) {
      alert(`Chat: ${err.message}`);
    } finally {
      isSendingChat = false;
    }
  }

  function uid() {
    return (crypto.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function clamp(n, min, max) {
    return Math.max(min, Math.min(max, n));
  }

  function bytesText(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function fileIcon(name, mime) {
    const ext = (name.split('.').pop() || '').toLowerCase();
    if ((mime || '').startsWith('image/')) return '🖼️';
    if (['pdf'].includes(ext)) return '📕';
    if (['doc', 'docx', 'txt', 'md', 'rtf'].includes(ext)) return '📄';
    if (['xls', 'xlsx', 'csv'].includes(ext)) return '📊';
    if (['ppt', 'pptx', 'key'].includes(ext)) return '📽️';
    if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) return '🗜️';
    if (['mp4', 'mov', 'mkv', 'webm'].includes(ext)) return '🎞️';
    if (['mp3', 'wav', 'flac', 'ogg'].includes(ext)) return '🎵';
    return '📦';
  }

  function activateTool(tool) {
    selectedTool = tool;
    boardWrap.dataset.tool = tool;
    document.querySelectorAll('[data-tool]').forEach(btn => btn.classList.toggle('active', btn.dataset.tool === tool));
  }

  function scheduleSave() {
    isDirty = true;
    setStatus('Unsaved changes…');
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveBoard, SAVE_DEBOUNCE_MS);
  }

  async function saveBoard() {
    if (!isDirty || isSaving) return;
    isSaving = true;
    setStatus('Saving…');
    try {
      const res = await fetch(`/api/board/${BOARD_ID}/save`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ state, version }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Save failed');
      version = data.version;
      lastAppliedVersion = version;
      isDirty = false;
      setStatus('All changes saved');
    } catch (err) {
      console.error(err);
      setStatus(`Save failed: ${err.message}`);
    } finally {
      isSaving = false;
    }
  }

  function normalizeViewport(vp) {
    const incoming = vp || {};
    const x = Number.isFinite(incoming.x) ? incoming.x : DEFAULT_VIEW.x;
    const y = Number.isFinite(incoming.y) ? incoming.y : DEFAULT_VIEW.y;
    const zoom = clamp(Number.isFinite(incoming.zoom) ? incoming.zoom : 1, 0.2, 4);
    return { x, y, zoom };
  }

  function viewportSize() {
    return { width: viewport.clientWidth, height: viewport.clientHeight };
  }

  function constrainViewport() {
    const { width, height } = viewportSize();
    const zoom = clamp(state.viewport.zoom, 0.2, 4);
    const halfW = width / (2 * zoom);
    const halfH = height / (2 * zoom);
    state.viewport.zoom = zoom;
    state.viewport.x = clamp(state.viewport.x, halfW, WORLD_WIDTH - halfW);
    state.viewport.y = clamp(state.viewport.y, halfH, WORLD_HEIGHT - halfH);
  }

  function applyViewport() {
    constrainViewport();
    const { width, height } = viewportSize();
    const { x, y, zoom } = state.viewport;
    const screenX = width / 2 - x * zoom;
    const screenY = height / 2 - y * zoom;
    board.style.transform = `translate(${screenX}px, ${screenY}px) scale(${zoom})`;
    zoomChip.textContent = `${Math.round(zoom * 100)}%`;
    const gridSize = 40 * zoom;
    gridLayer.style.backgroundSize = `${gridSize}px ${gridSize}px, ${gridSize}px ${gridSize}px, auto`;
    gridLayer.style.backgroundPosition = `${screenX}px ${screenY}px, ${screenX}px ${screenY}px, 0 0`;
  }

  function pointFromClient(clientX, clientY) {
    const rect = viewport.getBoundingClientRect();
    return {
      x: (clientX - rect.left - rect.width / 2) / state.viewport.zoom + state.viewport.x,
      y: (clientY - rect.top - rect.height / 2) / state.viewport.zoom + state.viewport.y,
    };
  }

  function pointFromEvent(event) {
    const p = pointFromClient(event.clientX, event.clientY);
    return {
      x: clamp(p.x, 0, WORLD_WIDTH),
      y: clamp(p.y, 0, WORLD_HEIGHT),
    };
  }

  function zoomAt(clientX, clientY, factor) {
    const before = pointFromClient(clientX, clientY);
    state.viewport.zoom = clamp(state.viewport.zoom * factor, 0.2, 4);
    const rect = viewport.getBoundingClientRect();
    state.viewport.x = before.x - (clientX - rect.left - rect.width / 2) / state.viewport.zoom;
    state.viewport.y = before.y - (clientY - rect.top - rect.height / 2) / state.viewport.zoom;
    applyViewport();
    scheduleSave();
  }

  async function loadBoard() {
    setStatus('Loading board…');
    const res = await fetch(`/api/board/${BOARD_ID}`);
    const payload = await res.json();
    version = payload.version;
    lastAppliedVersion = payload.version;
    setPresence(payload.activeUsers || 0);
    Object.assign(state, payload.state);
    state.viewport = normalizeViewport(payload.state.viewport);
    if (!payload.state.viewport || (!payload.state.viewport.zoom && !payload.state.viewport.x && !payload.state.viewport.y)) {
      state.viewport = { ...DEFAULT_VIEW };
    }
    renderAll();
    applyViewport();
    setStatus('Ready');
  }

  async function pollForUpdates() {
    if (isDirty || isSaving) return;
    try {
      const res = await fetch(`/api/board/${BOARD_ID}`);
      const payload = await res.json();
      setPresence(payload.activeUsers || 0);
      if (payload.version > lastAppliedVersion) {
        version = payload.version;
        lastAppliedVersion = payload.version;
        Object.assign(state, payload.state);
        state.viewport = normalizeViewport(payload.state.viewport);
        renderAll();
        applyViewport();
        setStatus('Board refreshed');
        if ((payload.state.strokes?.length || 0) === 0 && (payload.state.texts?.length || 0) === 0 && (payload.state.assets?.length || 0) === 0) {
          lastChatId = 0;
          clearChatIfReset();
          fetchChatMessages();
        }
      }
    } catch (err) {
      console.error('Poll failed', err);
    }
  }

  async function sendPresenceHeartbeat() {
    try {
      const res = await fetch(`/api/board/${BOARD_ID}/presence`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionId }),
      });
      const payload = await res.json();
      if (!res.ok) {
        throw new Error(payload.error || 'Presence check failed');
      }
      setPresence(payload.activeUsers, payload.maxUsers);
    } catch (err) {
      console.error(err);
      if (String(err.message || '').includes('Board is full')) {
        alert(err.message);
      }
    }
  }

  function strokeWidth(tool) {
    if (tool === 'marker') return 16;
    if (tool === 'eraser') return 26;
    return 4;
  }

  function beginStroke(event) {
    if (!['pen', 'marker'].includes(selectedTool)) return;
    drawing = true;
    const p = pointFromEvent(event);
    currentStroke = {
      id: uid(),
      tool: selectedTool,
      color: selectedColor,
      width: strokeWidth(selectedTool),
      points: [p],
      opacity: selectedTool === 'marker' ? 0.35 : 1,
    };
    state.strokes.push(currentStroke);
    renderStroke(currentStroke);
  }

  function pointNearStroke(point, stroke, radius) {
    const threshold = radius + (stroke.width || 0) / 2;
    const thresholdSq = threshold * threshold;
    return stroke.points.some(strokePoint => {
      const dx = strokePoint.x - point.x;
      const dy = strokePoint.y - point.y;
      return (dx * dx + dy * dy) <= thresholdSq;
    });
  }

  function eraseAtPoint(point) {
    const radius = strokeWidth('eraser') / 2;
    const before = state.strokes.length;
    state.strokes = state.strokes.filter(stroke => !pointNearStroke(point, stroke, radius));
    if (state.strokes.length !== before) {
      renderAll();
      return true;
    }
    return false;
  }

  function beginErase(event) {
    erasing = true;
    eraserPoints = [];
    const p = pointFromEvent(event);
    eraserPoints.push(p);
    if (eraseAtPoint(p)) scheduleSave();
  }

  function continueErase(event) {
    if (!erasing) return;
    const p = pointFromEvent(event);
    eraserPoints.push(p);
    if (eraseAtPoint(p)) scheduleSave();
  }

  function endErase() {
    if (!erasing) return;
    erasing = false;
    eraserPoints = [];
  }

  function continueStroke(event) {
    if (!drawing || !currentStroke) return;
    const p = pointFromEvent(event);
    currentStroke.points.push(p);
    const path = document.querySelector(`[data-stroke-id="${currentStroke.id}"]`);
    if (path) path.setAttribute('d', toPath(currentStroke.points));
  }

  function endStroke() {
    if (!drawing) return;
    drawing = false;
    currentStroke = null;
    scheduleSave();
  }

  function toPath(points) {
    if (points.length === 1) {
      const p = points[0];
      return `M ${p.x} ${p.y} L ${p.x + 0.1} ${p.y + 0.1}`;
    }
    let d = `M ${points[0].x} ${points[0].y}`;
    for (let i = 1; i < points.length; i++) {
      const p = points[i];
      const prev = points[i - 1];
      const midX = (prev.x + p.x) / 2;
      const midY = (prev.y + p.y) / 2;
      d += ` Q ${prev.x} ${prev.y} ${midX} ${midY}`;
    }
    const last = points[points.length - 1];
    return d + ` T ${last.x} ${last.y}`;
  }

  function renderStroke(stroke) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', toPath(stroke.points));
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', stroke.color);
    path.setAttribute('stroke-width', stroke.width);
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('stroke-opacity', stroke.opacity || 1);
    path.dataset.strokeId = stroke.id;
    svg.appendChild(path);
  }

  function renderAll() {
    svg.innerHTML = '';
    objectsLayer.innerHTML = '';
    state.strokes.forEach(renderStroke);
    state.texts.forEach(renderText);
    state.assets.forEach(renderAsset);
  }

  function clearSelections() {
    objectsLayer.querySelectorAll('.selected').forEach(el => el.classList.remove('selected'));
  }

  function deleteSelectedObjects() {
    const selectedIds = [...objectsLayer.querySelectorAll('.asset.selected, .text-box.selected')]
      .map(el => el.dataset.id)
      .filter(Boolean);
    if (!selectedIds.length) return 0;
    const selectedSet = new Set(selectedIds);
    const textBefore = state.texts.length;
    const assetsBefore = state.assets.length;
    state.texts = state.texts.filter(item => !selectedSet.has(item.id));
    state.assets = state.assets.filter(item => !selectedSet.has(item.id));
    const deleted = (textBefore - state.texts.length) + (assetsBefore - state.assets.length);
    if (deleted > 0) {
      renderAll();
      scheduleSave();
    }
    return deleted;
  }

  function renderText(item) {
    const div = document.createElement('div');
    div.className = 'text-box';
    div.contentEditable = 'false';
    div.dataset.editing = 'false';
    div.dataset.id = item.id;
    div.style.left = `${item.x}px`;
    div.style.top = `${item.y}px`;
    div.style.width = `${item.width || 240}px`;
    div.style.minHeight = `${item.height || 56}px`;
    div.textContent = item.text || '';
    div.addEventListener('input', () => {
      if (div.dataset.editing !== 'true') return;
      item.text = div.textContent;
      item.height = div.offsetHeight;
      item.width = div.offsetWidth;
      scheduleSave();
    });
    div.addEventListener('dblclick', (event) => {
      event.preventDefault();
      event.stopPropagation();
      startEditingText(div, item, false);
    });
    div.addEventListener('blur', () => stopEditingText(div, item));
    div.addEventListener('focus', () => {
      clearSelections();
      div.classList.add('selected');
    });
    enableDrag(div, item);
    objectsLayer.appendChild(div);
  }

  function renderAsset(item) {
    const wrap = document.createElement('div');
    wrap.className = 'asset';
    wrap.dataset.id = item.id;
    wrap.style.left = `${item.x}px`;
    wrap.style.top = `${item.y}px`;
    wrap.style.width = `${item.width}px`;
    wrap.style.height = `${item.height}px`;

    if (item.kind === 'image') {
      const img = document.createElement('img');
      img.src = item.previewUrl;
      img.alt = item.name;
      wrap.appendChild(img);

      const handle = document.createElement('div');
      handle.className = 'resize-handle';
      handle.addEventListener('pointerdown', (ev) => beginResize(ev, wrap, item));
      wrap.appendChild(handle);
    } else {
      wrap.style.height = `${item.height || 118}px`;
      const card = document.createElement('div');
      card.className = 'file-card';
      card.innerHTML = `
        <div class="file-icon">${fileIcon(item.name, item.mimeType)}</div>
        <div class="file-meta">
          <div class="file-name" title="${item.name}">${item.name}</div>
          <div class="file-sub">${item.mimeType || 'file'} · ${bytesText(item.sizeBytes || 0)}</div>
          <div class="file-actions"><a class="link-btn" href="${item.downloadUrl}" download>Download</a></div>
        </div>`;
      wrap.appendChild(card);
    }
    wrap.addEventListener('pointerdown', () => {
      clearSelections();
      wrap.classList.add('selected');
    });
    enableDrag(wrap, item);
    objectsLayer.appendChild(wrap);
  }

  function enableDrag(el, item) {
    el.addEventListener('pointerdown', (event) => {
      if (event.target.closest('.resize-handle')) return;
      if (event.target.tagName === 'A') return;
      if (el.dataset.editing === 'true') return;
      if (!['select', 'text'].includes(selectedTool)) return;
      clearSelections();
      el.classList.add('selected');
      interaction = {
        mode: 'move',
        target: el,
        item,
        startX: event.clientX,
        startY: event.clientY,
        originX: item.x,
        originY: item.y,
      };
      el.setPointerCapture(event.pointerId);
      event.preventDefault();
      event.stopPropagation();
    });
  }

  function beginResize(event, el, item) {
    clearSelections();
    el.classList.add('selected');
    interaction = {
      mode: 'resize',
      target: el,
      item,
      startX: event.clientX,
      startY: event.clientY,
      originW: item.width,
      originH: item.height,
    };
    el.setPointerCapture(event.pointerId);
    event.preventDefault();
    event.stopPropagation();
  }

  function addTextAtPoint(point) {
    const item = { id: uid(), x: point.x, y: point.y, width: 240, height: 58, text: '' };
    state.texts.push(item);
    renderText(item);
    scheduleSave();
    const el = objectsLayer.querySelector(`.text-box[data-id="${item.id}"]`);
    if (el) {
      startEditingText(el, item, true);
    }
  }

  function placeCaret(el, toStart = false) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    range.collapse(toStart);
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function startEditingText(el, item, toStart = false) {
    el.contentEditable = 'true';
    el.dataset.editing = 'true';
    clearSelections();
    el.classList.add('selected');
    el.focus();
    placeCaret(el, toStart);
    item.width = el.offsetWidth;
    item.height = el.offsetHeight;
  }

  function stopEditingText(el, item) {
    if (el.dataset.editing !== 'true') return;
    el.dataset.editing = 'false';
    el.contentEditable = 'false';
    item.text = el.textContent;
    item.width = el.offsetWidth;
    item.height = el.offsetHeight;
    scheduleSave();
  }

  function beginPan(event) {
    interaction = {
      mode: 'pan',
      startX: event.clientX,
      startY: event.clientY,
      originX: state.viewport.x,
      originY: state.viewport.y,
    };
    boardWrap.classList.add('is-panning');
    boardWrap.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  }

  function shouldPan(event) {
    return selectedTool === 'pan' || panKeyDown || event.button === 1 || event.button === 2;
  }

  window.addEventListener('pointermove', (event) => {
    if (interaction?.mode === 'move') {
      const dx = (event.clientX - interaction.startX) / state.viewport.zoom;
      const dy = (event.clientY - interaction.startY) / state.viewport.zoom;
      interaction.item.x = clamp(interaction.originX + dx, 0, WORLD_WIDTH - 40);
      interaction.item.y = clamp(interaction.originY + dy, 0, WORLD_HEIGHT - 40);
      interaction.target.style.left = `${interaction.item.x}px`;
      interaction.target.style.top = `${interaction.item.y}px`;
    } else if (interaction?.mode === 'resize') {
      const dx = (event.clientX - interaction.startX) / state.viewport.zoom;
      const dy = (event.clientY - interaction.startY) / state.viewport.zoom;
      interaction.item.width = clamp(interaction.originW + dx, 120, 2400);
      interaction.item.height = clamp(interaction.originH + dy, 80, 1800);
      interaction.target.style.width = `${interaction.item.width}px`;
      interaction.target.style.height = `${interaction.item.height}px`;
    } else if (interaction?.mode === 'pan') {
      const dx = (event.clientX - interaction.startX) / state.viewport.zoom;
      const dy = (event.clientY - interaction.startY) / state.viewport.zoom;
      state.viewport.x = interaction.originX - dx;
      state.viewport.y = interaction.originY - dy;
      applyViewport();
    }
  });

  window.addEventListener('pointerup', () => {
    if (interaction) {
      if (interaction.mode === 'move' || interaction.mode === 'resize' || interaction.mode === 'pan') {
        scheduleSave();
      }
      interaction = null;
      boardWrap.classList.remove('is-panning');
    }
  });

  viewport.addEventListener('pointerdown', (event) => {
    if (shouldPan(event)) {
      beginPan(event);
      return;
    }
    if (event.target.closest('.asset, .text-box')) return;
    clearSelections();
    if (selectedTool === 'eraser') {
      beginErase(event);
      event.preventDefault();
      return;
    }
    if (['pen', 'marker'].includes(selectedTool)) {
      beginStroke(event);
      event.preventDefault();
      return;
    }
    if (selectedTool === 'text') {
      addTextAtPoint(pointFromEvent(event));
      event.preventDefault();
    }
  });

  viewport.addEventListener('pointermove', continueStroke);
  viewport.addEventListener('pointermove', continueErase);
  window.addEventListener('pointerup', endStroke);
  window.addEventListener('pointerup', endErase);
  viewport.addEventListener('pointerleave', endStroke);
  viewport.addEventListener('pointerleave', endErase);
  viewport.addEventListener('contextmenu', (event) => event.preventDefault());

  document.querySelectorAll('[data-tool]').forEach(btn => btn.addEventListener('click', () => activateTool(btn.dataset.tool)));
  document.querySelectorAll('[data-color]').forEach(btn => {
    btn.addEventListener('click', () => {
      selectedColor = btn.dataset.color;
      document.querySelectorAll('[data-color]').forEach(s => s.classList.toggle('active', s === btn));
    });
  });

  document.getElementById('undoBtn').addEventListener('click', () => {
    if (state.assets.length > 0) state.assets.pop();
    else if (state.texts.length > 0) state.texts.pop();
    else if (state.strokes.length > 0) state.strokes.pop();
    renderAll();
    scheduleSave();
  });

  document.getElementById('clearBtn').addEventListener('click', async () => {
    if (!confirm('Clear the whole board?')) return;
    setStatus('Clearing board…');
    try {
      const res = await fetch(`/api/board/${BOARD_ID}/clear`, { method: 'POST' });
      const payload = await res.json();
      if (!res.ok) throw new Error(payload.error || 'Failed to clear board');
      version = payload.version;
      lastAppliedVersion = version;
      isDirty = false;
      state.strokes = [];
      state.texts = [];
      state.assets = [];
      renderAll();
      setStatus('Board cleared');
      chatLog.innerHTML = '<div class="chat-empty">Chat lives for this board and resets when the board is cleared.</div>';
      lastChatId = 0;
    } catch (err) {
      console.error(err);
      setStatus(`Clear failed: ${err.message}`);
    }
  });

  document.getElementById('centerBtn').addEventListener('click', () => {
    state.viewport = { ...DEFAULT_VIEW, zoom: 1 };
    applyViewport();
    scheduleSave();
  });

  document.getElementById('zoomInBtn').addEventListener('click', () => {
    const rect = viewport.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1.15);
  });

  document.getElementById('zoomOutBtn').addEventListener('click', () => {
    const rect = viewport.getBoundingClientRect();
    zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, 1 / 1.15);
  });

  viewport.addEventListener('wheel', (event) => {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.08 : 1 / 1.08;
    zoomAt(event.clientX, event.clientY, factor);
  }, { passive: false });

  window.addEventListener('resize', applyViewport);

  window.addEventListener('keydown', (event) => {
    const activeEl = document.activeElement;
    const isTyping = activeEl && (
      activeEl.tagName === 'INPUT'
      || activeEl.tagName === 'TEXTAREA'
      || activeEl.isContentEditable
    );

    if (event.code === 'Space' && !event.repeat) {
      if (isTyping) return;
      panKeyDown = true;
      boardWrap.classList.add('is-panning');
      event.preventDefault();
      return;
    }

    if (event.key !== 'Delete' && event.key !== 'Backspace') return;
    if (isTyping) return;
    if (deleteSelectedObjects() > 0) {
      event.preventDefault();
    }
  });

  window.addEventListener('keyup', (event) => {
    if (event.code === 'Space') {
      panKeyDown = false;
      if (!interaction || interaction.mode !== 'pan') {
        boardWrap.classList.remove('is-panning');
      }
    }
  });

  async function uploadFiles(files, dropEvent) {
    const base = pointFromEvent(dropEvent);
    let offset = 0;
    for (const file of files) {
      const form = new FormData();
      form.append('file', file);
      try {
        setStatus(`Uploading ${file.name}…`);
        const res = await fetch(`/api/board/${BOARD_ID}/upload`, { method: 'POST', body: form });
        const payload = await res.json();
        if (!res.ok) throw new Error(payload.error || 'Upload failed');
        const f = payload.file;
        const item = f.isImage
          ? {
              id: uid(), kind: 'image', fileId: f.id, name: f.name, x: base.x + offset, y: base.y + offset,
              width: 320, height: 220, mimeType: f.mimeType, sizeBytes: f.sizeBytes,
              previewUrl: f.previewUrl, downloadUrl: f.downloadUrl,
            }
          : {
              id: uid(), kind: 'file', fileId: f.id, name: f.name, x: base.x + offset, y: base.y + offset,
              width: 240, height: 118, mimeType: f.mimeType, sizeBytes: f.sizeBytes,
              downloadUrl: f.downloadUrl,
            };
        state.assets.push(item);
        renderAsset(item);
        scheduleSave();
        offset += 28;
      } catch (err) {
        alert(`${file.name}: ${err.message}`);
      }
    }
    setStatus('All changes saved');
  }

  ['dragenter', 'dragover'].forEach(name => {
    viewport.addEventListener(name, (event) => {
      event.preventDefault();
      dropHint.classList.add('visible');
    });
  });
  ['dragleave', 'drop'].forEach(name => {
    viewport.addEventListener(name, (event) => {
      event.preventDefault();
      if (name === 'drop') dropHint.classList.remove('visible');
      if (name === 'dragleave' && event.target === viewport) dropHint.classList.remove('visible');
    });
  });

  viewport.addEventListener('drop', async (event) => {
    dropHint.classList.remove('visible');
    const files = [...event.dataTransfer.files];
    if (!files.length) return;
    await uploadFiles(files, event);
  });

  chatForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    await sendChatMessage(text);
  });

  window.addEventListener('beforeunload', () => {
    navigator.sendBeacon(`/api/board/${BOARD_ID}/presence/leave`, new Blob([JSON.stringify({ sessionId })], { type: 'application/json' }));
    if (isDirty) {
      navigator.sendBeacon(`/api/board/${BOARD_ID}/save`, new Blob([JSON.stringify({ state, version })], { type: 'application/json' }));
    }
  });

  loadBoard();
  fetchChatMessages();
  sendPresenceHeartbeat();
  setInterval(sendPresenceHeartbeat, PRESENCE_HEARTBEAT_MS);
  setInterval(pollForUpdates, POLL_MS);
  setInterval(fetchChatMessages, CHAT_POLL_MS);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)
