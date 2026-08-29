# BI 助手体验页的镜像。
#
# 为什么要打进 K8s
# ----------------
# 现有的 Teleport 应用 uri 全是 `*.svc.cluster.local`（98 个应用无一例外），
# 说明 app service 跑在集群里，出不到 dev-frontend-Hermes(10.10.2.57) 这台
# 集群外的 VM。搬进集群是顺着他们既有模式走，不用为我们破例开网络。
#
# 顺带解决另一件事：现在服务是 nohup 起的裸进程，机器一重启就没了。
#
# 依赖为什么这么少
# ----------------
# 实测这条 import 链（gateway.session_context + 两个插件 + PyJWT）只需要
# 4 个第三方包共约 20MB：cryptography / jiter / jwt / yaml。
# **不装 Hermes 的全套依赖**（venv 有 331MB，光 lark_oapi 就 100MB）——
# 那些是 agent 跑模型要的，体验页不跑模型。
#
# 这个判断是量出来的，不是猜的；量法见 DEPLOY.md §十二。上游 import 链变了
# 就会缺包启动失败 —— 那是对的失败方向：起不来，而不是悄悄少了什么。
FROM python:3.12-slim

# mysql 客户端：backend_starrocks 用它连库（StarRocks 走 MySQL 协议）。
# 换成 pymysql 可以省掉这一层，但那是另一个改动，别混在搬家里做。
RUN apt-get update \
 && apt-get install -y --no-install-recommends default-mysql-client \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
      PyJWT==2.* cryptography PyYAML

WORKDIR /app
# 只拷 import 链真正需要的部分。tests/website/apps 加起来 107MB，
# 和体验页无关。
COPY gateway/     /app/gateway/
COPY hermes_cli/  /app/hermes_cli/
COPY agent/       /app/agent/
COPY tools/       /app/tools/
COPY plugins/bi-gate/   /app/plugins/bi-gate/
COPY plugins/bi-query/  /app/plugins/bi-query/
COPY model_tools.py     /app/model_tools.py

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# profile 由 ConfigMap + Secret 挂进来，不进镜像 —— 里面有库密码。
ENV HERMES_HOME=/profile

EXPOSE 8800

# 默认 Teleport 模式：**没有默认身份**，配错了起不来而不是悄悄放开。
CMD ["python", "/app/plugins/bi-gate/webui.py", \
     "--profile", "/profile", "--teleport", "--host", "0.0.0.0", "--port", "8800"]
