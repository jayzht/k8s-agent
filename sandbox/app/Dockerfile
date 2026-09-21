# 沙箱演示应用镜像。
#
# 刻意基于 alpine（约 8MB）而非 node/python：镜像小、加载进 kind 快，
# 且 busybox 自带 httpd，可以支撑真实的就绪/存活探针演示。
FROM alpine:3.20

RUN mkdir -p /www && echo ok > /www/health

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
