"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { apiErrorMessage } from "@/lib/apiError";
import { Button, Input, Label, Modal } from "@/app/components/ui";

/** Tool names are called like functions in a prompt, so they get the same shape:
 *  lowercase, words joined by underscores, nothing else. */
export function toolNameFrom(...parts: string[]): string {
  return parts
    .join("_")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 60);
}

/* Name it here, build it there. A tool needs operations to do anything, and
   those are built on the tool page — so this asks for the one thing the caller
   can supply and then gets out of the way. Shared by the tools list and the
   agent editor, which both create tools and both then hand over to the editor. */
export function NewToolModal({
  defaultName,
  title,
  sub,
  onClose,
  beforeLeave,
}: {
  defaultName: string;
  title: string;
  sub: string;
  onClose: () => void;
  /** Runs before we navigate away. The agent editor uses it to save its draft,
      so leaving mid-edit cannot lose the changes that prompted the new tool. */
  beforeLeave?: () => Promise<void>;
}) {
  const router = useRouter();
  const [name, setName] = useState(defaultName);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const cleaned = toolNameFrom(name);

  async function create() {
    if (!cleaned || busy) return;
    setBusy(true);
    setError("");
    try {
      const tool = await api.createTool({ name: cleaned });
      await beforeLeave?.();
      router.push(`/tools/detail?id=${tool.id}`);
    } catch (e) {
      setError(apiErrorMessage(e, "Could not create the tool."));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={title}
      sub={sub}
      onClose={onClose}
      width="max-w-[460px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={create} disabled={busy || !cleaned}>
            {busy ? "Creating…" : "Create and open"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor="new-tool-name">Name</Label>
        <Input
          id="new-tool-name"
          value={name}
          autoFocus
          className="font-mono"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              create();
            }
          }}
        />
        <p className="text-[12.5px] leading-5 text-muted">
          {cleaned && cleaned !== name ? (
            <>
              Saved as <code className="rounded bg-subtle px-1 py-0.5 font-mono text-[11.5px]">{cleaned}</code> —
              the agent calls this by name, so it takes the shape of one.
            </>
          ) : (
            "The agent calls the tool by this name, so keep it short and literal."
          )}
        </p>
        {error && (
          <p role="alert" className="rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
