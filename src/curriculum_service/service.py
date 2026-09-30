"""课程方案领域服务：用例编排、发布冻结、并发仲裁、适用性冻结。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import applicability as applic
from . import operations as ops
from .canon import content_hash
from .errors import (
    DependencyConflictError,
    NotFoundError,
    PublishConflictError,
    QuorumError,
    StateConflictError,
    ValidationError,
)
from .locking import file_lock
from .models import (
    KIND_CORRECTION,
    KIND_MIGRATION,
    KIND_STANDARD,
    KIND_SUBSTITUTION,
    SCOPE_BATCH,
    SCOPE_MAJOR,
    STATE_COUNTERSIGN,
    STATE_DRAFT,
    STATE_PUBLISHED,
    Course,
    empty_content,
)
from .repository import Repository

CLOSURE_KEYS = ("courses", "contributors", "textbooks", "skill_standards", "signing_rules")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dependency_closure(content: dict) -> dict:
    """发布时冻结的依赖闭包（不含标题、变更轨迹等说明性字段）。"""
    return {key: content.get(key, []) for key in CLOSURE_KEYS}


def validate_content(content: Any) -> dict:
    """校验方案内容并返回规范化后的副本。"""
    if not isinstance(content, dict):
        raise ValidationError("方案内容必须是对象")
    missing = {"title", *CLOSURE_KEYS} - content.keys()
    if missing:
        raise ValidationError("方案内容缺少字段：" + "、".join(sorted(missing)))

    title = content.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValidationError("方案标题不能为空")

    contributors = content.get("contributors")
    if not isinstance(contributors, list) or not contributors:
        raise ValidationError("贡献方列表不能为空")
    parties: dict[str, dict] = {}
    for item in contributors:
        if not isinstance(item, dict) or not item.get("code") or not item.get("name"):
            raise ValidationError("贡献方必须包含 code 与 name")
        code = str(item["code"]).strip()
        if code in parties:
            raise ValidationError(f"贡献方编号重复：{code}")
        normalized = {"code": code, "name": str(item["name"]).strip(),
                      "role": str(item.get("role", "")).strip()}
        parties[code] = normalized

    rules = content.get("signing_rules")
    if not isinstance(rules, list) or not rules:
        raise ValidationError("签署规则不能为空")
    normalized_rules: dict[str, int] = {}
    for item in rules:
        if not isinstance(item, dict) or not item.get("party_code"):
            raise ValidationError("签署规则必须包含 party_code")
        party_code = str(item["party_code"]).strip()
        if party_code not in parties:
            raise ValidationError(f"签署规则引用了未知贡献方：{party_code}")
        try:
            min_signers = int(item["min_signers"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"贡献方 {party_code} 的 min_signers 必须是正整数") from exc
        if min_signers <= 0:
            raise ValidationError(f"贡献方 {party_code} 的 min_signers 必须是正整数")
        if party_code in normalized_rules:
            raise ValidationError(f"贡献方 {party_code} 存在多条签署规则")
        normalized_rules[party_code] = min_signers

    textbooks = content.get("textbooks")
    if not isinstance(textbooks, list):
        raise ValidationError("教材依赖必须是列表")
    book_codes: set[str] = set()
    for item in textbooks:
        if not isinstance(item, dict) or not item.get("code") or not item.get("title"):
            raise ValidationError("教材必须包含 code 与 title")
        code = str(item["code"]).strip()
        if code in book_codes:
            raise ValidationError(f"教材编号重复：{code}")
        book_codes.add(code)

    standards = content.get("skill_standards")
    if not isinstance(standards, list):
        raise ValidationError("技能标准必须是列表")
    skill_codes: set[str] = set()
    for item in standards:
        if not isinstance(item, dict) or not item.get("code") or not item.get("name"):
            raise ValidationError("技能标准必须包含 code 与 name")
        code = str(item["code"]).strip()
        if code in skill_codes:
            raise ValidationError(f"技能标准编号重复：{code}")
        skill_codes.add(code)

    courses = content.get("courses")
    if not isinstance(courses, list) or not courses:
        raise ValidationError("课程列表不能为空")
    course_codes: set[str] = set()
    normalized_courses: list[dict] = []
    for item in courses:
        course = Course.from_dict(item)
        if course.code in course_codes:
            raise ValidationError(f"课程编号重复：{course.code}")
        if course.party_code not in parties:
            raise ValidationError(f"课程 {course.code} 的贡献方未知：{course.party_code}")
        for book in course.textbooks:
            if book not in book_codes:
                raise ValidationError(f"课程 {course.code} 引用了未登记教材：{book}")
        for skill in course.skills:
            if skill not in skill_codes:
                raise ValidationError(f"课程 {course.code} 引用了未登记技能标准：{skill}")
        course_codes.add(course.code)
        normalized_courses.append(course.to_dict())

    return {
        "title": title.strip(),
        "courses": normalized_courses,
        "contributors": list(parties.values()),
        "textbooks": [
            {"code": str(i["code"]).strip(), "title": str(i["title"]).strip(),
             "edition": str(i.get("edition", "")).strip()}
            for i in textbooks
        ],
        "skill_standards": [
            {"code": str(i["code"]).strip(), "name": str(i["name"]).strip(),
             "version": str(i.get("version", "")).strip()}
            for i in standards
        ],
        "signing_rules": [
            {"party_code": p, "min_signers": n} for p, n in sorted(normalized_rules.items())
        ],
        "change_log": list(content.get("change_log", [])),
        "corrections": list(content.get("corrections", [])),
    }


class CurriculumService:
    """领域服务。所有写操作在独占临界区内以 IMMEDIATE 事务执行。"""

    def __init__(self, repo: Repository, lock_path: str | Path | None = None) -> None:
        self.repo = repo
        self._lock_path = None if lock_path is None else Path(lock_path)
        self._thread_lock = threading.RLock()

    @contextmanager
    def _exclusive(self):
        # 进程内所有连接访问都经 RLock 串行化（共享 SQLite 连接），
        # 配置文件锁时再叠加跨进程互斥。
        with self._thread_lock:
            if self._lock_path is None:
                yield
            else:
                with file_lock(self._lock_path):
                    yield

    @contextmanager
    def _tx(self):
        self.repo.begin_immediate()
        try:
            yield
            self.repo.commit()
        except BaseException:
            self.repo.rollback()
            raise

    # -- 基础目录 ---------------------------------------------------------
    def create_major(self, code: str, name: str, actor: str = "教务") -> dict:
        code = code.strip()
        if not code or not name.strip():
            raise ValidationError("专业编号与名称不能为空")
        with self._exclusive(), self._tx():
            try:
                self.repo.execute(
                    "INSERT INTO majors(code, name) VALUES (?,?)", (code, name.strip())
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"专业已存在：{code}") from exc
            self.repo.insert_event(actor, "create_major", "major", code, {"name": name})
        return {"code": code, "name": name.strip()}

    def create_batch(self, code: str, major_code: str, entry_date: str, actor: str = "教务") -> dict:
        with self._exclusive(), self._tx():
            if self.repo.query_one("SELECT 1 FROM majors WHERE code=?", (major_code,)) is None:
                raise NotFoundError(f"专业不存在：{major_code}")
            try:
                self.repo.execute(
                    "INSERT INTO batches(code, major_code, entry_date) VALUES (?,?,?)",
                    (code.strip(), major_code, entry_date),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"招生批次已存在：{code}") from exc
            self.repo.insert_event(actor, "create_batch", "batch", code.strip(),
                                   {"major_code": major_code, "entry_date": entry_date})
        return {"code": code.strip(), "major_code": major_code, "entry_date": entry_date}

    def register_student(self, student_id: str, name: str, major_code: str,
                         batch_code: str, enrolled_at: str, actor: str = "教务") -> dict:
        with self._exclusive(), self._tx():
            batch = self.repo.query_one(
                "SELECT * FROM batches WHERE code=? AND major_code=?", (batch_code, major_code)
            )
            if batch is None:
                raise NotFoundError(f"专业 {major_code} 下不存在招生批次：{batch_code}")
            if enrolled_at < batch["entry_date"]:
                raise ValidationError("入学日期不能早于批次入学日期")
            try:
                self.repo.execute(
                    "INSERT INTO students(id, name, major_code, batch_code, enrolled_at) "
                    "VALUES (?,?,?,?,?)",
                    (student_id.strip(), name.strip(), major_code, batch_code, enrolled_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError(f"学生已存在：{student_id}") from exc
            self.repo.insert_event(actor, "register_student", "student", student_id.strip(),
                                   {"batch_code": batch_code})
        return {"id": student_id.strip(), "name": name.strip(), "major_code": major_code,
                "batch_code": batch_code, "enrolled_at": enrolled_at}

    # -- 草案与内容维护 ----------------------------------------------------
    def _load_version(self, version_id: str) -> sqlite3.Row:
        row = self.repo.query_one(
            "SELECT * FROM curriculum_versions WHERE id=?", (version_id,)
        )
        if row is None:
            raise NotFoundError(f"方案版本不存在：{version_id}")
        return row

    def create_draft(self, major_code: str, scope: str, actor: str, *,
                     title: str = "未命名方案", batch_code: str | None = None,
                     kind: str = KIND_STANDARD, based_on_id: str | None = None) -> dict:
        if scope not in (SCOPE_MAJOR, SCOPE_BATCH):
            raise ValidationError("作用域必须是 MAJOR 或 BATCH")
        if kind not in (KIND_STANDARD, KIND_SUBSTITUTION, KIND_CORRECTION, KIND_MIGRATION):
            raise ValidationError("未知的演进类型")
        with self._exclusive(), self._tx():
            if self.repo.query_one("SELECT 1 FROM majors WHERE code=?", (major_code,)) is None:
                raise NotFoundError(f"专业不存在：{major_code}")
            if scope == SCOPE_BATCH:
                if not batch_code:
                    raise ValidationError("批次级方案必须指定招生批次")
                if self.repo.query_one(
                    "SELECT 1 FROM batches WHERE code=? AND major_code=?",
                    (batch_code, major_code),
                ) is None:
                    raise NotFoundError(f"专业 {major_code} 下不存在招生批次：{batch_code}")
            content = empty_content(title)
            based_on: str | None = None
            if based_on_id is not None:
                source = self._load_version(based_on_id)
                if source["major_code"] != major_code:
                    raise ValidationError("只能在同一专业内复制方案")
                if source["state"] != STATE_PUBLISHED:
                    raise StateConflictError("只能基于已发布版本修订")
                source_content = json.loads(source["content_json"])
                content = {k: v for k, v in source_content.items()
                           if k not in ("frozen_signatures", "freeze")}
                content["title"] = title
                based_on = based_on_id
            version_id = _new_id("ver")
            self.repo.execute(
                "INSERT INTO curriculum_versions(id, major_code, scope, batch_code, state, kind,"
                " based_on_id, title, content_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (version_id, major_code, scope, batch_code, STATE_DRAFT, kind,
                 based_on, title.strip(), json.dumps(content, ensure_ascii=False)),
            )
            self.repo.insert_event(actor, "create_draft", "version", version_id,
                                   {"scope": scope, "batch_code": batch_code, "kind": kind,
                                    "based_on": based_on})
        return self.get_version(version_id)

    def revise(self, source_version_id: str, kind: str, actor: str, *,
               title: str | None = None) -> dict:
        """基于已发布版本发起修订（课程替代/紧急更正/学期迁移/常规修订）。"""
        row = self._load_version(source_version_id)
        source = json.loads(row["content_json"])
        return self.create_draft(
            row["major_code"], row["scope"], actor,
            title=title or f"{source.get('title', '方案')} - 修订稿",
            batch_code=row["batch_code"], kind=kind, based_on_id=source_version_id,
        )

    def update_content(self, version_id: str, content: dict, actor: str) -> dict:
        with self._exclusive(), self._tx():
            row = self._load_version(version_id)
            if row["state"] not in ("共创", "更正"):
                raise StateConflictError(f"状态为 {row['state']} 的方案不能再修改内容")
            normalized = validate_content(content)
            self.repo.execute(
                "UPDATE curriculum_versions SET title=?, content_json=? WHERE id=?",
                (normalized["title"], json.dumps(normalized, ensure_ascii=False), version_id),
            )
            self.repo.insert_event(actor, "update_content", "version", version_id, {})
        return self.get_version(version_id)

    def _apply_operation(self, version_id: str, actor: str, mutate) -> dict:
        with self._exclusive(), self._tx():
            row = self._load_version(version_id)
            if row["state"] not in ("共创", "更正"):
                raise StateConflictError(f"状态为 {row['state']} 的方案不能再修改内容")
            content = json.loads(row["content_json"])
            new_content = mutate(content)
            self.repo.execute(
                "UPDATE curriculum_versions SET content_json=? WHERE id=?",
                (json.dumps(new_content, ensure_ascii=False), version_id),
            )
            self.repo.insert_event(actor, mutate.__name__, "version", version_id, {})
        return self.get_version(version_id)

    def substitute_course(self, version_id: str, old_code: str, new_course: dict,
                          actor: str) -> dict:
        return self._apply_operation(
            version_id, actor,
            lambda c: ops.apply_substitution(c, old_code, new_course, actor))

    def correct_course(self, version_id: str, course_code: str, changes: dict,
                       reason: str, actor: str) -> dict:
        return self._apply_operation(
            version_id, actor,
            lambda c: ops.apply_correction(c, course_code, changes, reason, actor))

    def migrate_semester(self, version_id: str, course_code: str, to_semester: int,
                         actor: str) -> dict:
        return self._apply_operation(
            version_id, actor,
            lambda c: ops.apply_semester_migration(c, course_code, to_semester, actor))

    # -- 会签与撤回 --------------------------------------------------------
    def submit_for_countersign(self, version_id: str, actor: str) -> dict:
        with self._exclusive(), self._tx():
            row = self._load_version(version_id)
            if row["state"] not in ("共创", "更正"):
                raise StateConflictError(f"状态为 {row['state']} 的方案不能提交会签")
            validate_content(json.loads(row["content_json"]))
            self.repo.execute(
                "UPDATE curriculum_versions SET state=? WHERE id=?",
                (STATE_COUNTERSIGN, version_id),
            )
            self.repo.insert_event(actor, "submit_countersign", "version", version_id, {})
        return self.get_version(version_id)

    def _party_rule(self, content: dict, party_code: str) -> int | None:
        for rule in content["signing_rules"]:
            if rule["party_code"] == party_code:
                return int(rule["min_signers"])
        return None

    def sign(self, version_id: str, party_code: str, signer: str, actor: str | None = None) -> dict:
        party_code = party_code.strip()
        signer = signer.strip()
        with self._exclusive(), self._tx():
            row = self._load_version(version_id)
            if row["state"] != STATE_COUNTERSIGN:
                raise StateConflictError("只有会签中的方案可以签署")
            content = json.loads(row["content_json"])
            parties = {p["code"] for p in content["contributors"]}
            if party_code not in parties:
                raise ValidationError(f"贡献方未知：{party_code}")
            if self._party_rule(content, party_code) is None:
                raise ValidationError(f"贡献方 {party_code} 不在签署规则中")
            inserted = self.repo.execute(
                "INSERT OR IGNORE INTO signatures(version_id, party_code, signer) VALUES (?,?,?)",
                (version_id, party_code, signer),
            )
            if inserted.rowcount:
                self.repo.insert_event(actor or signer, "sign", "version", version_id,
                                       {"party_code": party_code, "signer": signer})
        return self.get_version(version_id)

    def revoke_signature(self, version_id: str, party_code: str, signer: str,
                         reason: str, actor: str | None = None) -> dict:
        if not reason or not reason.strip():
            raise ValidationError("撤回签署必须填写原因")
        with self._exclusive(), self._tx():
            row = self._load_version(version_id)
            if row["state"] == STATE_PUBLISHED:
                raise StateConflictError("方案已发布冻结，签名不可撤回")
            existing = self.repo.query_one(
                "SELECT 1 FROM signatures WHERE version_id=? AND party_code=? AND signer=?",
                (version_id, party_code, signer),
            )
            if existing is None:
                raise NotFoundError("未找到该签署记录")
            self.repo.execute(
                "INSERT INTO signature_revocations(id, version_id, party_code, signer, reason)"
                " VALUES (?,?,?,?,?)",
                (_new_id("rev"), version_id, party_code, signer, reason.strip()),
            )
            self.repo.execute(
                "DELETE FROM signatures WHERE version_id=? AND party_code=? AND signer=?",
                (version_id, party_code, signer),
            )
            self.repo.insert_event(actor or signer, "revoke_signature", "version", version_id,
                                   {"party_code": party_code, "signer": signer})
        return self.get_version(version_id)

    def _signature_status(self, version_id: str, content: dict) -> dict:
        rows = self.repo.query_all(
            "SELECT party_code, signer FROM signatures WHERE version_id=?", (version_id,)
        )
        by_party: dict[str, list[str]] = {}
        for r in rows:
            by_party.setdefault(r["party_code"], []).append(r["signer"])
        parties = []
        quorum_met = True
        for rule in content.get("signing_rules", []):
            signers = sorted(by_party.get(rule["party_code"], []))
            need = int(rule["min_signers"])
            ok = len(signers) >= need
            quorum_met = quorum_met and ok
            parties.append({"party_code": rule["party_code"], "required": need,
                            "signed": signers, "met": ok})
        return {"quorum_met": quorum_met, "parties": parties}

    # -- 发布：冻结 + 仲裁 -------------------------------------------------
    def publish(self, version_id: str, effective_date: str, actor: str = "教务") -> dict:
        with self._exclusive():
            with self._tx():
                row = self._load_version(version_id)
                if row["state"] == STATE_PUBLISHED:
                    return self.get_version(version_id)
                if row["state"] != STATE_COUNTERSIGN:
                    raise StateConflictError(f"状态为 {row['state']} 的方案不能发布")
                content = validate_content(json.loads(row["content_json"]))
                status = self._signature_status(version_id, content)
                if not status["quorum_met"]:
                    raise QuorumError(json.dumps(
                        {"message": "多方法定人数未满足", "status": status}, ensure_ascii=False))

                closure = dependency_closure(content)
                digest = content_hash(closure)
                major_code = row["major_code"]
                batch_scope = row["batch_code"] if row["scope"] == SCOPE_BATCH else "*"
                slot = self.repo.query_one(
                    "SELECT * FROM effective_slots WHERE major_code=? AND batch_scope=?"
                    " AND effective_date=?",
                    (major_code, batch_scope, effective_date),
                )

                if slot is not None and slot["content_hash"] == digest:
                    # 同槽位、同依赖闭包：幂等，唯一合法结果就是已存在的版本。
                    self.repo.insert_event(actor, "publish_idempotent", "version",
                                           slot["version_id"], {"duplicate_version": version_id})
                    return self.get_version(slot["version_id"])

                if slot is not None and slot["content_hash"] != digest:
                    # 先提交者胜的线性化仲裁：生效槽位已被不同依赖闭包占用，
                    # 后来者一律拒绝，保证只有一个合法结果，且已成功者不会被翻转。
                    bound = self.repo.query_one(
                        "SELECT 1 FROM student_bindings WHERE version_id=?",
                        (slot["version_id"],),
                    )
                    payload = {
                        "message": "同一生效槽位已存在不同依赖闭包的合法版本，"
                                   "依赖冲突只能保留一个合法结果（先提交者胜）",
                        "existing_version": slot["version_id"],
                        "existing_hash": slot["content_hash"],
                        "rejected_version": version_id,
                        "rejected_hash": digest,
                        "students_bound": bound is not None,
                        "remedy": "请使用新版本并指定新生效日期；旧学生已绑定的方案不得覆盖",
                    }
                    if bound is not None:
                        raise DependencyConflictError(json.dumps(payload, ensure_ascii=False))
                    raise PublishConflictError(json.dumps(payload, ensure_ascii=False))

                next_no = self.repo.query_one(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 AS n FROM curriculum_versions"
                    " WHERE major_code=? AND scope=? AND COALESCE(batch_code,'')=COALESCE(?, '')",
                    (major_code, row["scope"], row["batch_code"]),
                )["n"]
                published_at = _now()
                frozen = [
                    {"party_code": r["party_code"], "signer": r["signer"],
                     "signed_at": r["signed_at"]}
                    for r in self.repo.query_all(
                        "SELECT party_code, signer, signed_at FROM signatures"
                        " WHERE version_id=? ORDER BY party_code, signer", (version_id,))
                ]
                content["frozen_signatures"] = frozen
                content["freeze"] = {"hash": digest, "frozen_at": published_at,
                                     "effective_date": effective_date}
                self.repo.execute(
                    "UPDATE curriculum_versions SET state=?, version_no=?, content_json=?,"
                    " content_hash=?, effective_date=?, published_at=? WHERE id=?",
                    (STATE_PUBLISHED, next_no, json.dumps(content, ensure_ascii=False),
                     digest, effective_date, published_at, version_id),
                )
                # 同内容幂等发布已在前面返回；此处必为新槽位，唯一索引兜底并发。
                try:
                    self.repo.execute(
                        "INSERT INTO effective_slots(id, major_code, batch_scope, effective_date,"
                        " version_id, content_hash) VALUES (?,?,?,?,?,?)",
                        (_new_id("slot"), major_code, batch_scope, effective_date,
                         version_id, digest),
                    )
                except sqlite3.IntegrityError as exc:
                    raise PublishConflictError(
                        json.dumps({"message": "并发发布仲裁落败：生效槽位刚被其他版本占用",
                                    "rejected_version": version_id}, ensure_ascii=False)
                    ) from exc
                self.repo.insert_event(actor, "publish", "version", version_id,
                                       {"version_no": next_no, "effective_date": effective_date,
                                        "hash": digest})
        return self.get_version(version_id)

    # -- 查询 --------------------------------------------------------------
    def get_version(self, version_id: str) -> dict:
        with self._thread_lock:
            return self._get_version_locked(version_id)

    def _get_version_locked(self, version_id: str) -> dict:
        row = self._load_version(version_id)
        content = json.loads(row["content_json"])
        result = {
            "id": row["id"],
            "major_code": row["major_code"],
            "scope": row["scope"],
            "batch_code": row["batch_code"],
            "version_no": row["version_no"],
            "state": row["state"],
            "kind": row["kind"],
            "based_on_id": row["based_on_id"],
            "supersedes_id": row["supersedes_id"],
            "title": row["title"],
            "effective_date": row["effective_date"],
            "published_at": row["published_at"],
            "created_at": row["created_at"],
            "content": content,
        }
        if row["state"] == STATE_COUNTERSIGN:
            result["countersign"] = self._signature_status(version_id, content)
        if row["content_hash"]:
            result["content_hash"] = row["content_hash"]
            result["hash_verified"] = (
                content_hash(dependency_closure(
                    {k: v for k, v in content.items() if k not in ("frozen_signatures", "freeze")}
                )) == row["content_hash"]
            )
        return result

    def list_versions(self, major_code: str, scope: str | None = None) -> list[dict]:
        with self._thread_lock:
            sql = ("SELECT id FROM curriculum_versions WHERE major_code=? "
                   "ORDER BY COALESCE(version_no, 0), created_at")
            params: tuple = (major_code,)
            if scope:
                sql = ("SELECT id FROM curriculum_versions WHERE major_code=? AND scope=?"
                       " ORDER BY COALESCE(version_no, 0), created_at")
                params = (major_code, scope)
            return [self._get_version_locked(r["id"]) for r in self.repo.query_all(sql, params)]

    def frozen_snapshot(self, version_id: str) -> dict:
        """返回发布时锁定的完整依赖快照。"""
        with self._thread_lock:
            version = self._get_version_locked(version_id)
            if version["state"] != STATE_PUBLISHED:
                raise StateConflictError("只有已发布版本存在冻结快照")
            content = version["content"]
            return {
                "version_id": version_id,
                "version_no": version["version_no"],
                "scope": version["scope"],
                "batch_code": version["batch_code"],
                "effective_date": version["effective_date"],
                "published_at": version["published_at"],
                "content_hash": version["content_hash"],
                "dependency_closure": dependency_closure(content),
                "frozen_signatures": content.get("frozen_signatures", []),
            }

    # -- 学生适用性 --------------------------------------------------------
    def _candidates(self, major_code: str, batch_code: str) -> list[applic.Candidate]:
        """当前拥有生效槽位的合法版本（仲裁落败者不在候选之列）。"""
        rows = self.repo.query_all(
            "SELECT cv.* FROM curriculum_versions cv"
            " JOIN effective_slots es ON es.version_id = cv.id"
            " WHERE cv.state=? AND cv.major_code=?"
            " AND (cv.scope='MAJOR' OR (cv.scope='BATCH' AND cv.batch_code=?))",
            (STATE_PUBLISHED, major_code, batch_code),
        )
        return [
            applic.Candidate(
                version_id=r["id"], scope=r["scope"], batch_code=r["batch_code"],
                effective_date=r["effective_date"], version_no=r["version_no"],
                published_at=r["published_at"], content_hash=r["content_hash"],
                title=r["title"], kind=r["kind"],
            )
            for r in rows
        ]

    def _drift_report(self, bound_row: sqlite3.Row, major_code: str,
                      batch_code: str, enrolled_at: str) -> dict:
        """对照入学锁定之后的目录变化（课程替代/更正/迁移/新版本/撤回）。"""
        bound_content = json.loads(bound_row["content_json"])
        current_candidates = self._candidates(major_code, batch_code)
        future = [c for c in current_candidates
                  if c.version_id != bound_row["id"] and c.effective_date > enrolled_at]
        newer_versions = [
            {"version_id": c.version_id, "version_no": c.version_no, "title": c.title,
             "scope": c.scope, "kind": c.kind, "effective_date": c.effective_date}
            for c in sorted(current_candidates, key=lambda c: (c.effective_date, c.version_no))
            if c.version_id != bound_row["id"]
            and (c.effective_date > bound_row["effective_date"] or c.version_no > bound_row["version_no"])
        ]

        catalog_changes: list[dict] = []
        if current_candidates:
            today_resolution = applic.resolve(current_candidates, "9999-12-31", batch_code)
            latest = self.repo.query_one(
                "SELECT * FROM curriculum_versions WHERE id=?",
                (today_resolution.chosen.version_id,),
            )
            if latest is not None and latest["id"] != bound_row["id"]:
                latest_content = json.loads(latest["content_json"])
                old_courses = {c["code"]: c for c in bound_content.get("courses", [])}
                new_courses = {c["code"]: c for c in latest_content.get("courses", [])}
                for code in sorted(set(old_courses) | set(new_courses)):
                    if code in old_courses and code not in new_courses:
                        catalog_changes.append(
                            {"course_code": code, "change": "已被替代/移除",
                             "in_latest_version": latest["id"]})
                    elif code not in old_courses and code in new_courses:
                        catalog_changes.append(
                            {"course_code": code, "change": "新增课程",
                             "in_latest_version": latest["id"]})
                    else:
                        o, n = old_courses[code], new_courses[code]
                        for field_name in ("hours", "assessment", "semester"):
                            if o[field_name] != n[field_name]:
                                catalog_changes.append({
                                    "course_code": code, "change": field_name,
                                    "bound_value": o[field_name], "latest_value": n[field_name],
                                    "in_latest_version": latest["id"],
                                })
        revocations = [
            {"party_code": r["party_code"], "signer": r["signer"], "reason": r["reason"],
             "revoked_at": r["revoked_at"]}
            for r in self.repo.query_all(
                "SELECT * FROM signature_revocations WHERE version_id=?"
                " ORDER BY revoked_at", (bound_row["id"],))
        ]
        return {
            "note": "以下变化均发生在入学锁定之后，不适用于该学生；其适用方案保持冻结版本",
            "newer_versions": newer_versions,
            "post_enrollment_slots": [
                {"version_id": c.version_id, "scope": c.scope, "kind": c.kind,
                 "effective_date": c.effective_date}
                for c in sorted(future, key=lambda c: c.effective_date)
            ],
            "catalog_changes": catalog_changes,
            "signature_revocations": revocations,
        }

    def resolve_student(self, student_id: str) -> dict:
        """返回学生的适用方案及完整决策依据；首次解析后永久冻结。"""
        with self._exclusive():
            with self._tx():
                student = self.repo.query_one(
                    "SELECT * FROM students WHERE id=?", (student_id,)
                )
                if student is None:
                    raise NotFoundError(f"学生不存在：{student_id}")
                binding = self.repo.query_one(
                    "SELECT * FROM student_bindings WHERE student_id=?", (student_id,)
                )
                if binding is None:
                    candidates = self._candidates(student["major_code"], student["batch_code"])
                    eligible = [
                        c for c in candidates
                        if c.effective_date <= student["enrolled_at"]
                        and (c.scope == SCOPE_MAJOR or c.batch_code == student["batch_code"])
                    ]
                    if not eligible:
                        raise NotFoundError(
                            f"学生 {student_id} 入学时点没有任何已生效方案")
                    resolution = applic.resolve(
                        candidates, student["enrolled_at"], student["batch_code"])
                    chosen = resolution.chosen
                    reason = {
                        "rules": [
                            "仅考虑生效日不晚于入学日的已发布版本",
                            "招生批次级版本优先于专业级版本",
                            "同级按生效日、版本号、发布时间、版本ID逐级决胜",
                        ],
                        "enrolled_at": student["enrolled_at"],
                        "scope_match": ("招生批次级精确匹配" if chosen.scope == SCOPE_BATCH
                                        else "无批次级版本，采用专业级版本"),
                        "considered": resolution.considered,
                    }
                    self.repo.execute(
                        "INSERT INTO student_bindings(student_id, version_id, reason_json)"
                        " VALUES (?,?,?)",
                        (student_id, chosen.version_id, json.dumps(reason, ensure_ascii=False)),
                    )
                    self.repo.insert_event(
                        "system", "bind_student", "student", student_id,
                        {"version_id": chosen.version_id, "hash": chosen.content_hash})
                    binding = self.repo.query_one(
                        "SELECT * FROM student_bindings WHERE student_id=?", (student_id,)
                    )
                    first_resolved = True
                else:
                    first_resolved = False

                bound_row = self._load_version(binding["version_id"])
                content = json.loads(bound_row["content_json"])
                recomputed = content_hash(dependency_closure(content))
                reason = json.loads(binding["reason_json"])
                result = {
                    "student": {
                        "id": student["id"], "name": student["name"],
                        "major_code": student["major_code"], "batch_code": student["batch_code"],
                        "enrolled_at": student["enrolled_at"],
                    },
                    "applicable_version": {
                        "id": bound_row["id"], "version_no": bound_row["version_no"],
                        "title": bound_row["title"], "scope": bound_row["scope"],
                        "batch_code": bound_row["batch_code"],
                        "effective_date": bound_row["effective_date"],
                        "published_at": bound_row["published_at"],
                        "kind": bound_row["kind"],
                    },
                    "why_this_version": {
                        "binding_frozen_at": binding["resolved_at"],
                        "first_resolved_now": first_resolved,
                        "frozen_binding": "学生入学后首次查询即永久绑定，此后任何变更不得改派",
                        **reason,
                    },
                    "frozen_plan": {
                        "content_hash": bound_row["content_hash"],
                        "hash_recomputed": recomputed,
                        "hash_verified": recomputed == bound_row["content_hash"],
                        "courses": content.get("courses", []),
                        "contributors": content.get("contributors", []),
                        "textbooks": content.get("textbooks", []),
                        "skill_standards": content.get("skill_standards", []),
                        "signing_rules": content.get("signing_rules", []),
                        "frozen_signatures": content.get("frozen_signatures", []),
                    },
                    "post_enrollment_drift": self._drift_report(
                        bound_row, student["major_code"], student["batch_code"],
                        student["enrolled_at"]),
                }
                return result

    def audit_log(self, limit: int = 100) -> list[dict]:
        with self._thread_lock:
            return self.repo.events(limit)
