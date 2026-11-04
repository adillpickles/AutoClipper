import { Suspense } from "react";

import { ResultsScreen } from "@/components/results-screen";
import { SiteHeader } from "@/components/site-header";

export default function ResultsPage() {
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main className="flex flex-1 flex-col py-8 sm:py-12">
        <Suspense
          fallback={
            <p className="text-zinc-600 dark:text-zinc-400">Loading…</p>
          }
        >
          <ResultsScreen />
        </Suspense>
      </main>
    </div>
  );
}
