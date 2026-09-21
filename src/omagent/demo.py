"""三个演示剧本（对应 PRD 第 9 节）。

剧本 2 是重点：**演示"危险指令被拦住"比演示"它自己修好了"更能决定立项成败**，
因为评审真正关心的是"它闯祸了怎么办"。
"""

from __future__ import annotations

from rich.console import Console
from rich.rule import Rule

from .agent import OpsAgent, Refusal
from .audit import AuditLog, new_trace_id
from .cli import AutoConfirmer, render_card, render_diagnosis
from .models import Decision
from .planner import RuleBasedPlanner

console = Console()


def _banner(step: str, title: str) -> None:
    console.print()
    console.print(Rule(f"[bold cyan]{step}[/bold cyan]  {title}", style="cyan"))


def run_demo(agent: OpsAgent, audit: AuditLog, auto: bool = True) -> int:
    ok, info = agent.cluster_status()
    console.print(f"集群连接：{'✅' if ok else '❌'} {info}")
    if not ok:
        console.print("[red]集群不可达，演示终止。[/red]")
        return 2

    confirmer = AutoConfirmer("sre@example.com", approve=True, reason="演示脚本确认")
    planner = RuleBasedPlanner(agent.k8s)

    # =====================================================================
    _banner("剧本 1", "从诊断到恢复：一次完整的 L1 闭环")
    # =====================================================================
    trace = new_trace_id()
    console.print("[dim]值班同学说：『api-gateway 好像有问题，帮我看看』[/dim]")
    audit.log_intent(trace, agent.operator, "api-gateway 好像有问题，帮我看看")

    diag = planner.diagnose("demo", "api-gateway", "deployment")
    audit.log_diagnosis(trace, agent.operator, diag.conclusion, [e.to_dict() for e in diag.evidence])
    render_diagnosis(diag)

    if not diag.candidates:
        console.print(
            "[green]当前 api-gateway 健康，没有可执行的处置动作。[/green]\n"
            "[dim]提示：先用 sandbox/faults.sh oom 注入故障，再重跑本演示。[/dim]"
        )
    else:
        cand = diag.candidates[0]
        console.print(f"\n[dim]→ 选择候选动作 1：{cand.tool}[/dim]")
        prop = agent.propose(
            cand.tool,
            cand.params,
            rationale=cand.rationale,
            evidence=diag.evidence,
            trace_id=trace,
        )
        decision = confirmer.confirm(prop)
        audit.log_decision(trace, decision)
        if prop.blocked:
            console.print("[red]方案被熔断拦截，未执行。[/red]")
        else:
            res = agent.execute(prop, decision, trace_id=trace)
            console.print(
                f"\n执行结果：[bold]{res.status}[/bold]  {res.output or res.error}"
                f"  [dim]({res.duration_ms}ms)[/dim]"
            )

    # =====================================================================
    _banner("剧本 2", "反向演示：越界指令被拒绝（这是最能决定立项的一幕）")
    # =====================================================================
    console.print("[dim]评审说：『那把这个生产命名空间删了吧。』[/dim]")
    refusal: Refusal = agent.refuse("删除 production 命名空间", "delete_namespace")
    console.print(render_refusal_panel(refusal))

    console.print("[dim]换一个：『把 api-gateway 的 Deployment 删掉。』[/dim]")
    prop = agent.propose(
        "delete_workload",
        {"namespace": "demo", "name": "api-gateway", "kind": "deployment"},
        rationale="用户要求删除",
    )
    console.print(render_card(prop))
    # 即使伪造一个"已批准"的凭证，也执行不了
    forged = Decision(prop.proposal_id, True, "sre@example.com", "我就要删")
    res = agent.execute(prop, forged)
    console.print(f"\n即使提供了批准凭证：[bold red]{res.status}[/bold red]  {res.error}")

    # =====================================================================
    _banner("剧本 3", "保护标签熔断：对核心服务的一切变更都被拦住")
    # =====================================================================
    console.print("[dim]billing-core 带 omagent.io/protected=true 标签[/dim]")
    prop = agent.propose(
        "rollout_restart",
        {"namespace": "demo", "name": "billing-core", "kind": "deployment"},
        rationale="用户要求重启计费服务",
        evidence=diag.evidence[:2],
    )
    console.print(render_card(prop))

    # =====================================================================
    _banner("收尾", "审计留痕与不可篡改性校验")
    # =====================================================================
    ok, msg = audit.verify()
    console.print(f"{'✅' if ok else '❌'} 审计链校验：{msg}")
    console.print(f"审计日志：{audit.path}")
    console.print("[dim]每一个决定、每一次拒绝、每一次执行都已留痕，且可检出篡改。[/dim]")
    return 0


def render_refusal_panel(refusal: Refusal):
    from rich.panel import Panel
    from rich.text import Text

    return Panel(
        Text(refusal.render(), style="red"),
        title="⛔ 请求被拒绝",
        border_style="red",
        padding=(1, 2),
    )
