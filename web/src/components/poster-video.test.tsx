// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PosterVideo } from "@/components/poster-video";

// jsdom can't decode video: the poster capture is stubbed.
const poster = vi.hoisted(() => ({
  clipPoster:
    vi.fn<(url: string, options?: { width?: number }) => Promise<string>>(),
}));

vi.mock("@/lib/clip-poster", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/clip-poster")>(
      "@/lib/clip-poster",
    );
  return { ...actual, clipPoster: poster.clipPoster };
});

function renderVideo(props: { src: string; posterWidth?: number }) {
  return render(
    <div className="relative">
      <PosterVideo label="Highlights video" {...props} />
    </div>,
  );
}

describe("PosterVideo", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    poster.clipPoster.mockReset();
  });

  it("shows a frame from inside the video before it plays, at the asked width", async () => {
    poster.clipPoster.mockResolvedValue("data:image/jpeg;base64,BBBB");
    const { container } = renderVideo({ src: "/v.mp4", posterWidth: 960 });
    const video = container.querySelector("video") as HTMLVideoElement;

    expect(video.getAttribute("preload")).toBe("none");
    expect(video.hasAttribute("controls")).toBe(true);
    expect(poster.clipPoster).toHaveBeenCalledWith("/v.mp4", { width: 960 });
    expect(screen.getByTestId("clip-frame-placeholder")).toBeTruthy();

    await act(async () => {});
    expect(video.getAttribute("poster")).toBe("data:image/jpeg;base64,BBBB");
    expect(screen.queryByTestId("clip-frame-placeholder")).toBeNull();
  });

  it("falls back to seeking into the video when no poster can be made, and rewinds on play", async () => {
    poster.clipPoster.mockRejectedValue(new Error("tainted canvas"));
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    const { container } = renderVideo({ src: "/v.mp4" });
    const video = container.querySelector("video") as HTMLVideoElement;
    await act(async () => {});
    expect(video.getAttribute("preload")).toBe("metadata");

    Object.defineProperty(video, "duration", { value: 30, configurable: true });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(1.5);
    fireEvent.seeked(video);
    expect(screen.queryByTestId("clip-frame-placeholder")).toBeNull();

    fireEvent.play(video);
    expect(video.currentTime).toBe(0);
  });

  it("replaces a video that fails to load with a note and a Retry that loads it again (#238)", async () => {
    poster.clipPoster.mockRejectedValue(new Error("network"));
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    const { container } = renderVideo({ src: "/v.mp4" });
    await act(async () => {});
    expect(screen.getByTestId("clip-frame-placeholder")).toBeTruthy();

    fireEvent.error(container.querySelector("video") as HTMLVideoElement);
    // The pulsing placeholder doesn't stay up forever.
    expect(screen.queryByTestId("clip-frame-placeholder")).toBeNull();
    expect(container.querySelector("video")).toBeNull();
    expect(screen.getByRole("status").textContent).toMatch(
      /can.t be played here/i,
    );

    poster.clipPoster.mockResolvedValue("data:image/jpeg;base64,CCCC");
    // jsdom won't focus a <video> without a tabindex (browsers do), so watch the call.
    const focus = vi.spyOn(HTMLMediaElement.prototype, "focus");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    const retried = container.querySelector("video") as HTMLVideoElement;
    expect(retried).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    // A fresh attempt: the poster is captured again.
    expect(poster.clipPoster).toHaveBeenCalledTimes(2);
    await act(async () => {});
    expect(retried.getAttribute("poster")).toBe("data:image/jpeg;base64,CCCC");
    // Retry removed its own button, so focus moves to the new player.
    expect(focus.mock.contexts).toContain(retried);

    // If the retry fails too, focus lands on the message, not <body>.
    fireEvent.error(retried);
    expect(document.activeElement).toBe(screen.getByRole("status"));
  });

  it("tells the parent about a failure and drops the placeholder when it handles it", async () => {
    poster.clipPoster.mockReturnValue(new Promise<string>(() => {}));
    const onFailed = vi.fn();
    const { container } = render(
      <div className="relative">
        <PosterVideo src="/v.mp4" label="Clip" onFailed={onFailed} />
      </div>,
    );
    fireEvent.error(container.querySelector("video") as HTMLVideoElement);
    expect(onFailed).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("clip-frame-placeholder")).toBeNull();
    // The parent decides what replaces it; until then the native video stays.
    expect(container.querySelector("video")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
