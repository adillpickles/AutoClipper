// What the progress screen says and shows for each analysis stage (#188): a plain-language
// label per contract stage (api-contract.md § 3.4) and a small decorative animation of the work.

import type { CSSProperties, ReactNode } from "react";

import type { AnalysisResult } from "@/lib/api/types";

/** Screen copy per contract stage. Unknown stages fall back to the server's `message`. */
export const STAGE_LABELS: Readonly<Record<string, string>> = {
  resolving_vod: "Looking up the VOD",
  fetching_clips: "Collecting viewer clips",
  downloading_chat: "Reading chat",
  downloading_audio: "Listening to the audio",
  computing_features: "Measuring every 10 s of the stream",
  ranking: "Scoring and picking the top moments",
};

export interface PipelineCount {
  label: string;
  /** null when the pipeline couldn't count it (e.g. no chat replay). */
  value: number | null;
  /** The stage after which the count is known. */
  stage: string;
}

/**
 * The live numbers the screen shows once the result is in: chat messages read, windows scored
 * and moments found. Only counts the pipeline reports; nothing is estimated.
 */
export function pipelineCounts(result: AnalysisResult): PipelineCount[] {
  return [
    {
      label: "Chat messages read",
      value: result.n_chat_messages ?? null,
      stage: "downloading_chat",
    },
    {
      label: "Windows scored",
      value: result.n_windows ?? null,
      stage: "ranking",
    },
    {
      label: "Moments found",
      value: result.candidates.length,
      stage: "ranking",
    },
  ];
}

const NUMBER = new Intl.NumberFormat("en-US");

export function formatCount(value: number): string {
  return NUMBER.format(value);
}

function delay(seconds: number): CSSProperties {
  return { animationDelay: `${seconds}s` };
}

/** A decorative animation of `stage`'s work; static when `animated` is false. */
export function StageVisual({
  stage,
  animated,
}: {
  stage: string;
  animated: boolean;
}) {
  const anim = (name: string) => (animated ? name : "");
  let body: ReactNode;
  switch (stage) {
    case "fetching_clips":
      body = (
        <div className="flex items-end gap-2">
          {[0, 1, 2, 3].map((i) => (
            <span
              key={i}
              className={`block h-10 w-6 rounded-[4px] border-2 border-source-viewer bg-source-viewer-soft ${anim("stage-anim-rise")}`}
              style={delay(i * 0.25)}
            />
          ))}
        </div>
      );
      break;
    case "downloading_chat":
      body = (
        <div className="flex w-32 flex-col gap-1.5">
          {[
            ["w-20", "self-start"],
            ["w-14", "self-end"],
            ["w-24", "self-start"],
          ].map(([width, align], i) => (
            <span
              key={i}
              className={`block h-3 rounded-pill bg-accent-soft ${width} ${align} ${anim("stage-anim-rise")}`}
              style={delay(i * 0.4)}
            />
          ))}
        </div>
      );
      break;
    case "downloading_audio":
      body = (
        <div className="flex h-12 items-center gap-1">
          {[0.5, 0.8, 1, 0.65, 0.9, 0.45, 0.75, 1, 0.55].map((height, i) => (
            <span
              key={i}
              className={`block w-1.5 origin-center rounded-pill bg-accent ${anim("stage-anim-wave")}`}
              style={{ height: `${height * 100}%`, ...delay(i * 0.09) }}
            />
          ))}
        </div>
      );
      break;
    case "computing_features":
      body = (
        <div className="grid grid-cols-12 gap-1">
          {Array.from({ length: 24 }, (_, i) => (
            <span
              key={i}
              className={`block size-2.5 rounded-[3px] bg-accent ${animated ? "stage-anim-scan" : "opacity-40"}`}
              style={delay((i % 12) * 0.08)}
            />
          ))}
        </div>
      );
      break;
    case "ranking":
      body = (
        <div className="flex h-12 items-end gap-1.5">
          {[0.45, 0.7, 1, 0.6, 0.35].map((height, i) => (
            <span
              key={i}
              className={`block w-4 origin-bottom rounded-t-[4px] ${height === 1 ? "bg-accent" : "bg-accent-soft"} ${anim("stage-anim-grow")}`}
              style={{ height: `${height * 100}%`, ...delay(i * 0.12) }}
            />
          ))}
        </div>
      );
      break;
    default:
      // resolving_vod, and any stage this client doesn't know.
      body = (
        <div className="flex items-center gap-2">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className={`block size-3 rounded-pill bg-accent ${animated ? "stage-anim-scan" : "opacity-60"}`}
              style={delay(i * 0.2)}
            />
          ))}
        </div>
      );
  }
  return (
    <div
      aria-hidden="true"
      data-testid="stage-visual"
      data-stage={stage}
      data-animated={animated ? "true" : "false"}
      className="flex h-16 items-center justify-center"
    >
      {body}
    </div>
  );
}
