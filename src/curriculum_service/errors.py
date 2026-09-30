"""领域错误类型。"""
from __future__ import annotations


class CurriculumError(Exception):
    """所有服务端领域错误的基类。"""

    code = "domain_error"
    http_status = 400


class ValidationError(CurriculumError):
    """请求数据不满足领域校验。"""

    code = "validation_error"
    http_status = 400


class NotFoundError(CurriculumError):
    """引用的实体不存在。"""

    code = "not_found"
    http_status = 404


class StateConflictError(CurriculumError):
    """实体当前状态不允许该操作。"""

    code = "state_conflict"
    http_status = 409


class QuorumError(CurriculumError):
    """签署未达到多方法定人数。"""

    code = "quorum_not_met"
    http_status = 422


class PublishConflictError(CurriculumError):
    """并发发布仲裁：同一草案只能产生一个合法版本。"""

    code = "publish_conflict"
    http_status = 409


class DependencyConflictError(CurriculumError):
    """同一生效槽位出现互不兼容的依赖闭包，只能有一个合法结果。"""

    code = "dependency_conflict"
    http_status = 409


# 兼容旧名称
DomainError = CurriculumError
