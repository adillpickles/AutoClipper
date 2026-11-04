import { describe, expect, it } from "vitest";

import { ApiRequestError } from "@/lib/api/errors";
import {
  createMockClient,
  exportAt,
  jobAt,
  MOCK_STEP_MS,
} from "@/lib/api/mock";

const T0 = Date.UTC(2026, 10, 4, 12, 0, 0);
const stored = (videoId: string) => ({
  job_id: "job_test",
  video_id: videoId,
  video_path: null,
  created_ms: T0,
});

describe("jobAt", () => {
  it("is queued, then walks through the six stages, then is done", () => {
    expect(jobAt(stored("9000000001"), T0).state).toBe("queued");
    expect(jobAt(stored("9000000001"), T0).progress).toBeNull();
    for (let stage = 1; stage <= 6; stage++) {
      const job = jobAt(stored("9000000001"), T0 + stage * MOCK_STEP_MS);
      expect(job.state).toBe("running");
      expect(job.progress?.stage_index).toBe(stage);
      expect(job.progress?.stage_count).toBe(6);
    }
    const done = jobAt(stored("9000000001"), T0 + 7 * MOCK_STEP_MS);
    expect(done.state).toBe("done");
    expect(done.progress).toBeNull();
    expect(done.finished_at).toBe("2026-11-04T12:00:14Z");
  });

  it("measures only the audio download", () => {
    const audio = jobAt(
      stored("9000000001"),
      T0 + 4 * MOCK_STEP_MS + MOCK_STEP_MS / 2,
    );
    expect(audio.progress?.stage).toBe("downloading_audio");
    expect(audio.progress?.stage_fraction).toBeCloseTo(0.5);
    expect(
      jobAt(stored("9000000001"), T0 + 2 * MOCK_STEP_MS).progress
        ?.stage_fraction,
    ).toBeNull();
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
    const running = jobAt(stored("9000000005"), T0 + 4 * MOCK_STEP_MS + 500);
    expect(running.progress?.stage_fraction).toBeNull();
  });
});

describe("createMockClient", () => {
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
    now += 8 * MOCK_STEP_MS;
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
    now += 8 * MOCK_STEP_MS;
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
    now += 8 * MOCK_STEP_MS;
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
