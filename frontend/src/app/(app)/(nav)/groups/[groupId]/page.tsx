"use client";

import { useParams } from "next/navigation";

import { useDirectory } from "@/components/conversations/ConversationDirectory";
import { GroupChat } from "@/components/conversations/GroupChat";
import { Spinner } from "@/components/ui/spinner";

/** 一个群的对话。 */
export default function GroupPage() {
  const { groupId } = useParams<{ groupId: string }>();
  const { characters } = useDirectory();
  if (characters === null) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Spinner className="size-6 text-muted-foreground" />
      </div>
    );
  }
  return <GroupChat groupId={groupId} characters={characters} />;
}
