import { PreflightBanner } from "@/components/preflight-banner";
import { SiteHeader } from "@/components/site-header";
import { StartForm } from "@/components/start-form";

const STEPS = [
  {
    title: "Add the VOD",
    body: "Paste a Twitch link or ID. If you already have the video, drop it in or add its file path.",
  },
  {
    title: "Analyze",
    body: "Chat activity and loudness score the stream, then viewer-made clips are merged into the shortlist.",
  },
  {
    title: "Review",
    body: "The top moments come back ranked, each with a one-line reason and a place on the timeline.",
  },
  {
    title: "Export",
    body: "Download captioned 9:16 clips, or join the moments into one 16:9 highlights video.",
  },
] as const;

const OUTPUTS = [
  {
    title: "Vertical clips",
    body: "The top moments as 9:16 clips with burned-in captions, ready to post.",
  },
  {
    title: "Highlights video",
    body: "The same moments joined, in stream order, into one 16:9 video to watch.",
  },
] as const;

export default function Home() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-4 sm:px-8">
      <SiteHeader />
      <main
        id="main-content"
        tabIndex={-1}
        className="flex flex-1 flex-col gap-8 py-8 focus:outline-none sm:gap-10 sm:py-14"
      >
        <section className="flex flex-col gap-5">
          <p className="text-xs font-semibold tracking-[0.14em] text-accent uppercase">
            Twitch VOD to clips
          </p>
          <div className="flex flex-col gap-3">
            <h1 className="font-display text-3xl font-bold tracking-tight text-balance text-ink-primary sm:text-5xl">
              Find the moments worth posting.
            </h1>
            <p className="max-w-2xl text-base/7 text-pretty text-ink-secondary sm:text-lg/8">
              AutoClipper reads the chat, the audio, and the clips viewers
              already made, then turns a long Twitch VOD into ranked vertical
              clips and a highlights video. You preview, trim if you want, and
              download.
            </p>
          </div>
          <ol className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {STEPS.map((step, index) => (
              <li
                key={step.title}
                className="flex gap-3 rounded-card border border-border-subtle bg-surface-panel p-4 shadow-card"
              >
                <span
                  aria-hidden="true"
                  className="flex size-7 shrink-0 items-center justify-center rounded-pill bg-accent-soft font-mono text-xs font-semibold text-accent-soft-ink"
                >
                  {index + 1}
                </span>
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-ink-primary">
                    {step.title}
                  </span>
                  <span className="mt-1 block text-sm leading-5 text-ink-secondary">
                    {step.body}
                  </span>
                </span>
              </li>
            ))}
          </ol>
        </section>

        <PreflightBanner />
        <StartForm />

        <section
          aria-labelledby="outputs-heading"
          className="flex flex-col gap-3"
        >
          <h2
            id="outputs-heading"
            className="font-display text-base font-semibold text-ink-primary"
          >
            What you leave with
          </h2>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {OUTPUTS.map((output) => (
              <li
                key={output.title}
                className="rounded-card border border-border-subtle bg-surface-elevated px-4 py-3"
              >
                <p className="text-sm font-semibold text-ink-primary">
                  {output.title}
                </p>
                <p className="mt-1 text-sm leading-5 text-ink-secondary">
                  {output.body}
                </p>
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
