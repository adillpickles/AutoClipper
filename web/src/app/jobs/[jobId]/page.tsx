import { Suspense } from "react";

import { JobProgress } from "@/components/job-progress";
import { SiteHeader } from "@/components/site-header";
import { JobProgressSkeleton } from "@/components/ui/skeleton";

export default function JobPage() {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="flex flex-1 flex-col py-8 focus:outline-none sm:py-16"
      >
        <Suspense fallback={<JobProgressSkeleton heading="h1" />}>
          <JobProgress />
        </Suspense>
      </main>
    </div>
  );
}
