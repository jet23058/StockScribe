#!/usr/bin/env python3
"""Local web UI for StockScribe."""

from __future__ import annotations

import json
import mimetypes
import pathlib
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from stock_scribe import StockScribe, StockScribeError


ROOT = pathlib.Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


class StockScribeHandler(BaseHTTPRequestHandler):
    server_version = "StockScribeLocal/1.0"

    def do_GET(self) -> None:
        if self.path.startswith("/api/jobs/"):
            job_id = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            self._send_job(job_id)
            return
        if self.path == "/" or self.path.startswith("/?"):
            self._serve_file(WEB_ROOT / "index.html")
            return
        requested = self.path.split("?", 1)[0].lstrip("/")
        self._serve_file(WEB_ROOT / requested)

    def do_HEAD(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._serve_file(WEB_ROOT / "index.html", include_body=False)
            return
        requested = self.path.split("?", 1)[0].lstrip("/")
        self._serve_file(WEB_ROOT / requested, include_body=False)

    def do_POST(self) -> None:
        if self.path == "/api/jobs":
            self._create_job()
            return
        if self.path != "/api/snapshot":
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_json()
            url = str(payload.get("url") or "").strip()
            if not url:
                raise StockScribeError("Please enter an article URL.")
            snapshot = StockScribe().snapshot_url(
                url,
                start=_blank_to_none(payload.get("start")),
                end=_blank_to_none(payload.get("end")),
                market=str(payload.get("market") or "auto"),
                force_refresh=bool(payload.get("force_refresh")),
            )
        except StockScribeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except Exception as exc:
            self._send_json({"error": f"Unexpected server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json(snapshot, HTTPStatus.OK)

    def _create_job(self) -> None:
        try:
            payload = self._read_json()
            url = str(payload.get("url") or "").strip()
            if not url:
                raise StockScribeError("Please enter an article URL.")
        except StockScribeError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "state": "queued",
            "progress": {"stage": "queued", "current": 0, "total": 1, "message": "等待開始"},
            "result": None,
            "error": None,
            "created_at": time.time(),
        }
        with JOBS_LOCK:
            JOBS[job_id] = job

        thread = threading.Thread(target=_run_snapshot_job, args=(job_id, payload), daemon=True)
        thread.start()
        self._send_json({"job_id": job_id}, HTTPStatus.ACCEPTED)

    def _send_job(self, job_id: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if job:
                payload = dict(job)
            else:
                payload = None
        if payload is None:
            self._send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(payload, HTTPStatus.OK)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _serve_file(self, path: pathlib.Path, *, include_body: bool = True) -> None:
        path = path.resolve()
        if not str(path).startswith(str(WEB_ROOT.resolve())) or not path.exists() or path.is_dir():
            self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _blank_to_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _run_snapshot_job(job_id: str, payload: dict[str, Any]) -> None:
    def set_progress(progress: dict[str, Any]) -> None:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["state"] = "running"
            job["progress"] = progress

    try:
        set_progress({"stage": "fetch", "current": 0, "total": 1, "message": "讀取文章網址"})
        snapshot = StockScribe().snapshot_url(
            str(payload.get("url") or "").strip(),
            start=_blank_to_none(payload.get("start")),
            end=_blank_to_none(payload.get("end")),
            market=str(payload.get("market") or "auto"),
            force_refresh=bool(payload.get("force_refresh")),
            progress=set_progress,
        )
    except StockScribeError as exc:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["state"] = "error"
            job["error"] = str(exc)
            job["progress"] = {"stage": "error", "current": 1, "total": 1, "message": str(exc)}
        return
    except Exception as exc:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["state"] = "error"
            job["error"] = f"Unexpected server error: {exc}"
            job["progress"] = {"stage": "error", "current": 1, "total": 1, "message": job["error"]}
        return

    with JOBS_LOCK:
        job = JOBS[job_id]
        job["state"] = "done"
        job["result"] = snapshot
        job["progress"] = {
            "stage": "done",
            "current": 1,
            "total": 1,
            "message": f"完成：找到 {len(snapshot.get('stocks', []))} 檔股票",
        }


def main() -> int:
    host = "127.0.0.1"
    port = 8000
    server = ThreadingHTTPServer((host, port), StockScribeHandler)
    print(f"StockScribe UI running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping StockScribe UI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
