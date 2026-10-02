"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Tooltip } from "./ui";

/* Attaching an image, on both test surfaces — the chrome only.
 *
 * Three ways in, because a person testing an agent uses all three: a button, a
 * drag onto the panel, and Cmd-V. Paste is the one that matters most here — a
 * screenshot goes from Cmd-Shift-4 to the agent without ever touching the
 * filesystem.
 *
 * Everything about the image ITSELF — the size and format rules, the browser
 * downscale, the byte stream and the `talqing.image_result` ack — is the SDK's
 * `useTalqingImages` and `talqingImageDataUrl`, which the two surfaces import
 * directly. This file used to carry a second copy of all of it, because the SDK
 * shipped its own `@livekit/components-react` and a React context does not
 * cross two module instances. It is one copy now.
 */

export const ATTACH_HINT = "Attach an image";
export const NO_VISION_HINT =
  "This agent's model cannot read images. Publish it on a model that can.";

/** Files dragged onto a surface, or pasted into it.
 *
 * Returns the props to spread on the panel and whether a drag is over it, so the
 * surface can say it will take the drop rather than swallowing it silently.
 * Paste is bound to the window: a screenshot is pasted at the page, not at a
 * particular element, and only one of these surfaces is ever mounted. */
export function useImageDropzone({
  enabled,
  onFiles,
}: {
  enabled: boolean;
  onFiles: (files: File[]) => void;
}) {
  const [dragging, setDragging] = useState(false);
  // Drag events fire for every child element, so a plain boolean flickers off
  // the moment the pointer crosses an inner border. Counting enter/leave is
  // what keeps the outline steady across the whole panel.
  const depth = useRef(0);
  const onFilesRef = useRef(onFiles);
  useEffect(() => {
    onFilesRef.current = onFiles;
  }, [onFiles]);

  useEffect(() => {
    if (!enabled) return;
    function onPaste(event: ClipboardEvent) {
      const files = Array.from(event.clipboardData?.files ?? []).filter((file) =>
        file.type.startsWith("image/"),
      );
      if (files.length === 0) return;
      event.preventDefault();
      onFilesRef.current(files);
    }
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [enabled]);

  useEffect(() => {
    if (enabled) return;
    depth.current = 0;
    setDragging(false);
  }, [enabled]);

  const dropProps = {
    onDragEnter: (event: React.DragEvent) => {
      if (!enabled || !event.dataTransfer.types.includes("Files")) return;
      event.preventDefault();
      depth.current += 1;
      setDragging(true);
    },
    onDragOver: (event: React.DragEvent) => {
      if (!enabled || !event.dataTransfer.types.includes("Files")) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    },
    onDragLeave: () => {
      if (!enabled) return;
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    },
    onDrop: (event: React.DragEvent) => {
      if (!enabled) return;
      event.preventDefault();
      depth.current = 0;
      setDragging(false);
      const files = Array.from(event.dataTransfer.files).filter((file) =>
        file.type.startsWith("image/"),
      );
      if (files.length) onFilesRef.current(files);
    },
  };

  return { dragging, dropProps };
}

/** The paperclip, with the file picker behind it.
 *
 * `disabledReason` is why it cannot be used right now — a model that cannot see,
 * a call that has not connected. Rendered on hover rather than by hiding the
 * control, so the answer is where the question is. */
export function AttachButton({
  onFiles,
  disabledReason,
  label = ATTACH_HINT,
}: {
  onFiles: (files: File[]) => void;
  disabledReason?: string | null;
  label?: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  const pick = useCallback(() => input.current?.click(), []);
  return (
    <Tooltip label={disabledReason || label}>
      <span className="inline-flex">
        <Button
          type="button"
          variant="secondary"
          size="sm"
          aria-label={label}
          disabled={Boolean(disabledReason)}
          onClick={pick}
        >
          <PaperclipIcon />
          <input
            ref={input}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            className="hidden"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? []);
              // Reset first: picking the same file twice in a row fires no
              // change event otherwise, which reads as the button not working.
              event.target.value = "";
              if (files.length) onFiles(files);
            }}
          />
        </Button>
      </span>
    </Tooltip>
  );
}

export function PaperclipIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M21 11.5 12.4 20a5 5 0 0 1-7.1-7.1l8.5-8.5a3.4 3.4 0 1 1 4.8 4.8l-8.5 8.5a1.7 1.7 0 1 1-2.4-2.4l7.9-7.8" />
    </svg>
  );
}
