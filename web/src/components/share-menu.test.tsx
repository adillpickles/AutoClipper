// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ClipShareMenu,
  OK_STATUS_MS,
  ShareMenu,
} from "@/components/share-menu";
import type { Candidate, Export } from "@/lib/api/types";
import type { ShareEnvironment } from "@/lib/share";

const LINK = "https://www.twitch.tv/videos/9000000001?t=2m5s";

function domError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

function mp4Fetch() {
  return vi.fn(
    async () =>
      new Response(new Blob([new Uint8Array([1, 2, 3])]), { status: 200 }),
  ) as unknown as typeof fetch;
}

function renderMenu(environment: ShareEnvironment) {
  return render(
    <ShareMenu
      fileUrl="/api/v1/exports/exp_1/file"
      filename="streamer_2026-10-13_chat-lost-it_00-02-05.mp4"
      title="Chat lost it"
      channel="Streamer"
      videoId="9000000001"
      startS={125}
      subject="clip 1"
      environment={environment}
    />,
  );
}

function trigger() {
  return screen.getByRole("button", { name: "Share clip 1" });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ShareMenu", () => {
  it("opens a menu, focuses the first item, and moves with the arrow keys", () => {
    renderMenu({ share: vi.fn(), canShare: () => true, fetch: mp4Fetch() });
    expect(trigger().getAttribute("aria-haspopup")).toBe("menu");
    expect(trigger().getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(trigger());
    expect(trigger().getAttribute("aria-expanded")).toBe("true");
    const items = screen.getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("Share video"),
      expect.stringContaining("Download"),
      expect.stringContaining("Copy caption"),
      expect.stringContaining("Copy VOD timestamp link"),
    ]);
    expect(document.activeElement).toBe(items[0]);

    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(items[1], { key: "End" });
    expect(document.activeElement).toBe(items[3]);
    fireEvent.keyDown(items[3], { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(items[0], { key: "ArrowUp" });
    expect(document.activeElement).toBe(items[3]);
  });

  it("closes on Escape without letting an enclosing dialog see it", () => {
    const windowEscape = vi.fn();
    window.addEventListener("keydown", windowEscape);
    renderMenu({});
    fireEvent.click(trigger());
    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger());
    expect(windowEscape).not.toHaveBeenCalled();
    window.removeEventListener("keydown", windowEscape);
  });

  it("closes when pressing outside", () => {
    renderMenu({});
    fireEvent.click(trigger());
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("offers only download and copy actions without the Web Share API", () => {
    renderMenu({});
    fireEvent.click(trigger());
    const labels = screen
      .getAllByRole("menuitem")
      .map((item) => item.textContent);
    expect(labels).toHaveLength(3);
    expect(labels.join(" ")).not.toContain("Share");
    const download = screen.getByRole("menuitem", { name: /Download/ });
    expect(download.getAttribute("href")).toBe("/api/v1/exports/exp_1/file");
    expect(download.getAttribute("download")).toBe(
      "streamer_2026-10-13_chat-lost-it_00-02-05.mp4",
    );
  });

  it("shares the MP4 file loaded when the menu opened", async () => {
    const share = vi.fn(async () => {});
    const fetchMock = mp4Fetch();
    renderMenu({ share, canShare: () => true, fetch: fetchMock });
    fireEvent.click(trigger());
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Share video/ }));
    });
    expect(share).toHaveBeenCalledTimes(1);
    const data = (share.mock.calls[0] as unknown as [ShareData])[0];
    expect(data.files?.[0].name).toBe(
      "streamer_2026-10-13_chat-lost-it_00-02-05.mp4",
    );
    expect(data.text).toContain(LINK);
    expect(screen.getByRole("status").textContent).toContain("Shared.");
    expect(document.activeElement).toBe(trigger());
  });

  it("shares the caption and link where files can't be shared", async () => {
    const share = vi.fn(async () => {});
    const fetchMock = mp4Fetch();
    renderMenu({
      share,
      canShare: (data) => !data?.files,
      fetch: fetchMock,
    });
    fireEvent.click(trigger());
    expect(fetchMock).not.toHaveBeenCalled();
    await act(async () => {
      fireEvent.click(
        screen.getByRole("menuitem", { name: /Share caption and link/ }),
      );
    });
    const data = (share.mock.calls[0] as unknown as [ShareData])[0];
    expect(data.files).toBeUndefined();
    expect(data.text).toContain("Chat lost it · Streamer on Twitch");
    expect(screen.getByRole("status").textContent).toContain(
      "Shared the caption and link",
    );
  });

  it("says nothing was shared when the share sheet is cancelled", async () => {
    const share = vi.fn(async () => {
      throw domError("AbortError");
    });
    renderMenu({ share, canShare: () => true, fetch: mp4Fetch() });
    fireEvent.click(trigger());
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Share video/ }));
    });
    expect(share).toHaveBeenCalled();
    expect(screen.getByRole("status").textContent).toBe("");
    expect(screen.queryByText(/Shared/)).toBeNull();
  });

  it("asks to press Share again when the browser refused the late share", async () => {
    const share = vi.fn(async () => {
      throw domError("NotAllowedError");
    });
    renderMenu({ share, canShare: () => true, fetch: mp4Fetch() });
    fireEvent.click(trigger());
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Share video/ }));
    });
    expect(screen.getByRole("status").textContent).toContain(
      "Press Share again",
    );
  });

  it("copies the caption and the timestamp link", async () => {
    const writeText = vi.fn(async () => {});
    renderMenu({ writeText });
    fireEvent.click(trigger());
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Copy caption/ }));
    });
    expect(writeText).toHaveBeenLastCalledWith(
      `Chat lost it · Streamer on Twitch\nWatch it on the VOD: ${LINK}`,
    );
    expect(screen.getByRole("status").textContent).toContain("Caption copied.");

    fireEvent.click(trigger());
    await act(async () => {
      fireEvent.click(
        screen.getByRole("menuitem", { name: /Copy VOD timestamp link/ }),
      );
    });
    expect(writeText).toHaveBeenLastCalledWith(LINK);
    expect(screen.getByRole("status").textContent).toContain("Link copied.");
  });

  it("falls back to execCommand when the Clipboard API rejects", async () => {
    const writeText = vi.fn(async () => {
      throw domError("NotAllowedError");
    });
    const execCommand = vi.fn(() => true);
    Object.defineProperty(document, "execCommand", {
      value: execCommand,
      configurable: true,
    });
    renderMenu({ writeText });
    fireEvent.click(trigger());
    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Copy caption/ }));
    });
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(screen.getByRole("status").textContent).toContain("Caption copied.");
  });

  it("shows the text to copy by hand when every clipboard path fails", async () => {
    const writeText = vi.fn(async () => {
      throw domError("NotAllowedError");
    });
    Object.defineProperty(document, "execCommand", {
      value: vi.fn(() => false),
      configurable: true,
    });
    renderMenu({ writeText });
    fireEvent.click(trigger());
    await act(async () => {
      fireEvent.click(
        screen.getByRole("menuitem", { name: /Copy VOD timestamp link/ }),
      );
    });
    expect(screen.getByRole("status").textContent).toContain("Couldn't copy");
    await waitFor(() =>
      expect(
        (
          screen.getByRole("textbox", {
            name: "Text to copy",
          }) as HTMLTextAreaElement
        ).value,
      ).toBe(LINK),
    );
  });

  it("labels the link 'Copy VOD link' for a whole-stream video", () => {
    render(
      <ShareMenu
        fileUrl="/hl.mp4"
        filename="hl.mp4"
        title="Stream highlights"
        videoId="9000000001"
        subject="the highlights video"
        environment={{}}
      />,
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Share the highlights video" }),
    );
    expect(
      screen.getAllByRole("menuitem").map((item) => item.textContent),
    ).toContain("Copy VOD linkOpens the full stream on Twitch");
  });
});

describe("ShareMenu review fixes", () => {
  function signalFetch() {
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = init?.signal;
          if (signal) {
            signals.push(signal);
            signal.addEventListener("abort", () =>
              reject(domError("AbortError")),
            );
          }
        }),
    );
    return { fetchMock: fetchMock as unknown as typeof fetch, signals };
  }

  it("aborts the prefetch when the menu closes, and keeps one file at a time", () => {
    const { fetchMock, signals } = signalFetch();
    renderMenu({ share: vi.fn(), canShare: () => true, fetch: fetchMock });
    fireEvent.click(trigger());
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);

    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Escape" });
    expect(signals[0].aborted).toBe(true);

    // Opening again starts one new download; the old one stays aborted.
    fireEvent.click(trigger());
    expect(signals).toHaveLength(2);
    expect(signals[1].aborted).toBe(false);
    fireEvent.pointerDown(document.body);
    expect(signals[1].aborted).toBe(true);
  });

  it("aborts the prefetch when the component unmounts", () => {
    const { fetchMock, signals } = signalFetch();
    const { unmount } = renderMenu({
      share: vi.fn(),
      canShare: () => true,
      fetch: fetchMock,
    });
    fireEvent.click(trigger());
    unmount();
    expect(signals[0].aborted).toBe(true);
  });

  it("closes on Tab and hands focus back to the trigger without blocking Tab", () => {
    renderMenu({});
    fireEvent.click(trigger());
    const notPrevented = fireEvent.keyDown(screen.getAllByRole("menuitem")[0], {
      key: "Tab",
    });
    expect(notPrevented).toBe(true);
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(trigger());
  });

  it("asks once to press Share again, then falls back to download and copy", async () => {
    const share = vi.fn(async () => {
      throw domError("NotAllowedError");
    });
    renderMenu({ share, canShare: () => true, fetch: mp4Fetch() });
    fireEvent.click(trigger());

    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Share video/ }));
    });
    expect(screen.getByRole("status").textContent).toContain(
      "Press Share again",
    );
    // The menu stays open with the loaded file for the second press.
    expect(screen.getByRole("menu")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("menuitem", { name: /Share video/ }));
    });
    expect(share).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status").textContent).toContain(
      "Download the video or copy the caption",
    );
    const labels = screen
      .getAllByRole("menuitem")
      .map((item) => item.textContent ?? "");
    expect(labels.some((label) => label.startsWith("Share"))).toBe(false);
    expect(labels[0]).toContain("Download");

    // Reopening doesn't bring the refused share back.
    fireEvent.keyDown(screen.getAllByRole("menuitem")[0], { key: "Escape" });
    fireEvent.click(trigger());
    expect(screen.queryByRole("menuitem", { name: /Share video/ })).toBeNull();
  });

  it("clears a success message after a few seconds", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      renderMenu({ writeText: vi.fn(async () => {}) });
      fireEvent.click(trigger());
      await act(async () => {
        fireEvent.click(screen.getByRole("menuitem", { name: /Copy caption/ }));
      });
      expect(screen.getByRole("status").textContent).toBe("Caption copied.");
      act(() => {
        vi.advanceTimersByTime(OK_STATUS_MS);
      });
      expect(screen.getByRole("status").textContent).toBe("");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ClipShareMenu", () => {
  const candidate: Candidate = {
    candidate_id: "c001",
    rank: 2,
    score: 1,
    start_s: 100,
    end_s: 130,
    source: "discovered",
    n_viewer_clips: 0,
    top_signals: [],
    title: "Big play",
  } as Candidate;
  const done = {
    export_id: "exp_1",
    state: "done",
    start_s: 125,
    end_s: 130,
    download_url: "/api/v1/exports/exp_1/file",
  } as Export;

  it("renders nothing until the export is done", () => {
    const { container } = render(
      <ClipShareMenu
        clip={{
          kind: "export",
          export: { ...done, state: "running" },
          url: null,
        }}
        candidate={candidate}
        filename="f.mp4"
        videoId="9000000001"
      />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("shares the export's own start", async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    render(
      <ClipShareMenu
        clip={{ kind: "export", export: done, url: "/x.mp4" }}
        candidate={candidate}
        filename="f.mp4"
        videoId="9000000001"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Share clip 2" }));
    await act(async () => {
      fireEvent.click(
        screen.getByRole("menuitem", { name: /Copy VOD timestamp link/ }),
      );
    });
    expect(writeText).toHaveBeenCalledWith(LINK);
  });
});
