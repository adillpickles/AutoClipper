import type { Metadata } from "next";

import { SettingsView } from "@/components/settings-view";
import { SiteHeader } from "@/components/site-header";

export const metadata: Metadata = {
  title: "Settings — AutoClipper",
  description:
    "Choose a folder where finished clips and highlights videos are saved.",
};

export default function SettingsPage() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="flex flex-1 flex-col gap-8 py-8 focus:outline-none sm:gap-10 sm:py-12"
      >
        <SettingsView />
      </main>
    </div>
  );
}
