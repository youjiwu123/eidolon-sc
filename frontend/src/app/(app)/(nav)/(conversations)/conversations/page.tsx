"use client";

import Link from "next/link";

import { copy } from "@/i18n";

/** 对话列表（`/conversations`，已登录的用户打开 `/` 也到这里）。宽屏时右栏还没选角色，居中一句 muted 标题；窄屏时外壳只显示列表，这一页不出现。 */
export default function ConversationsHome() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6">
      <h2 className="text-center text-header text-muted-foreground">{copy.conversationList.noneSelected}</h2>
      <Link
        href="/groups"
        className="rounded-full border px-4 py-1.5 text-meta text-muted-foreground hover:bg-muted"
      >
        {copy.groups.entry}
      </Link>
    </div>
  );
}
