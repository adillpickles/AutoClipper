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
        schema_version: "1.0",
        job_runner: "pipeline",
      }),
    );
    const client = createHttpClient("http://127.0.0.1:8000", fetchMock);

    const health = await client.getHealth();

    expect(health).toEqual({
      status: "ok",
      schema_version: "1.0",
      job_runner: "pipeline",
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

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8000/api/v1/jobs/job_1",
      "http://127.0.0.1:8000/api/v1/jobs/job_1/result",
      "http://127.0.0.1:8000/api/v1/jobs/job_1/candidates/c001/media",
      "http://127.0.0.1:8000/api/v1/exports/exp_1",
      "http://127.0.0.1:8000/api/v1/highlights/hl_1",
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
});
