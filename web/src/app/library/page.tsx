import type { Metadata } from "next";

import { LibraryView } from "@/components/library-view";
import { SiteHeader } from "@/components/site-header";

export const metadata: Metadata = {
  title: "Library — AutoClipper",
  description:
    "Browse past and running Twitch VOD analyses, reopen results, and manage saved jobs.",
};

export default function LibraryPage() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="flex flex-1 flex-col gap-8 py-8 focus:outline-none sm:gap-10 sm:py-12"
      >
        <LibraryView />
      </main>
    </div>
  );
}
