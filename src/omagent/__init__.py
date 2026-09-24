"""O&M Agent —— 面向 Kubernetes 的对话式运维助手。

产品的全部主张可以压成一句话：

    看东西不用问，动东西要问。

对应到代码里的两条路径：``OpsAgent.run_readonly()`` 自动执行，
``OpsAgent.execute_write()`` 没有人工批准就执行不了。
"""

__version__ = "0.2.0"

from .agent import GateViolation, OpsAgent
from .models import Decision, ExecutionResult, Impact, Proposal, Target
from .tools import FORBIDDEN, READONLY_TOOLS, TOOLS, WRITE_TOOLS

__all__ = [
    "OpsAgent",
    "GateViolation",
    "Decision",
    "ExecutionResult",
    "Impact",
    "Proposal",
    "Target",
    "FORBIDDEN",
    "READONLY_TOOLS",
    "TOOLS",
    "WRITE_TOOLS",
    "__version__",
]
