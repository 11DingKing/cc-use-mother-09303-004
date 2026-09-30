"""端到端领域回归测试（纯标准库，内存 SQLite）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from curriculum_service.canon import content_hash
from curriculum_service.errors import (
    DependencyConflictError,
    PublishConflictError,
    QuorumError,
    StateConflictError,
    ValidationError,
)
from curriculum_service.repository import Repository
from curriculum_service.service import CurriculumService, dependency_closure


def content(db_hours: int = 48, db_assessment: str = "考查", db_semester: int = 2,
            extra_course: dict | None = None) -> dict:
    courses = [
        {"code": "OOP101", "name": "面向对象程序设计", "hours": 64, "assessment": "考试",
         "semester": 1, "party_code": "FN", "textbooks": ["TB-OOP"], "skills": ["SK-PRG"]},
        {"code": "DB201", "name": "数据库系统原理", "hours": db_hours,
         "assessment": db_assessment, "semester": db_semester, "party_code": "CN",
         "textbooks": ["TB-DB"], "skills": ["SK-DB"]},
    ]
    if extra_course:
        courses.append(extra_course)
    return {
        "title": "合作办学软件工程方案",
        "contributors": [
            {"code": "CN", "name": "中方工学院", "role": "主办院系"},
            {"code": "FN", "name": "外方合作大学", "role": "课程合作方"},
        ],
        "textbooks": [
            {"code": "TB-OOP", "title": "面向对象编程导论", "edition": "第3版"},
            {"code": "TB-DB", "title": "数据库系统原理", "edition": "第2版"},
        ],
        "skill_standards": [
            {"code": "SK-PRG", "name": "程序设计能力标准", "version": "v2.1"},
            {"code": "SK-DB", "name": "数据工程能力标准", "version": "v1.4"},
        ],
        "signing_rules": [
            {"party_code": "CN", "min_signers": 1},
            {"party_code": "FN", "min_signers": 1},
        ],
        "courses": courses,
    }


def new_service(tmp_dir: str | None = None) -> CurriculumService:
    if tmp_dir is None:
        repo = Repository(":memory:")
        # 内存库多连接不共享；测试全部走同一连接，文件锁置空走线程锁。
        return CurriculumService(repo, lock_path=None)
    import tempfile
    path = Path(tempfile.mkdtemp(dir=tmp_dir)) / "svc.sqlite3"
    repo = Repository(path)
    return CurriculumService(repo, lock_path=f"{path}.lock")


def seed_catalog(svc: CurriculumService) -> None:
    svc.create_major("SE", "软件工程")
    svc.create_batch("2025", "SE", "2025-09-01")
    svc.create_batch("2026", "SE", "2026-09-01")
    svc.register_student("S1", "张三", "SE", "2025", "2025-09-01")
    svc.register_student("S2", "李四", "SE", "2026", "2026-09-01")


def make_published(svc: CurriculumService, scope: str = "MAJOR", batch_code: str | None = None,
                   effective: str = "2025-09-01", body: dict | None = None,
                   based_on: str | None = None) -> str:
    draft = svc.create_draft("SE", scope, actor="教务", batch_code=batch_code,
                             based_on_id=based_on)["id"]
    svc.update_content(draft, body or content(), actor="教务")
    svc.submit_for_countersign(draft, actor="教务")
    svc.sign(draft, "CN", "王教务")
    svc.sign(draft, "FN", "Dr. Smith")
    return svc.publish(draft, effective, actor="教务")["id"]


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = new_service()
        seed_catalog(self.svc)

    def test_unknown_textbook_reference_rejected(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        bad = content()
        bad["courses"][0]["textbooks"].append("TB-GHOST")
        with self.assertRaises(ValidationError):
            self.svc.update_content(draft, bad, actor="教务")

    def test_unknown_skill_reference_rejected(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        bad = content()
        bad["courses"][1]["skills"].append("SK-X")
        with self.assertRaises(ValidationError):
            self.svc.update_content(draft, bad, actor="教务")

    def test_signing_rule_must_reference_known_party(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        bad = content()
        bad["signing_rules"].append({"party_code": "ZZ", "min_signers": 1})
        with self.assertRaises(ValidationError):
            self.svc.update_content(draft, bad, actor="教务")

    def test_duplicate_course_code_rejected(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        bad = content()
        bad["courses"].append(dict(bad["courses"][0]))
        with self.assertRaises(ValidationError):
            self.svc.update_content(draft, bad, actor="教务")

    def test_bad_assessment_rejected(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        with self.assertRaises(ValidationError):
            self.svc.update_content(draft, content(db_assessment="论文"), actor="教务")


class CountersignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = new_service()
        seed_catalog(self.svc)

    def test_quorum_blocks_publish(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        self.svc.update_content(draft, content(), actor="教务")
        self.svc.submit_for_countersign(draft, actor="教务")
        self.svc.sign(draft, "CN", "王教务")
        with self.assertRaises(QuorumError) as ctx:
            self.svc.publish(draft, "2025-09-01")
        detail = json.loads(str(ctx.exception))
        self.assertFalse(detail["status"]["quorum_met"])
        fn = next(p for p in detail["status"]["parties"] if p["party_code"] == "FN")
        self.assertFalse(fn["met"])

    def test_two_signers_from_same_party_count_as_one_party_only(self) -> None:
        # 规则要求每方 2 人时，同一方两人满足，另一方零人仍不满足。
        body = content()
        body["signing_rules"] = [
            {"party_code": "CN", "min_signers": 2},
            {"party_code": "FN", "min_signers": 1},
        ]
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        self.svc.update_content(draft, body, actor="教务")
        self.svc.submit_for_countersign(draft, actor="教务")
        self.svc.sign(draft, "CN", "王教务")
        self.svc.sign(draft, "CN", "李院长")
        with self.assertRaises(QuorumError):
            self.svc.publish(draft, "2025-09-01")
        self.svc.sign(draft, "FN", "Dr. Smith")
        published = self.svc.publish(draft, "2025-09-01")
        self.assertEqual(published["state"], "发布")

    def test_revoke_before_publish_requires_re_sign(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        self.svc.update_content(draft, content(), actor="教务")
        self.svc.submit_for_countersign(draft, actor="教务")
        self.svc.sign(draft, "CN", "王教务")
        self.svc.sign(draft, "FN", "Dr. Smith")
        self.svc.revoke_signature(draft, "CN", "王教务", reason="教材版次待确认")
        with self.assertRaises(QuorumError):
            self.svc.publish(draft, "2025-09-01")
        self.svc.sign(draft, "CN", "李院长")
        self.svc.publish(draft, "2025-09-01")

    def test_revoke_after_publish_forbidden(self) -> None:
        vid = make_published(self.svc)
        with self.assertRaises(StateConflictError):
            self.svc.revoke_signature(vid, "CN", "王教务", reason="任何理由")

    def test_editing_after_submit_forbidden(self) -> None:
        draft = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        self.svc.update_content(draft, content(), actor="教务")
        self.svc.submit_for_countersign(draft, actor="教务")
        with self.assertRaises(StateConflictError):
            self.svc.update_content(draft, content(db_hours=60), actor="教务")


class FreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = new_service()
        seed_catalog(self.svc)

    def test_publish_freezes_closure_and_signatures(self) -> None:
        vid = make_published(self.svc)
        snap = self.svc.frozen_snapshot(vid)
        self.assertEqual(len(snap["frozen_signatures"]), 2)
        expected = content_hash(dependency_closure(content()))
        self.assertEqual(snap["content_hash"], expected)
        version = self.svc.get_version(vid)
        self.assertTrue(version["hash_verified"])

    def test_published_version_is_immutable(self) -> None:
        vid = make_published(self.svc)
        with self.assertRaises(StateConflictError):
            self.svc.update_content(vid, content(db_hours=99), actor="教务")
        with self.assertRaises(StateConflictError):
            self.svc.correct_course(vid, "DB201", {"hours": 99}, "x", actor="教务")


class ApplicabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = new_service()
        seed_catalog(self.svc)
        self.v1 = make_published(self.svc, effective="2025-09-01")
        # 旧生先查询，完成绑定
        self.first = self.svc.resolve_student("S1")
        self.assertEqual(self.first["applicable_version"]["id"], self.v1)

    def _publish_v2(self, kind: str, mutate) -> str:
        draft = self.svc.revise(self.v1, kind, actor="教务")["id"]
        mutate(draft)
        self.svc.submit_for_countersign(draft, actor="教务")
        self.svc.sign(draft, "CN", "王教务")
        self.svc.sign(draft, "FN", "Dr. Smith")
        return self.svc.publish(draft, "2026-09-01", actor="教务")["id"]

    def test_correction_does_not_drift_old_student(self) -> None:
        v2 = self._publish_v2(
            "correction",
            lambda d: self.svc.correct_course(d, "DB201", {"hours": 56, "assessment": "考试"},
                                              reason="大纲复核", actor="教务"))
        again = self.svc.resolve_student("S1")
        self.assertEqual(again["applicable_version"]["id"], self.v1)
        db = next(c for c in again["frozen_plan"]["courses"] if c["code"] == "DB201")
        self.assertEqual((db["hours"], db["assessment"]), (48, "考查"))
        self.assertTrue(again["frozen_plan"]["hash_verified"])
        drift = again["post_enrollment_drift"]
        self.assertTrue(any(v["version_id"] == v2 for v in drift["newer_versions"]))
        self.assertTrue(any(ch["course_code"] == "DB201" and ch["change"] == "hours"
                            for ch in drift["catalog_changes"]))

    def test_substitution_does_not_drift_old_student(self) -> None:
        new_course = {"code": "DB301", "name": "数据工程实践", "hours": 40,
                      "assessment": "考试", "semester": 3, "party_code": "CN",
                      "textbooks": ["TB-DB"], "skills": ["SK-DB"]}
        v2 = self._publish_v2(
            "substitution",
            lambda d: self.svc.substitute_course(d, "DB201", new_course, actor="教务"))
        again = self.svc.resolve_student("S1")
        self.assertEqual(again["applicable_version"]["id"], self.v1)
        codes = {c["code"] for c in again["frozen_plan"]["courses"]}
        self.assertIn("DB201", codes)
        self.assertNotIn("DB301", codes)
        drift = again["post_enrollment_drift"]
        self.assertTrue(any(ch["change"] == "已被替代/移除" and ch["course_code"] == "DB201"
                            for ch in drift["catalog_changes"]))
        # 新生看到的是替代后的课程
        new_student = self.svc.resolve_student("S2")
        self.assertEqual(new_student["applicable_version"]["id"], v2)
        self.assertIn("DB301", {c["code"] for c in new_student["frozen_plan"]["courses"]})

    def test_semester_migration_does_not_drift_old_student(self) -> None:
        self._publish_v2("migration",
                         lambda d: self.svc.migrate_semester(d, "DB201", 3, actor="教务"))
        again = self.svc.resolve_student("S1")
        db = next(c for c in again["frozen_plan"]["courses"] if c["code"] == "DB201")
        self.assertEqual(db["semester"], 2)

    def test_revocation_recorded_but_not_drifting(self) -> None:
        # 撤回发生在 v2 的会签阶段；旧生绑定 v1 不受影响，且解释里可见历史。
        draft = self.svc.revise(self.v1, "correction", actor="教务")["id"]
        self.svc.correct_course(draft, "DB201", {"hours": 56}, reason="x", actor="教务")
        self.svc.submit_for_countersign(draft, actor="教务")
        self.svc.sign(draft, "CN", "临时签署人")
        self.svc.revoke_signature(draft, "CN", "临时签署人", reason="误签")
        self.svc.sign(draft, "CN", "王教务")
        self.svc.sign(draft, "FN", "Dr. Smith")
        self.svc.publish(draft, "2026-09-01", actor="教务")
        again = self.svc.resolve_student("S1")
        self.assertEqual(again["applicable_version"]["id"], self.v1)
        self.assertEqual(again["post_enrollment_drift"]["signature_revocations"], [])

    def test_explanation_records_rules_and_losers(self) -> None:
        why = self.first["why_this_version"]
        self.assertIn("rules", why)
        self.assertTrue(why["first_resolved_now"])
        again = self.svc.resolve_student("S1")
        self.assertFalse(again["why_this_version"]["first_resolved_now"])

    def test_batch_version_overrides_major_for_that_batch_only(self) -> None:
        # 为 2026 级发布批次级方案
        v_batch = make_published(self.svc, scope="BATCH", batch_code="2026",
                                 effective="2026-09-01",
                                 body=content(db_hours=56, db_assessment="考试"))
        s2 = self.svc.resolve_student("S2")
        self.assertEqual(s2["applicable_version"]["id"], v_batch)
        why = s2["why_this_version"]
        self.assertEqual(why["scope_match"], "招生批次级精确匹配")
        # 专业级 v1 作为被覆盖候选出现在解释中
        self.assertTrue(any("覆盖" in c.get("reason", "") for c in why["considered"]))

    def test_version_effective_after_enrollment_is_excluded(self) -> None:
        # 2026 级学生入学前不存在更早方案时不应选到未来版本；
        # 这里构造一个 2026 级仅有生效日 2027 的版本 -> 无可适用方案
        svc = new_service()
        seed_catalog(svc)
        make_published(svc, scope="BATCH", batch_code="2026", effective="2027-09-01")
        from curriculum_service.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            svc.resolve_student("S2")


class ConcurrentPublishTest(unittest.TestCase):
    """多人同时发布 / 依赖冲突：只能有一个合法结果。"""

    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.svc = new_service(self.tmp)
        seed_catalog(self.svc)

    def _two_drafts_signed(self, body_a: dict, body_b: dict, effective: str = "2025-09-01"):
        def draft(body: dict) -> str:
            d = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
            self.svc.update_content(d, body, actor="教务")
            self.svc.submit_for_countersign(d, actor="教务")
            self.svc.sign(d, "CN", "王教务")
            self.svc.sign(d, "FN", "Dr. Smith")
            return d

        return draft(body_a), draft(body_b)

    def test_concurrent_identical_content_single_legal_result(self) -> None:
        body = content()
        da, db = self._two_drafts_signed(body, body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(self.svc.publish, da, "2025-09-01"),
                    pool.submit(self.svc.publish, db, "2025-09-01")]
            results = [f.result() for f in futs]
        # 同依赖闭包：两个请求都成功，但都归一到先提交的同一合法版本
        self.assertEqual(results[0]["content_hash"], results[1]["content_hash"])
        resolution = self.svc.resolve_student("S1")
        slot_version = resolution["applicable_version"]["id"]
        self.assertEqual(results[0]["id"], slot_version)
        self.assertEqual(results[1]["id"], slot_version)
        # 槽位只有一行
        slots = self.svc.repo.query_all("SELECT * FROM effective_slots")
        self.assertEqual(len(slots), 1)

    def test_concurrent_conflicting_closures_single_winner(self) -> None:
        body_a = content(db_hours=48)
        body_b = content(db_hours=56)
        da, db = self._two_drafts_signed(body_a, body_b)
        errors: list[Exception] = []
        versions: list[dict] = []
        lock = threading.Lock()

        def pub(draft_id: str) -> None:
            try:
                versions.append(self.svc.publish(draft_id, "2025-09-01"))
            except PublishConflictError as exc:
                with lock:
                    errors.append(exc)

        t1 = threading.Thread(target=pub, args=(da,))
        t2 = threading.Thread(target=pub, args=(db,))
        t1.start(); t2.start(); t1.join(); t2.join()

        # 并发下恰好一个成功、一个落败
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(versions), 1)
        winner = versions[0]
        detail = json.loads(str(errors[0]))
        self.assertEqual(detail["existing_version"], winner["id"])
        self.assertIn(winner["id"], (da, db))
        self.assertEqual(detail["rejected_version"], db if winner["id"] == da else da)
        loser_id = db if winner["id"] == da else da
        # 落败版本立即重试仍被拒绝（槽位已被占用，尚无学生绑定时为仲裁冲突）
        with self.assertRaises(PublishConflictError):
            self.svc.publish(loser_id, "2025-09-01")
        # 学生只能解析到唯一赢家
        resolution = self.svc.resolve_student("S1")
        self.assertEqual(resolution["applicable_version"]["id"], winner["id"])
        # 旧生绑定后，冲突发布升级为依赖冲突，保护承诺路径
        with self.assertRaises(DependencyConflictError):
            self.svc.publish(loser_id, "2025-09-01")
        # 胜者重发是幂等
        again = self.svc.publish(winner["id"], "2025-09-01")
        self.assertEqual(again["id"], winner["id"])

    def test_occupied_slot_with_bound_student_rejects_conflict(self) -> None:
        v1 = make_published(self.svc, effective="2025-09-01")
        self.svc.resolve_student("S1")  # 已有学生绑定
        body_b = content(db_hours=56)
        d = self.svc.create_draft("SE", "MAJOR", actor="教务")["id"]
        self.svc.update_content(d, body_b, actor="教务")
        self.svc.submit_for_countersign(d, actor="教务")
        self.svc.sign(d, "CN", "王教务")
        self.svc.sign(d, "FN", "Dr. Smith")
        with self.assertRaises(DependencyConflictError) as ctx:
            self.svc.publish(d, "2025-09-01")
        detail = json.loads(str(ctx.exception))
        self.assertEqual(detail["existing_version"], v1)
        # 旧生依旧适用 v1
        self.assertEqual(self.svc.resolve_student("S1")["applicable_version"]["id"], v1)

    def test_different_effective_dates_coexist(self) -> None:
        v1 = make_published(self.svc, effective="2025-09-01")
        v2 = make_published(self.svc, effective="2026-09-01", body=content(db_hours=56))
        self.assertNotEqual(v1, v2)
        self.assertEqual(self.svc.resolve_student("S1")["applicable_version"]["id"], v1)
        self.assertEqual(self.svc.resolve_student("S2")["applicable_version"]["id"], v2)


if __name__ == "__main__":
    unittest.main()
