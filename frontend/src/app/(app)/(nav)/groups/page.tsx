"use client";

import { useDirectory } from "@/components/conversations/ConversationDirectory";
import { GroupDirectory } from "@/components/conversations/GroupDirectory";
import { Spinner } from "@/components/ui/spinner";

/** 群聊列表。 */
export default function GroupsPage() {
  const { characters } = useDirectory();
  if (characters === null) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Spinner className="size-6 text-muted-foreground" />
      </div>
    );
  }
  return <GroupDirectory characters={characters} />;
}
