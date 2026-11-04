import { Suspense } from "react";

import { JobProgress } from "@/components/job-progress";
import { SiteHeader } from "@/components/site-header";

export default function JobPage() {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main className="flex flex-1 flex-col py-10 sm:py-16">
        <Suspense
          fallback={
            <p className="text-zinc-600 dark:text-zinc-400">Loading…</p>
          }
        >
          <JobProgress />
        </Suspense>
      </main>
    </div>
  );
}
