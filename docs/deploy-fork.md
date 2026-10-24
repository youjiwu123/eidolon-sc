# 从零部署这份定制版

这份文档写给「拿到这个 fork 想自己跑起来」的人。上游的完整说明在 `README.md`；这里只讲最短路径和已知的坑。
下面的命令在 Windows + PowerShell 下验证过，Linux/macOS 把路径写法换掉即可。

## 0. 需要什么

| 需要 | 说明 |
|---|---|
| Docker Desktop | 跑四个容器（后端、前端、素材、HTTPS） |
| 一个对话模型 | 默认用 DeepSeek；任何 OpenAI 兼容接口都行 |
| 一个语音合成 | Fish Audio（她会说话） |
| 一个语音识别 | 阿里云百炼（你说话她才听得懂） |
| 一个判断模型 | Jev（OpenRouter）或百炼，用来判好感、选记忆、群聊里挑谁说话 |
| 一个向量化 + 重排 | 硅基流动（记忆和剧情检索要靠它） |
| 生图（可选） | 不配就不发照片，其他功能不受影响 |

**最小可跑**：对话模型 + 判断模型。语音、生图、检索都可以后面再加。

## 1. 拿到代码

```powershell
git clone https://github.com/youjiwu123/eidolon-sc-group-chat.git
cd eidolon-sc
```

> 这个仓库**不含** `corpus/`（剧情库与立绘，2.8 GB，版权原因）和 `.env`（密钥）。没有它们也能跑，见第 5 节。

## 2. 写 `.env`

```powershell
Copy-Item .env.example .env
```

最少要填这几项：

```ini
# 她自己说话
CHAT_LLM_BASE_URL=https://api.deepseek.com/v1
CHAT_LLM_API_KEY=sk-你的
CHAT_LLM_MODEL=deepseek-chat

# 判断模型（好感、记忆、群聊选人）
JUDGE_PROVIDER=jev
JEV_BASE_URL=https://openrouter.ai/api/v1
JEV_API_KEY=sk-or-v1-你的
JEV_MODEL=typesafe/jev-1.13

# 向量化 + 重排（记忆与剧情检索）
SILICONFLOW_API_KEY=sk-你的

# 本地开发用开发登录（生产环境改成真 OAuth 并删掉这一行）
EIDOLON_DEV_LOGIN=1

# 后台管理员：身份写成 provider:subject，和你的登录方式对得上
EIDOLON_ADMINS=github:local-admin
```

语音（想让她开口）：`TTS_PROVIDER=fish`、`FISH_AUDIO_API_KEY`。
语音识别（想用麦克风）：`STT_PROVIDER=bailian`、`DASHSCOPE_API_KEY`。
生图（想让她发照片）：`NOVELAI_API_KEY`，或把 `NOVELAI_API_FORMAT` 改成 `openai` 接别家。

## 3. 起服务

```powershell
docker compose up -d --build
```

第一次会构建前后端镜像，几分钟。

| 服务 | 地址 |
|---|---|
| 网页 | http://localhost:8080 |
| 素材 | http://localhost:8765 |
| 后端 | 容器内 8000，不对外 |

**端口说明**：Windows 上 3000 有时落在系统保留区间，所以宿主机映射到 **8080**。想换就改 `docker-compose.yml` 里的 `"8080:3000"`。

## 4. 第一次进去

1. 打开 http://localhost:8080
2. 用开发登录，随便填个名字（这就是你的账号）
3. 进「设置 → 登录与数据 → 后台」或直接开 `/admin`
4. 在后台把 **对话模型、语音合成、语音识别、判断模型、生图** 填好并保存——后台的配置**优先于 `.env`，而且改完立刻生效，不用重启**

> 后台进不去（404）？说明你的账号不是管理员：`EIDOLON_ADMINS` 里写的身份必须和这个用户的一条登录方式一致。
> 开发登录创建的账号默认没有登录方式，需要给它在 `sign_in_methods` 里补一条（例如 `github / local-admin`）。

## 5. 没有 `corpus/` 会怎样

| 功能 | 没有 corpus |
|---|---|
| 和她聊天 | ✅ 正常（人设卡在 `characters/`，仓库里有） |
| 好感度、群聊、动作、搭话 | ✅ 正常 |
| 语音、照片 | ✅ 正常（只要配了对应的 key） |
| 回忆起原作剧情 | ❌ 检索不到，她会说「记不太清了」 |
| 通话立绘 | ❌ 没有 stands 素材，通话层只有背景色 |

要补的话，按 `docs/` 里的剧情导入说明，把你的剧情数据放进 `corpus/`，再跑一次导入（`eidolon story import`）。

## 6. HTTPS（手机要用麦克风就必须有）

浏览器只在 HTTPS 下允许麦克风，所以手机访问必须走 HTTPS。两条路：

**A. Tailscale（推荐，证书自动受信）**

```powershell
tailscale cert 你的机器名.你的tailnet.ts.net
# 把生成的两个文件放进 certs/，并在 deploy/https-nginx.conf 里改成你的域名
docker compose --profile https up -d
```

**B. 局域网自签证书**

```powershell
python scripts/make-lan-cert.py      # 生成 certs/lan.crt 和 lan.key
docker compose --profile https up -d
```

然后在手机上访问 `http://<你的IP>:8765/ca.crt` 装根证书，并在系统里手动打开「信任」。

## 7. 已知的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面打不开，容器却显示 Up | Docker 端口转发卡住 | 重启那个容器：`docker compose restart frontend` |
| 443 返回 502 | 重建前端后 nginx 还记着旧 IP | `docker compose restart https` |
| 改了后端代码不生效 | 镜像没重建 | `docker compose build backend && docker compose up -d backend` |
| 后台 404 | 账号不是管理员 | 见第 4 节末尾 |
| 手机登录被拒（403） | 来源校验 | 开发部署（`EIDOLON_DEV_LOGIN=1` 且没配域名）会放行；生产环境要配 `EIDOLON_DOMAIN` |
| 群聊很慢 | 对话模型思考强度拉满 | 后台把「思考强度」降一档 |
| 打电话她不出声 | TTS 没配或音色 ID 空 | 后台「语音合成」填 key，角色 `voice_id` 不能为空 |

## 8. 群聊、好感这些定制功能在哪

改动都集中在这几个文件，合并上游时注意冲突：

```
backend/src/eidolon/group_chat.py      群聊（新文件，整块）
backend/src/eidolon/affinity.py        六档、人设
backend/src/eidolon/affinity_judge.py  档位放宽的判据
backend/src/eidolon/engine.py          档位与动作透传
backend/src/eidolon/app.py             /api/groups 路由
backend/src/eidolon/reply_format.py    动作说明
frontend/src/components/conversations/Group*.tsx   群聊界面
```

设计背景见 `.scratch/group-chat/spec.md`，变更记录见 `README.local.md`。
