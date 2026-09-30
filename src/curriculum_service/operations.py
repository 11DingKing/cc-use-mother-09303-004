"""课程方案内容的纯函数式变更操作。

三类受管控的演进都在这里完成：课程替代、紧急更正、学期迁移。
它们只产生"新内容"，从不改写已发布版本；变更轨迹写入内容中的
``change_log``，随发布快照一并冻结。
"""
from __future__ import annotations

import copy
from datetime import date

from .errors import StateConflictError, ValidationError
from .models import ASSESSMENTS, Course


def _index_courses(content: dict) -> dict[str, dict]:
    return {c["code"]: c for c in content.get("courses", [])}


def _log(content: dict, op: str, detail: dict, actor: str) -> None:
    content.setdefault("change_log", []).append(
        {"op": op, "detail": detail, "actor": actor, "at": date.today().isoformat()}
    )


def apply_substitution(content: dict, old_code: str, new_course: dict, actor: str) -> dict:
    """课程替代：用一门新课整体替换旧课（编号、学时、考核可同时变化）。"""
    result = copy.deepcopy(content)
    courses = result.get("courses", [])
    if not any(c["code"] == old_code for c in courses):
        raise ValidationError(f"待替代课程不存在：{old_code}")
    parsed = Course.from_dict(new_course)
    if parsed.code != old_code and any(c["code"] == parsed.code for c in courses):
        raise ValidationError(f"替代课程编号已存在：{parsed.code}")
    result["courses"] = [c for c in courses if c["code"] != old_code]
    result["courses"].append(parsed.to_dict())
    _log(result, "课程替代", {"old_code": old_code, "new_code": parsed.code}, actor)
    return result


def apply_correction(content: dict, course_code: str, changes: dict, reason: str, actor: str) -> dict:
    """紧急更正：仅允许更正学时与考核方式，并强制记录更正原因。

    更正保留课程编号（培养路径可追溯），轨迹同时进入 change_log 与
    corrections 两个冻结字段。
    """
    if not reason or not reason.strip():
        raise ValidationError("紧急更正必须填写原因")
    allowed = {"hours", "assessment"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"紧急更正不允许修改字段：{'、'.join(sorted(unknown))}")
    result = copy.deepcopy(content)
    courses = result.get("courses", [])
    current = next((c for c in courses if c["code"] == course_code), None)
    if current is None:
        raise ValidationError(f"待更正课程不存在：{course_code}")
    before: dict = {}
    if "hours" in changes:
        hours = int(changes["hours"])
        if hours <= 0:
            raise ValidationError("学时必须为正整数")
        before["hours"] = current["hours"]
        current["hours"] = hours
    if "assessment" in changes:
        assessment = str(changes["assessment"]).strip()
        if assessment not in ASSESSMENTS:
            raise ValidationError(f"考核方式必须是：{'/'.join(ASSESSMENTS)}")
        before["assessment"] = current["assessment"]
        current["assessment"] = assessment
    if not before:
        raise ValidationError("紧急更正至少要给出学时或考核方式的变化")
    after = {k: current[k] for k in before}
    record = {
        "course_code": course_code,
        "before": before,
        "after": after,
        "reason": reason.strip(),
        "actor": actor,
        "at": date.today().isoformat(),
    }
    result.setdefault("corrections", []).append(record)
    _log(result, "紧急更正", {"course_code": course_code, "fields": sorted(before)}, actor)
    return result


def apply_semester_migration(content: dict, course_code: str, to_semester: int, actor: str) -> dict:
    """学期迁移：调整课程开课学期，不动学时与考核。"""
    to_semester = int(to_semester)
    if to_semester <= 0:
        raise ValidationError("目标学期必须为正整数")
    result = copy.deepcopy(content)
    current = next((c for c in result.get("courses", []) if c["code"] == course_code), None)
    if current is None:
        raise ValidationError(f"待迁移课程不存在：{course_code}")
    if current["semester"] == to_semester:
        raise StateConflictError(f"课程 {course_code} 已在第 {to_semester} 学期")
    old = current["semester"]
    current["semester"] = to_semester
    _log(result, "学期迁移", {"course_code": course_code, "from": old, "to": to_semester}, actor)
    return result
