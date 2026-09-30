"""合作办学课程版本服务端。

领域不变量（见 domain/contract.json）：

* 培养版本：方案按专业与招生批次版本化，发布后不可变。
* 多方法定人数：签署规则逐贡献方规定人数，不足不得发布。
* 学生适用性：学生入学时绑定生效版本，后续变更不得回溯漂移。
* 依赖冻结：发布时固化贡献方、教材、技能标准、签署规则与签名的完整快照。
"""
from __future__ import annotations

from .errors import (
    CurriculumError,
    DependencyConflictError,
    DomainError,
    NotFoundError,
    PublishConflictError,
    QuorumError,
    StateConflictError,
    ValidationError,
)
from .service import CurriculumService

__all__ = [
    "CurriculumService",
    "CurriculumError",
    "DomainError",
    "NotFoundError",
    "ValidationError",
    "StateConflictError",
    "QuorumError",
    "PublishConflictError",
    "DependencyConflictError",
]

__version__ = "0.2.0"
