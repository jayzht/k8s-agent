# O&M Agent 容器镜像。
#
# 与同类 CLI 工具（k8sgpt、kubectl-ai）的差别要说清楚：那些是挂在**你已有**集群上的
# 无状态命令行工具，装个二进制就能用。这个项目是**有状态的多用户 Web 服务**
# ——它有用户库、会话、审计链、案例库，所以镜像里必须有一个可写的持久化目录。
#
# 用法见 docker-compose.yml 或 README 的「容器里跑」一节。
FROM python:3.12-slim

# 不要以 root 跑：这个进程手里握着"批准集群变更"的能力，
# 它被攻破的代价和它自己的权限直接挂钩。
RUN useradd --create-home --uid 10001 omagent

WORKDIR /app

# 先只拷依赖描述，让依赖层能被缓存住——改代码不必重装依赖
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir . && rm -rf /root/.cache/pip

# 前端是原生静态文件，没有构建步骤，直接拷
COPY web/ ./web/
# 沙箱拓扑生成器与故障脚本：容器里也能造演示环境
COPY sandbox/ ./sandbox/
COPY scripts/ ./scripts/

# 运行期写入的东西（会话 / 审计 / 用户库 / 案例库）全在 $OMAGENT_HOME/var 下。
# 见 omagent/config.py 里关于 ROOT 的说明——不设这个变量的话，
# pip 安装后 ROOT 会落到 site-packages，非 root 根本写不进去。
# KUBECONFIG 给一个默认值，指向文档里让你挂载的那个路径。
# 这样 README 里那条 docker run 就是**真的能跑**的——之前它挂了 kubeconfig
# 却没设这个变量，于是去找 in-cluster 凭据并报 "Service host/port is not set"。
# 集群内部署（走 ServiceAccount）不受影响：K8sClient 发现该路径不存在时
# 会自动回退到 in-cluster 凭据。
ENV OMAGENT_HOME=/data \
    KUBECONFIG=/kubeconfig/config \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown -R omagent:omagent /data /app

USER omagent

EXPOSE 8765

# 健康检查直接用现成的 /api/health —— 没有额外依赖
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["omagent"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8765"]
