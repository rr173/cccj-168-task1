"""领域基础类型:枚举、错误、规范化哈希。

快照一致性的根基:所有进入决策证明的对象都经过 canonical() 序列化,
保证同一逻辑快照在任何时间、任何进程得到同一个哈希。
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum


# ---------------------------------------------------------------- 枚举

class Evaluability(str, Enum):
    PENDING = "PENDING"                # 可评估性待定(缺失数据未处理)
    EVALUABLE = "EVALUABLE"            # 可评估
    NON_EVALUABLE = "NON_EVALUABLE"    # 不可评估(不进入 DLT 计数,但保留为事实)


class DataStatus(str, Enum):
    PENDING = "PENDING"      # 存在缺失数据(如实验室结果未回报)
    COMPLETE = "COMPLETE"    # 数据完整


class SubjectStatus(str, Enum):
    ACTIVE = "ACTIVE"
    REPLACED = "REPLACED"    # 已被替补;其给药/事件仍作为事实保留在快照中
    WITHDRAWN = "WITHDRAWN"


class CohortStatus(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"        # 紧急暂停:阻断后续给药,保留已给药事实
    ESCALATED = "ESCALATED"  # 委员会决定递增,本队列关闭
    STOPPED = "STOPPED"      # 委员会决定停止


class Intent(str, Enum):
    ESCALATE = "ESCALATE"    # 递增到下一剂量水平
    EXPAND = "EXPAND"        # 扩展当前队列(3+3 中 1 例 DLT 时扩至 6 例)
    STOP = "STOP"            # 停止
    HOLD = "HOLD"            # 暂缓,继续观察


class Vote(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class SessionStatus(str, Enum):
    OPEN = "OPEN"
    DECIDED = "DECIDED"
    SUPERSEDED = "SUPERSEDED"  # 快照已过期,会话作废(投票记录保留)


# ---------------------------------------------------------------- 错误

class PlatformError(Exception):
    status = 400
    code = "PLATFORM_ERROR"

    def __init__(self, message: str = "", details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class NotFound(PlatformError):
    status = 404
    code = "NOT_FOUND"


class Conflict(PlatformError):
    status = 409
    code = "CONFLICT"


class StaleSnapshot(Conflict):
    """投票/决策所依据的快照已过期:迟到 AE、替补、数据处理或规则修订
    使队列状态前进,禁止把不同快照拼成结论。"""
    code = "STALE_SNAPSHOT"


class DuplicateVote(Conflict):
    code = "DUPLICATE_VOTE"


class CohortPaused(Conflict):
    code = "COHORT_PAUSED"


class Frozen(Conflict):
    code = "COHORT_FROZEN"


class PreconditionFailed(PlatformError):
    status = 422
    code = "PRECONDITION_FAILED"


class InvalidInput(PlatformError):
    status = 400
    code = "INVALID_INPUT"


# ---------------------------------------------------------------- 规范化哈希

def canonical(obj) -> str:
    """确定性 JSON 序列化:键排序、无空白、UTF-8。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(obj) -> str:
    return sha256_hex(canonical(obj))
