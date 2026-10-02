import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/* className joiner with Tailwind-conflict resolution.

   clsx flattens the usual inputs — strings, arrays, falsy values, and
   {class: boolean} objects — into one class string. twMerge then resolves
   *conflicting* Tailwind utilities so the last class wins by intent rather
   than by accidental CSS source order:

       cn("w-full", "w-[200px]")  ->  "w-[200px]"
       cn("px-3", "px-5")         ->  "px-5"

   This is the standard pairing (clsx + tailwind-merge) used across the
   Tailwind ecosystem. It is what lets a primitive ship a default (e.g. FIELD's
   w-full) that a caller can override at the call site without the two fighting
   to a stalemate. twMerge only collapses classes within the same group, so
   orthogonal utilities (border side vs. border color, bg color vs. bg size…)
   are both kept. */
export type { ClassValue };

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
