"use client";
import { useEffect, useState } from "react";
import type { MemberResponse } from "@/lib/control";
import { controlApi } from "@/lib/api";
import { cn } from "@/lib/cn";
import { Tooltip } from "./ui";

/* `created_by` is a control-plane user id stored beside data-plane rows — in a
   different database on a different host — so there is no query that can join
   the two. The roster is small and bounded, and every list that wants
   attribution wants the same one, so a page fetches it once from the control
   API and resolves ids against it here. */
export function useOrgMembers(): Map<string, MemberResponse> {
  const [members, setMembers] = useState<Map<string, MemberResponse>>(new Map());
  useEffect(() => {
    controlApi
      .orgMembers()
      .then((page) => setMembers(new Map(page.items.map((m) => [m.id, m]))))
      .catch((error) => {
        // Attribution is a garnish — it must not take out the list it decorates.
        // It should not vanish quietly either, hence the log.
        console.error("Could not load members for created-by attribution", error);
      });
  }, []);
  return members;
}

/** Who created a resource, as an avatar with the name on hover. Renders nothing
 *  for rows created before attribution existed, rather than an empty slot. */
export function Creator({
  userId,
  members,
  className,
}: {
  userId?: string | null;
  members: Map<string, MemberResponse>;
  className?: string;
}) {
  if (!userId) return null;
  const member = members.get(userId);
  // Removal deletes the membership, not the work: the row keeps pointing at
  // someone who is no longer here, and saying so is more use than a blank.
  const label = member ? `Created by ${member.name || member.email}` : "Created by a former member";
  const initial = (member?.name || member?.email || "?").trim().charAt(0).toUpperCase();
  return (
    <Tooltip label={label} className={cn("inline-flex", className)}>
      {member?.picture_url ? (
        // eslint-disable-next-line @next/next/no-img-element -- Google avatar, a remote host next/image would need allow-listing for
        <img src={member.picture_url} alt="" className="h-[18px] w-[18px] rounded-full object-cover" />
      ) : (
        <span className="grid h-[18px] w-[18px] place-items-center rounded-full border border-line-2 bg-subtle text-[9.5px] font-semibold text-ink-soft">
          {initial}
        </span>
      )}
    </Tooltip>
  );
}
