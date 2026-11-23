import { PreflightBanner } from "@/components/preflight-banner";
import { SiteHeader } from "@/components/site-header";
import { StartForm } from "@/components/start-form";

export default function Home() {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main className="flex flex-1 flex-col gap-10 py-10 sm:py-16">
        <div className="flex flex-col gap-4">
          <h1 className="font-display text-3xl font-bold tracking-tight text-balance text-ink-primary sm:text-5xl">
            Find the best moments of a Twitch VOD.
          </h1>
          <p className="text-lg/8 text-pretty text-ink-secondary">
            Paste a link to a past broadcast. AutoClipper reads the chat, the
            audio and the clips viewers made, then hands you the top moments as
            ready-to-post vertical clips.
          </p>
        </div>
        <PreflightBanner />
        <StartForm />
      </main>
    </div>
  );
}
