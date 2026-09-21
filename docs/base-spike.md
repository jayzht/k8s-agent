# 开源基座 Spike 报告：HolmesGPT vs 自研实现

> 执行时间：2026-09 ｜ 环境：本地 kind 沙箱集群（`om-sandbox`，v1.37.0）
> 被测版本：HolmesGPT **0.42.0**（PyPI 安装，独立 venv）
> 模型：DeepSeek `deepseek-flash`（经 OpenAI 兼容接口）
> 结论：**建议"买能力、自持安全内核"——而不是整体替换。**

---

## 一、Spike 是怎么做的

1. 在独立 venv 中安装 HolmesGPT（避免污染主环境）
2. 用 `OPENAI_API_KEY` + `OPENAI_API_BASE` 指向 DeepSeek，模型名 `openai/deepseek-flash`
3. 在沙箱集群注入真实 OOM 故障（`sandbox/faults.sh oom`）
4. 执行 `holmes ask "命名空间 demo 下的 deployment api-gateway 有什么问题？根因是什么？"`
5. 与自研实现（规则引擎 / LLM 规划器 + 安全内核）对比

---

## 二、踩到的三个坑（都是真实的工程成本）

| # | 现象 | 原因 | 处理 |
|---|---|---|---|
| 1 | `OSError: [Errno 30] Read-only file system: '/home/ubuntu/.holmes'` | HolmesGPT 需要写 `~/.holmes` 存工具集状态、OAuth token、bash 白名单 | 把 `HOME` 指向可写目录 |
| 2 | **K8s toolset 全部 FAILED，Agent 只能"靠猜"** | `kubectl` 不在 Holmes 进程的 PATH 里 | 把 `bin/` 加入 PATH 后恢复正常 |
| 3 | `kubectl describe` 等命令报 `[OOM]` 未返回 | HolmesGPT 有 `TOOL_MEMORY_LIMIT_MB`（默认 800MB）限制工具输出 | 可调，但说明大输出场景需要调参 |

> 坑 2 值得单独强调：**工具集不可用时它不会明确报错，而是退化成"基于本地文件推断"**。
> 第一次运行时它读了 `/home/ubuntu/O&M-agent/sandbox/app/entrypoint.sh`，
> 直接看源码推断出故障。诊断结论碰巧是对的，但**依据是错的**——
> 这是在真实环境里会误导人的失败模式。

---

## 三、发现：它有一个能读任意本地文件的 bash toolset

第一次运行（K8s toolset 失效）时，HolmesGPT 的输出里出现了：

- 对 `sandbox/app/entrypoint.sh` 内容的直接引用
- "这个沙箱 faults.sh 可注入 4 种故障，且注入动作不落盘"
- 甚至列出了 `MODE=oom+limit 64Mi→OOMKilled(137)` 这样的**注入脚本对应表**

它读到了我的**故障注入脚本**，相当于看到了答案。

**这不是 HolmesGPT 的 bug，而是它的设计**：bash/filesystem toolset 很强，能兜底探索。
但在生产环境意味着：

> 一个运行在运维环境里的 Agent，可以读取宿主机/容器内任意可读文件——
> 包括 kubeconfig、云凭据、.env、其他服务的密钥挂载。

**结论：生产部署必须显式关闭 bash/filesystem toolset，或严格限制到只读白名单目录。**
这条已经写进下面的选型建议。

---

## 四、诊断质量对比（同一个真实 OOM 故障）

### HolmesGPT 的输出

- 准确识别 `MODE=oom` + 内存 limit 被降到 64Mi
- 影响面：滚动更新卡在 2/3、Service 端点从 3 减到 2、旧 RS 无法缩容
- 修复建议：回滚（方案 A）或就地改回配置（方案 B），并提醒**必须同时还原 MODE**
- 明确指出："如果 MODE=oom 确实是压测配置，则必须把 limit 提到 256Mi 以上"

**质量评价：高。** 它的推理链完整，甚至注意到"只改 limit 不改 MODE 会导致继续分配 256MB 而再次 OOM"这个容易漏的点。

### 自研 LLM 规划器的输出

- 同样识别 OOMKilled + 64Mi
- **多了一个推理动作**：对比新旧 ReplicaSet（旧的 128Mi 且全 Ready，新的 64Mi 且 OOM），
  用**对照**论证"根因是 limit 配置过小而非应用或依赖故障"
- 给出 `rollout_undo` 与 `patch_resources` 两个候选，并标注 PDB 约束

**质量评价：相当。** 自研版在"对照取证"上更结构化；HolmesGPT 在影响面描述上更细。

### 一个必须承认的差距

HolmesGPT 的 Kubernetes toolset **覆盖面**明显更宽（它自己发现并用了
`kubectl describe replicaset`、RS 注解里的 revision、Service endpoints 等），
而且它有 40+ 数据源（Prometheus / Loki / Grafana / ArgoCD / Datadog…）。
自研实现目前只有 K8s 一手 API，且在 bash 探索能力上是空白。

---

## 五、能力矩阵对比

| 维度 | HolmesGPT 0.42.0 | 自研实现 | 谁更强 |
|---|---|---|---|
| 数据源/工具生态 | **40+ 集成** | 仅 K8s 原生 API | HolmesGPT |
| 诊断推理质量 | 高 | 规则引擎模板化／LLM 较好 | 接近 |
| 自由探索（bash） | **有**（也是风险） | 无 | 取决于场景 |
| 工具级审批门禁 | 有（变更工具恒定人工批准） | 有 | 持平 |
| **动作分级（Tier 白名单）** | 无（统一走 catch-all） | **T0–T3 四档，配置化** | **自研** |
| **爆炸半径控制** | 不明确 | **命名空间白名单/保护标签/影响上限/冷却/冻结窗口** | **自研** |
| **影响面分析** | 描述性文字 | **结构化（PDB/单点/有状态/PVC/回滚 ETA）** | **自研** |
| **强制服务端 dry-run** | 未见强制 | **强制，未通过不生成方案** | **自研** |
| **审计** | 常规日志 | **哈希链，可检出篡改** | **自研** |
| 评测 harness | 有 benchmark | 37 用例 + 策略分类 + CI 护栏 | 持平 |
| 部署复杂度 | 较重（Helm/依赖树大） | 轻（单包） | 自研 |
| 自主可控 | 跟随上游 | 完全可控 | 自研 |

---

## 六、选型结论

### 建议：**分层组合，而非二选一**

```
┌─────────────────────────────────────────────┐
│  规划/诊断层                                 │
│  ├─ HolmesGPT 的 toolset 生态（取证能力）    │
│  └─ 自研 LLM 规划器（对照推理、成本可控）    │
├─────────────────────────────────────────────┤
│  ★ 安全内核（自持，不外包）★                 │
│  Tier 白名单 · 熔断 · 影响面 · dry-run       │
│  · 审批门禁 · 哈希链审计                     │
└─────────────────────────────────────────────┘
```

**具体行动项**：

1. **取证层可以复用 HolmesGPT 的 toolset**（尤其 Prometheus/Loki 等可观测数据源接入），
   这是它最值钱的部分，也是自研最费时间的部分
2. **执行边界必须自持**：Tier 分级、熔断、dry-run、审批门禁、审计——这些是我们的差异化资产，
   也不应该依赖上游的默认行为
3. **生产部署必须关闭 bash/filesystem toolset**，或限制到只读白名单目录
   （理由见第三节：它能读到故障注入脚本，同样也能读到 kubeconfig 和密钥）
4. **保留适配层**：业务逻辑不写进 HolmesGPT，保证 3 个月内可替换
5. **注意上游给的默认权限比我们的 L1 更宽**：HolmesGPT 默认 ClusterRole 对
   `deployments`/`statefulsets` 授予了 `delete`，接入时必须裁剪

### 为什么不整体替换

- 我们的 Tier 分级、爆炸半径控制、强制 dry-run、哈希链审计，HolmesGPT 都没有等价物
- 它统一走一个 catch-all 变更工具，粒度比"分档白名单"粗
- 依赖树和部署复杂度显著更重
- 一旦出问题，责任边界在"上游行为"和"我们的配置"之间会变模糊

### 为什么不全盘自研

- 40+ 数据源接入是实打实的工程量，重复造没有收益
- 它的 benchmark 与社区能持续提供新的故障模式覆盖
- 上游已经验证过"工具级固定审批切分"这个关键设计（我们也采用了同一模式）

---

## 七、复现方式

```bash
bash scripts/setup-holmes.sh          # 独立 venv 安装
source .venv-holmes/bin/activate
export HOME="$PWD/var/holmes-home"    # 坑 1：可写 HOME
export PATH="$PWD/bin:$PATH"          # 坑 2：kubectl 必须在 PATH
export KUBECONFIG="$PWD/var/kubeconfig"
export OPENAI_API_KEY="$DEEPSEEK_API_KEY"
export OPENAI_API_BASE="https://api.deepseek.com/v1"
# 代理只走外部 API，本地集群必须绕过
export no_proxy=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc,.cluster.local

sandbox/faults.sh oom
holmes ask "命名空间 demo 下的 deployment api-gateway 有什么问题？根因是什么？" \
  --model "openai/deepseek-flash"
```
