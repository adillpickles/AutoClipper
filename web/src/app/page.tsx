import { PreflightBanner } from "@/components/preflight-banner";
import { SiteHeader } from "@/components/site-header";
import { StartForm } from "@/components/start-form";
import { SignalStrip } from "@/components/ui/signal-strip";

const STEPS = [
  {
    title: "Add the VOD",
    body: "Paste a Twitch link or ID. Already downloaded it? Attach the file too.",
  },
  {
    title: "Analyze",
    body: "Chat activity and loudness score every few seconds of the stream; viewer-made clips join the shortlist.",
  },
  {
    title: "Review",
    body: "The top 10 come back ranked, each with a one-line reason and its place on the timeline.",
  },
  {
    title: "Export",
    body: "Download captioned 9:16 clips, or join the moments into one 16:9 highlights video.",
  },
] as const;

export default function Home() {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="grid flex-1 grid-cols-1 content-start gap-x-12 gap-y-8 py-8 focus:outline-none sm:py-14 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]"
      >
        <section
          aria-labelledby="home-heading"
          className="flex flex-col gap-4 lg:col-span-2"
        >
          <h1
            id="home-heading"
            className="max-w-[15ch] font-display text-display font-semibold text-balance text-ink-primary"
          >
            Find the moments worth posting.
          </h1>
          <p className="max-w-[36rem] text-base/7 text-pretty text-ink-secondary sm:text-[1.0625rem]/8">
            AutoClipper reads a Twitch VOD&apos;s chat, its audio and the clips
            viewers already made, then hands back the top moments as captioned
            vertical clips and a highlights video.
          </p>
        </section>

        <div className="flex min-w-0 flex-col gap-4 lg:col-start-2 lg:row-span-2 lg:row-start-2">
          <PreflightBanner />
          <StartForm />
        </div>

        <figure className="flex flex-col gap-2.5 rounded-card border border-border-subtle bg-surface-panel/70 p-4 shadow-card lg:col-start-1 lg:row-start-2 lg:self-start">
          <SignalStrip className="h-20 sm:h-24" />
          <figcaption className="flex items-center justify-between gap-3 border-t border-border-subtle pt-2.5 text-xs text-ink-tertiary">
            <span className="min-w-0 truncate">
              Chat spikes become ranked moments
            </span>
            <span className="shrink-0 font-mono whitespace-nowrap tabular-nums">
              0:00–4:12:07
            </span>
          </figcaption>
        </figure>

        <section
          aria-labelledby="steps-heading"
          className="flex flex-col gap-4 lg:col-start-1 lg:row-start-3"
        >
          <h2
            id="steps-heading"
            className="font-display text-sm font-semibold text-ink-secondary"
          >
            How it works
          </h2>
          <ol className="relative flex flex-col gap-5 before:absolute before:top-3 before:bottom-3 before:left-[0.6875rem] before:w-px before:bg-border-strong">
            {STEPS.map((step, index) => (
              <li key={step.title} className="relative flex gap-4">
                <span
                  aria-hidden="true"
                  className="z-10 flex size-6 shrink-0 items-center justify-center rounded-pill border border-border-strong bg-surface-canvas font-mono text-[0.6875rem] font-semibold text-ink-secondary"
                >
                  {index + 1}
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-ink-primary">
                    {step.title}
                  </span>
                  <span className="mt-0.5 block text-sm leading-6 text-pretty text-ink-secondary">
                    {step.body}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </section>
      </main>
    </div>
  );
}
