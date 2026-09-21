#!/bin/sh
# 沙箱演示应用的入口脚本。
#
# 设计原则（重要）：**日志必须是"真实服务会打的日志"，不能自述故障原因。**
#
# 早期版本的 OOM 模式会打印 "allocating ~256MB to trigger the memory limit"，
# 等于把答案写在题面上——LLM 直接引用这句话就"诊断"出来了，评测因此失真。
# 现在改为：应用只打正常业务日志（含若干**与故障无关的噪声告警**），
# 诊断者必须自己从退出码、内存上限、重启次数等**真实信号**推断。
set -eu

MODE="${MODE:-normal}"
START_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# ── 真实服务都会打的启动日志 ────────────────────────────────
startup() {
  log "INFO  config loaded from /etc/app/config.yaml (env=${APP_ENV:-prod})"
  log "INFO  initializing connection pool (size=10, timeout=5s)"
  log "INFO  connection pool ready"
  log "WARN  deprecated option 'legacy_cache' is set; scheduled for removal"
  log "INFO  http server listening on :8080"
  log "INFO  health endpoint registered at /health"
}

# ── 与故障无关的噪声：真实系统里到处都是，用于测试是否会误判 ──
noise() {
  log "INFO  GET /api/v1/orders 200 12ms user=U4417"
  log "WARN  slow query detected: SELECT * FROM sessions took 1180ms"
  log "ERROR metrics flush failed: dial tcp 10.96.0.14:4317: connect: connection reset by peer"
  log "INFO  GET /health 200 1ms"
  log "WARN  retrying metrics flush (attempt 2/5)"
  log "INFO  POST /api/v1/orders 201 45ms user=U2093"
}

serve_forever() {
  while true; do
    printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok' \
      | nc -l -p 8080
  done
}

case "$MODE" in
  normal)
    startup
    log "INFO  ready to serve traffic"
    serve_forever
    ;;

  delaying)
    # 正常的慢启动：配置中心拉取超时是常见原因，日志如实反映
    startup
    log "INFO  fetching feature flags from config service"
    log "WARN  config service slow (attempt 1), retrying in ${DELAY_SECONDS:-30}s"
    sleep "${DELAY_SECONDS:-30}"
    log "INFO  ready to serve traffic"
    serve_forever
    ;;

  oom)
    MB="${OOM_MB:-256}"
    startup
    log "INFO  warming up local cache"
    noise
    # 真实应用确实会告警内存压力——但这只是众多 WARN 之一，并非"答案"
    log "WARN  heap usage approaching configured ceiling"
    noise
    # 实际的内存增长。**不打印任何解释性文字**：被 OOMKill 这件事应由
    # 退出码 137 与容器内存上限这些外部信号来体现，而不是靠应用自述。
    payload=$(yes '{"id":1,"payload":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}' \
              | head -c $((MB * 1000000)))
    log "INFO  GET /api/v1/orders 200 18ms user=U8830"
    log "INFO  cache warmup complete"
    # 保住 payload 不被优化掉，然后挂住等内核来杀
    printf '%s' "${#payload}" >/dev/null
    sleep 3600
    ;;

  crash)
    # 半路启动失败：打印到一半就退出，是真实的配置/依赖问题形态
    startup
    log "ERROR failed to load routing table from /etc/app/routes.yaml"
    log "FATAL cannot continue without routing table"
    exit 1
    ;;

  *)
    log "FATAL unknown MODE=${MODE}"
    exit 2
    ;;
esac
