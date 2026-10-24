#!/bin/sh
# test_reading_draft 要 dev 依赖里的 pyopenjtalk-plus（runtime 镜像里没有）：
# 临时装进测试容器，再跑那一组测试。
set -e
python -m ensurepip --upgrade >/dev/null 2>&1 || true
timeout 300 python -m pip install -q --no-cache-dir \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  pytest pytest-asyncio pytest-timeout pyopenjtalk-plus >/dev/null 2>&1 || true
for v in $(env | sed -n 's/^\(EIDOLON_[A-Z_]*\)=.*/\1/p'); do
  unset "$v"
done
export PYTHONDONTWRITEBYTECODE=1
cd "${REPO_BACKEND:-/repo/backend}"
python -m pytest -q -p no:cacheprovider --timeout=60 "$@"
