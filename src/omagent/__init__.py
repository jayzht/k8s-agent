"""O&M Agent —— 面向 Kubernetes 的 L1 档位运维 Agent。

设计核心（详见 docs/PRD-v0.1.md）：
L1 档位下的产品竞争力不在"Agent 能否自主决策"，而在"它能否把一次变更讲得让人
30 秒内敢点确认"。因此本包的重心是**安全内核**：工具级固定审批切分、强制服务端
dry-run、影响面分析、熔断规则、不可篡改审计。

规划器（planner）是可插拔的，且**不是安全边界**——即使换成 LLM，它能做的也只是
提出一个 Candidate，必须经过完整的安全内核和人工批准才可能被执行。
"""

__version__ = "0.1.0"

from .agent import TOOLS, GateViolation, OpsAgent, Refusal
from .models import Decision, Evidence, ExecutionResult, Impact, Proposal, Target, Tier

__all__ = [
    "OpsAgent",
    "TOOLS",
    "GateViolation",
    "Refusal",
    "Tier",
    "Target",
    "Evidence",
    "Impact",
    "Proposal",
    "Decision",
    "ExecutionResult",
    "__version__",
]
