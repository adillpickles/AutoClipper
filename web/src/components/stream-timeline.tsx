"use client";

// Whole-stream timeline view (#67, PRD § 4): plots every candidate across the VOD duration,
// keeps short moments anchored at their true start time inside an enlarged clickable hit area,
// shows a candidate-activity strip, and opens the selected candidate's ClipCard below.

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

const ANCHOR_COLOR: Record<Candidate["source"], string> = {
  discovered: "bg-sky-700 dark:bg-sky-300",
  viewer_clips: "bg-violet-700 dark:bg-violet-300",
  both: "bg-emerald-700 dark:bg-emerald-300",
};

const SHORT_LABEL_STYLE: Record<Candidate["source"], string> = {
  discovered:
    "border-sky-700/40 bg-white text-sky-900 dark:border-sky-300/50 dark:bg-zinc-900 dark:text-sky-200",
  viewer_clips:
    "border-violet-700/40 bg-white text-violet-900 dark:border-violet-300/50 dark:bg-zinc-900 dark:text-violet-200",
  both: "border-emerald-700/40 bg-white text-emerald-900 dark:border-emerald-300/50 dark:bg-zinc-900 dark:text-emerald-200",
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
  const activityBins = buildActivityBins(candidates, durationS, 48);
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

        {/* Candidate activity strip (reflects selected candidate scores only) */}
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
            <span className="font-medium text-zinc-700 dark:text-zinc-300">
              Candidate activity
            </span>
            <span className="text-zinc-600 dark:text-zinc-400">
              Ranked moments only — blank segments had no selected candidates
            </span>
          </div>
          <div
            role="img"
            aria-label="Candidate activity across the stream (ranked moments only; blank segments had no selected candidates)"
            className="grid h-3 w-full overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800"
            style={{
              gridTemplateColumns: `repeat(${activityBins.length}, minmax(0, 1fr))`,
            }}
          >
            {activityBins.map((intensity, index) => (
              <span
                key={index}
                aria-hidden="true"
                className="h-full bg-amber-500"
                style={{ opacity: intensity > 0 ? 0.15 + 0.85 * intensity : 0 }}
              />
            ))}
          </div>
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

            if (marker.isShort) {
              const labelAlign =
                marker.anchorOffsetInHitPct >= 65
                  ? "justify-start pr-1"
                  : marker.anchorOffsetInHitPct <= 35
                    ? "justify-end pl-1"
                    : "justify-center";
              return (
                <button
                  key={candidate.candidate_id}
                  type="button"
                  data-lane={marker.lane}
                  data-short="true"
                  data-anchor-left-pct={marker.anchorLeftPct.toFixed(2)}
                  aria-pressed={isSelected}
                  aria-label={`Rank #${candidate.rank}, ${span}: ${reasonFor(candidate)}`}
                  title={`#${candidate.rank} (${span}) · ${reasonFor(candidate)}`}
                  onClick={() => onSelect(candidate)}
                  style={{
                    left: `${marker.hitLeftPct}%`,
                    width: `${marker.hitWidthPct}%`,
                    top: `${topPx}px`,
                    height: `${LANE_HEIGHT_PX}px`,
                  }}
                  className={`group absolute box-border flex min-w-0 items-center overflow-hidden rounded-lg bg-transparent transition hover:bg-zinc-200/70 focus-visible:z-20 focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:outline-none dark:hover:bg-zinc-800/70 ${labelAlign} ${
                    isSelected
                      ? "z-10 bg-zinc-200/80 ring-2 ring-zinc-950 ring-offset-2 ring-offset-zinc-50 dark:bg-zinc-800/80 dark:ring-white dark:ring-offset-zinc-950"
                      : ""
                  }`}
                >
                  {/* Exact true-time span fill when wider than a hairline */}
                  {marker.anchorSpanInHitPct > 0 && (
                    <span
                      aria-hidden="true"
                      data-timeline-span="true"
                      style={{
                        left: `${marker.anchorOffsetInHitPct}%`,
                        width: `${marker.anchorSpanInHitPct}%`,
                      }}
                      className={`pointer-events-none absolute inset-y-1 rounded-xs opacity-35 ${ANCHOR_COLOR[candidate.source]}`}
                    />
                  )}
                  {/* True-time start anchor pinned exactly to start_s without left-shifting */}
                  <span
                    aria-hidden="true"
                    data-timeline-anchor="true"
                    style={{
                      left: `${marker.anchorOffsetInHitPct}%`,
                    }}
                    className={`pointer-events-none absolute inset-y-0.5 w-0.5 -translate-x-1/2 rounded-full ${ANCHOR_COLOR[candidate.source]}`}
                  />
                  <span
                    className={`relative z-10 max-w-full truncate rounded border px-0.5 py-0.5 text-[10px] leading-none font-semibold tabular-nums shadow-2xs sm:px-1 sm:text-[11px] ${SHORT_LABEL_STYLE[candidate.source]}`}
                  >
                    #{candidate.rank}
                  </span>
                </button>
              );
            }

            return (
              <button
                key={candidate.candidate_id}
                type="button"
                data-lane={marker.lane}
                data-short="false"
                data-anchor-left-pct={marker.anchorLeftPct.toFixed(2)}
                aria-pressed={isSelected}
                aria-label={`Rank #${candidate.rank}, ${span}: ${reasonFor(candidate)}`}
                title={`#${candidate.rank} (${span}) · ${reasonFor(candidate)}`}
                onClick={() => onSelect(candidate)}
                style={{
                  left: `${marker.hitLeftPct}%`,
                  width: `${marker.hitWidthPct}%`,
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
