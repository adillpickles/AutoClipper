// Mock mode of the API client: the § 11 scenarios of docs/architecture/api-contract.md, run in
// the browser. A job's state follows from the time since it was created (queued for one step,
// then one step per stage), so polling shows queued, running and the final state, and a page
// reload or a new tab picks up where it was: jobs are kept in localStorage, like the server keeps
// them until it restarts.

import type { ApiClient } from "@/lib/api/client";
import { ApiRequestError } from "@/lib/api/errors";
import chatMissing from "@/lib/api/fixtures/result-chat-missing.json";
import emptyResult from "@/lib/api/fixtures/result-empty.json";
import exampleResult from "@/lib/api/fixtures/result-example.json";
import noClips from "@/lib/api/fixtures/result-no-clips.json";
import type {
  AnalysisResult,
  AnalysisWarning,
  ApiError,
  CandidateMedia,
  CreateExportRequest,
  CreateHighlightsRequest,
  CreateJobRequest,
  Export,
  HighlightsSegment,
  HighlightsVideo,
  Job,
  Progress,
} from "@/lib/api/types";
import { EXPORT_STAGES, HIGHLIGHTS_STAGES, JOB_STAGES } from "@/lib/api/stages";
import { INVALID_VOD_MESSAGE, looksAbsolute, parseVod } from "@/lib/api/vod";

/** Length of one mock step (queued, then each stage). */
export const MOCK_STEP_MS = 2000;

const STORAGE_KEY = "autoclipper.mock.v1";
const NO_MEDIA_VIDEO_ID = "9000000007";
/** Sample files the results screen (#30) serves from `public/mock-media/`. */
const PREVIEW_URL = "/mock-media/preview.mp4";
const CLIP_URL = "/mock-media/clip.mp4";
const HIGHLIGHTS_URL = "/mock-media/highlights.mp4";

interface Scenario {
  result?: AnalysisResult;
  error?: ApiError;
  failAtStage?: number; // 1-based
  warnings: AnalysisWarning[];
  fractions: boolean; // whether downloading_audio reports a measured fraction
}

const CHAT_MISSING: AnalysisWarning = {
  code: "chat_missing",
  message:
    "Chat replay isn't available for this VOD, so moments are ranked from audio and viewer clips only.",
};
const AUDIO_MISSING: AnalysisWarning = {
  code: "audio_missing",
  message:
    "Audio isn't available for this VOD, so moments are ranked from chat and viewer clips only.",
};

function scenarioFor(videoId: string): Scenario {
  const result = (fixture: unknown): AnalysisResult =>
    fixture as AnalysisResult;
  switch (videoId) {
    case "9000000002":
      return { result: result(noClips), warnings: [], fractions: true };
    case "9000000003":
      return {
        result: result(chatMissing),
        warnings: [CHAT_MISSING],
        fractions: true,
      };
    case "9000000004":
      return {
        error: {
          code: "vod_unavailable",
          message:
            "This VOD isn't available on Twitch. It may have been deleted, expired or made private.",
          details: null,
        },
        failAtStage: 1,
        warnings: [],
        fractions: false,
      };
    case "9000000005":
      return {
        error: {
          code: "no_usable_signals",
          message:
            "This VOD has no chat replay, no usable audio and no viewer clips, so there's nothing to rank.",
          details: null,
        },
        failAtStage: 4,
        warnings: [CHAT_MISSING, AUDIO_MISSING],
        fractions: false,
      };
    case "9000000006":
      return { result: result(emptyResult), warnings: [], fractions: true };
    default: {
      const base = result(exampleResult);
      return {
        result: { ...base, vod: { ...base.vod, video_id: videoId } },
        warnings: [],
        fractions: true,
      };
    }
  }
}

interface StoredJob {
  job_id: string;
  video_id: string;
  video_path: string | null;
  created_ms: number;
}

interface StoredExport {
  export: Omit<
    Export,
    | "state"
    | "progress"
    | "error"
    | "download_url"
    | "started_at"
    | "updated_at"
    | "finished_at"
  >;
  created_ms: number;
  /** Creation order; exports made in the same millisecond still render first in, first out. */
  seq?: number;
}

interface StoredHighlights {
  highlights: Omit<
    HighlightsVideo,
    | "state"
    | "progress"
    | "error"
    | "download_url"
    | "duration_s"
    | "started_at"
    | "updated_at"
    | "finished_at"
  >;
  created_ms: number;
  /** Creation order within highlights videos; renders interleave with exports by time (§ 9.3). */
  seq?: number;
}

interface Store {
  jobs: Record<string, StoredJob>;
  exports: Record<string, StoredExport>;
  highlights: Record<string, StoredHighlights>;
  /** One enqueue counter across both render kinds, so equal timestamps stay first in, first out. */
  next_seq: number;
}

let memory: Store = { jobs: {}, exports: {}, highlights: {}, next_seq: 0 };

function load(): Store {
  try {
    const text = window.localStorage.getItem(STORAGE_KEY);
    if (text) {
      const parsed = JSON.parse(text) as Partial<Store>;
      memory = {
        jobs: parsed.jobs ?? {},
        exports: parsed.exports ?? {},
        highlights: parsed.highlights ?? {},
        next_seq: parsed.next_seq ?? 0,
      };
    }
  } catch {
    // No localStorage (blocked, server render): keep the in-memory copy.
  }
  return memory;
}

function save(store: Store): void {
  memory = store;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // See load().
  }
}

function randomId(prefix: string): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return `${prefix}_${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function notFound(code: string, message: string): ApiRequestError {
  return new ApiRequestError(404, { code, message, details: null });
}

const JOB_NOT_FOUND = () =>
  notFound(
    "job_not_found",
    "This analysis no longer exists. The server may have restarted; analyze the VOD again.",
  );

/**
 * Steps each analysis stage takes in the mock. The audio download is the one stage with a
 * measured fraction, so it lasts several polls and the progress bar visibly moves.
 */
const STAGE_STEPS: readonly number[] = [1, 1, 1, 4, 1, 1];

function runSteps(videoId: string): number {
  const lastStage = scenarioFor(videoId).failAtStage ?? JOB_STAGES.length;
  return STAGE_STEPS.slice(0, lastStage).reduce((a, b) => a + b, 0);
}

/**
 * When each job starts running: one analysis at a time, first in first out (§ 9.3), and never
 * before it has been queued for one step.
 */
export function schedule(
  jobs: Record<string, StoredJob>,
  stepMs = MOCK_STEP_MS,
): Map<string, number> {
  const starts = new Map<string, number>();
  let free = -Infinity;
  const ordered = Object.values(jobs).sort(
    (a, b) => a.created_ms - b.created_ms || a.job_id.localeCompare(b.job_id),
  );
  for (const job of ordered) {
    const start = Math.max(job.created_ms + stepMs, free);
    starts.set(job.job_id, start);
    free = start + runSteps(job.video_id) * stepMs;
  }
  return starts;
}

/**
 * The job as the mock server would report it `now` (milliseconds since the epoch), given when
 * it starts running (`startMs`, from `schedule`; by default as if it were alone).
 */
export function jobAt(
  stored: StoredJob,
  now: number,
  startMs = stored.created_ms + MOCK_STEP_MS,
  stepMs = MOCK_STEP_MS,
): Job {
  const scenario = scenarioFor(stored.video_id);
  const lastStage = scenario.failAtStage ?? JOB_STAGES.length;
  const base = {
    job_id: stored.job_id,
    video_id: stored.video_id,
    video_path: stored.video_path,
    created_at: iso(stored.created_ms),
  };
  if (now < startMs) {
    return {
      ...base,
      state: "queued",
      progress: null,
      warnings: [],
      error: null,
      started_at: null,
      updated_at: base.created_at,
      finished_at: null,
    };
  }
  const startedAt = iso(startMs);
  let stageStart = startMs;
  for (let index = 1; index <= lastStage; index++) {
    const length = STAGE_STEPS[index - 1] * stepMs;
    if (now < stageStart + length) {
      const fraction =
        scenario.fractions &&
        JOB_STAGES[index - 1].stage === "downloading_audio"
          ? Math.min(1, (now - stageStart) / length)
          : null;
      return {
        ...base,
        state: "running",
        progress: progress(JOB_STAGES, index, fraction),
        warnings: [],
        error: null,
        started_at: startedAt,
        updated_at: iso(now),
        finished_at: null,
      };
    }
    stageStart += length;
  }
  const finishedAt = iso(stageStart);
  if (scenario.error) {
    return {
      ...base,
      state: "failed",
      progress: progress(JOB_STAGES, lastStage, null),
      warnings: scenario.warnings,
      error: scenario.error,
      started_at: startedAt,
      updated_at: finishedAt,
      finished_at: finishedAt,
    };
  }
  return {
    ...base,
    state: "done",
    progress: null,
    warnings: scenario.warnings,
    error: null,
    started_at: startedAt,
    updated_at: finishedAt,
    finished_at: finishedAt,
  };
}

function jobNow(store: Store, jobId: string, now: number): Job | null {
  const stored = store.jobs[jobId];
  if (!stored) return null;
  return jobAt(stored, now, schedule(store.jobs).get(jobId));
}

function progress(
  stages: ReadonlyArray<{ stage: string; message: string }>,
  index: number,
  fraction: number | null,
): Progress {
  const { stage, message } = stages[index - 1];
  return {
    stage,
    stage_index: index,
    stage_count: stages.length,
    stage_fraction: fraction,
    message,
  };
}

function doneJob(
  jobId: string,
  now: number,
): { stored: StoredJob; result: AnalysisResult } {
  const store = load();
  const stored = store.jobs[jobId];
  const job = jobNow(store, jobId, now);
  if (!stored || !job) throw JOB_NOT_FOUND();
  if (job.state !== "done") {
    throw new ApiRequestError(409, {
      code: "job_not_done",
      message: "This analysis hasn't finished yet.",
      details: { state: job.state },
    });
  }
  const result = scenarioFor(stored.video_id).result as AnalysisResult;
  return {
    stored,
    result: { ...result, generated_at: job.finished_at ?? result.generated_at },
  };
}

function mediaFor(
  stored: StoredJob,
  result: AnalysisResult,
  candidateId: string,
): CandidateMedia {
  const candidate = result.candidates.find(
    (c) => c.candidate_id === candidateId,
  );
  if (!candidate)
    throw notFound(
      "candidate_not_found",
      "This moment isn't part of the analysis.",
    );
  const duration = result.vod.duration_s;
  if (stored.video_id === NO_MEDIA_VIDEO_ID && stored.video_path === null) {
    return {
      job_id: stored.job_id,
      candidate_id: candidateId,
      video_id: stored.video_id,
      preview: {
        status: "unavailable",
        kind: null,
        url: null,
        media_start_s: null,
        media_end_s: null,
        reason: "source_unavailable",
        message:
          "Preview isn't available: Twitch doesn't offer this VOD's video for download. It may have been deleted or be subscriber-only.",
      },
      export: {
        status: "unavailable",
        reason: "source_unavailable",
        message:
          "Export isn't available: Twitch doesn't offer this VOD's video for download. If you have the video file, analyze the VOD again with it.",
        min_start_s: null,
        max_end_s: null,
        max_duration_s: null,
        layouts: [],
      },
    };
  }
  return {
    job_id: stored.job_id,
    candidate_id: candidateId,
    video_id: stored.video_id,
    preview: {
      status: "available",
      kind: "video_file",
      url: PREVIEW_URL,
      media_start_s: 0,
      media_end_s: duration,
      reason: null,
      message: null,
    },
    export: {
      status: "available",
      reason: null,
      message: null,
      min_start_s: 0,
      max_end_s: duration,
      max_duration_s: 90,
      // Like `autoclipper serve` without AUTOCLIPPER_EXPORT_REGION (contract § 5).
      layouts: ["center_crop"],
    },
  };
}

/** Length of one render step: renders are quick in the mock so the cards fill in visibly. */
export const MOCK_RENDER_STEP_MS = 800;

function renderStages(captions: boolean) {
  return EXPORT_STAGES.map((s, i) => ({ ...s, index: i + 1 })).filter(
    (s) => s.stage !== "transcribing" || captions,
  );
}

function highlightsStages(captions: boolean) {
  return HIGHLIGHTS_STAGES.map((s, i) => ({ ...s, index: i + 1 })).filter(
    (s) => s.stage !== "transcribing" || captions,
  );
}

/** One queued render of either kind: what the shared render worker (§ 9.3) needs to plan. */
interface RenderSlot {
  id: string;
  created_ms: number;
  seq: number;
  steps: number;
}

function renderSlots(
  exports: Record<string, StoredExport>,
  highlights: Record<string, StoredHighlights>,
): RenderSlot[] {
  return [
    ...Object.values(exports).map((item) => ({
      id: item.export.export_id,
      created_ms: item.created_ms,
      seq: item.seq ?? 0,
      steps: renderStages(item.export.captions).length,
    })),
    ...Object.values(highlights).map((item) => ({
      id: item.highlights.highlights_id,
      created_ms: item.created_ms,
      seq: item.seq ?? 0,
      steps: highlightsStages(item.highlights.captions).length,
    })),
  ];
}

/**
 * When each render starts: one render at a time, first in first out across exports and
 * highlights videos (§ 9.3), after being queued for at least one step.
 */
export function renderSchedule(
  exports: Record<string, StoredExport>,
  highlights: Record<string, StoredHighlights> = {},
  stepMs = MOCK_RENDER_STEP_MS,
): Map<string, number> {
  const starts = new Map<string, number>();
  let free = -Infinity;
  const ordered = renderSlots(exports, highlights).sort(
    (a, b) =>
      a.created_ms - b.created_ms || a.seq - b.seq || a.id.localeCompare(b.id),
  );
  for (const slot of ordered) {
    const start = Math.max(slot.created_ms + stepMs, free);
    starts.set(slot.id, start);
    free = start + slot.steps * stepMs;
  }
  return starts;
}

/** The export as the mock server would report it at `now`, given when it starts rendering. */
export function exportAt(
  stored: StoredExport,
  now: number,
  startMs = stored.created_ms + MOCK_RENDER_STEP_MS,
  stepMs = MOCK_RENDER_STEP_MS,
): Export {
  const stages = renderStages(stored.export.captions);
  const base = stored.export;
  if (now < startMs) {
    return {
      ...base,
      state: "queued",
      progress: null,
      error: null,
      download_url: null,
      started_at: null,
      updated_at: base.created_at,
      finished_at: null,
    };
  }
  const step = Math.floor((now - startMs) / stepMs);
  if (step < stages.length) {
    const current = stages[step];
    return {
      ...base,
      state: "running",
      progress: {
        stage: current.stage,
        stage_index: current.index,
        stage_count: EXPORT_STAGES.length,
        stage_fraction: null,
        message: current.message,
      },
      error: null,
      download_url: null,
      started_at: iso(startMs),
      updated_at: iso(now),
      finished_at: null,
    };
  }
  const finishedAt = iso(startMs + stages.length * stepMs);
  return {
    ...base,
    state: "done",
    progress: null,
    error: null,
    download_url: CLIP_URL,
    started_at: iso(startMs),
    updated_at: finishedAt,
    finished_at: finishedAt,
  };
}

function exportNow(store: Store, exportId: string, now: number): Export | null {
  const stored = store.exports[exportId];
  if (!stored) return null;
  return exportAt(
    stored,
    now,
    renderSchedule(store.exports, store.highlights).get(exportId),
  );
}

/** The highlights video as the mock server would report it at `now` (§ 7.2). */
export function highlightsAt(
  stored: StoredHighlights,
  now: number,
  startMs = stored.created_ms + MOCK_RENDER_STEP_MS,
  stepMs = MOCK_RENDER_STEP_MS,
): HighlightsVideo {
  const stages = highlightsStages(stored.highlights.captions);
  const base = stored.highlights;
  if (now < startMs) {
    return {
      ...base,
      state: "queued",
      progress: null,
      error: null,
      download_url: null,
      duration_s: null,
      started_at: null,
      updated_at: base.created_at,
      finished_at: null,
    };
  }
  const step = Math.floor((now - startMs) / stepMs);
  if (step < stages.length) {
    const current = stages[step];
    return {
      ...base,
      state: "running",
      progress: {
        stage: current.stage,
        stage_index: current.index,
        stage_count: HIGHLIGHTS_STAGES.length,
        stage_fraction: null,
        message: current.message,
      },
      error: null,
      download_url: null,
      duration_s: null,
      started_at: iso(startMs),
      updated_at: iso(now),
      finished_at: null,
    };
  }
  const finishedAt = iso(startMs + stages.length * stepMs);
  return {
    ...base,
    state: "done",
    progress: null,
    error: null,
    download_url: HIGHLIGHTS_URL,
    duration_s: base.segments.reduce(
      (sum, segment) => sum + (segment.end_s - segment.start_s),
      0,
    ),
    started_at: iso(startMs),
    updated_at: finishedAt,
    finished_at: finishedAt,
  };
}

function highlightsNow(
  store: Store,
  highlightsId: string,
  now: number,
): HighlightsVideo | null {
  const stored = store.highlights[highlightsId];
  if (!stored) return null;
  return highlightsAt(
    stored,
    now,
    renderSchedule(store.exports, store.highlights).get(highlightsId),
  );
}

export function createMockClient(now: () => number = Date.now): ApiClient {
  return {
    async getHealth() {
      return {
        status: "ok" as const,
        schema_version: "1.0",
        job_runner: "mock" as const,
      };
    },

    async createJob(request: CreateJobRequest) {
      const videoId = parseVod(request.vod);
      if (videoId === null) {
        throw new ApiRequestError(422, {
          code: "invalid_vod_link",
          message: INVALID_VOD_MESSAGE,
          details: null,
        });
      }
      const videoPath = request.video_path?.trim()
        ? request.video_path.trim()
        : null;
      if (videoPath !== null && !looksAbsolute(videoPath)) {
        throw new ApiRequestError(422, {
          code: "invalid_video_path",
          message:
            "That video file can't be opened. Check the path and that it's a complete video file.",
          details: null,
        });
      }
      const store = load();
      const at = now();
      for (const stored of Object.values(store.jobs)) {
        const job = jobNow(store, stored.job_id, at) as Job;
        if (
          stored.video_id === videoId &&
          stored.video_path === videoPath &&
          (job.state === "queued" || job.state === "running")
        ) {
          return { job, created: false };
        }
      }
      const stored: StoredJob = {
        job_id: randomId("job"),
        video_id: videoId,
        video_path: videoPath,
        created_ms: at,
      };
      const next = {
        ...store,
        jobs: { ...store.jobs, [stored.job_id]: stored },
      };
      save(next);
      return { job: jobNow(next, stored.job_id, at) as Job, created: true };
    },

    async getJob(jobId: string) {
      const job = jobNow(load(), jobId, now());
      if (!job) throw JOB_NOT_FOUND();
      return job;
    },

    async getResult(jobId: string) {
      return doneJob(jobId, now()).result;
    },

    async getCandidateMedia(jobId: string, candidateId: string) {
      const { stored, result } = doneJob(jobId, now());
      return mediaFor(stored, result, candidateId);
    },

    async createExport(request: CreateExportRequest) {
      const { stored, result } = doneJob(request.job_id, now());
      const media = mediaFor(stored, result, request.candidate_id).export;
      if (media.status !== "available") {
        throw new ApiRequestError(409, {
          code: "media_unavailable",
          message: media.message ?? "",
          details: { reason: media.reason },
        });
      }
      if (!media.layouts.includes(request.layout)) {
        throw new ApiRequestError(422, {
          code: "invalid_request",
          message: `The request is invalid: layout ${request.layout} isn't available.`,
          details: { fields: ["layout"] },
        });
      }
      const min = media.min_start_s ?? 0;
      const max = media.max_end_s ?? 0;
      const longest = media.max_duration_s ?? 0;
      if (!(
        min <= request.start_s &&
        request.start_s < request.end_s &&
        request.end_s <= max &&
        request.end_s - request.start_s <= longest
      )) {
        throw new ApiRequestError(422, {
          code: "invalid_export_bounds",
          message:
            "Clips can be at most 90 seconds long. Shorten the selection and try again.",
          details: {
            min_start_s: min,
            max_end_s: max,
            max_duration_s: longest,
          },
        });
      }
      const store = load();
      const at = now();
      for (const existing of Object.values(store.exports)) {
        const current = exportNow(
          store,
          existing.export.export_id,
          at,
        ) as Export;
        const same = (
          [
            "job_id",
            "candidate_id",
            "start_s",
            "end_s",
            "layout",
            "captions",
          ] as const
        ).every((key) => existing.export[key] === request[key]);
        if (
          same &&
          (current.state === "queued" || current.state === "running")
        ) {
          return { export: current, created: false };
        }
      }
      const seq = (store.next_seq ?? 0) + 1;
      const created: StoredExport = {
        export: {
          export_id: randomId("exp"),
          video_id: stored.video_id,
          created_at: iso(at),
          ...request,
        },
        created_ms: at,
        seq,
      };
      const next = {
        ...store,
        next_seq: seq,
        exports: { ...store.exports, [created.export.export_id]: created },
      };
      save(next);
      return {
        export: exportNow(next, created.export.export_id, at) as Export,
        created: true,
      };
    },

    async getExport(exportId: string) {
      const stored = load().exports[exportId];
      if (!stored) {
        throw notFound(
          "export_not_found",
          "This export no longer exists. The server may have restarted; export the clip again.",
        );
      }
      return exportNow(load(), exportId, now()) as Export;
    },

    async createHighlights(request: CreateHighlightsRequest) {
      const { stored, result } = doneJob(request.job_id, now());
      const order = request.order ?? "chronological";
      const count = request.count ?? 10;
      if (count < 1 || count > 20) {
        throw new ApiRequestError(422, {
          code: "invalid_request",
          message: "The request is invalid: count must be between 1 and 20.",
          details: { fields: ["count"] },
        });
      }
      let segments: HighlightsSegment[];
      if (request.segments != null) {
        if (request.segments.length === 0) {
          throw new ApiRequestError(422, {
            code: "invalid_request",
            message: "The request is invalid: segments can't be empty.",
            details: { fields: ["segments"] },
          });
        }
        if (request.segments.length > 20) {
          throw new ApiRequestError(422, {
            code: "invalid_request",
            message:
              "The request is invalid: a highlights video needs 1 to 20 segments.",
            details: { fields: ["segments"] },
          });
        }
        segments = [...request.segments];
      } else {
        segments = result.candidates.slice(0, count).map((candidate) => ({
          candidate_id: candidate.candidate_id,
          start_s: candidate.start_s,
          end_s: candidate.end_s,
        }));
        if (segments.length === 0) {
          throw new ApiRequestError(422, {
            code: "invalid_highlights_bounds",
            message:
              "This analysis has no moments to put in a highlights video.",
            details: {
              segment_index: null,
              max_duration_s: 90,
              max_total_duration_s: 900,
            },
          });
        }
      }
      // The contract's check order (§ 7.1): every segment's candidate, then export
      // availability for those candidates, then the bounds on the resolved, ordered list.
      const byId = new Map(result.candidates.map((c) => [c.candidate_id, c]));
      for (const segment of segments) {
        if (!byId.has(segment.candidate_id)) {
          throw notFound(
            "candidate_not_found",
            `The analysis has no candidate ${segment.candidate_id}.`,
          );
        }
      }
      for (const segment of segments) {
        const media = mediaFor(stored, result, segment.candidate_id).export;
        if (media.status !== "available") {
          throw new ApiRequestError(409, {
            code: "media_unavailable",
            message: media.message ?? "",
            details: { reason: media.reason },
          });
        }
      }
      const rank = (id: string) =>
        byId.get(id)?.rank ?? Number.MAX_SAFE_INTEGER;
      segments.sort((a, b) =>
        order === "rank"
          ? rank(a.candidate_id) - rank(b.candidate_id) || a.start_s - b.start_s
          : a.start_s - b.start_s ||
            a.end_s - b.end_s ||
            rank(a.candidate_id) - rank(b.candidate_id),
      );
      for (const [index, segment] of segments.entries()) {
        const media = mediaFor(stored, result, segment.candidate_id).export;
        const min = media.min_start_s ?? 0;
        const max = media.max_end_s ?? 0;
        const longest = media.max_duration_s ?? 0;
        if (!(
          min <= segment.start_s &&
          segment.start_s < segment.end_s &&
          segment.end_s <= max &&
          segment.end_s - segment.start_s <= longest
        )) {
          throw new ApiRequestError(422, {
            code: "invalid_highlights_bounds",
            message:
              "Each moment can be at most 90 seconds long and must stay inside the video. " +
              "Adjust the selection and try again.",
            details: {
              segment_index: index,
              max_duration_s: longest,
              max_total_duration_s: 900,
            },
          });
        }
      }
      const total = segments.reduce(
        (sum, segment) => sum + (segment.end_s - segment.start_s),
        0,
      );
      if (total > 900) {
        throw new ApiRequestError(422, {
          code: "invalid_highlights_bounds",
          message:
            "The moments run over 15 minutes together. Untick some and try again.",
          details: {
            segment_index: null,
            max_duration_s: 90,
            max_total_duration_s: 900,
          },
        });
      }
      const captions = request.captions ?? false;
      const store = load();
      const at = now();
      for (const existing of Object.values(store.highlights)) {
        const same =
          existing.highlights.job_id === request.job_id &&
          existing.highlights.order === order &&
          existing.highlights.captions === captions &&
          existing.highlights.segments.length === segments.length &&
          existing.highlights.segments.every(
            (segment, i) =>
              segment.candidate_id === segments[i].candidate_id &&
              segment.start_s === segments[i].start_s &&
              segment.end_s === segments[i].end_s,
          );
        const current = highlightsNow(
          store,
          existing.highlights.highlights_id,
          at,
        ) as HighlightsVideo;
        if (
          same &&
          (current.state === "queued" || current.state === "running")
        ) {
          return { highlights: current, created: false };
        }
      }
      const seq = (store.next_seq ?? 0) + 1;
      const created: StoredHighlights = {
        highlights: {
          highlights_id: randomId("hl"),
          job_id: request.job_id,
          video_id: stored.video_id,
          segments,
          order,
          captions,
          created_at: iso(at),
        },
        created_ms: at,
        seq,
      };
      const next = {
        ...store,
        next_seq: seq,
        highlights: {
          ...store.highlights,
          [created.highlights.highlights_id]: created,
        },
      };
      save(next);
      return {
        highlights: highlightsNow(
          next,
          created.highlights.highlights_id,
          at,
        ) as HighlightsVideo,
        created: true,
      };
    },

    async getHighlights(highlightsId: string) {
      const video = highlightsNow(load(), highlightsId, now());
      if (!video) {
        throw notFound(
          "highlights_not_found",
          "This highlights video no longer exists. The server may have restarted; make it again.",
        );
      }
      return video;
    },

    resolveUrl(url: string) {
      return url; // the mock's files are served by the app itself
    },
  };
}

/** Forget every mock job, export and highlights video (tests). */
export function resetMockStore(): void {
  save({ jobs: {}, exports: {}, highlights: {}, next_seq: 0 });
}
