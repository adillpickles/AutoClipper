import { describe, expect, it, vi } from "vitest";

import {
  ApiRequestError,
  createApiClient,
  isMockMode,
  resolveApiOrigin,
} from "@/lib/api/client";
import { createHttpClient, normalizeOrigin } from "@/lib/api/http";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("HTTP API client", () => {
  it("normalizes origins and resolves root-relative and absolute URLs", () => {
    expect(normalizeOrigin("  http://127.0.0.1:8000/  ")).toBe(
      "http://127.0.0.1:8000",
    );
    const client = createHttpClient(
      "http://127.0.0.1:8000/",
      vi.fn() as unknown as typeof fetch,
    );
    expect(client.resolveUrl("/api/v1/exports/exp_1/file")).toBe(
      "http://127.0.0.1:8000/api/v1/exports/exp_1/file",
    );
    expect(client.resolveUrl("https://cdn.example.com/clip.mp4")).toBe(
      "https://cdn.example.com/clip.mp4",
    );
  });

  it("calls GET /health without adding a JSON Content-Type header", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        status: "ok",
        schema_version: "1.1",
        job_runner: "pipeline",
        captions_available: true,
      }),
    );
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    const health = await client.getHealth();

    expect(health).toEqual({
      status: "ok",
      schema_version: "1.1",
      job_runner: "pipeline",
      captions_available: true,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/health");
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
    expect(init?.cache).toBe("no-store");
  });

  it("calls GET /preflight with no-store caching", async () => {
    const report = {
      status: "warn",
      checks: [
        {
          name: "captions",
          label: "Captions extra",
          status: "warn",
          message: "faster-whisper is not installed.",
          fix: "uv sync --extra captions",
        },
      ],
    };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(report));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    const preflight = await client.getPreflight();

    expect(preflight).toEqual(report);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8000/api/v1/preflight");
    expect(new Headers(init?.headers).has("Content-Type")).toBe(false);
    expect(init?.cache).toBe("no-store");
  });

  it("distinguishes 201 created from 200 deduplicated on POST endpoints", async () => {
    const job = { job_id: "job_1", video_id: "9000000001", state: "queued" };
    const exp = { export_id: "exp_1", state: "queued" };
    const hl = { highlights_id: "hl_1", state: "queued" };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(job, 201))
      .mockResolvedValueOnce(jsonResponse(job, 200))
      .mockResolvedValueOnce(jsonResponse(exp, 201))
      .mockResolvedValueOnce(jsonResponse(exp, 200))
      .mockResolvedValueOnce(jsonResponse(hl, 201))
      .mockResolvedValueOnce(jsonResponse(hl, 200));

    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    expect(
      (await client.createJob({ vod: "9000000001", video_path: null })).created,
    ).toBe(true);
    expect((await client.createJob({ vod: "9000000001" })).created).toBe(false);

    const exportBody = {
      job_id: "job_1",
      candidate_id: "c001",
      start_s: 10,
      end_s: 30,
      layout: "center_crop" as const,
      captions: true,
    };
    expect((await client.createExport(exportBody)).created).toBe(true);
    expect((await client.createExport(exportBody)).created).toBe(false);

    expect((await client.createHighlights({ job_id: "job_1" })).created).toBe(
      true,
    );
    expect((await client.createHighlights({ job_id: "job_1" })).created).toBe(
      false,
    );

    const [, postInit] = fetchMock.mock.calls[0];
    expect(postInit?.method).toBe("POST");
    expect(new Headers(postInit?.headers).get("Content-Type")).toBe(
      "application/json",
    );
  });

  it("reads settings with GET and saves them with PUT /settings", async () => {
    const saved = { output_folder: "D:/Clips", last_copy: null };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(async () => jsonResponse(saved));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    expect(await client.getSettings()).toEqual(saved);
    expect(await client.updateSettings({ output_folder: "D:/Clips" })).toEqual(
      saved,
    );

    const [[getUrl, getInit], [putUrl, putInit]] = fetchMock.mock.calls;
    expect(getUrl).toBe("http://127.0.0.1:8000/api/v1/settings");
    expect(getInit?.method ?? "GET").toBe("GET");
    expect(putUrl).toBe("http://127.0.0.1:8000/api/v1/settings");
    expect(putInit?.method).toBe("PUT");
    expect(JSON.parse(String(putInit?.body))).toEqual({
      output_folder: "D:/Clips",
    });
  });

  it("hits every GET endpoint with URL-encoded IDs", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(async () => jsonResponse({}));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    await client.getJob("job_1");
    await client.getResult("job_1");
    await client.getCandidateMedia("job_1", "c001");
    await client.getExport("exp_1");
    await client.getHighlights("hl_1");
    await client.getArchiveStatus("job 1", "clips");
    await client.getArchiveStatus("job_1", "highlights");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/v1/jobs/job_1",
      "http://127.0.0.1:8000/api/v1/jobs/job_1/result",
      "http://127.0.0.1:8000/api/v1/jobs/job_1/candidates/c001/media",
      "http://127.0.0.1:8000/api/v1/exports/exp_1",
      "http://127.0.0.1:8000/api/v1/highlights/hl_1",
      "http://127.0.0.1:8000/api/v1/jobs/job%201/archives/clips",
      "http://127.0.0.1:8000/api/v1/jobs/job_1/archives/highlights",
    ]);
  });

  it("throws ApiRequestError with the contract error envelope on non-2xx responses", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: "invalid_vod_link",
            message: "That isn't a Twitch VOD link.",
            details: null,
          },
        },
        422,
      ),
    );
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    await expect(client.createJob({ vod: "bad" })).rejects.toSatisfy(
      (err: unknown) =>
        err instanceof ApiRequestError &&
        err.status === 422 &&
        err.error.code === "invalid_vod_link" &&
        err.error.message === "That isn't a Twitch VOD link.",
    );
  });

  it("falls back to a generic ApiRequestError on non-JSON error responses", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response("Bad Gateway", { status: 502 }));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    await expect(client.getJob("job_1")).rejects.toSatisfy(
      (err: unknown) =>
        err instanceof ApiRequestError &&
        err.status === 502 &&
        err.error.code === "internal_error" &&
        err.error.message.includes("502"),
    );
  });

  it("selects HTTP or mock mode from configuration", () => {
    expect(resolveApiOrigin({ origin: "", mode: "" })).toBeNull();
    expect(isMockMode({ origin: "", mode: "" })).toBe(true);
    expect(
      resolveApiOrigin({ origin: "http://127.0.0.1:8000", mode: "" }),
    ).toBe("http://127.0.0.1:8000");
    expect(isMockMode({ origin: "http://127.0.0.1:8000", mode: "" })).toBe(
      false,
    );
    expect(
      resolveApiOrigin({ origin: "http://127.0.0.1:8000", mode: "mock" }),
    ).toBeNull();

    const httpClient = createApiClient(
      { origin: "http://127.0.0.1:8000", mode: "" },
      vi.fn() as unknown as typeof fetch,
    );
    expect(httpClient.resolveUrl("/api/v1/media/med_1")).toBe(
      "http://127.0.0.1:8000/api/v1/media/med_1",
    );

    const mockClient = createApiClient({ origin: "", mode: "" });
    expect(mockClient.resolveUrl("/mock-media/clip.mp4")).toBe(
      "/mock-media/clip.mp4",
    );
  });

  it("fetches and starts managed tool setup via /tools/setup", async () => {
    const setupBody = {
      state: "running",
      items: [],
      error: null,
      started_at: "2026-11-26T00:00:00Z",
      updated_at: "2026-11-26T00:00:00Z",
      finished_at: null,
    };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(setupBody), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(setupBody), { status: 202 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(setupBody), { status: 200 }),
      );
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    const status = await client.getToolSetup();
    expect(status.state).toBe("running");

    const started = await client.startToolSetup({ tools: ["ffmpeg"] });
    expect(started.started).toBe(true);
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/v1/tools/setup",
    );

    const alreadyRunning = await client.startToolSetup();
    expect(alreadyRunning.started).toBe(false);
  });

  it("calls GET /channels/vods with encoded channel and optional limit", async () => {
    const payload = {
      channel: {
        broadcaster_id: "9100000001",
        login: "user_001",
        display_name: "User_001",
        profile_image_url: null,
      },
      vods: [
        {
          video_id: "9000000001",
          title: "Synthetic stream 1",
          created_at: "2026-10-12T18:00:00Z",
          duration_s: 360,
          thumbnail_url: "https://example.invalid/thumb-320x180.jpg",
          url: "https://www.twitch.tv/videos/9000000001",
          view_count: 100,
          analyzed: false,
          last_job_id: null,
        },
      ],
      truncated: false,
    };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(payload));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    const response = await client.getChannelVods(
      "https://www.twitch.tv/user_001",
      15,
    );

    expect(response).toEqual(payload);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "http://127.0.0.1:8000/api/v1/channels/vods?channel=https%3A%2F%2Fwww.twitch.tv%2Fuser_001&limit=15",
    );
    expect(init?.cache).toBe("no-store");
  });

  it("maps channel error envelopes (missing credentials, unknown channel)", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValueOnce(
      jsonResponse(
        {
          error: {
            code: "twitch_credentials_missing",
            message: "Twitch credentials missing.",
            details: { missing: ["TWITCH_CLIENT_ID"] },
          },
        },
        503,
      ),
    );
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    await expect(client.getChannelVods("user_001")).rejects.toMatchObject({
      status: 503,
      error: {
        code: "twitch_credentials_missing",
        details: { missing: ["TWITCH_CLIENT_ID"] },
      },
    });
  });

  it("lists jobs with pagination and filters via GET /jobs and deletes via DELETE /jobs/{id}", async () => {
    const listBody = {
      items: [],
      total: 0,
      limit: 20,
      offset: 40,
      has_more: false,
      next_cursor: null,
    };
    const deleteBody = {
      job_id: "job_1",
      deleted: true,
      deleted_files: 2,
    };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(listBody))
      .mockResolvedValueOnce(jsonResponse(listBody))
      .mockResolvedValueOnce(jsonResponse(deleteBody))
      .mockResolvedValueOnce(jsonResponse({ ...deleteBody, deleted_files: 0 }));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    await expect(client.listJobs()).resolves.toEqual(listBody);
    await client.listJobs({
      limit: 20,
      offset: 40,
      q: "stream",
      state: "done",
    });
    await expect(
      client.deleteJob("job_1", { deleteExports: true }),
    ).resolves.toEqual(deleteBody);
    await client.deleteJob("job_1");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8000/api/v1/jobs",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/v1/jobs?limit=20&offset=40&q=stream&state=done",
    );
    expect(fetchMock.mock.calls[2][0]).toBe(
      "http://127.0.0.1:8000/api/v1/jobs/job_1?delete_exports=true",
    );
    expect(fetchMock.mock.calls[2][1]?.method).toBe("DELETE");
    expect(fetchMock.mock.calls[3][0]).toBe(
      "http://127.0.0.1:8000/api/v1/jobs/job_1",
    );
    expect(fetchMock.mock.calls[3][1]?.method).toBe("DELETE");
  });

  it("encodes the job cursor and sends one status request with the selected IDs", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ items: [], next_cursor: null }))
      .mockResolvedValueOnce(jsonResponse([]));
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);
    const before = "2026-11-26T15:00:00+00:00|20|job_20";

    await client.listJobs({ before });
    await expect(client.getJobStatuses(["job_a", "job_b"])).resolves.toEqual(
      [],
    );

    expect(
      new URL(String(fetchMock.mock.calls[0][0])).searchParams.get("before"),
    ).toBe(before);
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/v1/jobs/status",
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "POST",
      body: JSON.stringify({ job_ids: ["job_a", "job_b"] }),
    });
  });

  it("streams video uploads with progress, abort, and error mapping", async () => {
    class FakeXhr {
      method = "";
      url = "";
      headers = new Map<string, string>();
      status = 201;
      responseText = "";
      sent: unknown = null;
      aborted = false;
      upload: { onprogress: ((e: ProgressEvent) => void) | null } = {
        onprogress: null,
      };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;

      open(method: string, url: string) {
        this.method = method;
        this.url = url;
      }

      setRequestHeader(name: string, value: string) {
        this.headers.set(name, value);
      }

      send(body: unknown) {
        this.sent = body;
      }

      abort() {
        this.aborted = true;
        this.onabort?.();
      }
    }

    const instances: FakeXhr[] = [];
    const client = createHttpClient(
      "http://127.0.0.1:8000",
      vi.fn() as unknown as typeof fetch,
      () => {
        const xhr = new FakeXhr();
        instances.push(xhr);
        return xhr as unknown as XMLHttpRequest;
      },
    );

    const file = new File(["video-bytes"], "my vod.mp4", { type: "video/mp4" });
    const progressEvents: Array<number | null> = [];
    const uploadPromise = client.uploadVideo(file, {
      onProgress: (p) => progressEvents.push(p.fraction),
    });

    const xhr = instances[0];
    expect(xhr.method).toBe("POST");
    expect(xhr.url).toBe(
      "http://127.0.0.1:8000/api/v1/uploads?filename=my%20vod.mp4",
    );
    expect(xhr.headers.get("Content-Type")).toBe("application/octet-stream");
    expect(xhr.sent).toBe(file);

    xhr.upload.onprogress?.({
      lengthComputable: true,
      loaded: 5,
      total: 10,
    } as ProgressEvent);
    xhr.status = 201;
    xhr.responseText = JSON.stringify({
      upload_id: "upl_1234567890abcdef",
      filename: "my vod.mp4",
      size_bytes: 11,
      duration_s: 3.0,
    });
    xhr.onload?.();

    await expect(uploadPromise).resolves.toEqual({
      upload_id: "upl_1234567890abcdef",
      filename: "my vod.mp4",
      size_bytes: 11,
      duration_s: 3.0,
    });
    expect(progressEvents).toEqual([0, 0.5]);

    const controller = new AbortController();
    const abortedPromise = client.uploadVideo(file, {
      signal: controller.signal,
    });
    controller.abort();
    expect(instances[1].aborted).toBe(true);
    await expect(abortedPromise).rejects.toMatchObject({ name: "AbortError" });

    const errorPromise = client.uploadVideo(file);
    instances[2].status = 422;
    instances[2].responseText = JSON.stringify({
      error: {
        code: "invalid_video_upload",
        message: "That file isn't a valid or readable video.",
        details: null,
      },
    });
    instances[2].onload?.();
    await expect(errorPromise).rejects.toMatchObject({
      status: 422,
      error: { code: "invalid_video_upload" },
    });
  });
});
