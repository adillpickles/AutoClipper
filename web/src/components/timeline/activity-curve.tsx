// The stream's measured chat rate (amber area) and loudness (line) drawn behind the scrub track
// (#213). Decorative and hidden from assistive tech: the timeline gives screen readers a text
// summary instead (`activitySummary`). Renders nothing when the result has no activity. The
// paths are memoized: the timeline re-renders on every pointer move while scrubbing.

import { memo, useMemo } from "react";

import type { Activity } from "@/lib/api/types";

import {
  VIEW_H,
  VIEW_W,
  curvePaths,
  drawableActivity,
  normalizeSeries,
} from "./activity";

function activityPaths(
  activity: Activity | null | undefined,
  durationS: number,
): { chat: string[]; loudness: string[] } | null {
  const series = drawableActivity(activity, durationS);
  if (!activity || (!series.chat && !series.loudness)) return null;
  return {
    chat: series.chat
      ? curvePaths(
          normalizeSeries(series.chat, "chat"),
          activity.step_s,
          durationS,
          { area: true },
        )
      : [],
    loudness: series.loudness
      ? curvePaths(
          normalizeSeries(series.loudness, "loudness"),
          activity.step_s,
          durationS,
          { area: false },
        )
      : [],
  };
}

export const ActivityCurve = memo(function ActivityCurve({
  activity,
  durationS,
}: {
  activity: Activity | null | undefined;
  durationS: number;
}) {
  const paths = useMemo(
    () => activityPaths(activity, durationS),
    [activity, durationS],
  );
  if (!paths) return null;
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      data-testid="activity-curve"
      viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
      preserveAspectRatio="none"
      className="pointer-events-none absolute inset-0 h-full w-full"
    >
      {paths.chat.map((d, i) => (
        <path
          key={`chat-${i}`}
          data-activity="chat"
          d={d}
          className="fill-signal/20 stroke-signal/70"
          strokeWidth={1.5}
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
      {paths.loudness.map((d, i) => (
        <path
          key={`loudness-${i}`}
          data-activity="loudness"
          d={d}
          className="fill-none stroke-ink-secondary/80"
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      ))}
    </svg>
  );
});
