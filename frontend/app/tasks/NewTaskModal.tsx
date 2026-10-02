"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { apiErrorList, apiErrorMessage } from "@/lib/apiError";
import { Button, Input, Label, Modal } from "@/app/components/ui";

/* Name it here, build it there — the same shape agent and tool creation take.
   The prompt, the inputs and the output are all built on the task page, so this
   asks for the one thing the caller can supply and gets out of the way. A task
   starts as an empty draft; publishing is what makes it runnable. */

export function NewTaskModal({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; errors: string[] } | null>(null);

  const trimmed = name.trim();

  async function create() {
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      const task = await api.createTask(trimmed);
      router.push(`/tasks/detail?id=${task.id}`);
    } catch (e) {
      /* The list, not just the headline. A duplicate name is still a 400 here,
         and the headline alone leaves nothing to act on. */
      setError({ message: apiErrorMessage(e, "Could not create the task."), errors: apiErrorList(e) });
      setBusy(false);
    }
  }

  return (
    <Modal
      title="New task"
      sub="Name it here — the prompt, the inputs and the result it produces come next, on the task page."
      onClose={onClose}
      width="max-w-[460px]"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={create} disabled={busy || !trimmed}>
            {busy ? "Creating…" : "Create and open"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-2">
        <Label htmlFor="new-task-name">Name</Label>
        <Input
          id="new-task-name"
          value={name}
          autoFocus
          placeholder="Research a company"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              create();
            }
          }}
        />
        <p className="text-[12.5px] leading-5 text-muted">
          For people, not for the model — name it after the job it does.
        </p>
        {error && (
          <div
            role="alert"
            className="grid gap-1 rounded-lg border border-danger/25 bg-danger/[0.05] px-3 py-2 text-[12.5px] leading-5 text-danger"
          >
            <span>{error.message}</span>
            {error.errors.map((line, i) => (
              <span key={i} className="[overflow-wrap:anywhere]">{line}</span>
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}
