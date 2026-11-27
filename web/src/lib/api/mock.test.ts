import { beforeEach, describe, expect, it } from "vitest";

import { ApiRequestError } from "@/lib/api/errors";
import {
  createMockClient,
  exportAt,
  highlightsAt,
  jobAt,
  MOCK_RENDER_STEP_MS,
  MOCK_STEP_MS,
  renderSchedule,
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

  it("reports ok preflight in browser mock mode", async () => {
    const client = createMockClient(() => T0);
    await expect(client.getPreflight()).resolves.toEqual({
      status: "ok",
      checks: [],
    });
  });

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
    now += MOCK_RENDER_STEP_MS;
    expect((await client.getExport(created.export_id)).progress?.stage).toBe(
      "obtaining_media",
    );
    now += MOCK_RENDER_STEP_MS;
    expect((await client.getExport(created.export_id)).progress?.stage).toBe(
      "rendering",
    );
    now += MOCK_RENDER_STEP_MS;
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
  const item = (id: string, createdMs: number, captions = true) => ({
    export: {
      export_id: id,
      job_id: "job_x",
      candidate_id: "c001",
      video_id: "9000000001",
      start_s: 0,
      end_s: 30,
      layout: "center_crop",
      captions,
      created_at: "2026-11-04T12:00:00Z",
    },
    created_ms: createdMs,
  });

  it("is queued first, then renders its three stages", () => {
    const stored = item("exp_x", T0);
    expect(exportAt(stored, T0).state).toBe("queued");
    expect(exportAt(stored, T0 + 2 * MOCK_RENDER_STEP_MS).progress?.stage).toBe(
      "transcribing",
    );
    expect(exportAt(stored, T0 + 4 * MOCK_RENDER_STEP_MS).state).toBe("done");
  });

  it("keeps creation order for exports made in the same millisecond", async () => {
    resetMockStore();
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000041" });
    now += 12 * MOCK_STEP_MS;
    const request = {
      job_id: job.job_id,
      layout: "center_crop",
      captions: false,
    } as const;
    const ids: string[] = [];
    for (const [id, start] of [
      ["c001", 118],
      ["c002", 251.5],
      ["c003", 40],
    ] as const) {
      const { export: created } = await client.createExport({
        ...request,
        candidate_id: id,
        start_s: start,
        end_s: start + 20,
      });
      ids.push(created.export_id);
    }
    now += 3 * MOCK_RENDER_STEP_MS; // the first is done after its 2 stages, the others wait
    const states = await Promise.all(
      ids.map(async (id) => (await client.getExport(id)).state),
    );
    expect(states).toEqual(["done", "running", "queued"]);
  });

  it("renders one export at a time, in order", () => {
    const exports = {
      exp_a: item("exp_a", T0),
      exp_b: item("exp_b", T0 + 1, false),
    };
    const starts = renderSchedule(exports);
    expect(starts.get("exp_a")).toBe(T0 + MOCK_RENDER_STEP_MS);
    expect(starts.get("exp_b")).toBe(T0 + 4 * MOCK_RENDER_STEP_MS); // after a's three stages
    const at = T0 + 3 * MOCK_RENDER_STEP_MS;
    expect(exportAt(exports.exp_b, at, starts.get("exp_b")).state).toBe(
      "queued",
    );
    expect(exportAt(exports.exp_a, at, starts.get("exp_a")).state).toBe(
      "running",
    );
  });
});

describe("highlightsAt", () => {
  const item = (id: string, createdMs: number, captions = false) => ({
    highlights: {
      highlights_id: id,
      job_id: "job_x",
      video_id: "9000000001",
      segments: [
        { candidate_id: "c001", start_s: 118, end_s: 148 },
        { candidate_id: "c003", start_s: 40, end_s: 62 },
      ],
      order: "chronological" as const,
      captions,
      created_at: "2026-11-04T12:00:00Z",
    },
    created_ms: createdMs,
  });

  it("is queued first, then walks its stages, then is done with the total duration", () => {
    const stored = item("hl_x", T0);
    expect(highlightsAt(stored, T0).state).toBe("queued");
    expect(highlightsAt(stored, T0).duration_s).toBeNull();
    expect(
      highlightsAt(stored, T0 + MOCK_RENDER_STEP_MS).progress,
    ).toMatchObject({
      stage: "obtaining_media",
      stage_index: 1,
      stage_count: 3,
    });
    expect(
      highlightsAt(stored, T0 + MOCK_RENDER_STEP_MS).progress?.message,
    ).toBe("Getting the video");
    expect(
      highlightsAt(stored, T0 + 2 * MOCK_RENDER_STEP_MS).progress?.stage,
    ).toBe("rendering");
    const done = highlightsAt(stored, T0 + 3 * MOCK_RENDER_STEP_MS);
    expect(done.state).toBe("done");
    expect(done.progress).toBeNull();
    expect(done.download_url).toBe("/mock-media/highlights.mp4");
    expect(done.duration_s).toBeCloseTo(52); // 30 s + 22 s
  });

  it("transcribes between obtaining and rendering when captions are on", () => {
    const stored = item("hl_c", T0, true);
    expect(
      highlightsAt(stored, T0 + 2 * MOCK_RENDER_STEP_MS).progress?.stage,
    ).toBe("transcribing");
    expect(highlightsAt(stored, T0 + 4 * MOCK_RENDER_STEP_MS).state).toBe(
      "done",
    );
  });

  it("shares one render queue with exports, first in, first out", () => {
    const exports = {
      exp_a: {
        export: {
          export_id: "exp_a",
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
      },
    };
    const highlights = { hl_a: item("hl_a", T0 + 1) };
    const starts = renderSchedule(exports, highlights);
    expect(starts.get("exp_a")).toBe(T0 + MOCK_RENDER_STEP_MS);
    // after the export's three stages
    expect(starts.get("hl_a")).toBe(T0 + 4 * MOCK_RENDER_STEP_MS);
  });
});

describe("createMockClient highlights", () => {
  beforeEach(() => resetMockStore());

  it("makes a highlights video from the top candidates and walks to done", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000001" });
    now += 12 * MOCK_STEP_MS;

    const { highlights: created } = await client.createHighlights({
      job_id: job.job_id,
    });
    expect(created.state).toBe("queued");
    expect(created.segments.map((s) => s.candidate_id)).toEqual([
      "c003",
      "c001",
      "c002",
    ]); // stream order
    now += 3 * MOCK_RENDER_STEP_MS;
    const done = await client.getHighlights(created.highlights_id);
    expect(done.state).toBe("done");
    expect(done.download_url).toBe("/mock-media/highlights.mp4");
    expect(done.duration_s).toBeCloseTo(77); // 22 + 30 + 25
  });

  it("puts the best first with rank order and honours explicit trims", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000001" });
    now += 12 * MOCK_STEP_MS;

    const { highlights } = await client.createHighlights({
      job_id: job.job_id,
      order: "rank",
      count: 2,
    });
    expect(highlights.segments.map((s) => s.candidate_id)).toEqual([
      "c001",
      "c002",
    ]);

    const trimmed = await client.createHighlights({
      job_id: job.job_id,
      segments: [{ candidate_id: "c001", start_s: 120, end_s: 140 }],
    });
    expect(trimmed.highlights.segments).toEqual([
      { candidate_id: "c001", start_s: 120, end_s: 140 },
    ]);
    expect(trimmed.highlights.state).toBe("queued");
    now += 5 * MOCK_RENDER_STEP_MS; // behind the earlier request on the shared queue
    const done = await client.getHighlights(trimmed.highlights.highlights_id);
    expect(done.duration_s).toBeCloseTo(20);
  });

  it("returns the running video instead of making the same one twice", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000001" });
    now += 12 * MOCK_STEP_MS;
    const first = await client.createHighlights({ job_id: job.job_id });
    const again = await client.createHighlights({
      job_id: job.job_id,
      segments: first.highlights.segments,
    });
    expect(again.created).toBe(false);
    expect(again.highlights.highlights_id).toBe(first.highlights.highlights_id);
  });

  it("rejects the same bad requests the contract does", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const empty = (await client.createJob({ vod: "9000000006" })).job;
    const normal = (await client.createJob({ vod: "9000000011" })).job;
    now += 24 * MOCK_STEP_MS; // both jobs, one after the other

    await expect(
      client.createHighlights({ job_id: empty.job_id }),
    ).rejects.toMatchObject({
      status: 422,
      error: {
        code: "invalid_highlights_bounds",
        details: { segment_index: null },
      },
    });
    await expect(
      client.createHighlights({
        job_id: normal.job_id,
        segments: [{ candidate_id: "c999", start_s: 0, end_s: 30 }],
      }),
    ).rejects.toMatchObject({
      status: 404,
      error: { code: "candidate_not_found" },
    });
    await expect(
      client.createHighlights({
        job_id: normal.job_id,
        segments: [{ candidate_id: "c001", start_s: 0, end_s: 120 }],
      }),
    ).rejects.toMatchObject({
      status: 422,
      error: {
        code: "invalid_highlights_bounds",
        details: { segment_index: 0 },
      },
    });
  });

  it("reports bounds by ordered index and candidates before bounds", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000011" });
    now += 12 * MOCK_STEP_MS;

    // An unknown candidate is reported even when an earlier segment breaks the bounds.
    await expect(
      client.createHighlights({
        job_id: job.job_id,
        segments: [
          { candidate_id: "c001", start_s: 0, end_s: 120 },
          { candidate_id: "c999", start_s: 0, end_s: 30 },
        ],
      }),
    ).rejects.toMatchObject({
      status: 404,
      error: { code: "candidate_not_found" },
    });
    // The offending segment's index is into the resolved, ordered list (§ 7.1): c001's
    // over-long trim is input index 0 but ordered index 1, after c003's earlier start.
    await expect(
      client.createHighlights({
        job_id: job.job_id,
        segments: [
          { candidate_id: "c001", start_s: 200, end_s: 320 },
          { candidate_id: "c003", start_s: 40, end_s: 62 },
          { candidate_id: "c002", start_s: 251.5, end_s: 276.5 },
        ],
      }),
    ).rejects.toMatchObject({
      status: 422,
      error: {
        code: "invalid_highlights_bounds",
        details: { segment_index: 1 },
      },
    });
  });

  it("checks every candidate before any media availability", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const noMedia = (await client.createJob({ vod: "9000000007" })).job;
    now += 12 * MOCK_STEP_MS;

    // c001 has no media in this scenario and c999 doesn't exist: the contract's check
    // order still answers candidate_not_found first (§ 7.1).
    await expect(
      client.createHighlights({
        job_id: noMedia.job_id,
        segments: [
          { candidate_id: "c001", start_s: 0, end_s: 30 },
          { candidate_id: "c999", start_s: 0, end_s: 30 },
        ],
      }),
    ).rejects.toMatchObject({
      status: 404,
      error: { code: "candidate_not_found" },
    });
  });

  it("keeps one first-in-first-out queue across exports and highlights", async () => {
    resetMockStore();
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000041" });
    now += 12 * MOCK_STEP_MS;
    // Three renders in the same millisecond: two exports, then a highlights video.
    const ids: string[] = [];
    for (const [id, start] of [
      ["c001", 118],
      ["c002", 251.5],
    ] as const) {
      const { export: created } = await client.createExport({
        job_id: job.job_id,
        candidate_id: id,
        start_s: start,
        end_s: start + 20,
        layout: "center_crop",
        captions: false,
      });
      ids.push(created.export_id);
    }
    const { highlights } = await client.createHighlights({
      job_id: job.job_id,
    });

    now += 4 * MOCK_RENDER_STEP_MS; // the first export is done, the second is rendering
    const states = await Promise.all([
      client.getExport(ids[0]).then((e) => e.state),
      client.getExport(ids[1]).then((e) => e.state),
      client.getHighlights(highlights.highlights_id).then((v) => v.state),
    ]);
    expect(states).toEqual(["done", "running", "queued"]); // the video doesn't jump the queue
  });

  it("answers an unknown highlights video with highlights_not_found", async () => {
    const error = await createMockClient(() => T0)
      .getHighlights("hl_missing")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiRequestError);
    expect((error as ApiRequestError).error.code).toBe("highlights_not_found");
  });

  it("enforces the 0.5 s minimum on vertical clip exports while allowing sub-0.5 s highlights", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const { job } = await client.createJob({ vod: "9000000001" });
    now += 12 * MOCK_STEP_MS;

    await expect(
      client.createExport({
        job_id: job.job_id,
        candidate_id: "c001",
        start_s: 120,
        end_s: 120.1,
        layout: "center_crop",
        captions: false,
      }),
    ).rejects.toMatchObject({
      status: 422,
      error: {
        code: "invalid_export_bounds",
        message: expect.stringContaining("at least 0.5 seconds"),
      },
    });

    // Exact 0.5 s boundary (including float subtraction 4.1 - 3.6) succeeds for exports.
    const atMin = await client.createExport({
      job_id: job.job_id,
      candidate_id: "c001",
      start_s: 3.6,
      end_s: 4.1,
      layout: "center_crop",
      captions: false,
    });
    expect(atMin.created).toBe(true);

    // Highlights keep their separate minimum behavior and still accept a 0.1 s segment.
    const shortHl = await client.createHighlights({
      job_id: job.job_id,
      segments: [{ candidate_id: "c001", start_s: 120, end_s: 120.1 }],
    });
    expect(shortHl.created).toBe(true);
  });
});

describe("createMockClient channels", () => {
  beforeEach(() => resetMockStore());

  it("lists channel VODs, tracks finished session jobs, and slices to limit", async () => {
    let now = T0;
    const client = createMockClient(() => now);

    const initial = await client.getChannelVods("https://twitch.tv/User_001");
    expect(initial.channel.login).toBe("user_001");
    expect(initial.channel.display_name).toBe("User_001");
    expect(initial.truncated).toBe(false);
    expect(initial.vods).toHaveLength(5);

    const byId = new Map(initial.vods.map((v) => [v.video_id, v]));
    expect(byId.get("9000000001")?.analyzed).toBe(false);
    expect(byId.get("9000000002")?.analyzed).toBe(true);

    const { job } = await client.createJob({ vod: "9000000001" });
    now += 12 * MOCK_STEP_MS;
    const after = await client.getChannelVods("user_001");
    const updated = after.vods.find((v) => v.video_id === "9000000001");
    expect(updated?.analyzed).toBe(true);
    expect(updated?.last_job_id).toBe(job.job_id);

    const limited = await client.getChannelVods("user_001", 2);
    expect(limited.vods).toHaveLength(2);
    expect(limited.truncated).toBe(true);
  });

  it("serves empty, unknown, missing-credentials, error and invalid channel scenarios", async () => {
    const client = createMockClient(() => T0);

    const empty = await client.getChannelVods("empty_channel");
    expect(empty.vods).toEqual([]);
    expect(empty.truncated).toBe(false);

    await expect(
      client.getChannelVods("unknown_channel"),
    ).rejects.toMatchObject({
      status: 404,
      error: { code: "channel_not_found" },
    });
    await expect(client.getChannelVods("no_credentials")).rejects.toMatchObject(
      {
        status: 503,
        error: { code: "twitch_credentials_missing" },
      },
    );
    await expect(client.getChannelVods("twitch_error")).rejects.toMatchObject({
      status: 502,
      error: { code: "upstream_unavailable" },
    });
    await expect(
      client.getChannelVods("https://www.twitch.tv/videos/9000000001"),
    ).rejects.toMatchObject({
      status: 422,
      error: { code: "invalid_channel" },
    });
    await expect(client.getChannelVods("user_001", 0)).rejects.toMatchObject({
      status: 422,
      error: { code: "invalid_request" },
    });
  });
});

describe("createMockClient library (listJobs and deleteJob)", () => {
  beforeEach(() => resetMockStore());

  it("lists jobs newest first with metadata, progress, renders, filters, and pagination", async () => {
    let now = T0;
    const client = createMockClient(() => now);

    const empty = await client.listJobs();
    expect(empty).toEqual({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
      has_more: false,
    });

    const first = (await client.createJob({ vod: "9000000001" })).job;
    now += 15 * MOCK_STEP_MS;
    await client.createExport({
      job_id: first.job_id,
      candidate_id: "c001",
      start_s: 118,
      end_s: 148,
      layout: "center_crop",
      captions: false,
    });
    now += 5 * MOCK_RENDER_STEP_MS;

    const failed = (await client.createJob({ vod: "9000000004" })).job;
    now += 5 * MOCK_STEP_MS;
    const running = (await client.createJob({ vod: "9000000003" })).job;
    now += 2 * MOCK_STEP_MS; // running stage 2

    const page1 = await client.listJobs({ limit: 2, offset: 0 });
    expect(page1.total).toBe(3);
    expect(page1.has_more).toBe(true);
    expect(page1.items.map((i) => i.job_id)).toEqual([
      running.job_id,
      failed.job_id,
    ]);
    expect(page1.items[0].state).toBe("running");
    expect(page1.items[0].progress?.stage).toBe("fetching_clips");
    expect(page1.items[1].state).toBe("failed");
    expect(page1.items[1].error?.code).toBe("vod_unavailable");

    const page2 = await client.listJobs({ limit: 2, offset: 2 });
    expect(page2.has_more).toBe(false);
    expect(page2.items).toHaveLength(1);
    expect(page2.items[0]).toMatchObject({
      job_id: first.job_id,
      state: "done",
      progress: null,
      title: "Synthetic stream 1",
      broadcaster_display_name: "user_001",
      candidate_count: 3,
      result_available: true,
      export_count: 1,
      highlights_count: 0,
    });

    const filtered = await client.listJobs({ q: "user_001", state: "done" });
    expect(filtered.items.map((i) => i.job_id)).toEqual([first.job_id]);
  });

  it("deletes finished jobs with optional export removal and rejects in-progress jobs", async () => {
    let now = T0;
    const client = createMockClient(() => now);
    const active = (await client.createJob({ vod: "9000000001" })).job;

    await expect(client.deleteJob(active.job_id)).rejects.toMatchObject({
      status: 409,
      error: { code: "job_in_progress" },
    });

    now += 12 * MOCK_STEP_MS;
    await client.createExport({
      job_id: active.job_id,
      candidate_id: "c001",
      start_s: 118,
      end_s: 148,
      layout: "center_crop",
      captions: false,
    });
    now += 4 * MOCK_RENDER_STEP_MS;

    const kept = await client.deleteJob(active.job_id);
    expect(kept).toEqual({
      job_id: active.job_id,
      deleted: true,
      deleted_files: 0,
    });
    await expect(client.getJob(active.job_id)).rejects.toMatchObject({
      status: 404,
      error: { code: "job_not_found" },
    });

    const second = (await client.createJob({ vod: "9000000002" })).job;
    now += 12 * MOCK_STEP_MS;
    await client.createHighlights({ job_id: second.job_id });
    now += 4 * MOCK_RENDER_STEP_MS;

    const wiped = await client.deleteJob(second.job_id, {
      deleteExports: true,
    });
    expect(wiped).toEqual({
      job_id: second.job_id,
      deleted: true,
      deleted_files: 1,
    });
  });
});
