"use client";

import { useEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { cn } from "@/lib/cn";

const STORAGE_KEY = "talqing.tools.editor.formWidthPercent";
const DEFAULT_FORM_WIDTH = 70;
const MIN_FORM_WIDTH = 50;
const MAX_FORM_WIDTH = 85;

function clampWidth(value: number): number {
  return Math.min(MAX_FORM_WIDTH, Math.max(MIN_FORM_WIDTH, value));
}

export function ResizableToolEditorLayout({
  form,
  aside,
}: {
  form: ReactNode;
  /** The right-hand pane: the operation tree, or a test run against it. */
  aside: ReactNode;
}): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [formWidth, setFormWidth] = useState(DEFAULT_FORM_WIDTH);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (!saved) return;
    const parsed = Number(saved);
    if (Number.isFinite(parsed)) setFormWidth(clampWidth(parsed));
  }, []);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, String(formWidth));
  }, [formWidth]);

  function widthFromClientX(clientX: number): number {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return formWidth;
    return clampWidth(((clientX - rect.left) / rect.width) * 100);
  }

  function startDrag(event: ReactPointerEvent<HTMLButtonElement>): void {
    event.preventDefault();
    setDragging(true);

    function onPointerMove(moveEvent: PointerEvent): void {
      setFormWidth(widthFromClientX(moveEvent.clientX));
    }

    function onPointerUp(): void {
      setDragging(false);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLButtonElement>): void {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setFormWidth((width) => clampWidth(width - 2));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setFormWidth((width) => clampWidth(width + 2));
    } else if (event.key === "Home") {
      event.preventDefault();
      setFormWidth(MIN_FORM_WIDTH);
    } else if (event.key === "End") {
      event.preventDefault();
      setFormWidth(MAX_FORM_WIDTH);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setFormWidth(DEFAULT_FORM_WIDTH);
    }
  }

  return (
    <div
      ref={containerRef}
      className={cn(
        "grid grid-cols-1 gap-y-4 lg:h-full lg:min-h-0 lg:grid-cols-[minmax(0,var(--form-width))_14px_minmax(0,var(--aside-width))] lg:gap-x-0",
        dragging && "cursor-col-resize",
      )}
      style={{
        "--form-width": `${formWidth}fr`,
        "--aside-width": `${100 - formWidth}fr`,
      } as CSSProperties}
    >
      <div className="min-w-0 scroll-thin lg:min-h-0 lg:overflow-y-auto lg:pr-3">{form}</div>
      <div className="hidden min-h-0 items-stretch justify-center lg:flex">
        <button
          type="button"
          role="separator"
          aria-label="Resize form and flowchart panels"
          aria-orientation="vertical"
          aria-valuemin={MIN_FORM_WIDTH}
          aria-valuemax={MAX_FORM_WIDTH}
          aria-valuenow={Math.round(formWidth)}
          title="Drag to resize panels"
          className={cn(
            "group flex w-3 cursor-col-resize items-center justify-center rounded-full bg-line outline-none transition-colors hover:bg-line-strong focus-visible:ring-2 focus-visible:ring-ink/10",
            dragging && "bg-line-strong",
          )}
          onPointerDown={startDrag}
          onKeyDown={resizeWithKeyboard}
        >
          <span
            className={cn(
              "flex h-14 w-1.5 flex-col items-center justify-center gap-1 rounded-full bg-muted shadow-[0_1px_2px_rgba(12,13,15,0.12)] transition-colors group-hover:bg-ink group-focus-visible:bg-ink",
              dragging && "bg-ink",
            )}
          >
            <span className="h-1 w-1 rounded-full bg-white/80" />
            <span className="h-1 w-1 rounded-full bg-white/80" />
            <span className="h-1 w-1 rounded-full bg-white/80" />
          </span>
        </button>
      </div>
      <div className="min-w-0 scroll-thin lg:min-h-0 lg:overflow-y-auto lg:pl-3">{aside}</div>
    </div>
  );
}
