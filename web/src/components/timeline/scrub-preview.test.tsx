// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScrubPreview, SEEK_INTERVAL_MS } from "./scrub-preview";
import type { PreviewSource } from "./use-preview-media";

const SOURCE: PreviewSource = {
  candidateId: "c001",
  url: "/media/whole.mp4",
  startS: 100,
  endS: 400,
};

function preview(timeS: number) {
  return (
    <ScrubPreview
      visible
      leftPct={50}
      align="center"
      timeLabel="t"
      momentLabel={null}
      hint={null}
      source={SOURCE}
      timeS={timeS}
      onSourceError={vi.fn()}
    />
  );
}

/** Make the jsdom video look loaded and record every seek. */
function instrument(video: HTMLVideoElement) {
  const seeks: number[] = [];
  const state = { seeking: false, currentTime: 0 };
  Object.defineProperty(video, "readyState", { value: 1, configurable: true });
  Object.defineProperty(video, "seeking", {
    get: () => state.seeking,
    configurable: true,
  });
  Object.defineProperty(video, "currentTime", {
    get: () => state.currentTime,
    set: (value: number) => {
      state.currentTime = value;
      seeks.push(value);
    },
    configurable: true,
  });
  return { seeks, state };
}

describe("ScrubPreview seek throttle", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("seeks at most once per interval and lands on the latest position", () => {
    const { rerender } = render(preview(110));
    const video = screen.getByTestId("scrub-frame") as HTMLVideoElement;
    const { seeks } = instrument(video);

    // Metadata arrives: the pending position (VOD 110 s = 10 s into the file) is applied.
    fireEvent.loadedMetadata(video);
    expect(seeks).toEqual([10]);

    // Two quick moves inside the interval: neither seeks yet.
    rerender(preview(111));
    rerender(preview(112));
    expect(seeks).toEqual([10]);

    // After the interval only the latest position is applied.
    vi.advanceTimersByTime(SEEK_INTERVAL_MS);
    expect(seeks).toEqual([10, 12]);
  });

  it("waits for an in-flight seek to finish before the next one", () => {
    const { rerender } = render(preview(110));
    const video = screen.getByTestId("scrub-frame") as HTMLVideoElement;
    const { seeks, state } = instrument(video);
    fireEvent.loadedMetadata(video);
    expect(seeks).toEqual([10]);

    state.seeking = true;
    vi.advanceTimersByTime(SEEK_INTERVAL_MS * 3);
    rerender(preview(150));
    vi.advanceTimersByTime(SEEK_INTERVAL_MS * 3);
    expect(seeks).toEqual([10]);

    state.seeking = false;
    fireEvent.seeked(video);
    expect(seeks).toEqual([10, 50]);
  });

  it("keeps the same video element when the label hides and shows again", () => {
    const { rerender } = render(preview(110));
    const video = screen.getByTestId("scrub-frame");

    rerender(
      <ScrubPreview
        visible={false}
        leftPct={50}
        align="center"
        timeLabel="t"
        momentLabel={null}
        hint={null}
        source={null}
        timeS={110}
        onSourceError={vi.fn()}
      />,
    );
    expect(screen.getByTestId("scrub-frame")).toBe(video);
    expect(video.getAttribute("data-showing")).toBe("false");

    rerender(preview(120));
    expect(screen.getByTestId("scrub-frame")).toBe(video);
    expect(video.getAttribute("src")).toBe("/media/whole.mp4");
  });
});

describe("ScrubPreview placement", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  /** Render inside a card whose top is `cardTop` px above the track (viewport y 500). */
  function placeIn(cardTop: number, height: number, leftPct = 50) {
    const card = document.createElement("section");
    const track = document.createElement("div");
    card.appendChild(track);
    document.body.appendChild(card);
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: cardTop, width: 800, height: 400 }),
    );
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 500, width: 800, height: 96 }),
    );
    // jsdom has no layout: give the label its height and the track as its offset parent.
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(
      height,
    );
    vi.spyOn(HTMLElement.prototype, "offsetParent", "get").mockReturnValue(
      track,
    );
    render(
      <ScrubPreview
        visible
        leftPct={leftPct}
        align="center"
        timeLabel="3:00"
        momentLabel={null}
        hint={null}
        source={null}
        timeS={180}
        onSourceError={vi.fn()}
        boundsRef={{ current: card }}
      />,
      { container: track },
    );
    return screen.getByTestId("scrub-preview");
  }

  it("sits above the track when the card has room", () => {
    const label = placeIn(300, 120);
    expect(label.dataset.placement).toBe("above");
    expect(label.style.top).toBe("-128px");
    expect(label.style.translate).toBe("");
  });

  it("stays inside the card, beside the playhead, when there isn't room above", () => {
    const label = placeIn(420, 137, 30);
    expect(label.dataset.placement).toBe("beside-right");
    // 8 px below the card's top: nothing above the timeline card is covered.
    expect(label.style.top).toBe("-72px");
    expect(label.style.translate).toMatch(/^12px( 0(px)?)?$/);
  });
});
