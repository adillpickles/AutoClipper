// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ClipExport } from "@/components/clip-card";
import { StreamTimeline } from "@/components/stream-timeline";
import type { Candidate, CandidateMedia, Export } from "@/lib/api/types";

const api = vi.hoisted(() => ({
  getCandidateMedia: vi.fn(),
  resolveUrl: vi.fn((url: string) => `http://api.test${url}`),
}));

vi.mock("@/lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  getApiClient: () => api,
}));

const OVERLAPPING_CANDIDATES: Candidate[] = [
  {
    candidate_id: "c001",
    rank: 1,
    score: 3.4,
    start_s: 118,
    end_s: 148,
    source: "both",
    n_viewer_clips: 3,
    top_signals: ["chat_msg_rate_z", "audio_rms_max_z"],
    title: "Loud audio & laughing chat",
  },
  {
    candidate_id: "c002",
    rank: 2,
    score: 2.9,
    start_s: 130,
    end_s: 165,
    source: "discovered",
    n_viewer_clips: 0,
    top_signals: ["audio_rms_max_z"],
    title: "Loud audio spike",
  },
  {
    candidate_id: "c003",
    rank: 3,
    score: 1.5,
    start_s: 260,
    end_s: 290,
    source: "viewer_clips",
    n_viewer_clips: 1,
    top_signals: [],
  },
];

function media(
  candidateId: string,
  preview: Partial<CandidateMedia["preview"]>,
): CandidateMedia {
  return {
    job_id: "job_1",
    candidate_id: candidateId,
    video_id: "9000000001",
    preview: {
      status: "available",
      kind: "video_file",
      url: "/api/v1/media/med_whole",
      media_start_s: 0,
      media_end_s: 360,
      reason: null,
      message: null,
      ...preview,
    },
    export: {
      status: "available",
      reason: null,
      message: null,
      min_start_s: 0,
      max_end_s: 360,
      max_duration_s: 90,
      layouts: ["center_crop"],
    },
  };
}

function readyClip(candidate: Candidate): ClipExport {
  const exp: Export = {
    export_id: `exp_${candidate.candidate_id}`,
    job_id: "job_1",
    candidate_id: candidate.candidate_id,
    video_id: "9000000001",
    start_s: candidate.start_s,
    end_s: candidate.end_s,
    layout: "center_crop",
    captions: true,
    state: "done",
    progress: null,
    error: null,
    download_url: `/mock-media/${candidate.candidate_id}.mp4`,
    created_at: "2026-11-20T12:00:00Z",
    started_at: "2026-11-20T12:00:01Z",
    updated_at: "2026-11-20T12:00:02Z",
    finished_at: "2026-11-20T12:00:02Z",
  };
  return { kind: "export", export: exp, url: exp.download_url };
}

function renderTimeline(
  overrides: Partial<
    Omit<Parameters<typeof StreamTimeline>[0], "onSelect">
  > = {},
) {
  const onSelect = vi.fn();
  const props = {
    jobId: "job_1",
    videoId: "9000000001",
    durationS: 360,
    candidates: OVERLAPPING_CANDIDATES,
    selectedId: "c001" as string | null,
    clips: {} as Record<string, ClipExport>,
    onRetry: vi.fn(),
    ...overrides,
    onSelect,
  };
  const view = render(<StreamTimeline {...props} />);
  return { ...view, props, onSelect };
}

/** Give the scrub track a 360 px box at x = 0 so 1 px = 1 s on a 360 s VOD. */
function sizeTrack(): HTMLElement {
  const track = screen.getByTestId("scrub-track");
  track.getBoundingClientRect = () =>
    ({
      left: 0,
      width: 360,
      top: 0,
      height: 80,
      right: 360,
      bottom: 80,
    }) as DOMRect;
  return track;
}

async function flushPromises() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("StreamTimeline", () => {
  beforeEach(() => {
    api.getCandidateMedia.mockReset();
    api.getCandidateMedia.mockImplementation((_job: string, id: string) =>
      Promise.resolve(media(id, {})),
    );
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("draws each moment's span at its true time with a rank label", () => {
    renderTimeline();

    const span1 = document.querySelector(
      '[data-span-id="c001"]',
    ) as HTMLElement;
    const span3 = document.querySelector(
      '[data-span-id="c003"]',
    ) as HTMLElement;
    expect(span1.getAttribute("data-span-left-pct")).toBe(
      ((118 / 360) * 100).toFixed(2),
    );
    expect(span1.getAttribute("data-span-width-pct")).toBe(
      ((30 / 360) * 100).toFixed(2),
    );
    expect(
      document.querySelector('[data-rank-label="c001"]')?.textContent,
    ).toBe("1");
    expect(Number.parseFloat(span3.style.left)).toBeCloseTo(
      (260 / 360) * 100,
      3,
    );

    // Overlapping c001 and c002 rank chips sit in different rows; c003 reuses row 0.
    expect(
      screen
        .getByRole("button", { name: /^Rank #1/ })
        .getAttribute("data-lane"),
    ).toBe("0");
    expect(
      screen
        .getByRole("button", { name: /^Rank #2/ })
        .getAttribute("data-lane"),
    ).toBe("1");
    expect(
      screen
        .getByRole("button", { name: /^Rank #3/ })
        .getAttribute("data-lane"),
    ).toBe("0");

    // The legend explains the spans; no made-up activity curve is drawn.
    const legend = screen.getByRole("list", { name: "Timeline legend" });
    expect(legend.textContent).toContain("Top moment (#1 = best)");
    expect(legend.textContent).not.toMatch(/chat|loudness/i);
    expect(document.querySelector("svg path")).toBeNull();
  });

  it("keeps rank labels inside the track at the stream's start and end", () => {
    const edges: Candidate[] = [
      { ...OVERLAPPING_CANDIDATES[0], start_s: 0, end_s: 8 },
      { ...OVERLAPPING_CANDIDATES[1], start_s: 352, end_s: 360 },
      OVERLAPPING_CANDIDATES[2],
    ];
    renderTimeline({ candidates: edges });

    const first = document.querySelector(
      '[data-rank-label="c001"]',
    ) as HTMLElement;
    const last = document.querySelector(
      '[data-rank-label="c002"]',
    ) as HTMLElement;
    const middle = document.querySelector(
      '[data-rank-label="c003"]',
    ) as HTMLElement;
    expect(first.getAttribute("data-anchor")).toBe("left");
    expect(first.style.left).toBe("2px");
    expect(last.getAttribute("data-anchor")).toBe("right");
    expect(last.style.right).toBe("2px");
    expect(middle.getAttribute("data-anchor")).toBe("center");
  });

  it("counts a moment that runs to the end when the playhead is at End", () => {
    const tail: Candidate[] = [
      { ...OVERLAPPING_CANDIDATES[0], start_s: 330, end_s: 360 },
    ];
    const { onSelect } = renderTimeline({ candidates: tail });
    const slider = screen.getByRole("slider");

    fireEvent.keyDown(slider, { key: "End" });
    expect(slider.getAttribute("aria-valuenow")).toBe("360");
    expect(slider.getAttribute("aria-valuetext")).toContain("inside moment #1");
    fireEvent.keyDown(slider, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(tail[0]);

    // The track's right edge maps to the end too.
    onSelect.mockClear();
    fireEvent.click(sizeTrack(), { clientX: 360 });
    expect(onSelect).toHaveBeenCalledWith(tail[0]);
  });

  it("opens the moment under the pointer when the track is clicked", () => {
    const { onSelect } = renderTimeline();
    const track = sizeTrack();

    // 140 s is inside both #1 and #2; the better rank wins.
    fireEvent.click(track, { clientX: 140 });
    expect(onSelect).toHaveBeenLastCalledWith(OVERLAPPING_CANDIDATES[0]);

    fireEvent.click(track, { clientX: 275 });
    expect(onSelect).toHaveBeenLastCalledWith(OVERLAPPING_CANDIDATES[2]);

    // Empty stretch: nothing to open.
    onSelect.mockClear();
    fireEvent.click(track, { clientX: 20 });
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("doesn't open a moment on the click that ends a drag, but does on a plain tap", () => {
    const { onSelect } = renderTimeline();
    const track = sizeTrack();

    // Drag from an empty stretch across #3 and release on it (touch or mouse).
    fireEvent.pointerDown(track, { clientX: 200, pointerId: 1 });
    fireEvent.pointerMove(track, { clientX: 240, pointerId: 1 });
    fireEvent.pointerMove(track, { clientX: 275, pointerId: 1 });
    fireEvent.pointerUp(track, { clientX: 275, pointerId: 1 });
    fireEvent.pointerLeave(track, { pointerId: 1 });
    fireEvent.click(track, { clientX: 275 });
    expect(onSelect).not.toHaveBeenCalled();

    // A tap with only a little jitter still opens the moment.
    fireEvent.pointerDown(track, { clientX: 275, pointerId: 2 });
    fireEvent.pointerMove(track, { clientX: 277, pointerId: 2 });
    fireEvent.pointerUp(track, { clientX: 277, pointerId: 2 });
    fireEvent.click(track, { clientX: 277 });
    expect(onSelect).toHaveBeenCalledWith(OVERLAPPING_CANDIDATES[2]);
  });

  it("captures the pointer while dragging so scrubbing continues past the track's edges", () => {
    renderTimeline();
    const track = sizeTrack();
    const capture = vi.fn();
    track.setPointerCapture = capture;

    fireEvent.pointerDown(track, { clientX: 100, pointerId: 7 });
    expect(capture).toHaveBeenCalledWith(7);
    // Past the right edge the playhead clamps to the end.
    fireEvent.pointerMove(track, { clientX: 900, pointerId: 7 });
    expect(screen.getByTestId("scrub-preview").textContent).toContain("6:00");
  });

  it("shows the time and moment title while hovering, and hides it on leave", async () => {
    renderTimeline();
    const track = sizeTrack();

    fireEvent.pointerEnter(track);
    fireEvent.pointerMove(track, { clientX: 150 });
    const preview = screen.getByTestId("scrub-preview");
    expect(preview.textContent).toContain("2:30");
    expect(preview.textContent).toContain("#2 · Loud audio spike");
    expect(preview.textContent).toContain("Click to open");
    expect(
      Number.parseFloat(screen.getByTestId("playhead").style.left),
    ).toBeCloseTo((150 / 360) * 100, 3);

    fireEvent.pointerMove(track, { clientX: 20 });
    expect(screen.getByTestId("scrub-preview").textContent).not.toContain("#");

    fireEvent.pointerLeave(track);
    expect(
      screen.getByTestId("scrub-preview").getAttribute("data-visible"),
    ).toBe("false");
    expect(screen.queryByTestId("playhead")).toBeNull();
    await flushPromises();
  });

  it("scrubs and opens moments from the keyboard on the scrub bar", () => {
    const { onSelect } = renderTimeline({ selectedId: null });
    const slider = screen.getByRole("slider", {
      name: "Scrub through the stream",
    });

    // Starts at the selected (top-ranked) moment.
    expect(slider.getAttribute("aria-valuenow")).toBe("118");
    expect(slider.getAttribute("aria-valuetext")).toContain("inside moment #1");

    fireEvent.keyDown(slider, { key: "PageDown" });
    expect(slider.getAttribute("aria-valuenow")).toBe("130");
    fireEvent.keyDown(slider, { key: "PageDown" });
    expect(slider.getAttribute("aria-valuenow")).toBe("260");
    expect(screen.getByTestId("scrub-preview").textContent).toContain(
      "Press Enter to open",
    );

    fireEvent.keyDown(slider, { key: "Enter" });
    expect(onSelect).toHaveBeenLastCalledWith(OVERLAPPING_CANDIDATES[2]);

    fireEvent.keyDown(slider, { key: "Home" });
    expect(slider.getAttribute("aria-valuenow")).toBe("0");
    expect(slider.getAttribute("aria-valuetext")).toContain(
      "no ranked moment here",
    );
    onSelect.mockClear();
    fireEvent.keyDown(slider, { key: " " });
    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.keyDown(slider, { key: "ArrowRight" });
    expect(slider.getAttribute("aria-valuenow")).toBe("4"); // 1 % of 360 s, rounded
    fireEvent.keyDown(slider, { key: "End" });
    expect(slider.getAttribute("aria-valuenow")).toBe("360");
  });

  it("moves between rank chips with arrows, keeps one tab stop, and opens on click", () => {
    const { onSelect } = renderTimeline();
    const chip1 = screen.getByRole("button", { name: /^Rank #1/ });
    const chip2 = screen.getByRole("button", { name: /^Rank #2/ });
    const chip3 = screen.getByRole("button", { name: /^Rank #3/ });

    expect(chip1.tabIndex).toBe(0);
    expect(chip2.tabIndex).toBe(-1);
    expect(chip1.getAttribute("aria-pressed")).toBe("true");

    chip1.focus();
    fireEvent.keyDown(chip1, { key: "ArrowRight" });
    expect(document.activeElement).toBe(chip2);
    fireEvent.keyDown(chip2, { key: "End", ctrlKey: true });
    expect(document.activeElement).toBe(chip2);
    fireEvent.keyDown(chip2, { key: "End" });
    expect(document.activeElement).toBe(chip3);
    fireEvent.keyDown(chip3, { key: "Home" });
    expect(document.activeElement).toBe(chip1);
    expect(onSelect).not.toHaveBeenCalled();

    fireEvent.click(chip3);
    expect(onSelect).toHaveBeenCalledWith(OVERLAPPING_CANDIDATES[2]);
  });

  it("shows the chip's label on keyboard focus but not when a mouse press focuses it", () => {
    let focusVisible = false;
    const original = Element.prototype.matches;
    const spy = vi
      .spyOn(Element.prototype, "matches")
      .mockImplementation(function (this: Element, selector: string) {
        return selector === ":focus-visible"
          ? focusVisible
          : original.call(this, selector);
      });
    try {
      renderTimeline();
      const chip2 = screen.getByRole("button", { name: /^Rank #2/ });
      const preview = () =>
        screen.getByTestId("scrub-preview").getAttribute("data-visible");

      fireEvent.focus(chip2);
      expect(preview()).toBe("false");

      fireEvent.blur(chip2);
      focusVisible = true;
      fireEvent.focus(chip2);
      expect(preview()).toBe("true");
      expect(screen.getByTestId("scrub-preview").textContent).toContain(
        "#2 · Loud audio spike",
      );
    } finally {
      spy.mockRestore();
    }
  });

  it("opens the selected moment's clip card below the timeline", () => {
    const clips: Record<string, ClipExport> = {
      c001: readyClip(OVERLAPPING_CANDIDATES[0]),
      c002: readyClip(OVERLAPPING_CANDIDATES[1]),
    };
    const { rerender, props } = renderTimeline({ clips });

    fireEvent.error(screen.getByLabelText(/^Clip 1,/));
    expect(screen.getByText(/ready but can't be played here/i)).toBeTruthy();

    rerender(<StreamTimeline {...props} selectedId="c002" />);

    expect(
      screen
        .getByRole("button", { name: /^Rank #2/ })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(screen.queryByText(/ready but can't be played here/i)).toBeNull();
    expect(screen.getByLabelText(/^Clip 2,/).getAttribute("src")).toBe(
      "/mock-media/c002.mp4",
    );
    expect(
      screen.getByRole("link", { name: /Edit clip 2/ }).getAttribute("href"),
    ).toBe("/jobs/job_1/results/c002");
  });

  it("shows a preview frame while scrubbing when one preview covers the whole VOD", async () => {
    renderTimeline();
    const track = sizeTrack();

    fireEvent.pointerEnter(track);
    await flushPromises();
    // Best rank first, and it covers the whole VOD, so no further requests.
    expect(api.getCandidateMedia).toHaveBeenCalledTimes(1);
    expect(api.getCandidateMedia).toHaveBeenCalledWith("job_1", "c001");

    fireEvent.pointerMove(track, { clientX: 20 });
    const frame = screen.getByTestId("scrub-frame") as HTMLVideoElement;
    expect(frame.getAttribute("src")).toBe(
      "http://api.test/api/v1/media/med_whole",
    );
    expect(frame.muted).toBe(true);

    // Leaving keeps the same video mounted (hidden), so hovering again doesn't reload it.
    fireEvent.pointerLeave(track);
    expect(screen.getByTestId("scrub-frame")).toBe(frame);
    fireEvent.pointerMove(track, { clientX: 30 });
    expect(screen.getByTestId("scrub-frame")).toBe(frame);
    expect(frame.getAttribute("data-showing")).toBe("true");

    // A file that fails to load falls back to the text-only label.
    fireEvent.error(frame);
    expect(frame.getAttribute("data-showing")).toBe("false");
    expect(screen.getByTestId("scrub-preview").textContent).toContain("0:30");
  });

  it("unloads the preview video when the timeline goes away", async () => {
    const { unmount } = renderTimeline();
    const track = sizeTrack();
    fireEvent.pointerEnter(track);
    await flushPromises();
    fireEvent.pointerMove(track, { clientX: 20 });
    const frame = screen.getByTestId("scrub-frame") as HTMLVideoElement;
    const load = vi.fn();
    frame.load = load;
    frame.pause = vi.fn();

    unmount();
    expect(frame.hasAttribute("src")).toBe(false);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("stops asking about previews after one is still preparing, until later", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      api.getCandidateMedia.mockImplementation((_job: string, id: string) =>
        Promise.resolve(
          media(id, {
            status: "preparing",
            kind: null,
            url: null,
            media_start_s: null,
            media_end_s: null,
          }),
        ),
      );
      renderTimeline();
      const track = sizeTrack();

      fireEvent.pointerEnter(track);
      await flushPromises();
      expect(api.getCandidateMedia).toHaveBeenCalledTimes(1);

      // Another hover right away asks nothing: the local file is still being prepared.
      fireEvent.pointerLeave(track);
      fireEvent.pointerEnter(track);
      await flushPromises();
      expect(api.getCandidateMedia).toHaveBeenCalledTimes(1);

      // Later, it asks again, starting with the same moment; now the whole file is ready.
      vi.setSystemTime(Date.now() + 5000);
      api.getCandidateMedia.mockImplementation((_job: string, id: string) =>
        Promise.resolve(media(id, {})),
      );
      fireEvent.pointerEnter(track);
      await flushPromises();
      expect(api.getCandidateMedia).toHaveBeenCalledTimes(2);
      expect(api.getCandidateMedia).toHaveBeenLastCalledWith("job_1", "c001");
    } finally {
      vi.useRealTimers();
    }
  });

  it("uses segment previews only where they cover the scrubbed time", async () => {
    api.getCandidateMedia.mockImplementation((_job: string, id: string) =>
      Promise.resolve(
        id === "c001"
          ? media(id, {
              url: "/api/v1/media/med_seg1",
              media_start_s: 100,
              media_end_s: 170,
            })
          : id === "c003"
            ? media(id, {
                url: "/api/v1/media/med_seg3",
                media_start_s: 250,
                media_end_s: 300,
              })
            : media(id, {
                status: "unavailable",
                kind: null,
                url: null,
                media_start_s: null,
                media_end_s: null,
                reason: "fetch_failed",
              }),
      ),
    );
    renderTimeline();
    const track = sizeTrack();

    fireEvent.pointerEnter(track);
    await flushPromises();
    await flushPromises();
    expect(api.getCandidateMedia).toHaveBeenCalledTimes(3);

    fireEvent.pointerMove(track, { clientX: 270 });
    expect(
      (screen.getByTestId("scrub-frame") as HTMLVideoElement).getAttribute(
        "src",
      ),
    ).toBe("http://api.test/api/v1/media/med_seg3");

    fireEvent.pointerMove(track, { clientX: 140 });
    expect(
      (screen.getByTestId("scrub-frame") as HTMLVideoElement).getAttribute(
        "src",
      ),
    ).toBe("http://api.test/api/v1/media/med_seg1");

    // No preview covers 0:20: text-only label, the video stays mounted but hidden.
    fireEvent.pointerMove(track, { clientX: 20 });
    expect(screen.getByTestId("scrub-frame").getAttribute("data-showing")).toBe(
      "false",
    );
    expect(screen.getByTestId("scrub-preview").textContent).toContain("0:20");
  });

  it("shows a text-only label when preview media is unavailable or the request fails", async () => {
    api.getCandidateMedia.mockImplementation((_job: string, id: string) =>
      id === "c001"
        ? Promise.reject(new Error("network down"))
        : Promise.resolve(
            media(id, {
              status: "unavailable",
              kind: null,
              url: null,
              media_start_s: null,
              media_end_s: null,
              reason: "source_unavailable",
            }),
          ),
    );
    renderTimeline();
    const track = sizeTrack();

    fireEvent.pointerEnter(track);
    await flushPromises();
    await flushPromises();
    fireEvent.pointerMove(track, { clientX: 140 });

    expect(screen.queryByTestId("scrub-frame")).toBeNull();
    expect(screen.getByTestId("scrub-preview").textContent).toContain(
      "#1 · Loud audio & laughing chat",
    );
  });

  it("draws no activity curve or legend entries when the result has none", () => {
    renderTimeline();

    expect(screen.queryByTestId("activity-curve")).toBeNull();
    expect(screen.queryByTestId("activity-summary")).toBeNull();
    const legend = screen.getByRole("list", { name: "Timeline legend" });
    expect(legend.textContent).not.toContain("Chat rate");
    expect(legend.textContent).not.toContain("Loudness");
  });

  it("draws measured chat and loudness behind the spans, with gaps", () => {
    const chat = Array.from({ length: 36 }, (_, i) => (i === 12 ? 8 : 1));
    const loudness: (number | null)[] = Array.from({ length: 36 }, () => -30);
    loudness[20] = null; // a muted stretch
    loudness[21] = null;
    renderTimeline({ activity: { step_s: 10, chat, loudness } });

    const curve = screen.getByTestId("activity-curve");
    expect(curve.getAttribute("aria-hidden")).toBe("true");
    expect(curve.querySelectorAll('[data-activity="chat"]')).toHaveLength(1);
    // The gap splits the loudness line in two.
    expect(curve.querySelectorAll('[data-activity="loudness"]')).toHaveLength(
      2,
    );
    const legend = screen.getByRole("list", { name: "Timeline legend" });
    expect(legend.textContent).toContain("Chat rate");
    expect(legend.textContent).toContain("Loudness");
    expect(screen.getByTestId("activity-summary").textContent).toContain(
      "chat is busiest around 2:05, at 8.0 messages a second",
    );
  });

  it("draws only the family that arrived", () => {
    renderTimeline({
      activity: { step_s: 10, chat: null, loudness: Array(36).fill(-25) },
    });

    const curve = screen.getByTestId("activity-curve");
    expect(curve.querySelectorAll('[data-activity="chat"]')).toHaveLength(0);
    expect(curve.querySelectorAll('[data-activity="loudness"]')).toHaveLength(
      1,
    );
    const legend = screen.getByRole("list", { name: "Timeline legend" });
    expect(legend.textContent).not.toContain("Chat rate");
    expect(screen.getByTestId("activity-summary").textContent).toContain(
      "No chat was measured.",
    );
  });

  it("renders an empty state when there are no candidates", () => {
    renderTimeline({ candidates: [], selectedId: null, videoId: "9000000006" });

    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "No moments on this timeline",
      }),
    ).toBeTruthy();
    expect(screen.queryByRole("slider")).toBeNull();
    expect(api.getCandidateMedia).not.toHaveBeenCalled();
  });
});
