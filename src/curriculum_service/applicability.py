"""学生适用版本的解析规则（纯函数，无 IO，便于回归测试）。

规则（与领域契约"学生适用性"不变量对应）：

1. 只考虑生效日不晚于学生入学日的已发布版本；
2. 招生批次级（BATCH）版本优先于专业级（MAJOR）版本；
3. 同级中取生效日最新者，再以版本号、发布时间、版本 ID 逐级确定决胜，
   结果唯一且可复现。

解析只在学生首次查询时发生一次；结果由服务层冻结，之后任何变更
都不得改派（见 service.resolve_student）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .errors import NotFoundError
from .models import SCOPE_BATCH, SCOPE_MAJOR


@dataclass(frozen=True)
class Candidate:
    version_id: str
    scope: str
    batch_code: Optional[str]
    effective_date: str
    version_no: int
    published_at: str
    content_hash: str
    title: str
    kind: str


@dataclass(frozen=True)
class Resolution:
    chosen: Candidate
    """参与决胜但落选的候选（含落选原因），用于"为何采用该版本"的解释。"""
    considered: list[dict]


def _sort_key(c: Candidate) -> tuple:
    return (c.effective_date, c.version_no, c.published_at, c.version_id)


def resolve(candidates: list[Candidate], enrolled_at: str, batch_code: str) -> Resolution:
    considered: list[dict] = []
    eligible: list[Candidate] = []
    for c in candidates:
        if c.effective_date > enrolled_at:
            considered.append(
                {
                    "version_id": c.version_id,
                    "scope": c.scope,
                    "effective_date": c.effective_date,
                    "excluded": True,
                    "reason": "生效日晚于入学日，入学时该版本尚未生效",
                }
            )
            continue
        if c.scope == SCOPE_BATCH and c.batch_code != batch_code:
            considered.append(
                {
                    "version_id": c.version_id,
                    "scope": c.scope,
                    "batch_code": c.batch_code,
                    "excluded": True,
                    "reason": "批次级版本面向其他招生批次",
                }
            )
            continue
        eligible.append(c)

    batch_level = [c for c in eligible if c.scope == SCOPE_BATCH]
    major_level = [c for c in eligible if c.scope == SCOPE_MAJOR]

    # 具体作用域优先：存在批次级候选时，专业级全部作为被覆盖项记录。
    pool = batch_level if batch_level else major_level
    if batch_level:
        for c in sorted(major_level, key=_sort_key, reverse=True):
            considered.append(
                {
                    "version_id": c.version_id,
                    "scope": c.scope,
                    "effective_date": c.effective_date,
                    "excluded": True,
                    "reason": "存在更具体的招生批次级版本，专业级被覆盖",
                }
            )

    chosen = max(pool, key=_sort_key)
    for c in sorted(pool, key=_sort_key, reverse=True):
        if c.version_id == chosen.version_id:
            continue
        if c.effective_date < chosen.effective_date:
            reason = "生效日早于中选版本"
        elif c.version_no < chosen.version_no:
            reason = "同生效日下版本号较早"
        else:
            reason = "决胜规则（发布时间/版本 ID）排序在后"
        considered.append(
            {
                "version_id": c.version_id,
                "scope": c.scope,
                "effective_date": c.effective_date,
                "excluded": True,
                "reason": reason,
            }
        )
    return Resolution(chosen=chosen, considered=considered)
