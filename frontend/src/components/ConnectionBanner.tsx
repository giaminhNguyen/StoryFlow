export function ConnectionBanner({ disconnected }: { disconnected: boolean }) {
  if (!disconnected) return null;
  return (
    <div role="alert" className="banner banner-warn">
      Backend unreachable - retrying. Showing the last known data.
    </div>
  );
}
