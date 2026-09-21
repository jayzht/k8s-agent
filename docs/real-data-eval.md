# 真实数据评测报告：ITBench-Lite

> 目的：给诊断能力做一次**非自证**的评测。
> 本项目原有 50 条用例全是自己写的，规则引擎 100% 通过——高度自证。
> ITBench-Lite（IBM Research）提供**我没有参与构造的**真实 K8s 事故数据。
>
> 数据：HuggingFace `ibm-research/ITBench-Lite`，SRE 快照 v0.2，**35 个场景**。
> 每个场景含真实 K8s 对象快照、事件、OTel 日志/链路、以及人工标注的根因真值。
> 接入代码：`src/omagent/itbench.py`，命令：`omagent itbench`。

---

## 一、先说最重要的一件事：能力范围与真实故障分布严重不匹配

把 35 个场景的**真值根因**统计出来：

| 根因实体类型 | 数量 | 本项目能否处理 |
|---|---|---|
| **ConfigMap** | **12** | ❌ 动作白名单里没有"改配置" |
| Chaos 实验对象（Network/Stress/JVM/PodChaos） | 11 | ❌ 那是**故障注入器本身**，不该被当作根因 |
| Pod | 3 | ⚠️ 部分可处理 |
| Deployment | 2 | ⚠️ 部分可处理 |
| Namespace / HPA / Schedule / 其他 | 7 | ❌ 超出边界 |

**35 个场景里，真正落在本项目能力范围内的只有 3 个。**

这不是"数据集不好"，而是一个必须承认的事实：

> **真实事故的根因大量在"配置"层，而本项目的分类体系是围着"Pod 状态"建的。**

我上一轮说"缺数据"是不准确的。准确的说法是：**公开真实数据存在，但它和本项目的能力范围不匹配。**

---

## 二、3 个可评分场景的实测结果

用真值**指定工作负载**，只评"诊断与处置"这一半（不评"从全集群遥测定位根因"——那是本项目尚未具备的能力）。

| 场景 | 真实根因 | 期望分类 → 实际 | 判定 |
|---|---|---|---|
| Scenario-33 | Deployment `ad` 的 `nodeSelector` 指向不存在的节点 | `pending_unschedulable` → `pending_unschedulable` | ✅ **命中** |
| Scenario-24 | Deployment `checkout` 的 `KAFKA_ADDR` 配错 | `not_ready` → `not_ready` | ◐ **部分正确** |
| Scenario-16 | Deployment `shipping` 的 `QUOTE_ADDR=quote:0000`（Service 实际 8080） | `healthy` → `healthy` | · 当时**设计上看不到** |

**首轮结果：可评分 2 个，完全命中 1 个（50%）。** 对比自造用例的 100%——这才是诚实的差距。

### 补上配置层之后的复测

针对暴露的缺口实现了**配置层诊断**（三方交叉比对：日志端口 vs env 配置 vs Service 真实端口，
外加"无日志时"的降级检查），复测结果：

| 场景 | 真值 | 期望 → 实际 | 判定 |
|---|---|---|---|
| Scenario-33 | `nodeSelector` 指向不存在的节点 | `pending_unschedulable` → 同 | ✅ |
| Scenario-16 | `QUOTE_ADDR=quote:0000` | `config_misconfiguration` → 同 | ✅ |
| Scenario-24 | `KAFKA_ADDR=kafka:9999` | `config_misconfiguration` → 同 | ✅ |

**3/3 命中**，且**归因正确**——结论里指出的环境变量名与端口，与真实注入的故障完全一致：

```
而环境变量 **QUOTE_ADDR=quote:0000** 正是这个地址，
同名 Service 实际暴露的端口却是 **[8080]** —— 端口对不上，配置写错了。
```

**动作也从"无效"变成"有效"**：Scenario-24 原先建议 `rollout_restart`（重启修不好配错的地址），
现在建议 `rollout_undo`——env 在 pod template 里，回滚会连配置一起恢复。

---

## 三、三条具体发现

### 发现 1：有一整类真实故障，我**设计上看不到**

Scenario-16 里两个 Pod 全部 Running + Ready + 0 重启，Endpoints 正常——**从 Pod 状态看完全健康**。
真实故障是 Deployment 的 `QUOTE_ADDR` 环境变量指向了错误端口，导致应用层调用失败。

我的 Agent 输出：`deployment/shipping 当前健康：2 个 Pod 全部就绪。`

**这个场景属于 ITBench 里最常见的一类**（ConfigMap / 配置类根因占 12/35）。
意味着：**我只能看到"Pod 坏了"的故障，看不到"Pod 好好的但业务坏了"的故障。**

### 发现 2：分类对了，动作仍可能是错的

Scenario-24 我的分类是对的（`not_ready`），但结论写的是：

> "有 1 个 Pod 处于 Running 但未 Ready，通常是**就绪探针失败**或依赖未就绪。"

**归因错了**——真实根因是环境变量配错。而且我提议 `rollout_restart`：
**重启修不好一个配错的地址**，Pod 重建后照样连不上。

值得注意的是：fixture 里其实**已经带了 `deployment_spec.env`**（转换器提取了），
但规则引擎从不读它。**信息取到了，却没用上。**

### 发现 3：命中那条，动作也偏保守

Scenario-33 分类与根因描述都对（明确提到 `nodeSelector`），但提议的是 `scale_workload`（缩容缓解），
而不是"修正 nodeSelector"。后者不在我的动作白名单里（工作负载 spec 修改中我只开放了 resources/HPA）。

这不算错——缩容确实是合理的缓解——但**离"真正修好"还差一步**，而结论里没有说清这一点。

---

## 四、这条路值不值得继续

**值得，但要换个用法。**

| 做法 | 评价 |
|---|---|
| 为刷 ITBench 分数去扩分类 | ❌ 那 12 个 ConfigMap 场景需要"改配置 + 回滚配置"的能力，是另一个产品 |
| 把 35 个场景当作**能力边界探针**保留 | ✅ 已经接好了（`omagent itbench`），随时可重跑 |
| 从这批数据里提炼**该补的能力** | ✅ 见下 |

**从数据里提炼出的、真正该补的一件事**：

> **「Pod 健康 ≠ 服务可用」这条线我只做了一半。**
> 上一轮补了 Service/Endpoints 相关性检查（selector 失配），
> 但**"配置正确性"这一半没做**——env 指向错误地址、ConfigMap 内容不对，
> 都会让 Pod 全绿而业务全红。

这件事的价值不来自 ITBench 的分数，而来自**它在真实数据里占的比例（12/35）**。

---

## 五、复现

```bash
# 1. 取数据（真值与对象快照）
export https_proxy=<your-proxy>
BASE=https://huggingface.co/datasets/ibm-research/ITBench-Lite/resolve/main/snapshots/sre/v0.2-B96DF826-4BB2-4B62-97AB-6D84254C53D7
mkdir -p var/itbench/{gt,data}
for s in $(...); do
  curl -sSLf "$BASE/$s/ground_truth.yaml" -o "var/itbench/gt/$s.yaml"
  curl -sSLf "$BASE/$s/k8s_objects_raw.tsv" -o "var/itbench/data/${s}_k8s_objects_raw.tsv"
  curl -sSLf "$BASE/$s/k8s_events_raw.tsv"  -o "var/itbench/data/${s}_k8s_events_raw.tsv"
done

# 2. 评测
python -m omagent.cli itbench --scenarios Scenario-33,Scenario-16,Scenario-24
```

---

## 六、诚实清单

- **只评了 3 个场景**，不是 35 个。其余 32 个的根因类型超出本项目能力范围，硬跑等于拿"不该我负责的故障"算我答错
- **期望值是人工核对的**（`REVIEWED_EXPECTATIONS`），不是自动从真值推的——自动推会导致"用我的分类器生成期望、再用它考我的分类器"的循环论证
- **不评"根因定位"**：本项目的用法是"给我一个工作负载我诊断它"，而 ITBench 考的是"从全集群遥测定位是哪个实体"。后者是意图层之上的另一个能力，我还没做
- 场景的对象快照是**时间窗口的最后一份**，可能与故障峰值时刻有偏差
- 3 个样本量太小，**50% 这个数字本身没有统计意义**，有意义的是它暴露的**三类问题**
