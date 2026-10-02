"use client";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

/* Guard against losing an unsaved draft, for both ways out of an editor.

   A tab close or reload is the browser's to confirm (`beforeunload`). An
   in-app link is not — Next intercepts the click and swaps the route with no
   prompt at all — so those clicks are caught in the capture phase and turned
   into a confirmation the editor renders itself.

   Only plain left-clicks on same-origin, same-tab links are intercepted;
   modifier-clicks, downloads, and external targets keep their normal meaning. */
export function useUnsavedChanges(dirty: boolean) {
  const router = useRouter();
  const [pendingPath, setPendingPath] = useState<string | null>(null);

  useEffect(() => {
    if (!dirty) return;

    function onBeforeUnload(event: BeforeUnloadEvent): void {
      event.preventDefault();
      event.returnValue = "";
    }

    function onDocumentClick(event: MouseEvent): void {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }
      const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
      if (!(anchor instanceof HTMLAnchorElement)) return;
      if (anchor.target && anchor.target !== "_self") return;
      if (anchor.hasAttribute("download")) return;

      const nextUrl = new URL(anchor.href, window.location.href);
      if (nextUrl.origin !== window.location.origin) return;
      const nextPath = `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`;
      const currentPath = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (nextPath === currentPath) return;

      event.preventDefault();
      setPendingPath(nextPath);
    }

    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("click", onDocumentClick, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onDocumentClick, true);
    };
  }, [dirty]);

  /** Navigate, asking first if there is anything to lose. For buttons, not links. */
  const requestLeave = useCallback(
    (path: string) => {
      if (dirty) setPendingPath(path);
      else router.push(path);
    },
    [dirty, router],
  );

  const confirmLeave = useCallback(() => {
    if (!pendingPath) return;
    setPendingPath(null);
    router.push(pendingPath);
  }, [pendingPath, router]);

  const cancelLeave = useCallback(() => setPendingPath(null), []);

  return { pendingPath, requestLeave, confirmLeave, cancelLeave };
}
