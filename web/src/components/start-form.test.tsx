// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { Activity } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StartForm } from "@/components/start-form";
import { ApiRequestError } from "@/lib/api/errors";
import type { UploadVideoOptions } from "@/lib/api/types";

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

const uploaded = {
  upload_id: "upl_abc123",
  filename: "vod.mp4",
  size_bytes: 12,
  duration_s: 3,
};

function selectVideo() {
  const file = new File(["0123456789ab"], "vod.mp4", { type: "video/mp4" });
  const picker = screen.getByLabelText("Choose a VOD video file");
  expect(picker.tabIndex).toBe(-1);
  fireEvent.change(picker, { target: { files: [file] } });
  fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
    target: { value: "9000000001" },
  });
  return file;
}

function submit() {
  fireEvent.click(
    screen.getByRole("button", { name: "Find the best moments" }),
  );
}

function vodInput() {
  return screen.getByLabelText("Twitch VOD link") as HTMLInputElement;
}

function submitButton() {
  return screen.getByRole("button", {
    name: /Find the best moments|Starting…|Uploading video…/,
  }) as HTMLButtonElement;
}

// Next.js (cacheComponents) keeps a page it navigates away from inside a
// hidden <Activity> and shows the same instance again on Back (#184).
function renderInActivity() {
  const view = render(
    <Activity mode="visible">
      <StartForm />
    </Activity>,
  );
  return {
    hide: () =>
      view.rerender(
        <Activity mode="hidden">
          <StartForm />
        </Activity>,
      ),
    show: () =>
      view.rerender(
        <Activity mode="visible">
          <StartForm />
        </Activity>,
      ),
  };
}

function dispatchPageShow(persisted: boolean) {
  const event = new Event("pageshow");
  Object.defineProperty(event, "persisted", { value: persisted });
  act(() => {
    window.dispatchEvent(event);
  });
}

type StartMode = "link" | "path" | "upload";

function fillForm(mode: StartMode) {
  if (mode === "upload") {
    selectVideo();
    return;
  }
  if (mode === "path") {
    fireEvent.click(
      screen.getByRole("button", { name: "Use file path on this computer" }),
    );
    fireEvent.change(screen.getByLabelText(/VOD file on this computer/), {
      target: { value: "/Users/you/Videos/vod.mp4" },
    });
  }
  fireEvent.change(vodInput(), { target: { value: "9000000001" } });
}

function expectedJobRequest(mode: StartMode) {
  if (mode === "upload") {
    return { vod: "9000000001", video_path: null, upload_id: "upl_abc123" };
  }
  return {
    vod: "9000000001",
    video_path: mode === "path" ? "/Users/you/Videos/vod.mp4" : null,
  };
}

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

  it.each(["network failure", "expired upload ID"])(
    "retries with the selected file after a %s",
    async (failure) => {
      state.uploadVideo.mockResolvedValue(uploaded);
      state.createJob.mockResolvedValue({ job: { job_id: "job_retried" } });
      if (failure === "network failure") {
        state.uploadVideo.mockRejectedValueOnce(new Error("Network error"));
      } else {
        state.createJob.mockRejectedValueOnce(
          new ApiRequestError(422, {
            code: "invalid_video_upload",
            message: "That upload is missing. Upload the video again.",
            details: null,
          }),
        );
      }

      render(<StartForm />);
      const file = selectVideo();
      submit();
      expect((await screen.findByRole("alert")).textContent).toMatch(/upload/i);
      expect(screen.getByText(file.name)).toBeTruthy();
      submit();
      await waitFor(() =>
        expect(state.push).toHaveBeenCalledWith("/jobs/job_retried"),
      );
      expect(state.uploadVideo).toHaveBeenCalledTimes(2);
      expect(state.uploadVideo.mock.calls[1][0]).toBe(file);
    },
  );

  it("aborts an upload on unmount and ignores a late success", async () => {
    let signal: AbortSignal | undefined;
    let complete!: (result: typeof uploaded) => void;
    state.uploadVideo.mockImplementationOnce(
      (_file: File, options: UploadVideoOptions) => {
        signal = options.signal;
        return new Promise((resolve) => {
          complete = resolve;
        });
      },
    );

    const { unmount } = render(<StartForm />);
    selectVideo();
    submit();
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);

    await act(async () => complete(uploaded));
    expect(state.createJob).not.toHaveBeenCalled();
    expect(state.push).not.toHaveBeenCalled();
  });

  it("ignores a job response after leaving the page", async () => {
    let complete!: (result: { job: { job_id: string } }) => void;
    state.createJob.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    const { unmount } = render(<StartForm />);
    fireEvent.change(screen.getByLabelText("Twitch VOD link"), {
      target: { value: "9000000001" },
    });
    submit();
    expect(state.createJob).toHaveBeenCalledOnce();
    unmount();

    await act(async () => complete({ job: { job_id: "job_abandoned" } }));
    expect(state.push).not.toHaveBeenCalled();
  });

  describe("returning to the start page (#184)", () => {
    it.each<StartMode>(["link", "path", "upload"])(
      "is usable again after a %s start and Back navigation",
      async (mode) => {
        state.uploadVideo.mockResolvedValue(uploaded);
        state.createJob
          .mockResolvedValueOnce({ job: { job_id: "job_first" } })
          .mockResolvedValueOnce({ job: { job_id: "job_second" } });

        const page = renderInActivity();
        fillForm(mode);
        submit();
        await waitFor(() =>
          expect(state.push).toHaveBeenCalledWith("/jobs/job_first"),
        );
        // Until the job page opens, the form stays locked.
        expect(submitButton().textContent).toBe("Starting…");
        expect(submitButton().disabled).toBe(true);

        page.hide();
        page.show();

        expect(submitButton().textContent).toBe("Find the best moments");
        expect(submitButton().disabled).toBe(false);
        expect(vodInput().disabled).toBe(false);
        expect(vodInput().value).toBe("9000000001");
        expect(screen.queryByRole("progressbar")).toBeNull();

        submit();
        await waitFor(() =>
          expect(state.push).toHaveBeenCalledWith("/jobs/job_second"),
        );
        expect(state.createJob).toHaveBeenCalledTimes(2);
        expect(state.createJob).toHaveBeenNthCalledWith(
          2,
          expectedJobRequest(mode),
        );
        // A verified upload is reused rather than sent again.
        expect(state.uploadVideo).toHaveBeenCalledTimes(
          mode === "upload" ? 1 : 0,
        );
      },
    );

    it("accepts a new link after coming back", async () => {
      state.createJob
        .mockResolvedValueOnce({ job: { job_id: "job_first" } })
        .mockResolvedValueOnce({ job: { job_id: "job_second" } });
      const page = renderInActivity();
      fillForm("link");
      submit();
      await waitFor(() => expect(state.push).toHaveBeenCalledOnce());

      page.hide();
      page.show();
      fireEvent.change(vodInput(), { target: { value: "9000000003" } });
      submit();

      await waitFor(() =>
        expect(state.push).toHaveBeenCalledWith("/jobs/job_second"),
      );
      expect(state.createJob).toHaveBeenLastCalledWith({
        vod: "9000000003",
        video_path: null,
      });
    });

    it("resets an upload that was still running when the page was left", async () => {
      let signal: AbortSignal | undefined;
      state.uploadVideo.mockImplementationOnce(
        (_file: File, options: UploadVideoOptions) => {
          signal = options.signal;
          options.onProgress?.({ loaded: 6, total: 12, fraction: 0.5 });
          return new Promise(() => {});
        },
      );

      const page = renderInActivity();
      fillForm("upload");
      submit();
      await screen.findByRole("progressbar");
      expect(submitButton().textContent).toBe("Uploading video…");

      page.hide();
      expect(signal?.aborted).toBe(true);
      page.show();

      expect(submitButton().textContent).toBe("Find the best moments");
      expect(submitButton().disabled).toBe(false);
      expect(screen.queryByRole("progressbar")).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Cancel upload" }),
      ).toBeNull();
      expect(screen.getByText("vod.mp4")).toBeTruthy();
      expect(state.createJob).not.toHaveBeenCalled();
    });

    it("is usable again after a back/forward cache restore", async () => {
      state.createJob.mockResolvedValue({ job: { job_id: "job_first" } });
      render(<StartForm />);
      fillForm("link");
      submit();
      await waitFor(() => expect(state.push).toHaveBeenCalledOnce());
      expect(submitButton().disabled).toBe(true);

      // A first load (not restored from the cache) changes nothing.
      dispatchPageShow(false);
      expect(submitButton().textContent).toBe("Starting…");

      dispatchPageShow(true);
      expect(submitButton().textContent).toBe("Find the best moments");
      expect(submitButton().disabled).toBe(false);
    });

    it("clears a stale Starting… state when the link changes", async () => {
      state.createJob.mockResolvedValue({ job: { job_id: "job_first" } });
      render(<StartForm />);
      fillForm("link");
      submit();
      await waitFor(() => expect(state.push).toHaveBeenCalledOnce());
      expect(submitButton().textContent).toBe("Starting…");

      fireEvent.change(vodInput(), { target: { value: "9000000003" } });

      expect(submitButton().textContent).toBe("Find the best moments");
      expect(submitButton().disabled).toBe(false);
    });

    it("keeps a request still in flight pending when the input changes", () => {
      state.createJob.mockReturnValue(new Promise(() => {}));
      render(<StartForm />);
      fillForm("link");
      submit();

      fireEvent.change(vodInput(), { target: { value: "9000000003" } });

      expect(submitButton().textContent).toBe("Starting…");
      expect(submitButton().disabled).toBe(true);
    });
  });

  it("starts only one job on a double click or a resubmit before the job page opens", async () => {
    state.createJob.mockResolvedValue({ job: { job_id: "job_once" } });
    render(<StartForm />);
    fillForm("link");

    const button = submitButton();
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(state.push).toHaveBeenCalledOnce());

    // Enter in a field still submits the form while the button is disabled.
    fireEvent.submit(button.closest("form")!);
    await act(async () => {});
    expect(state.createJob).toHaveBeenCalledOnce();
    expect(state.push).toHaveBeenCalledOnce();
  });
});
