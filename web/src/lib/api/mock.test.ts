import { beforeEach, describe, expect, it } from "vitest";

import { ApiRequestError } from "@/lib/api/errors";
import {
  createMockClient,
  exportAt,
  jobAt,
  MOCK_STEP_MS,
  resetMockStore,
  schedule,
} from "@/lib/api/mock";

const T0 = Date.UTC(2026, 10, 4, 12, 0, 0);
const stored = (videoId: string) => ({
  job_id: "job_test",
  video_id: videoId,
  video_path: null,
  created_ms: T0,
});

describe("jobAt", () => {
  // Stage lengths in steps: 1, 1, 1, 4 (audio), 1, 1, after one step queued.
  const STARTS = [1, 2, 3, 4, 8, 9];

  it("is queued, then walks through the six stages, then is done", () => {
    expect(jobAt(stored("9000000001"), T0).state).toBe("queued");
    expect(jobAt(stored("9000000001"), T0).progress).toBeNull();
    STARTS.forEach((step, i) => {
      const job = jobAt(stored("9000000001"), T0 + step * MOCK_STEP_MS);
      expect(job.state).toBe("running");
      expect(job.progress?.stage_index).toBe(i + 1);
      expect(job.progress?.stage_count).toBe(6);
    });
    const done = jobAt(stored("9000000001"), T0 + 10 * MOCK_STEP_MS);
    expect(done.state).toBe("done");
    expect(done.progress).toBeNull();
    expect(done.started_at).toBe("2026-11-04T12:00:02Z");
    expect(done.finished_at).toBe("2026-11-04T12:00:20Z");
  });

  it("measures only the audio download, across several polls", () => {
    const at = (steps: number) =>
      jobAt(stored("9000000001"), T0 + steps * MOCK_STEP_MS);
    expect(at(4).progress?.stage).toBe("downloading_audio");
    expect(at(4).progress?.stage_fraction).toBeCloseTo(0);
    expect(at(5).progress?.stage_fraction).toBeCloseTo(0.25);
    expect(at(7).progress?.stage_fraction).toBeCloseTo(0.75);
    expect(at(2).progress?.stage_fraction).toBeNull();
  });

  it("starts a job only when the one before it has finished", () => {
    const first = { ...stored("9000000001"), job_id: "job_a" };
    const second = {
      ...stored("9000000002"),
      job_id: "job_b",
      created_ms: T0 + 100,
    };
    const starts = schedule({ job_a: first, job_b: second });
    expect(starts.get("job_a")).toBe(T0 + MOCK_STEP_MS);
    expect(starts.get("job_b")).toBe(T0 + 10 * MOCK_STEP_MS);
    const middle = T0 + 5 * MOCK_STEP_MS;
    expect(jobAt(second, middle, starts.get("job_b")).state).toBe("queued");
    expect(jobAt(first, middle, starts.get("job_a")).state).toBe("running");
  });

  it("fails 9000000004 at the first stage with vod_unavailable", () => {
    const job = jobAt(stored("9000000004"), T0 + 60_000);
    expect(job.state).toBe("failed");
    expect(job.error?.code).toBe("vod_unavailable");
    expect(job.progress?.stage).toBe("resolving_vod");
  });

  it("fails 9000000005 while downloading audio, with no measured fraction", () => {
    const job = jobAt(stored("9000000005"), T0 + 60_000);
    expect(job.error?.code).toBe("no_usable_signals");
    expect(job.progress).toEqual({
      stage: "downloading_audio",
      stage_index: 4,
      stage_count: 6,
      stage_fraction: null,
      message: "Downloading audio",
    });
    expect(job.warnings.map((w) => w.code)).toEqual([
      "chat_missing",
      "audio_missing",
    ]);
    const running = jobAt(stored("9000000005"), T0 + 5 * MOCK_STEP_MS);
    expect(running.progress?.stage_fraction).toBeNull();
  });
});

describe("createMockClient", () => {
  beforeEach(() => resetMockStore());

  it("runs a job to its scenario's result", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job, created } = await client.createJob({
      vod: "twitch.tv/videos/9000000002",
    });
    expect(created).toBe(true);
    expect(job.state).toBe("queued");
    await expect(client.getResult(job.job_id)).rejects.toMatchObject({
      status: 409,
      error: { code: "job_not_done", details: { state: "queued" } },
    });
    now += 12 * MOCK_STEP_MS;
    const result = await client.getResult(job.job_id);
    expect(result.vod.video_id).toBe("9000000002");
    expect(result.n_viewer_clips_total).toBe(0);
    expect(result.candidates.map((c) => c.source)).toEqual(["discovered"]);
  });

  it("returns the queued or running job for the same input", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const first = await client.createJob({ vod: "9000000011" });
    const again = await client.createJob({
      vod: " https://www.twitch.tv/videos/9000000011 ",
    });
    expect(again.created).toBe(false);
    expect(again.job.job_id).toBe(first.job.job_id);
    now += 60_000;
    const rerun = await client.createJob({ vod: "9000000011" });
    expect(rerun.created).toBe(true);
  });

  it("rejects bad links and relative paths with the contract's codes", async () => {
    const client = createMockClient(() => T0);
    await expect(
      client.createJob({ vod: "twitch.tv/user_001" }),
    ).rejects.toMatchObject({
      status: 422,
      error: { code: "invalid_vod_link" },
    });
    await expect(
      client.createJob({ vod: "9000000001", video_path: "vod.mp4" }),
    ).rejects.toMatchObject({ error: { code: "invalid_video_path" } });
  });

  it("keeps the scenarios' results", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const warned = (await client.createJob({ vod: "9000000003" })).job;
    now += 12 * MOCK_STEP_MS;
    const empty = (await client.createJob({ vod: "9000000006" })).job;
    now += 12 * MOCK_STEP_MS;
    const result = await client.getResult(warned.job_id);
    expect(result.warnings.map((w) => w.code)).toEqual(["chat_missing"]);
    expect((await client.getJob(warned.job_id)).warnings).toEqual(
      result.warnings,
    );
    expect((await client.getResult(empty.job_id)).candidates).toEqual([]);
  });

  it("gives 9000000007 media only with a local file", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const plain = (await client.createJob({ vod: "9000000007" })).job;
    now += 12 * MOCK_STEP_MS;
    const local = (
      await client.createJob({
        vod: "9000000007",
        video_path: "/videos/vod.mp4",
      })
    ).job;
    now += 12 * MOCK_STEP_MS;
    expect(
      (await client.getCandidateMedia(plain.job_id, "c001")).preview.status,
    ).toBe("unavailable");
    expect(
      (await client.getCandidateMedia(local.job_id, "c001")).preview.status,
    ).toBe("available");
  });

  it("answers an unknown job with job_not_found", async () => {
    const error = await createMockClient(() => T0)
      .getJob("job_missing")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect((error as ApiRequestError).error.code).toBe("job_not_found");
  });

  it("exports through its stages, skipping captions when they're off", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000021" });
    now += 12 * MOCK_STEP_MS;
    const { export: created } = await client.createExport({
      job_id: job.job_id,
      candidate_id: "c001",
      start_s: 118,
      end_s: 148,
      layout: "center_crop",
      captions: false,
    });
    expect(created.state).toBe("queued");
    now += MOCK_STEP_MS;
    expect((await client.getExport(created.export_id)).progress?.stage).toBe(
      "obtaining_media",
    );
    now += MOCK_STEP_MS;
    expect((await client.getExport(created.export_id)).progress?.stage).toBe(
      "rendering",
    );
    now += MOCK_STEP_MS;
    const done = await client.getExport(created.export_id);
    expect(done.state).toBe("done");
    expect(done.download_url).not.toBeNull();
  });

  it("refuses exports of 9000000007 and over-long ones", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const noMedia = (await client.createJob({ vod: "9000000007" })).job;
    const other = (await client.createJob({ vod: "9000000031" })).job;
    now += 24 * MOCK_STEP_MS; // both jobs, one after the other
    const request = {
      candidate_id: "c001",
      start_s: 0,
      end_s: 30,
      layout: "center_crop",
      captions: true,
    } as const;
    await expect(
      client.createExport({ ...request, job_id: noMedia.job_id }),
    ).rejects.toMatchObject({
      status: 409,
      error: {
        code: "media_unavailable",
        details: { reason: "source_unavailable" },
      },
    });
    await expect(
      client.createExport({ ...request, job_id: other.job_id, end_s: 100 }),
    ).rejects.toMatchObject({ error: { code: "invalid_export_bounds" } });
  });
});

describe("exportAt", () => {
  it("is queued first", () => {
    const stored = {
      export: {
        export_id: "exp_x",
        job_id: "job_x",
        candidate_id: "c001",
        video_id: "9000000001",
        start_s: 0,
        end_s: 30,
        layout: "center_crop",
        captions: true,
        created_at: "2026-11-04T12:00:00Z",
      },
      created_ms: T0,
    };
    expect(exportAt(stored, T0).state).toBe("queued");
    expect(exportAt(stored, T0 + 2 * MOCK_STEP_MS).progress?.stage).toBe(
      "transcribing",
    );
  });
});
