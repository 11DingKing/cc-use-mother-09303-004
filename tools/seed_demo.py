"""构造端到端演示场景：

首届合作班 2025 级入学，旧培养方案（数据库 48 学时/考查）发布并绑定；
入学后双方修订学时与考核方式（56 学时/考试）并对课程做学期迁移，
形成 2026 级生效的新方案。验证：

* 2025 级学生始终适用入学时锁定的旧版本，查询给出完整原因；
* 2026 级学生适用新版本；
* 冻结快照哈希不变。

用法：python3 tools/seed_demo.py [--db data/demo.sqlite3]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from curriculum_service.repository import Repository
from curriculum_service.service import CurriculumService


def base_content(title: str, db_hours: int, db_assessment: str, db_semester: int) -> dict:
    return {
        "title": title,
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
        "courses": [
            {"code": "OOP101", "name": "面向对象程序设计", "hours": 64, "assessment": "考试",
             "semester": 1, "party_code": "FN", "textbooks": ["TB-OOP"], "skills": ["SK-PRG"]},
            {"code": "DB201", "name": "数据库系统原理", "hours": db_hours,
             "assessment": db_assessment, "semester": db_semester, "party_code": "CN",
             "textbooks": ["TB-DB"], "skills": ["SK-DB"]},
        ],
    }


def publish(svc: CurriculumService, draft_id: str, effective: str) -> str:
    svc.submit_for_countersign(draft_id, actor="中方教务人员")
    svc.sign(draft_id, "CN", "王教务", actor="王教务")
    svc.sign(draft_id, "FN", "Dr. Smith", actor="Dr. Smith")
    return svc.publish(draft_id, effective, actor="中方教务人员")["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "demo.sqlite3"))
    args = parser.parse_args()

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    repo = Repository(db_path)
    svc = CurriculumService(repo, lock_path=f"{db_path}.lock")

    svc.create_major("SE", "软件工程（合作办学）", actor="系统")
    svc.create_batch("2025", "SE", "2025-09-01", actor="系统")
    svc.create_batch("2026", "SE", "2026-09-01", actor="系统")
    svc.register_student("S2025001", "张三", "SE", "2025", "2025-09-01", actor="系统")
    svc.register_student("S2026001", "李四", "SE", "2026", "2026-09-01", actor="系统")

    # 首届入学时的旧方案
    v1_draft = svc.create_draft("SE", "MAJOR", actor="中方教务人员",
                                title="2025 级培养方案（旧）")["id"]
    svc.update_content(v1_draft, base_content("2025 级培养方案（旧）", 48, "考查", 2),
                       actor="中方教务人员")
    v1 = publish(svc, v1_draft, "2025-09-01")

    old_student = svc.resolve_student("S2025001")

    # 入学后：双方修订学时与考核方式（紧急更正）+ 学期迁移，形成新方案
    v2_draft = svc.revise(v1, "correction", actor="中方教务人员",
                          title="2026 级培养方案（新）")["id"]
    svc.correct_course(v2_draft, "DB201", {"hours": 56, "assessment": "考试"},
                       reason="合作双方复核大纲，增加实践学时并改为考试考核",
                       actor="中方教务人员")
    svc.migrate_semester(v2_draft, "DB201", 3, actor="外方课程负责人")

    # 演示会签撤回：中方先签后撤（理由：等待外方教材版次确认），改由负责人签署
    svc.submit_for_countersign(v2_draft, actor="中方教务人员")
    svc.sign(v2_draft, "CN", "王教务", actor="王教务")
    svc.revoke_signature(v2_draft, "CN", "王教务",
                         reason="外方教材版次尚在确认，暂不代表中方完成会签", actor="王教务")
    svc.sign(v2_draft, "CN", "李院长", actor="李院长")
    svc.sign(v2_draft, "FN", "Dr. Smith", actor="Dr. Smith")
    v2 = svc.publish(v2_draft, "2026-09-01", actor="中方教务人员")["id"]

    old_after = svc.resolve_student("S2025001")
    new_student = svc.resolve_student("S2026001")

    report = {
        "v1": svc.frozen_snapshot(v1),
        "v2": svc.frozen_snapshot(v2),
        "2025级_首次查询": old_student,
        "2025级_新版发布后查询": old_after,
        "2026级_查询": new_student,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    assert old_after["applicable_version"]["id"] == v1, "旧学生适用方案发生漂移！"
    assert new_student["applicable_version"]["id"] == v2, "新学生应适用新版本！"
    db_course_old = next(c for c in old_after["frozen_plan"]["courses"] if c["code"] == "DB201")
    assert (db_course_old["hours"], db_course_old["assessment"], db_course_old["semester"]) == (48, "考查", 2)
    db_course_new = next(c for c in new_student["frozen_plan"]["courses"] if c["code"] == "DB201")
    assert (db_course_new["hours"], db_course_new["assessment"], db_course_new["semester"]) == (56, "考试", 3)
    print("\n断言全部通过：旧学生不漂移，新版本只适用于新生。")
    repo.close()


if __name__ == "__main__":
    main()
