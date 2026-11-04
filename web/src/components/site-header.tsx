import Link from "next/link";

export function SiteHeader() {
  return (
    <header className="flex items-center justify-between gap-4 py-6">
      <Link href="/" className="text-lg font-semibold tracking-tight">
        AutoClipper
      </Link>
      <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-900 dark:bg-amber-400/15 dark:text-amber-200">
        Mock data
      </span>
    </header>
  );
}
