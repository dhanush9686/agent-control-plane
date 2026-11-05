export function Logo({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="var(--fg)" />
      <path d="M7 20l5-6 4 4 8-10" stroke="var(--bg)" strokeWidth="2.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
