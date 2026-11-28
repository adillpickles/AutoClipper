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

  /**
   * Render inside a card whose top is at viewport y `cardTop` (the track is at y 500). The label
   * is `height` px tall (`sizerHeight` for its hidden tallest copy, the same unless given).
   */
  function placeIn(
    cardTop: number,
    height: number,
    leftPct = 50,
    {
      sizerHeight = height,
      momentLabel = null,
      cardWidth = 800,
    }: {
      sizerHeight?: number;
      momentLabel?: string | null;
      cardWidth?: number;
    } = {},
  ) {
    const card = document.createElement("section");
    const track = document.createElement("div");
    card.appendChild(track);
    document.body.appendChild(card);
    vi.spyOn(card, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: cardTop, width: cardWidth, height: 400 }),
    );
    vi.spyOn(track, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 500, width: cardWidth, height: 96 }),
    );
    // jsdom has no layout: give the label its size and the track as its offset parent.
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.dataset.testid === "scrub-preview-sizer"
          ? sizerHeight
          : height;
      },
    );
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(160);
    vi.spyOn(HTMLElement.prototype, "offsetParent", "get").mockReturnValue(
      track,
    );
    const view = (label: string | null) => (
      <ScrubPreview
        visible
        leftPct={leftPct}
        align="center"
        timeLabel="3:00"
        momentLabel={label}
        hint={label ? "Click to open" : null}
        source={null}
        timeS={180}
        onSourceError={vi.fn()}
        boundsRef={{ current: card }}
      />
    );
    const { rerender } = render(view(momentLabel), { container: track });
    return {
      label: screen.getByTestId("scrub-preview"),
      rerender: (label: string | null) => rerender(view(label)),
    };
  }

  it("sits above the track, centred on the playhead, when the card has room", () => {
    const { label } = placeIn(300, 120);
    expect(label.dataset.placement).toBe("above");
    expect(label.style.top).toBe("-128px");
    expect(label.style.left).toBe("320px");
    expect(label.style.translate).toBe("none");
  });

  it("stays inside the card, beside the playhead, when there isn't room above", () => {
    const { label } = placeIn(420, 137, 30);
    expect(label.dataset.placement).toBe("beside-right");
    // 8 px below the card's top: nothing above the timeline card is covered.
    expect(label.style.top).toBe("-72px");
    // 12 px clear of the playhead at 240 px.
    expect(label.style.left).toBe("252px");
  });

  it("keeps one placement while scrubbing on and off a moment (375 px)", () => {
    // 150 px above the track: the bare label (120 px) would fit, the label with a moment
    // (160 px) wouldn't. The hidden tallest copy (172 px) decides, so it never flips.
    const { label, rerender } = placeIn(350, 120, 12, {
      sizerHeight: 172,
      cardWidth: 343,
    });
    expect(label.dataset.placement).toBe("beside-right");
    const top = label.style.top;
    rerender("#1 · Huge clutch");
    expect(label.dataset.placement).toBe("beside-right");
    expect(label.style.top).toBe(top);
    rerender(null);
    expect(label.dataset.placement).toBe("beside-right");
  });

  it("clamps a centred label inside the card's left edge (320 px)", () => {
    // A 288 px card; centred at 19% the label would start 25 px before the card.
    const { label } = placeIn(300, 120, 19, { cardWidth: 288 });
    expect(label.dataset.placement).toBe("above");
    expect(label.style.left).toBe("8px");
  });

  it("clamps a label beside the playhead inside the card's right edge (320 px)", () => {
    // Beside the playhead at 40% it would end at 287 px, past the card's 280 px inner edge.
    const { label } = placeIn(420, 137, 40, { cardWidth: 288 });
    expect(label.dataset.placement).toBe("beside-right");
    expect(label.style.left).toBe("120px");
  });
});
