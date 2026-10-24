"""群聊「越聊越贵」的图：每轮消耗与累计消耗。

在 backend 容器里跑（那边有 Pillow）：

    docker compose cp scripts/turn-tokens-chart.py backend:/tmp/ && \
    docker compose exec -T backend python /tmp/turn-tokens-chart.py
"""

import datetime
import sqlite3

from PIL import Image, ImageDraw

W, H = 1200, 760
BG = (255, 255, 255)
INK = (40, 40, 40)
GRID = (225, 225, 225)
NEW = (37, 99, 235)
HIT = (147, 197, 253)
OUT = (234, 88, 12)
CUM = (22, 163, 74)

# DeepSeek 官方价（元 / 百万 token）
PRICE = {"miss": 2.0, "hit": 0.5, "out": 8.0}


def ts(iso: str) -> float:
    return datetime.datetime.fromisoformat(iso).replace(tzinfo=datetime.timezone.utc).timestamp()


db = sqlite3.connect("/data/eidolon.db")
msgs = db.execute(
    "select id, created_at from group_messages where role='character' order by id"
).fetchall()
calls = [
    (r[1], r[2], r[3], r[4])  # at, service, input, output  (cached 另取)
    for r in db.execute(
        "select id, at, service, coalesce(usage_input,0), coalesce(usage_output,0),"
        " coalesce(usage_cached,0) from service_calls"
        " where service in ('chat_llm','memory_llm') order by at"
    )
]
cached_of = {r[0]: r[1] for r in db.execute("select id, coalesce(usage_cached,0) from service_calls")}
ids = [r[0] for r in db.execute("select id from service_calls order by at")]
by_time = list(zip(ids, calls))

# 一次调用只算给最近的一条回复
taken = set()
turns = []
for _, at in msgs:
    t = ts(at)
    new_in = hit_in = out = 0
    for cid, (cat, _svc, cin, cout) in by_time:
        if cid in taken or not (t - 90 <= cat <= t + 5):
            continue
        taken.add(cid)
        new_in += cin
        hit_in += cached_of.get(cid, 0)
        out += cout
    turns.append({"new": int(new_in), "hit": int(hit_in), "out": int(out)})

n = len(turns)
cum = []
total = 0.0
for tt in turns:
    cost = (
        (tt["new"] - tt["hit"]) / 1e6 * PRICE["miss"]
        + tt["hit"] / 1e6 * PRICE["hit"]
        + tt["out"] / 1e6 * PRICE["out"]
    )
    total += cost
    cum.append(total)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

LEFT, RIGHT = 90, W - 40
TOP1, BOT1 = 60, 360
TOP2, BOT2 = 420, H - 70


def axis(y0, y1, vmax, label):
    d.line([(LEFT, y1), (RIGHT, y1)], fill=INK)
    d.line([(LEFT, y0), (LEFT, y1)], fill=INK)
    for i in range(5):
        y = y1 - (y1 - y0) * i / 4
        d.line([(LEFT, y), (RIGHT, y)], fill=GRID)
        d.text((LEFT - 60, y - 6), f"{vmax * i / 4:,.0f}", fill=INK)
    d.text((LEFT, y0 - 24), label, fill=INK)


def line(vals, y0, y1, vmax, color, width=2):
    pts = []
    for i, v in enumerate(vals):
        x = LEFT + (RIGHT - LEFT) * i / max(1, n - 1)
        y = y1 - (y1 - y0) * min(v, vmax) / vmax
        pts.append((x, y))
    if len(pts) > 1:
        d.line(pts, fill=color, width=width)


stack_max = max((t["new"] + t["out"]) for t in turns) if turns else 1
axis(TOP1, BOT1, stack_max, "tokens per turn (blue: input, orange: output)")
line([t["new"] for t in turns], TOP1, BOT1, stack_max, NEW)
line([t["out"] for t in turns], TOP1, BOT1, stack_max, OUT)

axis(TOP2, BOT2, cum[-1] if cum else 1, "cumulative cost (CNY)")
line(cum, TOP2, BOT2, cum[-1] if cum else 1, CUM)

d.text((LEFT, 16), f"Group chat: {n} replies, total {total:.2f} CNY", fill=INK)
d.text((LEFT, H - 40), f"turn 1 -> {n}", fill=INK)

img.save("/tmp/turn-tokens.png")
print("保存 /tmp/turn-tokens.png")
print(f"轮次 {n}，总花费 {total:.2f} 元")
print("前 5 轮 tokens:", [t["new"] + t["out"] for t in turns[:5]])
print("后 5 轮 tokens:", [t["new"] + t["out"] for t in turns[-5:]])
