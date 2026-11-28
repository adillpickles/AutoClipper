// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClipCard, type ClipExport } from "@/components/clip-card";
import type { Candidate, Export } from "@/lib/api/types";

// jsdom can't decode video: by default the poster never arrives.
const poster = vi.hoisted(() => ({
  clipPoster: vi.fn((url: string) => {
    void url;
    return new Promise<string>(() => {});
  }),
}));

vi.mock("@/lib/clip-poster", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/clip-poster")>(
      "@/lib/clip-poster",
    );
  return { ...actual, clipPoster: poster.clipPoster };
});

const RICH_CANDIDATE: Candidate = {
  candidate_id: "c001",
  rank: 1,
  score: 3.42,
  start_s: 118,
  end_s: 148,
  source: "both",
  n_viewer_clips: 3,
  top_signals: ["chat_msg_rate_z", "audio_rms_max_z"],
  title: "Loud audio & laughing chat",
  reason:
    "Chat jumped to 6× its usual rate with lots of laugh reactions; loudest moment of the stream; 3 viewer clips",
  signal_details: [
    {
      kind: "chat_rate",
      label: "Chat rate",
      detail: "6× usual stream rate (z=3.4)",
    },
    {
      kind: "audio_loudness",
      label: "Audio loudness",
      detail: "Loudest moment of the stream (z=2.6)",
    },
    {
      kind: "viewer_clips",
      label: "Viewer clips",
      detail: "3 viewer clips overlap this moment",
    },
  ],
};

describe("ClipCard explanations", () => {
  afterEach(() => {
    cleanup();
  });

  it("shows the clip title, one-line reason, and toggles the Why breakdown", () => {
    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={RICH_CANDIDATE}
          scoreFraction={1}
          clip={{ kind: "pending" }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    expect(
      screen.getByRole("heading", {
        level: 3,
        name: "Loud audio & laughing chat",
      }),
    ).toBeTruthy();
    expect(
      screen.getByText(
        "Chat jumped to 6× its usual rate with lots of laugh reactions; loudest moment of the stream; 3 viewer clips",
      ),
    ).toBeTruthy();

    const whyButton = screen.getByRole("button", { name: /Why/i });
    expect(whyButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("6× usual stream rate (z=3.4)")).toBeNull();

    fireEvent.click(whyButton);
    expect(whyButton.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Chat rate")).toBeTruthy();
    expect(screen.getByText("6× usual stream rate (z=3.4)")).toBeTruthy();
    expect(
      screen.getByText("Loudest moment of the stream (z=2.6)"),
    ).toBeTruthy();
    expect(screen.getByText("3 viewer clips overlap this moment")).toBeTruthy();

    fireEvent.click(whyButton);
    expect(whyButton.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("6× usual stream rate (z=3.4)")).toBeNull();
  });

  it("falls back cleanly for legacy 1.0 candidates without explanation fields", () => {
    const legacy: Candidate = {
      candidate_id: "c002",
      rank: 2,
      score: 2.1,
      start_s: 40,
      end_s: 70,
      source: "discovered",
      n_viewer_clips: 0,
      top_signals: ["audio_rms_max_z"],
    };

    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={legacy}
          scoreFraction={0.6}
          clip={{ kind: "pending" }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    expect(
      screen.getByRole("heading", { level: 3, name: "Loud audio spike" }),
    ).toBeTruthy();
    expect(screen.getByText("The streamer got loud")).toBeTruthy();
    const whyButton = screen.getByRole("button", { name: /Why/i });
    fireEvent.click(whyButton);
    expect(screen.getByText("Audio loudness")).toBeTruthy();
  });

  it("omits the Why toggle when there are no signals or viewer clips", () => {
    const quiet: Candidate = {
      candidate_id: "c003",
      rank: 3,
      score: 0.2,
      start_s: 10,
      end_s: 35,
      source: "discovered",
      n_viewer_clips: 0,
      top_signals: [],
    };

    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={quiet}
          scoreFraction={0.1}
          clip={{ kind: "pending" }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    expect(screen.getByText("Steady activity throughout")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Why/i })).toBeNull();
  });

  it("uses the clean server export filename on the download link", () => {
    render(
      <ul>
        <ClipCard
          jobId="job_1"
          candidate={RICH_CANDIDATE}
          scoreFraction={1}
          clip={{
            kind: "export",
            url: "/api/v1/exports/exp_1/file",
            export: {
              export_id: "exp_1",
              job_id: "job_1",
              candidate_id: "c001",
              video_id: "9000000001",
              start_s: 118,
              end_s: 148,
              layout: "center_crop",
              captions: true,
              state: "done",
              progress: null,
              error: null,
              download_url: "/api/v1/exports/exp_1/file",
              filename:
                "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-58.mp4",
              created_at: "2026-10-19T15:05:00Z",
              started_at: "2026-10-19T15:05:01Z",
              updated_at: "2026-10-19T15:05:48Z",
              finished_at: "2026-10-19T15:05:48Z",
            },
          }}
          videoId="9000000001"
          onRetry={vi.fn()}
        />
      </ul>,
    );

    const link = screen.getByRole("link", { name: /Download/i });
    expect(link.getAttribute("download")).toBe(
      "user_001_2026-10-12_Loud-audio-laughing-chat_00-01-58.mp4",
    );
  });
});

function exportIn(
  state: Export["state"],
  overrides: Partial<Export> = {},
): Export {
  return {
    export_id: "exp_1",
    job_id: "job_1",
    candidate_id: "c001",
    video_id: "9000000001",
    start_s: 118,
    end_s: 148,
    layout: "center_crop",
    captions: true,
    state,
    progress: null,
    error: null,
    download_url: state === "done" ? "/api/v1/exports/exp_1/file" : null,
    created_at: "2026-10-19T15:05:00Z",
    started_at: null,
    updated_at: "2026-10-19T15:05:00Z",
    finished_at: null,
    ...overrides,
  };
}

function renderCard(clip: ClipExport) {
  return render(
    <ul>
      <ClipCard
        jobId="job_1"
        candidate={RICH_CANDIDATE}
        scoreFraction={1}
        clip={clip}
        videoId="9000000001"
        onRetry={vi.fn()}
      />
    </ul>,
  );
}

describe("ClipCard media (#185)", () => {
  afterEach(() => {
    cleanup();
    poster.clipPoster.mockReset();
  });

  it("shows a frame of the clip before it plays instead of a black box (#217)", async () => {
    let resolvePoster: (url: string) => void = () => {};
    poster.clipPoster.mockReturnValue(
      new Promise<string>((resolve) => {
        resolvePoster = resolve;
      }),
    );
    const url = "http://127.0.0.1:8000/api/v1/exports/exp_1/file";
    const { container } = renderCard({
      kind: "export",
      export: exportIn("done"),
      url,
    });

    const video = container.querySelector("video");
    // The card never preloads the video itself: a placeholder covers it until the poster is in.
    expect(video?.getAttribute("preload")).toBe("none");
    expect(poster.clipPoster).toHaveBeenCalledWith(url, { width: undefined });
    expect(screen.getByTestId("clip-frame-placeholder")).toBeTruthy();

    await act(async () => resolvePoster("data:image/jpeg;base64,AAAA"));
    expect(video?.getAttribute("poster")).toBe("data:image/jpeg;base64,AAAA");
    expect(screen.queryByTestId("clip-frame-placeholder")).toBeNull();
  });

  it("falls back to preloading the video and seeking to a frame when no poster can be made", async () => {
    poster.clipPoster.mockRejectedValue(new Error("tainted canvas"));
    const load = vi
      .spyOn(HTMLMediaElement.prototype, "load")
      .mockImplementation(() => {});
    const { container } = renderCard({
      kind: "export",
      export: exportIn("done"),
      url: "http://127.0.0.1:8000/api/v1/exports/exp_1/file",
    });
    const video = container.querySelector("video") as HTMLVideoElement;
    await act(async () => {});
    expect(video.getAttribute("preload")).toBe("metadata");

    Object.defineProperty(video, "duration", { value: 30 });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(1.5);
    fireEvent.seeked(video);
    expect(screen.queryByTestId("clip-frame-placeholder")).toBeNull();
    // The first play starts at the beginning of the clip, not at the thumbnail frame.
    fireEvent.play(video);
    expect(video.currentTime).toBe(0);
    expect(load).toHaveBeenCalled();
    load.mockRestore();
  });

  it("plays the finished vertical export, letterboxed rather than cropped", () => {
    const { container } = renderCard({
      kind: "export",
      export: exportIn("done"),
      url: "http://127.0.0.1:8000/api/v1/exports/exp_1/file",
    });

    const video = container.querySelector("video");
    expect(video).not.toBeNull();
    expect(video?.getAttribute("src")).toBe(
      "http://127.0.0.1:8000/api/v1/exports/exp_1/file",
    );
    // An author object-fit wins over the browser's fullscreen default, so `object-cover` would
    // crop the 9:16 clip to a landscape band in fullscreen.
    expect(video?.classList.contains("object-contain")).toBe(true);
    expect(video?.classList.contains("object-cover")).toBe(false);
  });

  it.each<[string, ClipExport, RegExp]>([
    ["no export yet", { kind: "pending" }, /Waiting to render/],
    [
      "a queued export",
      { kind: "export", export: exportIn("queued"), url: null },
      /Waiting to render/,
    ],
    [
      "a running export",
      {
        kind: "export",
        export: exportIn("running", {
          progress: {
            stage: "rendering",
            stage_index: 3,
            stage_count: 3,
            stage_fraction: null,
            message: "Rendering clip",
          },
        }),
        url: null,
      },
      /^Rendering clip$/,
    ],
    [
      "a failed export",
      {
        kind: "export",
        export: exportIn("failed", {
          error: {
            code: "render_failed",
            message: "FFmpeg failed.",
            details: null,
          },
        }),
        url: null,
      },
      /FFmpeg failed\./,
    ],
    [
      "unavailable media",
      { kind: "unavailable", message: "Export isn't available." },
      /Export isn't available\./,
    ],
    [
      "a request error",
      { kind: "error", message: "Couldn't start the export." },
      /Couldn't start the export\./,
    ],
    [
      "a done export without a file URL",
      {
        kind: "export",
        export: exportIn("done", { download_url: null }),
        url: null,
      },
      /Rendering…/,
    ],
  ])("shows a status instead of any video for %s", (_name, clip, text) => {
    const { container } = renderCard(clip);

    expect(container.querySelector("video")).toBeNull();
    expect(screen.getByText(text)).toBeTruthy();
  });
});
