# 规则引擎盲区报告

> 由 `omagent fuzz` 生成：**让大模型在已知故障分类之外设计场景，再拿规则引擎去考**。
> 规则引擎自己产不出这个答案——它只会匹配已知模式。
>
> ⚠️ **期望值是模型假设，不是真值。** 判定是待复核的线索。
> 判定口径以**行为**为准（提了什么动作），分类标签不同不算失败——
> 模型会自造标签（如 `service_selector_label_mismatch`），那不是缺陷。

## 三轮汇总

| 轮次 | 生成 | 🚨 假阳性 | ⚠️ 漏报 | · 未识别 | ✓ 通过 |
|---|---|---|---|---|---|
| round1 | 6 | 1 | 2 | 1 | 2 |
| round2 | 6 | 3 | 0 | 0 | 3 |
| round3 | 6 | 2 | 2 | 1 | 1 |
| **合计** | **18** | **6** | **4** | **2** | **6** |

## 已修复（每一轮都闭合了一批）

| 发现 | 轮次 | 修复 |
|---|---|---|
| **退出码 137 ≠ OOMKilled** | R1 | 正向 OOM 证据优先；新增 `probe_kill` 类别；探针误杀建议回滚 |
| 探针误杀测不出来（只认 `reason=Killing`） | R2 | 改为按事件**消息**匹配，覆盖 404 / timeout / 各措辞 |
| **Pod 全就绪但 Service 无端点 → 报 healthy** | R1/R2 | 新增 `service_no_endpoints` 相关性检查，明确阻止重启 Pod |
| 节点被 cordon 导致 Pending → 只建议缩容 | R2 | 检测节点不可调度，优先 `uncordon_node` |
| 生成用例的期望值受自身实现影响 | R1 | 修正 `probe-003`——原期望值是照搬实现错误行为写的 |

> **最有价值的一条**：`probe-003` 这条**既有用例的期望值本身是错的**。
> 没有外部出题人，这个缺陷会被自己的用例长期掩盖。

---

# 未闭合的盲区明细

> 场景由大模型生成（`omagent fuzz`），期望值是**模型假设**，需人工复核。

| 判定 | 数量 |
|---|---|
| 🚨 假阳性提议 | 6 |
| ⚠️ 漏报 | 4 |
| · 特征未识别 | 2 |
| ✓ 通过 | 6 |

---

## [fuzz-002-StorageClass供给失败] StorageClass 供给失败让 Pod Pending，看起来像调度失败

**判定**：🚨 假阳性提议（最危险）

- 期望类别：`pvc_unbound_storage_provisioning_failed`
- 实际类别：`pending_unschedulable`
- 规则引擎提议：`['scale_workload']`
- 模型认为合理：`['get_workload', 'get_events']`

> 提议 ['scale_workload']，但模型认为合理的是 ['get_workload', 'get_events']（需人工判断谁对）

**为什么在已知类别之外**：Pod 是 Pending 且 reason 是 Unschedulable，事件 reason 也是 FailedScheduling，几乎完美地伪装成 pending_unschedulable。但调度器本身完全健康：事件正文写的是 'pod has unbound immediate PersistentVolumeClaims'，真正的原因是 CSI provisioner 无响应导致 PVC 供给超时，属于已知八类之外的『存储供给失败』。

**朴素实现会怎么答错**：Pending + Unschedulable + FailedScheduling 三个信号会被 pending_unschedulable 规则全量命中，朴素实现会给出 cordon_node / drain_node 或『节点资源不足，请扩容节点』的结论；它不会去注意 FailedScheduling 消息体的尾部条件，也不会去关联 PVC 上的 ProvisioningFailed 事件。此外 endpoints 里 postgres=0 还会诱使它误报 service_no_endpoints。

## [fuzz-002-节点被误cordon导致PodP] 节点被误 cordon 导致 Pod Pending，并非资源不足

**判定**：🚨 假阳性提议（最危险）

- 期望类别：`cordoned_node_capacity`
- 实际类别：`pending_unschedulable`
- 规则引擎提议：`['scale_workload']`
- 模型认为合理：`['uncordon_node', 'get_nodes', 'get_events', 'get_pods']`

> 提议了本场景不应使用的动作：['scale_workload']（模型认为此处不该用）

**为什么在已知类别之外**：表面信号是 pending_unschedulable，但根因是某个节点被手动 cordon 后未 uncordon，集群实际容量足够，不属于资源不足或节点故障。

**朴素实现会怎么答错**：朴素系统看到 Pod Pending / Unschedulable，会归类 pending_unschedulable，建议 scale_workload、patch_resources 或 drain_node；实际应先 get_nodes 查看 SchedulingDisabled，正确动作是 uncordon_node。

## [fuzz-003-Liveness探针路径错误导致] Liveness 探针路径错误导致容器反复被 kill，看似 OOMKilled

**判定**：🚨 假阳性提议（最危险）

- 期望类别：`liveness_probe_misconfig`
- 实际类别：`probe_kill`
- 规则引擎提议：`['rollout_undo', 'rollout_restart']`
- 模型认为合理：`['rollout_undo', 'get_events', 'get_logs', 'get_workload']`

> 提议了本场景不应使用的动作：['rollout_restart']（模型认为此处不该用）

**为什么在已知类别之外**：表面是 CrashLoopBackOff 且 last_exit_code=137，容易误判为 OOMKilled 或普通 crashloop；实际是 liveness probe 路径错误，容器内存充足，属于探针配置故障。

**朴素实现会怎么答错**：朴素系统看到 restarts 和 last_exit_code=137，会匹配 OOMKilled，建议 patch_resources 提高内存，或 delete_pod / rollout_restart；真正应看 events 中的 Liveness probe failed 和日志，正确动作是 rollout_undo 回滚最近变更。

## [fuzz-006-新Pod一直Pending调度其] 新 Pod 一直 Pending：调度其实成功了，卡在 RWO 卷 Multi-Attach

**判定**：🚨 假阳性提议（最危险）

- 期望类别：`rwo_pvc_multi_attach_stuck_rollout（RWO 卷多挂载导致滚动更新卡死）`
- 实际类别：`pending_unschedulable`
- 规则引擎提议：`['scale_workload']`
- 模型认为合理：`['get_events', 'get_pods', 'get_workload', 'get_nodes', 'rollout_undo']`

> 提议了本场景不应使用的动作：['scale_workload']（模型认为此处不该用）

**为什么在已知类别之外**：类表里最近的类别是 pending_unschedulable。但这里 Pod 已经被成功调度（Normal Scheduled 事件 + nodeName 已赋值），卡点是 ReadWriteOnce PVC 被旧 ReplicaSet 的 Pod 独占：RollingUpdate 默认 maxSurge，先把新 Pod 建到另一个节点，新旧 Pod 同时要求挂同一块 RWO 盘，kubelet 报 Multi-Attach，滚动更新永久卡死。根因在存储访问模式 + 发布策略，不在调度器。

**朴素实现会怎么答错**：实现看到 Pending + FailedAttachVolume/FailedMount 里的“volume/无法挂载”字样就归到 pending_unschedulable，建议 cordon_node/drain_node 甚至 delete_pod 或 scale_workload；实际上 Normal Scheduled 事件已证明调度成功，节点没问题，删掉新 Pod 只会立刻重建出同一个卡死的 Pod，唯一在工具集内的有效动作是 rollout_undo。

## [fuzz-006-新副本一个都建不出来admiss] 新副本一个都建不出来（admission webhook 证书过期），像调度失败其实根本没有 Pod

**判定**：🚨 假阳性提议（最危险）

- 期望类别：`admission_webhook_failure（准入 webhook 后端不可用/证书过期，Pod 创建被拒）`
- 实际类别：`no_pods`
- 规则引擎提议：`['rollout_restart']`
- 模型认为合理：`['get_events', 'get_workload', 'get_pods']`

> 提议 ['rollout_restart']，但模型认为合理的是 ['get_events', 'get_workload', 'get_pods']（需人工判断谁对）

**为什么在已知类别之外**：Deployment 完成滚动更新时旧 ReplicaSet 已缩容，新 ReplicaSet 创建 Pod 全部被拒，业务全量下线。表面症状是『Pod 起不来/无法创建』，非常像 pending_unschedulable。真实原因是校验用的 admission webhook（如 Gatekeeper/自研 validator）后端 Service 的 TLS 证书已过期，API Server 调用 webhook 时报 x509 错误，所有 CREATE 被拒。集群里有 0 个 Pod 对象——连被 drain 或被调度失败的对象都不存在。

**朴素实现会怎么答错**：一看到『Pod 创建失败/无法调度』就归类为 pending_unschedulable：建议 drain_node / cordon_node 腾挪节点，或给出 scale_workload / rollout_undo。可对象压根不存在，drain/cordon 只会连累节点上别的服务；rollback 也照样被 webhook 拒绝。正确做法是只读定位并修 webhook（证书/Servcie/Deployment 后端），在修好前不动集群。

## [fuzz-006-本次滚动更新引入非法配置值新Po] 本次滚动更新引入非法配置值：新 Pod CrashLoop，旧 Pod 仍正常

**判定**：🚨 假阳性提议（最危险）

- 期望类别：`bad_rollout_config_regression`
- 实际类别：`crashloop`
- 规则引擎提议：`['rollout_undo', 'rollout_restart']`
- 模型认为合理：`['rollout_undo', 'get_logs', 'get_events', 'get_workload', 'get_pods']`

> 提议了本场景不应使用的动作：['rollout_restart']（模型认为此处不该用）

**为什么在已知类别之外**：信号是典型的 CrashLoopBackOff + 高重启次数 + exit code 1，看着像 crashloop 甚至 oom_killed，但真正原因是这一次 Deployment 滚动更新把环境变量 APP_MODE 改成了应用不接受的取值（配置内容本身合法，Pod 规格才改错了）。同一 Deployment 的前一版本 ReplicaSet 的 Pod 全部 Ready、零重启，说明不是节点、不是资源、不是镜像，而是版本级配置回归——只能靠回滚修复。

**朴素实现会怎么答错**：朴素实现看到 reason=CrashLoopBackOff、restarts=6，会直接判 crashloop（或因为 exit code 1 误猜 oom_killed/probe_kill），建议 rollout_restart 或 delete_pod；这两者只会让新 ReplicaSet 的 Pod 再崩一轮，故障面反而扩大，正确动作是 rollout_undo 回到上一个正常版本。

## [fuzz-001-容器反复重启且退出码137看似O] 容器反复重启且退出码 137，看似 OOM 实为 liveness 探针误杀

**判定**：⚠️ 漏报（该动手没动手）

- 期望类别：`liveness_probe_kill_masquerading_as_oom`
- 实际类别：`probe_kill`
- 模型认为合理：`['get_events', 'get_logs', 'get_workload', 'rollout_undo']`

> 模型认为应做 ['get_events', 'get_logs', 'get_workload', 'rollout_undo']，规则引擎实际提议 （无）

**为什么在已知类别之外**：Pod 处于 CrashLoopBackOff 且 last_exit_code=137，信号强烈指向 oom_killed；但事件中没有 OOMKilled，只有 Liveness probe failed，说明容器是被 kubelet 因健康检查失败而 SIGKILL，根因是探针配置/启动慢，不属于已知的 oom_killed 类别。

**朴素实现会怎么答错**：只做关键词匹配的系统看到 CrashLoopBackOff + 退出码 137 + memory_limit，会直接判为 oom_killed 并建议 patch_resources 加内存；实际上加内存无法解决 liveness 探针误杀。

## [fuzz-001-离线批处理扩容被Resource] 离线批处理扩容被 ResourceQuota 卡住，旧副本正常所以看不出崩溃

**判定**：⚠️ 漏报（该动手没动手）

- 期望类别：`quota_exceeded_create_failed`
- 实际类别：`healthy`
- 模型认为合理：`['get_workload', 'get_events']`

> 模型认为应做 ['get_workload', 'get_events']，规则引擎实际提议 （无）

**为什么在已知类别之外**：没有任何 Pod 处于 CrashLoopBackOff / ImagePullBackOff / Unschedulable，也没有容器退出。故障发生在 ReplicaSet 的 Pod 创建阶段，被准入层的 ResourceQuota 拒绝（FailedCreate），属于已知八类之外的『配额耗尽导致副本无法创建』。集群里存活 Pod 的规格、探针、镜像全部健康，唯一的证据是 ReplicaSet 的 Warning 事件正文。

**朴素实现会怎么答错**：看到 Deployment 期望 4 副本、实际只有 2 个 Running，朴素实现会匹配成 not_ready 或 pending_unschedulable，去建议 delete_pod / rollout_restart / cordon_node；它不会读 ReplicaSet 对象上的 FailedCreate 事件，也不会对 'exceeded quota' 做关键词匹配，于是既给不出根因，还会做无害但无效的处置。

## [fuzz-004-重启7次退出码137像OOM实为] 重启 7 次、退出码 137：像 OOM，实为存活探针超时被 SIGKILL

**判定**：⚠️ 漏报（该动手没动手）

- 期望类别：`probe_induced_sigkill_misread_as_oom（探针超时导致的重启被误判为 OOMKilled）`
- 实际类别：`probe_kill`
- 模型认为合理：`['get_events', 'get_logs', 'get_pods', 'get_workload', 'rollout_undo']`

> 模型认为应做 ['get_events', 'get_logs', 'get_pods', 'get_workload', 'rollout_undo']，规则引擎实际提议 （无）

**为什么在已知类别之外**：已知类别里最接近的是 oom_killed 和 crashloop。但本场景根因是 v2.14.0 把 /healthz 改成同步深依赖检查 + livenessProbe timeoutSeconds=1，探针必然超时，kubelet 反复 SIGKILL 容器。集群里既没有 OOMKilling 事件，容器峰值内存也远低于 limit，退出码 137 只是 SIGKILL 的通用值，不是 OOM 证据。属于“信号指向 OOM、根因在探针/配置”的类别外故障。

**朴素实现会怎么答错**：状态匹配 + 退出码匹配的实现看到 CrashLoopBackOff 且 last_exit_code=137，直接判 oom_killed 并建议 patch_resources 提内存——但事件里没有 OOMKilling、峰值内存 118Mi/limit 512Mi，137 来自存活探针失败后的 SIGKILL，加内存完全无效。

## [fuzz-005-ServicetargetPor] Service targetPort 写错：Pod 全就绪、Endpoints 非空，但流量全部连接被拒

**判定**：⚠️ 漏报（该动手没动手）

- 期望类别：`service_targetport_mismatch`
- 实际类别：`healthy`
- 模型认为合理：`['get_endpoints', 'get_pods', 'get_workload', 'get_events', 'get_logs']`

> 模型认为应做 ['get_endpoints', 'get_pods', 'get_workload', 'get_events', 'get_logs']，规则引擎实际提议 （无）

**为什么在已知类别之外**：这不是 service_no_endpoints：就绪后端数是 3，Endpoints 对象正常填充，kube-proxy 也会把流量转过去，只是转到了容器没有监听的端口。所有 Pod 层信号（Ready、restarts=0、无 OOM、无探针失败）都是绿色的，故障完全落在 Service.spec.ports[].targetPort 与容器实际监听端口之间的错配上。

**朴素实现会怎么答错**：因为 endpoints=3 不等于 0，任何『endpoints==0 → service_no_endpoints』的规则都不会触发；同时 Pod 全 Ready、无重启、无 Warning 事件，关键词匹配系统会判『未发现故障/一切正常』，或反过来去怀疑 Pod/镜像，而真正写错的是 Service 的 targetPort。

## [fuzz-003-drain卡住且Pod被禁止驱逐] drain 卡住且 Pod 被禁止驱逐，看似节点故障实为 PDB 阻塞

**判定**：· 特征未识别


> 用例结构非法，已跳过：dictionary update sequence element #0 has length 3; 2 is required

## [fuzz-004-PDB拒绝驱逐drain卡死Po] PDB 拒绝驱逐，drain 卡死，Pod 长时间 Terminating

**判定**：· 特征未识别


> 用例结构非法，已跳过：dictionary update sequence element #0 has length 4; 2 is required
