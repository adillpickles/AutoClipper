import { Suspense } from "react";

import { ClipEditor } from "@/components/clip-editor";
import { SiteHeader } from "@/components/site-header";
import { ClipEditorSkeleton } from "@/components/ui/skeleton";

export default function ClipEditorPage() {
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="flex flex-1 flex-col py-6 focus:outline-none sm:py-12"
      >
        <Suspense fallback={<ClipEditorSkeleton heading="h1" />}>
          <ClipEditor />
        </Suspense>
      </main>
    </div>
  );
}
