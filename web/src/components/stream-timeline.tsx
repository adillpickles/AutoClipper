"use client";

// Whole-stream timeline view (#67, PRD § 4): plots every candidate across the VOD duration,
// stacks overlapping markers into non-overlapping lanes, shows a score activity heatmap strip,
// and opens the selected candidate's ClipCard below.

import { ClipCard, type ClipExport } from "@/components/clip-card";
import type { Candidate } from "@/lib/api/types";
import { formatTime } from "@/lib/format";
import { reasonFor } from "@/lib/reason";
import { buildActivityBins, layoutTimelineMarkers } from "@/lib/timeline";

// WCAG AA (>= 4.5:1) foreground/background pairs in both light and dark themes.
const MARKER_STYLE: Record<Candidate["source"], string> = {
  discovered:
    "bg-sky-700 text-white hover:bg-sky-800 dark:bg-sky-300 dark:text-zinc-950 dark:hover:bg-sky-200",
  viewer_clips:
    "bg-violet-700 text-white hover:bg-violet-800 dark:bg-violet-300 dark:text-zinc-950 dark:hover:bg-violet-200",
  both: "bg-emerald-700 text-white hover:bg-emerald-800 dark:bg-emerald-300 dark:text-zinc-950 dark:hover:bg-emerald-200",
};

const LANE_HEIGHT_PX = 36;
const LANE_GAP_PX = 8;

export function StreamTimeline({
  jobId,
  videoId,
  durationS,
  candidates,
  selectedId,
  onSelect,
  clips,
  onRetry,
}: {
  jobId: string;
  videoId: string;
  durationS: number;
  candidates: readonly Candidate[];
  selectedId: string | null;
  onSelect: (candidate: Candidate) => void;
  clips: Record<string, ClipExport>;
  onRetry: (candidate: Candidate) => void;
}) {
  const layout = layoutTimelineMarkers(candidates, durationS);
  const heatmapBins = buildActivityBins(candidates, durationS, 48);
  const topRanked =
    layout.markers.length > 0
      ? layout.markers.reduce((best, item) =>
          item.candidate.rank < best.candidate.rank ? item : best,
        )
      : null;
  const selected =
    layout.markers.find((m) => m.candidate.candidate_id === selectedId) ??
    topRanked;

  const trackHeightPx =
    layout.laneCount > 0
      ? layout.laneCount * LANE_HEIGHT_PX + (layout.laneCount - 1) * LANE_GAP_PX
      : LANE_HEIGHT_PX;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4 rounded-2xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-base font-semibold">Stream timeline</h3>
            <p className="text-xs text-zinc-600 dark:text-zinc-400">
              Select a highlighted moment to inspect, download, or edit its
              clip.
              {layout.laneCount > 1 &&
                ` Overlapping moments are arranged into ${layout.laneCount} rows.`}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-600 dark:text-zinc-400">
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-2.5 rounded-full bg-emerald-700 dark:bg-emerald-300"
              />
              Both
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-2.5 rounded-full bg-sky-700 dark:bg-sky-300"
              />
              Discovered
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-2.5 rounded-full bg-violet-700 dark:bg-violet-300"
              />
              Viewer clip
            </span>
          </div>
        </div>

        {/* Score density strip across the full VOD */}
        <div
          aria-hidden="true"
          className="grid h-3 w-full overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800"
          style={{
            gridTemplateColumns: `repeat(${heatmapBins.length}, minmax(0, 1fr))`,
          }}
        >
          {heatmapBins.map((intensity, index) => (
            <span
              key={index}
              className="h-full bg-amber-500"
              style={{ opacity: intensity > 0 ? 0.15 + 0.85 * intensity : 0 }}
            />
          ))}
        </div>

        {/* Non-overlapping marker lanes across [0, durationS] */}
        <div
          role="group"
          aria-label="Highlighted moments along the stream"
          className="relative w-full rounded-xl bg-zinc-50 py-2 dark:bg-zinc-950"
          style={{ height: `${trackHeightPx + 16}px` }}
        >
          {layout.markers.map((marker) => {
            const { candidate } = marker;
            const isSelected =
              selected?.candidate.candidate_id === candidate.candidate_id;
            const span = `${formatTime(candidate.start_s)}–${formatTime(candidate.end_s)}`;
            const topPx = 8 + marker.lane * (LANE_HEIGHT_PX + LANE_GAP_PX);
            return (
              <button
                key={candidate.candidate_id}
                type="button"
                data-lane={marker.lane}
                aria-pressed={isSelected}
                aria-label={`Rank #${candidate.rank}, ${span}: ${reasonFor(candidate)}`}
                title={`#${candidate.rank} (${span}) · ${reasonFor(candidate)}`}
                onClick={() => onSelect(candidate)}
                style={{
                  left: `${marker.leftPct}%`,
                  width: `${marker.widthPct}%`,
                  top: `${topPx}px`,
                  height: `${LANE_HEIGHT_PX}px`,
                }}
                className={`absolute box-border flex min-w-0 items-center justify-center overflow-hidden rounded-lg px-0.5 text-xs font-semibold transition focus-visible:z-20 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none ${MARKER_STYLE[candidate.source]} ${
                  isSelected
                    ? "z-10 ring-2 ring-zinc-950 ring-offset-2 ring-offset-zinc-50 dark:ring-white dark:ring-offset-zinc-950"
                    : ""
                }`}
              >
                <span className="truncate tabular-nums">#{candidate.rank}</span>
              </button>
            );
          })}
        </div>

        {/* Stream time axis ticks anchored to the exact marker percentage scale */}
        <div className="relative h-4 w-full text-xs text-zinc-500 tabular-nums dark:text-zinc-400">
          {layout.ticks.map((tick) => {
            const align =
              tick.pct === 0
                ? ""
                : tick.pct === 100
                  ? "-translate-x-full"
                  : "-translate-x-1/2";
            return (
              <span
                key={tick.pct}
                style={{ left: `${tick.pct}%` }}
                className={`absolute top-0 whitespace-nowrap ${align}`}
              >
                {formatTime(tick.seconds)}
              </span>
            );
          })}
        </div>
      </div>

      {selected && (
        <div className="mx-auto w-full max-w-sm">
          <ol role="list">
            <ClipCard
              key={selected.candidate.candidate_id}
              jobId={jobId}
              candidate={selected.candidate}
              scoreFraction={selected.scoreFraction}
              clip={
                clips[selected.candidate.candidate_id] ?? { kind: "pending" }
              }
              onRetry={() => onRetry(selected.candidate)}
              videoId={videoId}
            />
          </ol>
        </div>
      )}
    </div>
  );
}
