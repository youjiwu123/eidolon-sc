#!/bin/sh
# 在 runtime 镜像里跑后端测试：装 pytest（镜像里没有），清掉容器带进来的 .env 变量，
# 再跑全套。测试假设环境是干净的（比如 EIDOLON_ADMINS 为空）。
#
# 测试会往上两级找仓库根（`Path(__file__).parents[2]`，读 characters/、backend/eval/、
# deploy/ 之类），所以要么把仓库挂在 `/repo` 并用 `REPO_BACKEND=/repo/backend`，
# 要么就用镜像里的 /app（只跑不依赖仓库数据的那些）。
set -e
python -m ensurepip --upgrade >/dev/null 2>&1 || true
timeout 180 python -m pip install -q --no-cache-dir \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  pytest pytest-asyncio pytest-timeout >/dev/null 2>&1 || true
for v in $(env | sed -n 's/^\(EIDOLON_[A-Z_]*\)=.*/\1/p'); do
  unset "$v"
done
export PYTHONDONTWRITEBYTECODE=1
cd "${REPO_BACKEND:-/app}"
python -m pytest -q -p no:cacheprovider --timeout=45 --no-header "$@"
