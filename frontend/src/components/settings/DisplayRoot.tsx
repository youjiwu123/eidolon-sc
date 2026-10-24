"use client";

import { useDisplay } from "./DisplaySettings";

/** 把本机存的显示档位在打开页面时应用一次。 */
export function DisplayRoot() {
  useDisplay();
  return null;
}
