"""领域常量与课程值对象。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ValidationError

# 方案版本生命周期状态（对应 domain/contract.json 的 states）
STATE_DRAFT = "共创"
STATE_CORRECTION = "更正"
STATE_COUNTERSIGN = "会签"
STATE_PUBLISHED = "发布"
STATE_APPLICABLE = "适用"

EDITABLE_STATES = (STATE_DRAFT, STATE_CORRECTION)

# 版本演进类型
KIND_STANDARD = "standard"
KIND_SUBSTITUTION = "substitution"
KIND_CORRECTION = "correction"
KIND_MIGRATION = "migration"

KIND_LABELS = {
    KIND_STANDARD: "常规修订",
    KIND_SUBSTITUTION: "课程替代",
    KIND_CORRECTION: "紧急更正",
    KIND_MIGRATION: "学期迁移",
}

SCOPE_MAJOR = "MAJOR"
SCOPE_BATCH = "BATCH"

ASSESSMENTS = ("考试", "考查")


@dataclass
class Course:
    """一门课程的培养安排。"""

    code: str
    name: str
    hours: int
    assessment: str
    semester: int
    party_code: str
    textbooks: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict) -> "Course":
        try:
            course = cls(
                code=str(value["code"]).strip(),
                name=str(value["name"]).strip(),
                hours=int(value["hours"]),
                assessment=str(value["assessment"]).strip(),
                semester=int(value["semester"]),
                party_code=str(value["party_code"]).strip(),
                textbooks=[str(c).strip() for c in value.get("textbooks", [])],
                skills=[str(c).strip() for c in value.get("skills", [])],
            )
        except KeyError as exc:
            raise ValidationError(f"课程缺少字段：{exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"课程 {value.get('code')!r} 字段类型不合法：{exc}") from exc
        if not course.code or not course.name:
            raise ValidationError("课程编号与名称不能为空")
        if course.hours <= 0:
            raise ValidationError(f"课程 {course.code} 学时必须为正整数")
        if course.assessment not in ASSESSMENTS:
            raise ValidationError(f"课程 {course.code} 考核方式必须是：{'/'.join(ASSESSMENTS)}")
        if course.semester <= 0:
            raise ValidationError(f"课程 {course.code} 学期必须为正整数")
        return course

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "hours": self.hours,
            "assessment": self.assessment,
            "semester": self.semester,
            "party_code": self.party_code,
            "textbooks": list(self.textbooks),
            "skills": list(self.skills),
        }


def empty_content(title: str | None = None) -> dict:
    """新建草案的空内容骨架。"""
    return {
        "title": title,
        "courses": [],
        "contributors": [],
        "textbooks": [],
        "skill_standards": [],
        "signing_rules": [],
    }
