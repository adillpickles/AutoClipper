// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StartForm } from "@/components/start-form";
import { ApiRequestError } from "@/lib/api/errors";

const state = vi.hoisted(() => ({
  push: vi.fn(),
  createJob: vi.fn(),
  uploadVideo: vi.fn(),
  mockMode: true,
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: state.push }),
}));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    isMockMode: () => state.mockMode,
    getApiClient: () => ({
      createJob: state.createJob,
      uploadVideo: state.uploadVideo,
    }),
  };
});

describe("StartForm", () => {
  afterEach(() => {
    cleanup();
    state.mockMode = true;
    vi.clearAllMocks();
  });

  it("fills the VOD field from an example and names the parsed ID", () => {
    render(<StartForm />);

    fireEvent.click(screen.getByRole("button", { name: "Full link" }));

    const input = screen.getByLabelText("Twitch VOD link") as HTMLInputElement;
    expect(input.value).toBe("https://www.twitch.tv/videos/2345678901");
    expect(screen.getByText(/Ready to analyze VOD 2345678901/)).toBeTruthy();
    expect(
      screen.getByText(/Twitch VOD link above is still required/i),
    ).toBeTruthy();
  });

  it("explains a channel link and does not start a job", () => {
    render(<StartForm />);

    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "https://www.twitch.tv/somechannel" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    expect(screen.getByRole("alert").textContent).toMatch(/channel page/);
    expect(
      screen
        .getByRole("link", { name: /Open somechannel in Channels/ })
        .getAttribute("href"),
    ).toBe("/channels?channel=somechannel");
    expect(state.createJob).not.toHaveBeenCalled();
  });

  it("links to the channel browser from the form header", () => {
    render(<StartForm />);
    const link = screen.getByRole("link", { name: /Browse a channel's VODs/ });
    expect(link.getAttribute("href")).toBe("/channels");
  });

  it("rejects a relative file path and accepts an absolute example", () => {
    render(<StartForm />);

    fireEvent.click(
      screen.getByRole("button", { name: "Use file path on this computer" }),
    );
    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "2345678901" },
    });
    fireEvent.change(screen.getByLabelText(/VOD file on this computer/), {
      target: { value: "vod.mp4" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    expect(screen.getByRole("alert").textContent).toMatch(/full path/);
    expect(state.createJob).not.toHaveBeenCalled();

    const path = screen.getByLabelText(
      /VOD file on this computer/,
    ) as HTMLInputElement;
    expect(path.value).toBe("vod.mp4");
    expect(
      screen.getByText("/Users/you/Videos/vod.mp4", { selector: "code" }),
    ).toBeTruthy();
    expect(
      screen.getByText("C:\\Videos\\vod.mp4", { selector: "code" }),
    ).toBeTruthy();
  });

  it("refuses to analyze an unchanged example when a real backend is configured", () => {
    state.mockMode = false;
    render(<StartForm />);

    fireEvent.click(screen.getByRole("button", { name: "VOD ID" }));
    expect(
      screen.getByText(/Replace 2345678901 with your own VOD/),
    ).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );
    expect(screen.getByRole("alert").textContent).toMatch(
      /example of the format/,
    );
    expect(state.createJob).not.toHaveBeenCalled();
  });

  it("allows manually entered VOD IDs matching the sample when a real backend is configured", async () => {
    state.mockMode = false;
    state.createJob.mockResolvedValue({ job: { job_id: "job_123" } });
    render(<StartForm />);

    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "2345678901" },
    });
    expect(screen.getByText(/Ready to analyze VOD 2345678901/)).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );
    expect(state.createJob).toHaveBeenCalledWith({
      vod: "2345678901",
      video_path: null,
    });
  });

  it("supports drag-and-drop selection, rejects unsupported files, and streams upload with progress", async () => {
    state.mockMode = false;
    state.createJob.mockResolvedValue({ job: { job_id: "job_uploaded" } });

    let resolveUpload!: (value: {
      upload_id: string;
      filename: string;
      size_bytes: number;
      duration_s: number | null;
    }) => void;
    state.uploadVideo.mockImplementation(
      (
        _file: File,
        options?: {
          onProgress?: (p: {
            loaded: number;
            total: number | null;
            fraction: number | null;
          }) => void;
        },
      ) => {
        options?.onProgress?.({ loaded: 6, total: 12, fraction: 0.5 });
        return new Promise((resolve) => {
          resolveUpload = resolve;
        });
      },
    );

    render(<StartForm />);
    const dropzone = screen.getByTestId("vod-dropzone");

    fireEvent.dragEnter(dropzone);
    expect(dropzone.getAttribute("data-dragging")).toBe("true");
    expect(screen.getByText(/Drop video file to attach it/)).toBeTruthy();

    // Unsupported extension is rejected immediately without uploading.
    const badFile = new File(["hello"], "notes.txt", { type: "text/plain" });
    fireEvent.drop(dropzone, { dataTransfer: { files: [badFile] } });
    expect(screen.getByRole("alert").textContent).toMatch(
      /Unsupported file type/,
    );

    // Valid video file attaches and uploads on submit.
    const goodFile = new File(["0123456789ab"], "stream_vod.mp4", {
      type: "video/mp4",
    });
    fireEvent.drop(dropzone, { dataTransfer: { files: [goodFile] } });
    expect(screen.getByText("stream_vod.mp4")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "9000000007" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    await waitFor(() => {
      expect(
        screen.getByRole("progressbar").getAttribute("aria-valuenow"),
      ).toBe("50");
    });
    expect(screen.getByText(/Uploading video… 50%/)).toBeTruthy();

    resolveUpload({
      upload_id: "upl_abc123",
      filename: "stream_vod.mp4",
      size_bytes: 12,
      duration_s: 3.0,
    });

    await waitFor(() => {
      expect(state.createJob).toHaveBeenCalledWith({
        vod: "9000000007",
        video_path: null,
        upload_id: "upl_abc123",
      });
    });
    expect(state.push).toHaveBeenCalledWith("/jobs/job_uploaded");
  });

  it("cancels an in-flight upload and surfaces upload validation errors", async () => {
    state.mockMode = false;
    state.uploadVideo.mockImplementationOnce(
      (
        _file: File,
        options?: {
          signal?: AbortSignal;
        },
      ) =>
        new Promise((_resolve, reject) => {
          options?.signal?.addEventListener("abort", () => {
            reject(new DOMException("Upload cancelled.", "AbortError"));
          });
        }),
    );

    render(<StartForm />);
    const goodFile = new File(["0123456789ab"], "vod.mkv", {
      type: "video/x-matroska",
    });
    fireEvent.change(screen.getByLabelText("Choose a VOD video file"), {
      target: { files: [goodFile] },
    });
    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "9000000001" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    const cancelButton = await screen.findByRole("button", {
      name: "Cancel upload",
    });
    fireEvent.click(cancelButton);

    await waitFor(() => {
      expect(screen.getByRole("status").textContent).toMatch(
        /Upload cancelled/,
      );
    });
    expect(state.createJob).not.toHaveBeenCalled();

    // Server-side validation failure displays inline error.
    state.uploadVideo.mockRejectedValueOnce(
      new ApiRequestError(422, {
        code: "invalid_video_upload",
        message: "That file isn't a valid or readable video.",
        details: null,
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Find the best moments" }),
    );

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toMatch(
        /not a valid or readable video|isn't a valid or readable video/,
      );
    });
    expect(state.createJob).not.toHaveBeenCalled();
  });
});
