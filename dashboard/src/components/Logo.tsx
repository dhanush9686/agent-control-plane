export function Logo({ size = 24 }: { size?: number }) {
  return <img src="/logo.png" width={size} height={size} alt="" aria-hidden="true" className="rounded-lg object-cover" style={{ width: size, height: size }} />;
}
