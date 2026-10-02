import type { TelephonyProviderSpec } from "@talqing/sdk";

/** The carrier's mark, falling back to a generic handset if the spec has none. */
export function CarrierLogo({
  spec,
  size = 20,
}: {
  spec: TelephonyProviderSpec | undefined;
  size?: number;
}) {
  if (!spec?.logo_url) {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M4 6.5C4 5 5 4 6.5 4H8l1.5 4L8 9.5a11 11 0 0 0 6.5 6.5L16 14.5 20 16v1.5c0 1.5-1 2.5-2.5 2.5A13.5 13.5 0 0 1 4 6.5Z" />
      </svg>
    );
  }
  return (
    <img
      src={spec.logo_url}
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      className="block rounded-[4px] object-contain"
    />
  );
}
