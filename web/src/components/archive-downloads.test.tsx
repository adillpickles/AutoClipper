// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArchiveDownloads } from "@/components/archive-downloads";
import { ApiRequestError } from "@/lib/api/client";
import type { ArchiveStatus } from "@/lib/api/types";

const state = vi.hoisted(() => ({ getArchiveStatus: vi.fn() }));

vi.mock("@/lib/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/client")>(
      "@/lib/api/client",
    );
  return {
    ...actual,
    getApiClient: () => ({
      getArchiveStatus: state.getArchiveStatus,
      resolveUrl: (url: string) => `http://127.0.0.1:8000${url}`,
    }),
  };
});

function status(overrides: Partial<ArchiveStatus>): ArchiveStatus {
  return {
    kind: "clips",
    ready_count: 0,
    omitted: [],
    filename: "user_001_2026-10-12_clips.zip",
    download_url: null,
    ...overrides,
  };
}

let clicked: HTMLAnchorElement[];

beforeEach(() => {
  clicked = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push(this);
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  state.getArchiveStatus.mockReset();
});

describe("ArchiveDownloads", () => {
  it("starts the ZIP download when every clip is ready", async () => {
    state.getArchiveStatus.mockResolvedValue(
      status({
        ready_count: 3,
        download_url: "/api/v1/jobs/job_1/archives/clips/file",
      }),
    );
    render(<ArchiveDownloads jobId="job_1" />);

    fireEvent.click(
      screen.getByRole("button", { name: "Download all clips (ZIP)" }),
    );

    expect(
      await screen.findByText(
        "Downloading 3 clips as user_001_2026-10-12_clips.zip.",
      ),
    ).toBeTruthy();
    expect(state.getArchiveStatus).toHaveBeenCalledWith("job_1", "clips");
    expect(clicked).toHaveLength(1);
    expect(clicked[0].href).toBe(
      "http://127.0.0.1:8000/api/v1/jobs/job_1/archives/clips/file",
    );
    expect(clicked[0].download).toBe("user_001_2026-10-12_clips.zip");
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  it("lists the renders a partial ZIP leaves out", async () => {
    state.getArchiveStatus.mockResolvedValue(
      status({
        ready_count: 1,
        download_url: "/api/v1/jobs/job_1/archives/clips/file",
        omitted: [
          {
            render_id: "exp_2",
            candidate_id: "c002",
            rank: 2,
            reason: "running",
          },
          {
            render_id: "exp_3",
            candidate_id: "c003",
            rank: 3,
            reason: "failed",
          },
        ],
      }),
    );
    render(<ArchiveDownloads jobId="job_1" />);

    fireEvent.click(
      screen.getByRole("button", { name: "Download all clips (ZIP)" }),
    );

    expect(
      await screen.findByText(
        "Downloading 1 clip. Left out (listed in missing.txt):",
      ),
    ).toBeTruthy();
    expect(
      screen.getAllByRole("listitem").map((item) => item.textContent),
    ).toEqual(["Clip 2: still rendering", "Clip 3: render failed"]);
    expect(clicked).toHaveLength(1);
  });

  it("explains when no highlights video is ready and downloads nothing", async () => {
    state.getArchiveStatus.mockResolvedValue(
      status({
        kind: "highlights",
        filename: "user_001_2026-10-12_highlights.zip",
        omitted: [
          {
            render_id: "hl_1",
            candidate_id: null,
            rank: null,
            reason: "queued",
          },
        ],
      }),
    );
    render(<ArchiveDownloads jobId="job_1" />);

    fireEvent.click(
      screen.getByRole("button", { name: "Download all highlights (ZIP)" }),
    );

    expect(
      await screen.findByText(
        "None of the highlights videos have finished yet:",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("listitem").textContent).toBe(
      "Highlights video: waiting to render",
    );
    expect(state.getArchiveStatus).toHaveBeenCalledWith("job_1", "highlights");
    expect(clicked).toHaveLength(0);
  });

  it("says when nothing was rendered at all", async () => {
    state.getArchiveStatus.mockResolvedValue(status({}));
    render(<ArchiveDownloads jobId="job_1" />);

    fireEvent.click(
      screen.getByRole("button", { name: "Download all clips (ZIP)" }),
    );

    expect(
      await screen.findByText("There are no clips to download yet."),
    ).toBeTruthy();
    expect(clicked).toHaveLength(0);
  });

  it("shows the server's message when the check fails", async () => {
    state.getArchiveStatus.mockRejectedValue(
      new ApiRequestError(404, {
        code: "job_not_found",
        message: "This analysis no longer exists.",
        details: null,
      }),
    );
    render(<ArchiveDownloads jobId="job_1" />);

    fireEvent.click(
      screen.getByRole("button", { name: "Download all clips (ZIP)" }),
    );

    expect(
      await screen.findByText("This analysis no longer exists."),
    ).toBeTruthy();
    expect(clicked).toHaveLength(0);
  });
});
