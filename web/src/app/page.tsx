// Placeholder home page. Describes the planned flow only: nothing here is
// wired to the API yet, so it must not show data or claim working features.

const plannedSteps = [
  {
    title: "Paste a VOD link",
    description: "Start from a link to a past Twitch broadcast.",
  },
  {
    title: "Review the best moments",
    description:
      "Browse a ranked shortlist and a timeline built from chat activity, audio and viewer clips.",
  },
  {
    title: "Preview and trim",
    description: "Watch a moment and adjust where it starts and ends.",
  },
  {
    title: "Export a vertical clip",
    description: "Download a 9:16 MP4 with burned-in captions.",
  },
] as const;

export default function Home() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-6 sm:px-8">
      <header className="flex items-center justify-between gap-4 py-6">
        <span className="text-lg font-semibold tracking-tight">
          AutoClipper
        </span>
        <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-900 dark:bg-amber-400/15 dark:text-amber-200">
          In development
        </span>
      </header>

      <main className="flex flex-1 flex-col justify-center gap-12 py-12 sm:py-16">
        <div className="flex flex-col gap-6">
          <h1 className="text-4xl font-semibold tracking-tight text-balance sm:text-5xl">
            Find the{" "}
            <span className="rounded-md bg-amber-200 box-decoration-clone px-1.5 dark:bg-amber-400/30">
              highlights
            </span>{" "}
            in a long Twitch VOD.
          </h1>
          <p className="max-w-2xl text-lg/8 text-pretty text-zinc-600 dark:text-zinc-400">
            AutoClipper is being built to rank the best moments of a past
            broadcast from chat activity, audio and viewer clips, even when
            nobody clipped anything. Preview a moment, adjust its start and end,
            and export it as a vertical, captioned clip.
          </p>
        </div>

        <section aria-labelledby="planned-flow" className="flex flex-col gap-4">
          <h2
            id="planned-flow"
            className="text-sm font-medium text-zinc-500 dark:text-zinc-400"
          >
            Planned flow
          </h2>
          {/* role="list" keeps list semantics in Safari, since Tailwind removes list markers. */}
          <ol role="list" className="grid gap-3 sm:grid-cols-2">
            {plannedSteps.map((step, index) => (
              <li
                key={step.title}
                className="flex gap-4 rounded-xl border border-zinc-200 p-5 dark:border-zinc-800"
              >
                <span
                  aria-hidden="true"
                  className="font-mono text-sm/6 text-amber-700 dark:text-amber-300"
                >
                  {String(index + 1).padStart(2, "0")}
                </span>
                <div className="flex flex-col gap-1">
                  <h3 className="font-medium">{step.title}</h3>
                  <p className="text-sm/6 text-zinc-600 dark:text-zinc-400">
                    {step.description}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </section>
      </main>

      <footer className="border-t border-zinc-200 py-6 text-sm text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
        This is a placeholder page. The app cannot analyze a VOD yet.
      </footer>
    </div>
  );
}
