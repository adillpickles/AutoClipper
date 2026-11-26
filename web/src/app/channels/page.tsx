import type { Metadata } from "next";
import { Suspense } from "react";

import { ChannelBrowser } from "@/components/channel-browser";
import { SiteHeader } from "@/components/site-header";
import { Skeleton } from "@/components/ui/skeleton";

export const metadata: Metadata = {
  title: "Channel VODs — AutoClipper",
  description:
    "Browse a Twitch channel's recent past broadcasts, select multiple VODs, and queue them all for analysis.",
};

function ChannelPageFallback() {
  return (
    <div
      aria-busy="true"
      aria-label="Loading channel browser…"
      className="flex flex-col gap-6"
    >
      <Skeleton className="h-9 w-80 max-w-full" />
      <Skeleton className="h-44 w-full rounded-panel" />
    </div>
  );
}

export default function ChannelsPage() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="flex flex-1 flex-col gap-8 py-8 focus:outline-none sm:gap-10 sm:py-12"
      >
        <Suspense fallback={<ChannelPageFallback />}>
          <ChannelBrowser />
        </Suspense>
      </main>
    </div>
  );
}
