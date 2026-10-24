"use client";

import { useEffect, useState } from "react";

import { copy } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * 手机上看字太小或太大时的两档调节：整体缩放与字号。
 *
 * 整站的字号和间距都是 rem，所以改 `html` 的 font-size 就能一起缩放；字号另存一档，
 * 只影响文字（用 `--text-scale` 乘在 body 上，界面比例不变）。两档都存在本机。
 */

const SCALES = [0.9, 1.0, 1.1, 1.2] as const;
const FONTS = [0.9, 1.0, 1.1, 1.25] as const;

const SCALE_KEY = "ui-scale";
const FONT_KEY = "ui-font";

function read(key: string, fallback: number): number {
  const raw = window.localStorage.getItem(key);
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export function applyDisplay(scale: number, font: number) {
  // 界面比例：整站按比例放大缩小（图片、按钮一起）。
  document.documentElement.style.setProperty("zoom", String(scale));
  // 字号：只动根字号，文字变大变小，图片不跟着变。
  document.documentElement.style.fontSize = `${16 * font}px`;
}

/** 启动时应用一次；挂在应用外壳上，换页也不丢。 */
export function useDisplay() {
  useEffect(() => {
    applyDisplay(read(SCALE_KEY, 1), read(FONT_KEY, 1));
  }, []);
}

export function DisplaySettings() {
  const [scale, setScale] = useState(1);
  const [font, setFont] = useState(1);

  useEffect(() => {
    setScale(read(SCALE_KEY, 1));
    setFont(read(FONT_KEY, 1));
  }, []);

  const change = (nextScale: number, nextFont: number) => {
    setScale(nextScale);
    setFont(nextFont);
    window.localStorage.setItem(SCALE_KEY, String(nextScale));
    window.localStorage.setItem(FONT_KEY, String(nextFont));
    applyDisplay(nextScale, nextFont);
  };

  const row = (label: string, hint: string, values: readonly number[], current: number, onPick: (v: number) => void) => (
    <div className="space-y-2">
      <p className="text-base">{label}</p>
      <p className="text-meta text-muted-foreground">{hint}</p>
      <div className="flex gap-2">
        {values.map((value) => (
          <button
            key={value}
            type="button"
            onClick={() => onPick(value)}
            className={cn(
              "min-w-14 rounded-lg border px-3 py-1.5 text-base",
              current === value ? "border-foreground bg-muted font-medium" : "hover:bg-muted/60",
            )}
          >
            {Math.round(value * 100)}%
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <div className="space-y-6">
      {row(copy.display.scale, copy.display.scaleHint, SCALES, scale, (v) => change(v, font))}
      {row(copy.display.font, copy.display.fontHint, FONTS, font, (v) => change(scale, v))}
    </div>
  );
}
