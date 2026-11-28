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
});
