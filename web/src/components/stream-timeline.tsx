"use client";

// Whole-stream timeline view (#67, redesigned as a scrubbable editor-style timeline in #189,
// PRD § 4). A scrub track spans the VOD with shaded, numbered spans for the ranked moments.
// Hovering or dragging shows the time, the moment there and a preview frame when preview media
// exists, and clicking a span (or pressing Enter) opens that moment's clip card below. Rank chips
// under the track give one keyboard stop per top moment. There is no activity curve: the result
// has no per-window chat or audio values, and drawing the model's own picks as if they were
// measurements would mislead (AGENTS.md § 6.3).

import {
  useId,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
} from "react";

import { ClipCard, type ClipExport } from "@/components/clip-card";
import { EmptyState } from "@/components/ui/empty-state";
import type { Candidate, VodSummary } from "@/lib/api/types";
import { formatTime } from "@/lib/format";
import { titleFor } from "@/lib/reason";
import { layoutTimelineMarkers } from "@/lib/timeline";

import {
  adjacentMomentStart,
  clamp,
  momentAt,
  pctOf,
  popoverAlign,
  rankLabelAnchor,
  rulerTicks,
  safeDuration,
  spanGeometry,
  timeAtClientX,
} from "./timeline/geometry";
import { ScrubPreview } from "./timeline/scrub-preview";
import {
  previewSourceFor,
  usePreviewMedia,
} from "./timeline/use-preview-media";

/** Moments drawn as labelled spans with rank chips; the rest are lighter spans (PRD § 4). */
export const TOP_MOMENTS = 10;

const CHIP_LANE_PX = 26;
const CHIP_LANE_GAP_PX = 4;
/** Pointer travel (px) after which a press counts as a drag rather than a click. */
const DRAG_THRESHOLD_PX = 5;

type ScrubVia = "pointer" | "keyboard";

/** Whether focus arrived by keyboard (`:focus-visible`), not by a mouse or touch press. */
function isFocusVisible(element: Element): boolean {
  try {
    return element.matches(":focus-visible");
  } catch {
    return false;
  }
}

function spanLabel(candidate: Candidate): string {
  return `${formatTime(candidate.start_s)}–${formatTime(candidate.end_s)}`;
}

export function StreamTimeline({
  jobId,
  videoId,
  vod,
  durationS,
  candidates,
  selectedId,
  onSelect,
  clips,
  onRetry,
}: {
  jobId: string;
  videoId: string;
  vod?: Pick<VodSummary, "broadcaster_display_name" | "created_at">;
  durationS: number;
  candidates: readonly Candidate[];
  selectedId: string | null;
  onSelect: (candidate: Candidate) => void;
  clips: Record<string, ClipExport>;
  onRetry: (candidate: Candidate) => void;
}) {
  const headingId = useId();
  const hintId = useId();
  const chipRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const pressRef = useRef<{ x: number; dragged: boolean } | null>(null);
  // Set when a press ended a drag, so the click the browser fires after it doesn't open a moment.
  const suppressClickRef = useRef(false);
  const [cursorS, setCursorS] = useState<number | null>(null);
  const [scrubVia, setScrubVia] = useState<ScrubVia | null>(null);
  // The rank chip being hovered or focused: its moment names the label even where it overlaps a
  // better-ranked one.
  const [chipMomentId, setChipMomentId] = useState<string | null>(null);

  const duration = safeDuration(durationS);
  const ranked = useMemo(
    () => [...candidates].sort((a, b) => a.rank - b.rank),
    [candidates],
  );
  const top = useMemo(() => ranked.slice(0, TOP_MOMENTS), [ranked]);
  const chipLayout = useMemo(
    () => layoutTimelineMarkers(top, duration),
    [top, duration],
  );
  const ticks = useMemo(() => rulerTicks(duration), [duration]);
  const { sources, ensure, markFailed } = usePreviewMedia(jobId, top, duration);

  if (candidates.length === 0) {
    return (
      <EmptyState
        heading="h3"
        title="No moments on this timeline"
        description="This analysis did not find any moments to plot across the stream."
      />
    );
  }

  const topIds = new Set(top.map((c) => c.candidate_id));
  const others = ranked.filter((c) => !topIds.has(c.candidate_id));
  const selected =
    ranked.find((c) => c.candidate_id === selectedId) ?? ranked[0];
  // Score bar scaled within the top moments, as on the clip cards (§ 4.3).
  const topScores = top.map((c) => c.score);
  const low = Math.min(...topScores);
  const high = Math.max(...topScores);
  const scoreFraction =
    high > low
      ? clamp(0.15 + (0.85 * (selected.score - low)) / (high - low), 0.15, 1)
      : 1;
  const chronologicalTop = [...top].sort(
    (a, b) => a.start_s - b.start_s || a.rank - b.rank,
  );
  // Roving tab stop: the selected chip if it is a top moment, else the earliest one.
  const tabStopId = topIds.has(selected.candidate_id)
    ? selected.candidate_id
    : chronologicalTop[0]?.candidate_id;

  const sliderS = cursorS ?? selected.start_s;
  const sliderMoment = momentAt(sliderS, ranked, duration);
  const chipMoment =
    chipMomentId !== null
      ? (ranked.find((c) => c.candidate_id === chipMomentId) ?? null)
      : null;
  const scrubMoment = scrubVia ? (chipMoment ?? sliderMoment) : null;
  const previewSource =
    scrubVia !== null
      ? previewSourceFor(sliderS, sources, scrubMoment?.candidate_id ?? null)
      : null;
  const cursorPct = pctOf(sliderS, duration);
  const laneCount = Math.max(1, chipLayout.laneCount);
  const chipTrackPx =
    laneCount * CHIP_LANE_PX + (laneCount - 1) * CHIP_LANE_GAP_PX;

  function open(candidate: Candidate | null) {
    if (candidate) onSelect(candidate);
  }

  function showAt(timeS: number, via: ScrubVia, chipId: string | null = null) {
    setCursorS(clamp(timeS, 0, duration));
    setScrubVia(via);
    setChipMomentId(chipId);
  }

  function hide(via: ScrubVia) {
    setScrubVia((current) => (current === via ? null : current));
  }

  // --- Scrub track: pointer -------------------------------------------------------------------

  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    const press = pressRef.current;
    if (press && Math.abs(event.clientX - press.x) > DRAG_THRESHOLD_PX) {
      press.dragged = true;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    showAt(timeAtClientX(event.clientX, rect, duration), "pointer");
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    pressRef.current = { x: event.clientX, dragged: false };
    suppressClickRef.current = false;
    // Keep receiving moves past the track's edges while dragging.
    const track = event.currentTarget;
    if (typeof track.setPointerCapture === "function") {
      try {
        track.setPointerCapture(event.pointerId);
      } catch {
        // Not every environment can capture this pointer; scrubbing still works inside the track.
      }
    }
  }

  function handlePointerUp() {
    if (pressRef.current?.dragged) suppressClickRef.current = true;
    pressRef.current = null;
  }

  function handleTrackClick(event: MouseEvent<HTMLDivElement>) {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const timeS = timeAtClientX(event.clientX, rect, duration);
    setCursorS(timeS);
    open(momentAt(timeS, ranked, duration));
  }

  // --- Scrub track: keyboard (role="slider") ---------------------------------------------------

  function handleTrackFocus(event: FocusEvent<HTMLDivElement>) {
    void ensure();
    setChipMomentId(null);
    if (isFocusVisible(event.currentTarget)) setScrubVia("keyboard");
  }

  function handleTrackKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const step = Math.max(1, duration / 100) * (event.shiftKey ? 10 : 1);
    let next: number | null = null;
    switch (event.key) {
      case "ArrowRight":
      case "ArrowUp":
        next = sliderS + step;
        break;
      case "ArrowLeft":
      case "ArrowDown":
        next = sliderS - step;
        break;
      case "PageDown":
        next = adjacentMomentStart(sliderS, ranked, 1) ?? sliderS;
        break;
      case "PageUp":
        next = adjacentMomentStart(sliderS, ranked, -1) ?? sliderS;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = duration;
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        setScrubVia("keyboard");
        open(sliderMoment);
        return;
      default:
        return;
    }
    event.preventDefault();
    showAt(next, "keyboard");
  }

  // --- Rank chips -------------------------------------------------------------------------------

  function handleChipKeyDown(
    event: KeyboardEvent<HTMLButtonElement>,
    candidateId: string,
  ) {
    if (
      chronologicalTop.length <= 1 ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      event.shiftKey
    ) {
      return;
    }
    const index = chronologicalTop.findIndex(
      (c) => c.candidate_id === candidateId,
    );
    if (index === -1) return;
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = Math.min(chronologicalTop.length - 1, index + 1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = Math.max(0, index - 1);
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = chronologicalTop.length - 1;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    chipRefs.current[chronologicalTop[nextIndex].candidate_id]?.focus();
  }

  const popoverHint = scrubMoment
    ? scrubVia === "keyboard"
      ? "Press Enter to open"
      : "Click to open"
    : null;
  const sliderValueText = `${formatTime(sliderS)} of ${formatTime(duration)}${
    sliderMoment
      ? `, inside moment #${sliderMoment.rank}: ${titleFor(sliderMoment)}. Press Enter to open it.`
      : ", no ranked moment here."
  }`;

  return (
    <div className="flex min-w-0 flex-col gap-6">
      <section
        aria-labelledby={headingId}
        className="flex min-w-0 flex-col gap-4 rounded-panel border border-border-subtle bg-surface-panel p-4 shadow-panel sm:p-5"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h3
              id={headingId}
              className="font-display text-base font-semibold text-ink-primary"
            >
              Stream timeline
            </h3>
            <p className="text-xs text-ink-secondary">
              The whole stream, start to end. Hover or drag along it to preview;
              shaded spans are the ranked moments, so click one to open its
              clip.
            </p>
          </div>
          <ul
            aria-label="Timeline legend"
            className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-secondary sm:justify-end"
          >
            <li className="inline-flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="h-2.5 w-3 rounded-xs bg-accent"
              />
              Top moment (#1 = best)
            </li>
            {others.length > 0 && (
              <li className="inline-flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className="h-2.5 w-3 rounded-xs bg-ink-tertiary/25"
                />
                Lower-ranked moment
              </li>
            )}
          </ul>
        </div>

        <div className="flex min-w-0 flex-col gap-1">
          {/* Time ruler on the same percentage scale as every span below. */}
          <div
            aria-hidden="true"
            className="relative h-4 w-full text-[11px] text-ink-tertiary tabular-nums"
          >
            {ticks.map((tick, index) => {
              const edge =
                tick.pct === 0
                  ? ""
                  : tick.pct === 100
                    ? "-translate-x-full"
                    : "-translate-x-1/2";
              // Keep the ends and every other interior label on narrow screens.
              const narrowHidden =
                index % 2 === 1 && tick.pct !== 100 ? "hidden sm:inline" : "";
              return (
                <span
                  key={tick.seconds}
                  data-ruler-tick={tick.seconds}
                  style={{ left: `${tick.pct}%` }}
                  className={`absolute top-0 whitespace-nowrap ${edge} ${narrowHidden}`}
                >
                  {formatTime(tick.seconds)}
                </span>
              );
            })}
          </div>

          {/* Scrub track: moment spans, rank labels and playhead. */}
          <div className="relative">
            <ScrubPreview
              visible={scrubVia !== null}
              leftPct={cursorPct}
              align={popoverAlign(cursorPct)}
              timeLabel={formatTime(sliderS)}
              momentLabel={
                scrubMoment
                  ? `#${scrubMoment.rank} · ${titleFor(scrubMoment)}`
                  : null
              }
              hint={popoverHint}
              source={previewSource}
              timeS={sliderS}
              onSourceError={markFailed}
            />
            <div
              role="slider"
              tabIndex={0}
              aria-label="Scrub through the stream"
              aria-describedby={hintId}
              aria-valuemin={0}
              aria-valuemax={Math.round(duration)}
              aria-valuenow={Math.round(sliderS)}
              aria-valuetext={sliderValueText}
              data-testid="scrub-track"
              onPointerEnter={() => void ensure()}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerCancel={() => {
                pressRef.current = null;
                hide("pointer");
              }}
              onPointerLeave={() => hide("pointer")}
              onClick={handleTrackClick}
              onFocus={handleTrackFocus}
              onBlur={() => hide("keyboard")}
              onKeyDown={handleTrackKeyDown}
              className={`relative h-20 w-full touch-pan-y overflow-hidden rounded-card bg-surface-elevated ring-1 ring-border-subtle select-none ring-inset focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none sm:h-24 ${
                scrubMoment && scrubVia === "pointer"
                  ? "cursor-pointer"
                  : "cursor-crosshair"
              }`}
            >
              {/* Faint gridlines at the ruler's ticks. */}
              {ticks
                .filter((tick) => tick.pct > 0 && tick.pct < 100)
                .map((tick) => (
                  <span
                    key={tick.seconds}
                    aria-hidden="true"
                    style={{ left: `${tick.pct}%` }}
                    className="pointer-events-none absolute inset-y-0 w-px bg-border-subtle"
                  />
                ))}

              {/* Lower-ranked moments first so the top moments paint over them. */}
              {others.map((candidate) => {
                const { leftPct, widthPct } = spanGeometry(candidate, duration);
                const isSelected =
                  candidate.candidate_id === selected.candidate_id;
                return (
                  <span
                    key={candidate.candidate_id}
                    aria-hidden="true"
                    data-span-id={candidate.candidate_id}
                    style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                    className={`pointer-events-none absolute inset-y-0 min-w-0.5 ${
                      isSelected
                        ? "border-x-2 border-ink-primary bg-ink-tertiary/35"
                        : "bg-ink-tertiary/20"
                    }`}
                  />
                );
              })}
              {[...top].reverse().map((candidate) => {
                const { leftPct, widthPct } = spanGeometry(candidate, duration);
                const isSelected =
                  candidate.candidate_id === selected.candidate_id;
                const isHovered =
                  scrubMoment?.candidate_id === candidate.candidate_id;
                return (
                  <span
                    key={candidate.candidate_id}
                    aria-hidden="true"
                    data-span-id={candidate.candidate_id}
                    data-span-left-pct={leftPct.toFixed(2)}
                    data-span-width-pct={widthPct.toFixed(2)}
                    style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                    className={`pointer-events-none absolute inset-y-0 min-w-0.5 border-x border-accent ${
                      isSelected
                        ? "border-x-2 bg-accent/40"
                        : isHovered
                          ? "bg-accent/30"
                          : "bg-accent/15"
                    }`}
                  />
                );
              })}

              {/* Rank labels in track coordinates, pinned inside the track near its edges. */}
              {[...top].reverse().map((candidate) => {
                const { leftPct, widthPct } = spanGeometry(candidate, duration);
                const side = rankLabelAnchor(leftPct, widthPct);
                const position =
                  side === "left"
                    ? { left: "2px" }
                    : side === "right"
                      ? { right: "2px" }
                      : { left: `${leftPct + widthPct / 2}%` };
                return (
                  <span
                    key={candidate.candidate_id}
                    aria-hidden="true"
                    data-rank-label={candidate.candidate_id}
                    data-anchor={side}
                    style={position}
                    className={`pointer-events-none absolute top-1 rounded-pill bg-accent px-1 text-[10px] leading-4 font-semibold text-accent-ink tabular-nums shadow-2xs ${
                      side === "center" ? "-translate-x-1/2" : ""
                    }`}
                  >
                    {candidate.rank}
                  </span>
                );
              })}

              {/* Playhead */}
              {scrubVia !== null && (
                <span
                  aria-hidden="true"
                  data-testid="playhead"
                  style={{ left: `${cursorPct}%` }}
                  className="pointer-events-none absolute inset-y-0 z-10 w-0.5 -translate-x-1/2 bg-ink-primary"
                />
              )}
            </div>
          </div>

          {/* Rank chips: one button per top moment, packed into rows when they'd collide. */}
          <div
            role="group"
            aria-label="Top moments along the stream"
            className="relative mt-1 w-full"
            style={{ height: `${chipTrackPx}px` }}
          >
            {chipLayout.markers.map((marker) => {
              const { candidate } = marker;
              const isSelected =
                candidate.candidate_id === selected.candidate_id;
              const title = titleFor(candidate);
              return (
                <button
                  key={candidate.candidate_id}
                  ref={(node) => {
                    chipRefs.current[candidate.candidate_id] = node;
                  }}
                  type="button"
                  tabIndex={candidate.candidate_id === tabStopId ? 0 : -1}
                  data-lane={marker.lane}
                  aria-pressed={isSelected}
                  aria-label={`Rank #${candidate.rank}, ${spanLabel(candidate)}: ${title}`}
                  onClick={() => open(candidate)}
                  onKeyDown={(event) =>
                    handleChipKeyDown(event, candidate.candidate_id)
                  }
                  onFocus={(event) => {
                    void ensure();
                    // A mouse press also focuses the chip; only keyboard focus shows the label.
                    if (isFocusVisible(event.currentTarget)) {
                      showAt(
                        candidate.start_s,
                        "keyboard",
                        candidate.candidate_id,
                      );
                    }
                  }}
                  onBlur={() => hide("keyboard")}
                  onPointerEnter={() => {
                    void ensure();
                    showAt(
                      candidate.start_s,
                      "pointer",
                      candidate.candidate_id,
                    );
                  }}
                  onPointerLeave={() => hide("pointer")}
                  style={{
                    left: `${marker.hitLeftPct}%`,
                    width: `${marker.hitWidthPct}%`,
                    top: `${marker.lane * (CHIP_LANE_PX + CHIP_LANE_GAP_PX)}px`,
                    height: `${CHIP_LANE_PX}px`,
                  }}
                  className="group absolute flex items-center justify-center rounded-control focus-visible:ring-2 focus-visible:ring-focus-ring focus-visible:outline-none"
                >
                  {/* True span, aligned with the shaded span on the track above. */}
                  <span
                    aria-hidden="true"
                    style={{
                      left: `${marker.anchorOffsetInHitPct}%`,
                      width: `max(2px, ${marker.anchorSpanInHitPct}%)`,
                    }}
                    className="absolute top-0 h-0.5 rounded-pill bg-accent"
                  />
                  <span
                    className={`relative rounded-pill px-1.5 py-0.5 font-mono text-[11px] leading-none font-semibold tabular-nums motion-safe:transition-colors ${
                      isSelected
                        ? "bg-ink-primary text-surface-panel"
                        : "bg-accent text-accent-ink group-hover:bg-accent-hover"
                    }`}
                  >
                    #{candidate.rank}
                  </span>
                </button>
              );
            })}
          </div>

          <p id={hintId} className="text-xs text-ink-tertiary">
            <span className="sr-only">
              On the scrub bar, Left and Right arrows move through the stream,
              Page Up and Page Down jump between moments, and Enter opens the
              moment under the playhead. In the rank row, arrows move between
              moments.
            </span>
          </p>
        </div>
      </section>

      <div className="mx-auto w-full max-w-sm">
        <ol role="list">
          <ClipCard
            key={selected.candidate_id}
            jobId={jobId}
            candidate={selected}
            scoreFraction={scoreFraction}
            clip={clips[selected.candidate_id] ?? { kind: "pending" }}
            onRetry={() => onRetry(selected)}
            videoId={videoId}
            vod={vod}
          />
        </ol>
      </div>
    </div>
  );
}
