# 技能容器镜像。进程启动时构建为 limen-skillbox。
# 容器本身只 sleep；每次技能调用再 docker exec 解释器。
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs default-jre-headless \
 && rm -rf /var/lib/apt/lists/*
CMD ["sleep", "infinity"]
