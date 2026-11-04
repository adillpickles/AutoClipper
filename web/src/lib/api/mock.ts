// Mock mode of the API client: the § 11 scenarios of docs/architecture/api-contract.md, run in
// the browser. A job's state follows from the time since it was created (queued for one step,
// then one step per stage), so polling shows queued, running and the final state, and a page
// reload picks up where it was: jobs are kept in sessionStorage.

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
  CreateJobRequest,
  Export,
  Job,
  Progress,
} from "@/lib/api/types";
import { INVALID_VOD_MESSAGE, looksAbsolute, parseVod } from "@/lib/api/vod";

/** Length of one mock step (queued, then each stage). */
export const MOCK_STEP_MS = 2000;

const STORAGE_KEY = "autoclipper.mock.v1";
const NO_MEDIA_VIDEO_ID = "9000000007";
/** A locally generated, git-ignored sample (see web/README.md § Mock mode). */
const PREVIEW_URL = "/mock-media/preview.mp4";
const CLIP_URL = "/mock-media/clip.mp4";

export const JOB_STAGES: ReadonlyArray<{ stage: string; message: string }> = [
  { stage: "resolving_vod", message: "Looking up the VOD" },
  { stage: "fetching_clips", message: "Fetching viewer clips" },
  { stage: "downloading_chat", message: "Downloading chat replay" },
  { stage: "downloading_audio", message: "Downloading audio" },
  { stage: "computing_features", message: "Computing chat and audio features" },
  { stage: "ranking", message: "Ranking moments" },
];

const EXPORT_STAGES = [
  { stage: "obtaining_media", message: "Getting the video" },
  { stage: "transcribing", message: "Generating captions" },
  { stage: "rendering", message: "Rendering the clip" },
];

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
}

interface Store {
  jobs: Record<string, StoredJob>;
  exports: Record<string, StoredExport>;
}

let memory: Store = { jobs: {}, exports: {} };

function load(): Store {
  try {
    const text = window.sessionStorage.getItem(STORAGE_KEY);
    if (text) memory = JSON.parse(text) as Store;
  } catch {
    // No sessionStorage (private mode, server render): keep the in-memory copy.
  }
  return memory;
}

function save(store: Store): void {
  memory = store;
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(store));
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

/** The job as the mock server would report it `now` milliseconds since the epoch. */
export function jobAt(
  stored: StoredJob,
  now: number,
  stepMs = MOCK_STEP_MS,
): Job {
  const scenario = scenarioFor(stored.video_id);
  const elapsed = Math.max(0, now - stored.created_ms);
  const step = Math.floor(elapsed / stepMs); // 0 = queued, 1..6 = stages, 7 = finished
  const lastStage = scenario.failAtStage ?? JOB_STAGES.length;
  const base = {
    job_id: stored.job_id,
    video_id: stored.video_id,
    video_path: stored.video_path,
    created_at: iso(stored.created_ms),
  };
  if (step === 0) {
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
  const startedAt = iso(stored.created_ms + stepMs);
  if (step <= lastStage) {
    const index = step;
    const fraction =
      scenario.fractions && JOB_STAGES[index - 1].stage === "downloading_audio"
        ? Math.min(1, (elapsed - index * stepMs) / stepMs)
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
  const finishedAt = iso(stored.created_ms + (lastStage + 1) * stepMs);
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
  const stored = load().jobs[jobId];
  if (!stored) throw JOB_NOT_FOUND();
  const job = jobAt(stored, now);
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
      layouts: ["center_crop", "configured_region"],
    },
  };
}

/** The export as the mock server would report it at `now`. */
export function exportAt(
  stored: StoredExport,
  now: number,
  stepMs = MOCK_STEP_MS,
): Export {
  const stages = EXPORT_STAGES.map((s, i) => ({ ...s, index: i + 1 })).filter(
    (s) => s.stage !== "transcribing" || stored.export.captions,
  );
  const step = Math.floor(Math.max(0, now - stored.created_ms) / stepMs);
  const base = stored.export;
  if (step === 0) {
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
  const startedAt = iso(stored.created_ms + stepMs);
  if (step <= stages.length) {
    const current = stages[step - 1];
    return {
      ...base,
      state: "running",
      progress: {
        stage: current.stage,
        stage_index: current.index,
        stage_count: 3,
        stage_fraction: null,
        message: current.message,
      },
      error: null,
      download_url: null,
      started_at: startedAt,
      updated_at: iso(now),
      finished_at: null,
    };
  }
  const finishedAt = iso(stored.created_ms + (stages.length + 1) * stepMs);
  return {
    ...base,
    state: "done",
    progress: null,
    error: null,
    download_url: CLIP_URL,
    started_at: startedAt,
    updated_at: finishedAt,
    finished_at: finishedAt,
  };
}

export function createMockClient(now: () => number = Date.now): ApiClient {
  return {
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
        const job = jobAt(stored, at);
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
      save({ ...store, jobs: { ...store.jobs, [stored.job_id]: stored } });
      return { job: jobAt(stored, at), created: true };
    },

    async getJob(jobId: string) {
      const stored = load().jobs[jobId];
      if (!stored) throw JOB_NOT_FOUND();
      return jobAt(stored, now());
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
        const current = exportAt(existing, at);
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
      const created: StoredExport = {
        export: {
          export_id: randomId("exp"),
          video_id: stored.video_id,
          created_at: iso(at),
          ...request,
        },
        created_ms: at,
      };
      save({
        ...store,
        exports: { ...store.exports, [created.export.export_id]: created },
      });
      return { export: exportAt(created, at), created: true };
    },

    async getExport(exportId: string) {
      const stored = load().exports[exportId];
      if (!stored) {
        throw notFound(
          "export_not_found",
          "This export no longer exists. The server may have restarted; export the clip again.",
        );
      }
      return exportAt(stored, now());
    },

    resolveUrl(url: string) {
      return url; // the mock's files are served by the app itself
    },
  };
}
