import { JOB_STAGES } from "@/lib/api/stages";

export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div
      aria-hidden="true"
      className={`rounded-control bg-surface-subtle motion-safe:animate-pulse ${className}`}
    />
  );
}

export function JobProgressSkeleton({
  heading = "h1",
  label = "Loading analysis status…",
}: {
  heading?: "h1" | "none";
  label?: string;
}) {
  return (
    <div className="flex flex-col">
      <div role="status" aria-live="polite" className="sr-only">
        {heading === "h1" ? <h1>{label}</h1> : <span>{label}</span>}
      </div>
      <div
        aria-busy="true"
        aria-label={label}
        className="flex flex-col gap-6 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-8"
      >
        <div className="flex flex-col gap-3">
          <Skeleton className="h-6 w-28 rounded-pill" />
          <Skeleton className="h-8 w-3/4 max-w-md" />
          <Skeleton className="h-4 w-56 max-w-full" />
        </div>

        <div className="flex flex-col gap-2 rounded-card border border-border-subtle bg-surface-elevated p-4">
          <Skeleton className="h-2 w-full rounded-pill" />
          <Skeleton className="h-3.5 w-28" />
        </div>

        <div className="flex flex-col gap-3 border-t border-border-subtle pt-5">
          {JOB_STAGES.map((stage, index) => (
            <div key={stage.stage} className="flex items-center gap-3">
              <Skeleton className="size-6 shrink-0 rounded-pill" />
              <Skeleton
                className={`h-4 ${
                  index % 3 === 0 ? "w-44" : index % 3 === 1 ? "w-56" : "w-36"
                } max-w-[75%]`}
              />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function ClipCardSkeleton() {
  return (
    <li
      aria-hidden="true"
      className="flex flex-col overflow-hidden rounded-card border border-border-subtle bg-surface-panel shadow-card"
    >
      <div className="relative aspect-[9/16] w-full bg-surface-media">
        <div className="absolute top-3 left-3 size-9 rounded-pill bg-white/10 motion-safe:animate-pulse" />
        <div className="absolute inset-x-6 bottom-6 flex flex-col gap-2">
          <div className="h-3 w-2/3 rounded-pill bg-white/10 motion-safe:animate-pulse" />
          <div className="h-3 w-1/2 rounded-pill bg-white/10 motion-safe:animate-pulse" />
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-center justify-between gap-2">
          <Skeleton className="h-5 w-20 rounded-pill" />
          <Skeleton className="h-4 w-24" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-4/5" />
        </div>
        <div className="flex items-center gap-2.5">
          <Skeleton className="h-3 w-10" />
          <Skeleton className="h-1.5 flex-1 rounded-pill" />
        </div>
        <div className="mt-auto flex items-center gap-2 pt-1.5">
          <Skeleton className="h-10 w-24" />
          <Skeleton className="h-10 w-16" />
        </div>
      </div>
    </li>
  );
}

export function ClipListSkeleton({
  count = 4,
  heading = "h1",
  label = "Loading clips…",
}: {
  count?: number;
  heading?: "h1" | "none";
  label?: string;
}) {
  return (
    <div className="flex flex-col">
      <div role="status" aria-live="polite" className="sr-only">
        {heading === "h1" ? <h1>{label}</h1> : <span>{label}</span>}
      </div>
      <div aria-busy="true" aria-label={label} className="flex flex-col gap-8">
        <div className="flex flex-col gap-3">
          <Skeleton className="h-4 w-56 max-w-full" />
          <Skeleton className="h-9 w-2/3 max-w-lg" />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-col gap-2">
            <Skeleton className="h-6 w-36" />
            <Skeleton className="h-4 w-52 max-w-full" />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Skeleton className="h-10 w-36" />
            <Skeleton className="h-10 w-40" />
            <Skeleton className="h-10 w-32" />
          </div>
        </div>

        <ol
          role="list"
          className="grid grid-cols-1 gap-5 min-[480px]:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
        >
          {Array.from({ length: count }, (_, index) => (
            <ClipCardSkeleton key={index} />
          ))}
        </ol>
      </div>
    </div>
  );
}

export function ClipEditorSkeleton({
  heading = "h1",
  label = "Loading the clip editor…",
}: {
  heading?: "h1" | "none";
  label?: string;
}) {
  return (
    <div className="flex flex-col">
      <div role="status" aria-live="polite" className="sr-only">
        {heading === "h1" ? <h1>{label}</h1> : <span>{label}</span>}
      </div>
      <div aria-busy="true" aria-label={label} className="flex flex-col gap-8">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-4 w-28" />
          <Skeleton className="h-4 w-60 max-w-full" />
          <Skeleton className="h-8 w-2/3 max-w-lg" />
        </div>

        <div className="grid grid-cols-1 gap-8 lg:grid-cols-12">
          <div className="flex flex-col gap-4 lg:col-span-7">
            <div
              aria-hidden="true"
              className="aspect-video w-full rounded-panel border border-border-subtle bg-surface-media motion-safe:animate-pulse"
            />
            <div className="flex flex-wrap gap-2">
              <Skeleton className="h-10 w-32" />
              <Skeleton className="h-10 w-40" />
              <Skeleton className="h-10 w-36" />
            </div>
          </div>
          <div className="flex flex-col gap-5 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel sm:p-6 lg:col-span-5">
            <div className="flex items-center justify-between gap-2">
              <Skeleton className="h-6 w-32" />
              <Skeleton className="h-9 w-16" />
            </div>
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-11 w-full" />
          </div>
        </div>
      </div>
    </div>
  );
}
