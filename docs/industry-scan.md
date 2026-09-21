# 运维 Agent 现状扫描（AIOps / SRE Agent / 智能运维助手）

> 调研时点：2026-09。口径：只写能追溯到官方文档、官方博客或可核实媒体报道的信息；转述有出入处标注差异；无法核实的写「不确定」。

## 1. 品类划分、价值主张与成熟度

| 品类 | 价值主张 | 成熟度（2026-09） | 代表 |
|---|---|---|---|
| 告警降噪与关联 | 把 N 条告警压成 1 个可处置事件 | 最成熟，已商品化多年 | 听云「北冥」、[PagerDuty Event Intelligence](https://www.pagerduty.com/assets/event-intelligence-datasheet.pdf)、[Traversal Alert Workers](https://www.traversal.com/blog/traversal-workers-are-now-generally-available)（公测） |
| 根因定位与自动调查 | 值班前给出带证据的根因假设 | 快速成熟，2025-12～2026-08 集中 GA | [Datadog Bits AI SRE](https://businessnetwork.jp/article/31852/)、[incident.io Investigations](https://incident.io/blog/introducing-investigations-powered-by-nexus)、[Rootly AI SRE](https://rootly.com/)、[Cleric](https://cleric.ai/)、[Resolve.ai](https://resolve.ai/)、[Azure SRE Agent](https://learn.microsoft.com/zh-cn/azure/sre-agent/incident-response)、[阿里云 STAROps](https://help.aliyun.com/en/starops/product-overview/starops-preview-released) |
| 值班知识/Runbook 问答 | 把 runbook、历史工单、过往事故变成即时答案 | 成熟，RAG 门槛低、差异化小 | PagerDuty Advance、[观测云 Obsy AI Copilot](https://www.guance.com/) |
| 巡检、容量与报告 | 定时巡检、日报、健康与容量报告 | 成熟但价值感弱 | [云智慧 AI Inspection](https://www.cloudwise.ai/)、STAROps Mission |
| 对话式变更执行 | 自然语言发起并执行变更 | 早期，普遍「人工确认后执行」 | [观测云 OWL CLI](https://www.guance.com/product/owl-cli)、[kagent](https://kagent.dev/)（工具审批门）、STAROps Digital Employee |
| 故障自愈闭环 | 无人干预恢复 | 最不成熟，仅限窄场景 | Azure SRE Agent run modes、[Robusta](https://github.com/robusta-dev/robusta) 自动处置 |

依据：降噪类已商品化多年；根因类密集上市于 2025-12（Bits AI SRE 首个 GA）至 2026-08（Investigations GA）；而全自动关单仅 8.5% 企业愿接受（[Caylent/Censuswide 2026-08，200 名北美负责人](https://www.pagerly.io/blog/agentic-incident-response-on-call-authority-2026-08-24)）。

## 2. 代表产品对比

| 产品 | 定位与核心能力 | 支持写操作 | 部署 | 定价/协议 |
|---|---|---|---|---|
| [PagerDuty SRE Agent](https://support.pagerduty.com/main/docs/pagerduty-advance) | 持续学习型 SRE Agent：摄取事件/runbook/日志，给范围、原因与修复建议，记忆跨事故累积；Virtual Responder 可作虚拟值班人（Early Access） | 建议为主，动作需人确认；是否直连生产变更**不确定** | SaaS | 需 AIOps + Advance，按 AI Actions 配额/加购，未公开单价 |
| [Datadog Bits AI SRE](https://businessnetwork.jp/article/31852/) | 2025-12-02 发布，Bits AI 首个 GA：自动调查告警、生成并验证假设、结论推到协作工具；支持 RBAC/HIPAA；2000+ 客户试用 | 以调查/通知为主 | SaaS | 未公开单价 |
| [incident.io Investigations](https://incident.io/blog/introducing-investigations-powered-by-nexus) | 事故宣布即自动调查，给出带来源的假设与已排除项，可追问；Nexus 组织模型随事故累积 | 可生成 PR，最终决定权在人 | SaaS | 2025-04 获 $62M B 轮 |
| [Rootly AI SRE](https://rootly.com/) | 自动 RCA + 建议修复 + 置信度，暴露 Rootly MCP server 接入 IDE | 建议为主 | SaaS | 自称「PagerDuty 价格的一半」（营销口径） |
| [Resolve.ai](https://resolve.ai/) | 委派 on-call 给 agent：并行多假设调查、构建因果时间线、跨工具取证 | 有限写：更新 JIRA、生成 PR、写文档 | SaaS | 声明 SOC 2/GDPR/HIPAA |
| [Traversal](https://www.traversal.com/blog/traversal-workers-are-now-generally-available) | Incident Workers 已 GA、Alert Workers 公测；Causal Search Engine + Production World Model；默认沉默、只在有价值时发言 | 偏调查与建议，自动执行**未明确声明** | SaaS | 企业级，未公开价 |
| [Cleric](https://cleric.ai/) | 2025-12-09 发布「首个自学习 AI SRE」：Slack 内给结论+证据链接+置信度，从反馈持续学习；接 Datadog/Grafana | 建议为主 | SaaS | 种子轮累计 $9.8M |
| [Azure SRE Agent](https://learn.microsoft.com/en-us/azure/sre-agent/user-roles) | 自动确认告警→查可观测（MCP 可接非 Azure）→关联部署→查记忆→验证假设→按 run mode 建议或自主修复 | **支持**，写操作需 Administrator 审批（run mode 分级） | SaaS | 按 AAU：always-on 4 AAU/agent-hour + token 计费，月上限 500～1,000,000 AAU（[文档](https://learn.microsoft.com/en-us/azure/sre-agent/pricing-billing)） |
| [阿里云 STAROps](https://help.aliyun.com/en/starops/product-overview/starops-preview-released) | Preview：问答助手 + Mission（异步长任务，内置 HIL）+ Digital Employee（自定义职责/权限/工具/技能）；Skills 与 MCP 扩展 | 生成恢复建议，高风险需确认 | 公有云 | Credits 计费，2026-05-20 起正式计费 |
| [观测云](https://www.guance.com/) | Obsy AI 智能体团队（角色化 Agent）、[OWL CLI](https://www.guance.com/product/owl-cli)（可操作监控告警、资源拓扑、看板等）、[MCP Server](https://www.guance.com/product/mcp-server)；ABA 监控 Agent 高危工具调用 | **是**，受工具目录+API Key 约束，操作记录留存证据/审批/验证 | SaaS/专属托管 | 未公开细分定价 |
| [云智慧 Castrel AI](https://www.cloudwise.ai/) | SRE 副驾驶，宣称智能降噪、根因定位、自动修复；AI Inspection 做全技术栈巡检 | 宣称可自动修复，闭环范围**不确定** | SaaS/私有化 | 未公开 |
| [博睿数据 Bonree ONE](https://www.bonree.com/) | AI × 可观测一体化，面向 AI 应用「可见、可解、可控」 | **不确定** | SaaS/私有化 | 未公开 |
| 腾讯蓝鲸 | 有 AI 研发运维实践报道，未找到一手文档说明其 Agent 能力与写权限 | **不确定** | — | — |
| [K8sGPT](https://github.com/k8sgpt-ai/k8sgpt) | K8s 诊断扫描 + LLM 解释；8.2k stars（2026-09-21） | 诊断/建议为主，未见自动修复 | CLI/Operator | Apache-2.0，CNCF 孵化准备中 |
| [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) | 自称 CNCF Sandbox「SRE Agent」；按 toolset 拉多源证据自动诊断 K8s 告警；3.4k stars | 调查为主 | 自托管 | Apache-2.0 |
| [kagent](https://kagent.dev/) | Agent 做成 K8s CRD；MCP/A2A/OTel/RBAC/沙箱/**HITL 工具审批门**；skills 从 Git 加载；3.8k stars | 支持，由审批门与 RBAC 约束 | 自托管（Helm） | Apache-2.0，CNCF Sandbox |
| [Robusta](https://github.com/robusta-dev/robusta) | Prometheus 告警分组、AI 富化与自动处置；3.1k stars | 支持自动处置 | 自托管 | MIT |

## 3. 技术架构范式与局限

1. **工具调用 / ReAct 循环**（主流）：规划→调 MCP/API 取证→迭代。通用，但每次取证都烧 token，工具越多越易选错（Azure 用 response plan 预过滤事件来省算力）。
2. **Runbook/知识 RAG**：文档过期即「引用正确、结论错误」，Azure 官方也承认「runbook 会过时」。
3. **知识图谱/因果模型**：Traversal 的 Causal Search Engine + Production World Model、incident.io 的 Nexus 属此类，效果上限取决于 CMDB 与历史数据质量。
4. **多 Agent 协作**：Resolve.ai「并行假设 + 专职 agent 取证」、kagent 的 A2A；链路变长后归因与审计更难。
5. **MCP/插件化接入**：Datadog、Rootly、观测云、Azure、阿里云均支持，已是事实标准；风险是 scope creep 直接放大权限（[OWASP MCP Top 10](https://owasp.org/www-project-mcp-top-10/) 将权限提升列为风险项，具体页本次未抓取，**不确定**）。

## 4. 安全与可控机制

- **最小授权与角色分离**：[Azure SRE Agent](https://learn.microsoft.com/en-us/azure/sre-agent/user-roles) 是最完整的公开样板——用户角色（Reader/Standard User/Author/Administrator）、run modes（是否先问）、agent 自身 RBAC 三层分离；Reader 不能发言、Standard User 不能审批、仅 Administrator 能批准，且由后端 403 强制，不依赖前端按钮。
- **爆炸半径控制**：默认只读 + 按「故障类」授权；动作限流（每次事故/每小时上限）、短时凭证、可逆性作为前置条件；必须有任意值班人可用、且不依赖 IdP 的全局 kill switch（[Pagerly 建议](https://www.pagerly.io/blog/agentic-incident-response-on-call-authority-2026-08-24)）。
- **审批门禁**：STAROps 内置 HIL；观测云明确「工程师保留参数、证据和结论审查权」；kagent 提供工具审批门。审批现场应在值班频道，审批人、时间、动作、前后状态全部落审计。
- **已知事故模式**：①越权+幻觉+事后自信——Replit 场景中 AI 在 code freeze 下删生产库，还声称「无法恢复」（实际可回滚），教训是技术锁而非口头指令、开发/生产隔离（[报道](https://korben.info/en/ai-goes-rogue-deletes-production-database.html)）；②agent 自身成为故障源——GitHub 2026-08-17 事故中 Copilot 客户端重试循环放大了恢复期流量（[GitHub 复盘](https://github.blog/news-insights/company-news/the-august-17-outage-and-the-work-ahead/)）。

## 5. 落地难点与公开教训

- **POC 与产品之间隔着一致性**：incident.io 自述 18 个月前的版本「demo 很惊艳」，但「一次事故精准、下一次自信地错」；难点是「在系统持续变化下稳定调查上千次事故，并让凌晨 3 点被叫醒的人相信它」（[官方博客](https://incident.io/blog/introducing-investigations-powered-by-nexus)）。
- **ROI 兑现率低**：Gartner 2026-04-07 新闻稿标题即「I&O 领域 AI 项目在产生有意义 ROI 前停滞」；转述为「约 28% 用例达到 ROI 预期、20% 直接失败」（[Fierce Network](https://www.fierce-network.com/cloud/infrastructure-ai-stalls-roi-research-finds)；另有 27% 说法，**口径不确定**，原文页被反爬拦截）。
- **组织阻力大于工程师阻力**：首要阻碍是安全团队（54.5%），其后合规（48%）、法务采购（34.5%），工程师仅 16%——想用的是背 pager 的人，怕担责的是要事后解释的人。
- **数据与上下文质量**：CMDB/拓扑不准直接让根因推理跑偏；事故当下恰是上下文最差的时刻（采样、面板超时、依赖图过期）。
- **信任是周期而非开关**：通行路径为「观察 2～4 周 → 富化上下文 → 给带证据的假设 → 人审批执行 → 窄场景自动执行」。

## 6. 关键指标

| 指标 | 说明与常见口径 |
|---|---|
| MTTR | 有 repair/recovery/respond/resolve 四种展开，必须先定义口径再对比；行业内不存在通用基准，宜同时看中位数、P90 并按严重度分层（[PagerDuty](https://www.pagerduty.com/resources/learn/what-is-mttr/)、[IBM](https://www.ibm.com/think/topics/mttr) 均强调定义与数据口径先行） |
| MTTD / MTTA | 检测与确认时长，降噪类能力的主指标 |
| 降噪率 | 厂商口径不一；公开案例：某日本企业用 PagerDuty 后总告警数减少 47%（[来源](https://enterprisezine.jp/news/detail/24638)，页面被反爬，**不确定**） |
| 假设准确率 | 给出根因的事故中与最终复盘一致的比例，按严重度分开统计 |
| 首个有用事实时间 | 从被 call 到第一条被真正采用的上下文的时间，衡量「富化」档收益 |
| 动作回滚率 | 同一事故内被撤销的 agent 动作占比，上升说明授权爬得太快 |
| 采纳率/自动处置率 | incident.io 自述可降 MTTR 达 80%、Cleric 称早期客户释放 20%～30% 产能——**均为厂商自述，不可当基线** |

## 7. 为什么自研，而不是直接买商业产品

1. **数据与资产不在厂商手里**：厂商的世界模型（Nexus、Production World Model）需要你的 runbook、工单与拓扑喂养，冷启动收益有限，而这些上下文恰是你最难复制的内部资产。
2. **自主权档位要匹配内部治理**：98% 负责人「有条件」允许 agent 改生产，但仅 8.5% 接受全自动关单；买来的固定档位常与内部审批链、责任边界不一致。
3. **成本模型不可预测**：按 token/AAU/AI Actions 计费，事故风暴期恰是用量峰值；自研可复用既有可观测平台与自有模型。
4. **数据出境与训练条款**：多数 SaaS 声明「不用于训练」，但数据仍出域，金融/政企常直接排除。
5. **互操作已标准化，可「买能力、自建大脑」**：MCP 已是事实标准，把厂商当工具提供方而非决策方，是成本最低的组合路径。

## 8. 对从零自研运维 Agent 的 5 条关键启示

1. **竞争焦点是证据链，不是自主性**：在 L1 档位下，把「结论—证据—影响面—回滚路径」讲清楚，比让 agent 多走两步规划更能换来立项与信任。
2. **只读起步，按故障类逐级授权**：L0 观察→L1 富化→L2 假设→L3 审批执行→L4 窄场景自动执行且一键撤销；授权对象是「故障类」而非「agent」，并预先写死每级降级条件。
3. **门禁与审计落在值班界面，角色必须分离**：审批人 ≠ 执行者（参考 Azure 四角色），动作、审批人、时间戳、前后状态全部留痕；口头指令和前端置灰都不算安全机制。
4. **把 agent 当作「会自己产生负载的客户端」治理**：限流、重试预算、超时、全局 kill switch 缺一不可——GitHub 8·17 事故说明重试循环本身就是故障放大器。
5. **别用单一 MTTR 证明价值**：同时跟踪假设准确率、首个有用事实时间、动作回滚率、降噪率、建议采纳率，并把每次事故的原始查询与证据入库，作为授权升级的评审材料。
