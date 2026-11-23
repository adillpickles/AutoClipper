import { Suspense } from "react";

import { ClipEditor } from "@/components/clip-editor";
import { SiteHeader } from "@/components/site-header";

export default function ClipEditorPage() {
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main className="flex flex-1 flex-col py-8 sm:py-12">
        <Suspense
          fallback={<p className="text-sm text-ink-secondary">Loading…</p>}
        >
          <ClipEditor />
        </Suspense>
      </main>
    </div>
  );
}
