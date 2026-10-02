type IntegrationLogoProps = {
  kind: string;
  logoUrl?: string | null;
  size?: number;
};

function CustomMcpLogo({ size }: { size: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M8 6h8" />
      <path d="M8 12h8" />
      <path d="M8 18h8" />
      <rect x="3" y="3" width="4" height="6" rx="1.5" />
      <rect x="17" y="9" width="4" height="6" rx="1.5" />
      <rect x="3" y="15" width="4" height="6" rx="1.5" />
    </svg>
  );
}

export function IntegrationLogo({ kind, logoUrl, size = 18 }: IntegrationLogoProps) {
  if (logoUrl) {
    return (
      <img
        src={logoUrl}
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
        className="block rounded-[4px] object-contain"
      />
    );
  }
  return <CustomMcpLogo size={size} />;
}
