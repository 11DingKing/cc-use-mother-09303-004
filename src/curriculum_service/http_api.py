"""基于标准库 http.server 的 JSON HTTP 接口。

启动：``python -m curriculum_service --port 8000 --db data/curriculum.sqlite3``
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import CurriculumError
from .repository import Repository
from .service import CurriculumService


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CurriculumVersionService/0.2"

    @property
    def service(self) -> CurriculumService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise CurriculumError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(value, dict):
            raise CurriculumError("请求体必须是 JSON 对象")
        return value

    def _actor(self, payload: dict) -> str:
        return str(payload.pop("actor", None) or self.headers.get("X-Actor") or "教务")

    def _handle(self, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except CurriculumError as exc:
            detail: Any = str(exc)
            try:
                detail = json.loads(str(exc))
            except json.JSONDecodeError:
                pass
            _json_response(self, exc.http_status, {"error": exc.code, "detail": detail})
            return
        except Exception as exc:  # noqa: BLE001 - 未预期错误统一转 500
            _json_response(self, 500, {"error": "internal_error", "detail": str(exc)})
            return
        _json_response(self, 200, result if result is not None else {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"

        def route() -> Any:
            if match := re.fullmatch(r"/api/versions/([^/]+)", path):
                return self.service.get_version(match.group(1))
            if match := re.fullmatch(r"/api/versions/([^/]+)/snapshot", path):
                return self.service.frozen_snapshot(match.group(1))
            if match := re.fullmatch(r"/api/students/([^/]+)/resolution", path):
                return self.service.resolve_student(match.group(1))
            if match := re.fullmatch(r"/api/majors/([^/]+)/versions", path):
                return {"versions": self.service.list_versions(match.group(1))}
            if path == "/api/events":
                return {"events": self.service.audit_log(200)}
            if path == "/api/health":
                return {"ok": True, "service": self.server_version}
            _json_response(self, 404, {"error": "not_found", "detail": path})
            return None

        self._handle(route)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        payload = self._read_json()

        def route() -> Any:
            if path == "/api/majors":
                return self.service.create_major(payload["code"], payload["name"], self._actor(payload))
            if path == "/api/batches":
                return self.service.create_batch(
                    payload["code"], payload["major_code"], payload["entry_date"], self._actor(payload))
            if path == "/api/students":
                return self.service.register_student(
                    payload["id"], payload["name"], payload["major_code"],
                    payload["batch_code"], payload["enrolled_at"], self._actor(payload))
            if path == "/api/drafts":
                return self.service.create_draft(
                    payload["major_code"], payload.get("scope", "MAJOR"), self._actor(payload),
                    title=payload.get("title", "未命名方案"),
                    batch_code=payload.get("batch_code"),
                    kind=payload.get("kind", "standard"),
                    based_on_id=payload.get("based_on_id"))
            if match := re.fullmatch(r"/api/versions/([^/]+)/revise", path):
                return self.service.revise(
                    match.group(1), payload.get("kind", "standard"), self._actor(payload),
                    title=payload.get("title"))
            if match := re.fullmatch(r"/api/versions/([^/]+)/content", path):
                return self.service.update_content(match.group(1), payload["content"],
                                                   self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/substitute", path):
                return self.service.substitute_course(
                    match.group(1), payload["old_code"], payload["new_course"],
                    self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/correct", path):
                return self.service.correct_course(
                    match.group(1), payload["course_code"], payload["changes"],
                    payload["reason"], self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/migrate", path):
                return self.service.migrate_semester(
                    match.group(1), payload["course_code"], int(payload["to_semester"]),
                    self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/submit", path):
                return self.service.submit_for_countersign(match.group(1), self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/sign", path):
                return self.service.sign(
                    match.group(1), payload["party_code"], payload["signer"],
                    self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/revoke", path):
                return self.service.revoke_signature(
                    match.group(1), payload["party_code"], payload["signer"],
                    payload["reason"], self._actor(payload))
            if match := re.fullmatch(r"/api/versions/([^/]+)/publish", path):
                return self.service.publish(
                    match.group(1), payload["effective_date"], self._actor(payload))
            _json_response(self, 404, {"error": "not_found", "detail": path})
            return None

        self._handle(route)


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    repo = Repository(db_path)
    service = CurriculumService(repo, lock_path=f"{db_path}.lock")
    server = ThreadingHTTPServer((host, port), ApiHandler)
    server.service = service  # type: ignore[attr-defined]
    server.repo = repo  # type: ignore[attr-defined]
    return server
