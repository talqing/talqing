/** The chat-bubble mark the WhatsApp pages use — the Nav's glyph, larger. */
export const WhatsAppIcon = ({ className = "h-[18px] w-[18px]" }: { className?: string }) => (
  <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d="M4.5 19.5 5.6 16A8 8 0 1 1 8.4 18.6Z" />
    <path d="M9 9.5c.3 1.9 1.6 3.9 3.8 4.9l1.2-1.1 1.6.8" />
  </svg>
);
