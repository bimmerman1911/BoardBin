# Boardbin

A single-file Python web app that creates persistent whiteboard-style boards at UUID URLs.

## Features

- Visiting `/` creates a new board and redirects to `/<uuid>`.
- Persistent board state in SQLite.
- Files persist on disk across restarts.
- Pen, marker, eraser, text boxes, move/select mode, and pan mode.
- Drag-and-drop uploads up to 100 MB.
- Images appear on the board and can be resized.
- Other files appear as downloadable cards with file-type icons.
- Simple multi-user refresh model: the browser polls the server and reloads newer board state.
- Mobile-readable responsive layout, optimized for desktop.
- Mouse-wheel zoom, button zoom controls, and space-drag panning.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python boardbin.py
```

Then open `http://YOUR_SERVER:8000/`.

You can change host/port/debug values in `.env`.

## Storage

- SQLite database: `data/boards.sqlite3`
- Uploaded files: `data/uploads/`

Keep the `data/` directory if you want boards and uploads to survive restarts or deployments.

## Notes

- The board uses a large world-space canvas with pannable/zoomable navigation.
- Persistence is last-write-wins.
- Images are served inline; non-image files download as attachments.
- For production, run behind a reverse proxy such as Nginx and use Gunicorn:

```bash
gunicorn -w 2 -b 0.0.0.0:8000 boardbin:app
```
