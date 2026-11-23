"use client";

// Whole-stream timeline view (#67, PRD § 4): plots every candidate across the VOD duration,
// keeps short moments anchored at their true start time inside an enlarged clickable hit area,
// shows a candidate-activity strip, and opens the selected candidate's ClipCard below.

import { ClipCard, type ClipExport } from "@/components/clip-card";
import type { Candidate } from "@/lib/api/types";
import { formatTime } from "@/lib/format";
import { reasonFor } from "@/lib/reason";
import { buildActivityBins, layoutTimelineMarkers } from "@/lib/timeline";

// WCAG AA (>= 4.5:1) foreground/background pairs via semantic source tokens.
const MARKER_STYLE: Record<Candidate["source"], string> = {
  discovered:
    "bg-source-discovered text-source-discovered-ink hover:bg-source-discovered-hover",
  viewer_clips:
    "bg-source-viewer text-source-viewer-ink hover:bg-source-viewer-hover",
  both: "bg-source-both text-source-both-ink hover:bg-source-both-hover",
};

const ANCHOR_COLOR: Record<Candidate["source"], string> = {
  discovered: "bg-source-discovered",
  viewer_clips: "bg-source-viewer",
  both: "bg-source-both",
};

const SHORT_LABEL_STYLE: Record<Candidate["source"], string> = {
  discovered:
    "border-source-discovered-border bg-surface-panel text-source-discovered-soft-ink",
  viewer_clips:
    "border-source-viewer-border bg-surface-panel text-source-viewer-soft-ink",
  both: "border-source-both-border bg-surface-panel text-source-both-soft-ink",
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
      <div className="flex flex-col gap-4 rounded-panel border border-border-subtle bg-surface-panel p-5 shadow-panel">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="font-display text-base font-semibold text-ink-primary">
              Stream timeline
            </h3>
            <p className="text-xs text-ink-secondary">
              Select a highlighted moment to inspect, download, or edit its
              clip.
              {layout.laneCount > 1 &&
                ` Overlapping moments are arranged into ${layout.laneCount} rows.`}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-xs text-ink-secondary">
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-2.5 rounded-pill bg-source-both"
              />
              Both
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-2.5 rounded-pill bg-source-discovered"
              />
              Discovered
            </span>
            <span className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="size-2.5 rounded-pill bg-source-viewer"
              />
              Viewer clip
            </span>
          </div>
        </div>

        {/* Candidate activity strip (reflects selected candidate scores only) */}
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
            <span className="font-medium text-ink-primary">
              Candidate activity
            </span>
            <span className="text-ink-tertiary">
              Ranked moments only — blank segments had no selected candidates
            </span>
          </div>
          <div
            role="img"
            aria-label="Candidate activity across the stream (ranked moments only; blank segments had no selected candidates)"
            className="grid h-3 w-full overflow-hidden rounded-pill bg-surface-subtle"
            style={{
              gridTemplateColumns: `repeat(${activityBins.length}, minmax(0, 1fr))`,
            }}
          >
            {activityBins.map((intensity, index) => (
              <span
                key={index}
                aria-hidden="true"
                className="h-full bg-signal"
                style={{ opacity: intensity > 0 ? 0.15 + 0.85 * intensity : 0 }}
              />
            ))}
          </div>
        </div>

        {/* Non-overlapping marker lanes across [0, durationS] */}
        <div
          role="group"
          aria-label="Highlighted moments along the stream"
          className="relative w-full rounded-card bg-surface-elevated py-2 ring-1 ring-border-subtle ring-inset"
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
                  className={`group absolute box-border flex min-w-0 items-center overflow-hidden rounded-control bg-transparent transition hover:bg-surface-subtle/80 focus-visible:z-20 focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none ${labelAlign} ${
                    isSelected
                      ? "z-10 bg-surface-subtle ring-2 ring-ink-primary ring-offset-2 ring-offset-surface-elevated"
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
                    className={`pointer-events-none absolute inset-y-0.5 w-0.5 -translate-x-1/2 rounded-pill ${ANCHOR_COLOR[candidate.source]}`}
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
                className={`absolute box-border flex min-w-0 items-center justify-center overflow-hidden rounded-control px-0.5 font-mono text-xs font-semibold transition focus-visible:z-20 focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none ${MARKER_STYLE[candidate.source]} ${
                  isSelected
                    ? "z-10 ring-2 ring-ink-primary ring-offset-2 ring-offset-surface-elevated"
                    : ""
                }`}
              >
                <span className="truncate tabular-nums">#{candidate.rank}</span>
              </button>
            );
          })}
        </div>

        {/* Stream time axis ticks anchored to the exact marker percentage scale */}
        <div className="relative h-4 w-full text-xs text-ink-tertiary tabular-nums">
          {layout.ticks.map((tick) => {
            const align =
              tick.pct === 0
                ? ""
                : tick.pct === 100
                  ? "-translate-x-full"
                  : "-translate-x-1/2";
            const intermediateHidden =
              tick.pct === 25 || tick.pct === 75 ? "hidden sm:inline" : "";
            return (
              <span
                key={tick.pct}
                style={{ left: `${tick.pct}%` }}
                className={`absolute top-0 whitespace-nowrap ${align} ${intermediateHidden}`}
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
