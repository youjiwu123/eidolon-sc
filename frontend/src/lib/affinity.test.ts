import { describe, expect, it } from "vitest";

import { badgeOf, cueText, heartOf, TIERS, untilText } from "./affinity";

// 时刻都按东京时区写，测试不随跑测试的机器变。
const zone = "Asia/Tokyo";
const now = new Date("2026-10-13T20:00:00+09:00");

describe("heartOf：六档各一种心形", () => {
  it("冷淡 HeartOff、疏远 HeartCrack、信任空心、亲近半心、亲密实心、爱意双心", () => {
    expect(TIERS.map(heartOf)).toEqual(["off", "crack", "outline", "half", "filled", "double"]);
  });
});

describe("badgeOf：页首标识显示什么、读屏读什么", () => {
  it("不在冷却：她的颜色的心形加档名，读作「你们的关系：{档名}」", () => {
    expect(badgeOf({ tier: "close", cooldown_until: null }, "凛世", now, zone)).toEqual({
      kind: "tier",
      tier: "close",
      heart: "half",
      text: "亲近",
      label: "你们的关系：亲近",
    });
  });

  it("冷却中：沙漏加解除时刻，读作「{角色名}现在不想说话」", () => {
    expect(badgeOf({ tier: "cold", cooldown_until: "2026-10-14T06:30:00+09:00" }, "凛世", now, zone)).toEqual({
      kind: "cooldown",
      text: "明天 06:30",
      label: "凛世现在不想说话",
    });
  });
});

describe("untilText：解除时刻按制作人的时区写成今天或明天加时刻", () => {
  it("按给定的时区判断，不按 UTC 的日期", () => {
    // 东京已经是 14 日，UTC 还是 13 日。
    const tokyoMorning = new Date("2026-10-13T16:00:00Z");
    expect(untilText(new Date("2026-10-13T20:00:00Z"), tokyoMorning, zone)).toBe("今天 05:00");
    expect(untilText(new Date("2026-10-13T20:00:00Z"), now, zone)).toBe("明天 05:00");
  });
});

describe("cueText：胶囊上的那句话，进了或解除了冷却最先说，其次跨档", () => {
  it("进冷却、解除冷却、跨档升降、同档升降各一句", () => {
    const cue = (up: boolean, tier_changed: boolean, cooldown = false) =>
      cueText({ up, tier_changed, tier: up ? "close" : "cold", cooldown, thawed: false }, "凛世");

    expect(cue(false, true, true)).toBe("凛世现在不想说话了");
    expect(cueText({ up: true, tier_changed: false, tier: "cold", cooldown: false, thawed: true }, "凛世")).toBe(
      "凛世愿意和你说话了",
    );
    expect(cue(true, true)).toBe("和凛世更近了一步：亲近");
    expect(cue(false, true)).toBe("凛世和你疏远了一些：冷淡");
    expect(cue(true, false)).toBe("凛世有点开心");
    expect(cue(false, false)).toBe("凛世有点不高兴");
  });
});
