// The one typed client for the backend API (docs/architecture/api-contract.md). Components call
// it, never `fetch`; failures are `ApiRequestError`s (`@/lib/api/errors`). Set
// `NEXT_PUBLIC_API_ORIGIN` (e.g. `http://127.0.0.1:8000`) to talk to `autoclipper serve`; leave
// it unset (or set `NEXT_PUBLIC_API_MODE=mock`) to stay in browser mock mode (#33).

import { createHttpClient } from "@/lib/api/http";
import { createMockClient } from "@/lib/api/mock";
import type {
  AnalysisResult,
  ArchiveKind,
  AppSettings,
  CandidateMedia,
  ChannelVodsResponse,
  CreateExportRequest,
  CreateHighlightsRequest,
  CreateJobRequest,
  DeleteJobResponse,
  Export,
  ArchiveStatus,
  Health,
  HighlightsVideo,
  Job,
  JobListResponse,
  JobSummary,
  ListJobsParams,
  PreflightReport,
  ToolSetupRequest,
  ToolSetupStatus,
  UploadVideoOptions,
  VideoUpload,
  UpdateSettingsRequest,
} from "@/lib/api/types";

export interface ApiClient {
  /** `GET /health`: liveness and whether the backend runs mock or pipeline jobs (§ 9.5). */
  getHealth(): Promise<Health>;
  /** `GET /preflight`: local setup readiness checks (§ 9.6). */
  getPreflight(): Promise<PreflightReport>;
  /** `GET /tools/setup`: managed tool setup status and progress (§ 9.8). */
  getToolSetup(): Promise<ToolSetupStatus>;
  /** `POST /tools/setup`: start downloading missing managed tools (§ 9.8). */
  startToolSetup(
    request?: ToolSetupRequest,
  ): Promise<{ setup: ToolSetupStatus; started: boolean }>;
  /** `GET /settings`: the output folder and the latest copy into it (§ 9.9). */
  getSettings(): Promise<AppSettings>;
  /** `PUT /settings`: set or clear the output folder (422 `invalid_output_folder`). */
  updateSettings(request: UpdateSettingsRequest): Promise<AppSettings>;
  /** `GET /channels/vods`: resolve a channel URL or login and list recent archive VODs (§ 9.7). */
  getChannelVods(channel: string, limit?: number): Promise<ChannelVodsResponse>;
  /** `GET /jobs`: list persisted analysis jobs newest first (§ 3.7). */
  listJobs(params?: ListJobsParams): Promise<JobListResponse>;
  /** `POST /jobs/status`: look up the current summaries of specific jobs (§ 3.7). */
  getJobStatuses(jobIds: string[]): Promise<JobSummary[]>;
  /** `POST /uploads`: stream an already-downloaded VOD video file to the server (§ 3.1a). */
  uploadVideo(file: File, options?: UploadVideoOptions): Promise<VideoUpload>;
  /** `POST /jobs`: `created` is false when a queued or running job for the same input came back. */
  createJob(request: CreateJobRequest): Promise<{ job: Job; created: boolean }>;
  /** `GET /jobs/{job_id}`. */
  getJob(jobId: string): Promise<Job>;
  /** `DELETE /jobs/{job_id}`: remove a finished job and optionally delete rendered MP4s (§ 3.8). */
  deleteJob(
    jobId: string,
    options?: { deleteExports?: boolean },
  ): Promise<DeleteJobResponse>;
  /** `GET /jobs/{job_id}/result` (409 `job_not_done` before it's done). */
  getResult(jobId: string): Promise<AnalysisResult>;
  /** `GET /jobs/{job_id}/candidates/{candidate_id}/media`. */
  getCandidateMedia(
    jobId: string,
    candidateId: string,
  ): Promise<CandidateMedia>;
  /** `POST /exports`. */
  createExport(
    request: CreateExportRequest,
  ): Promise<{ export: Export; created: boolean }>;
  /** `GET /exports/{export_id}`. */
  getExport(exportId: string): Promise<Export>;
  /** Get availability and omissions before starting a native ZIP download. */
  getArchiveStatus(jobId: string, kind: ArchiveKind): Promise<ArchiveStatus>;
  /** `POST /highlights`: `created` is false when a queued or running video for the same
   * request came back. */
  createHighlights(
    request: CreateHighlightsRequest,
  ): Promise<{ highlights: HighlightsVideo; created: boolean }>;
  /** `GET /highlights/{highlights_id}`. */
  getHighlights(highlightsId: string): Promise<HighlightsVideo>;
  /** Resolve a root-relative URL from a response against the API origin (§ 1). */
  resolveUrl(url: string): string;
}

/** How often to poll status endpoints while work is queued or running (§ 9.1). */
export const POLL_INTERVAL_MS = 2000;

export interface ApiClientConfig {
  origin?: string;
  mode?: string;
}

/** Resolve the configured API origin, or `null` when browser mock mode is active. */
export function resolveApiOrigin(config?: ApiClientConfig): string | null {
  const mode = (config?.mode ?? process.env.NEXT_PUBLIC_API_MODE ?? "")
    .trim()
    .toLowerCase();
  if (mode === "mock") return null;
  const origin = (
    config?.origin ??
    process.env.NEXT_PUBLIC_API_ORIGIN ??
    ""
  ).trim();
  return origin.length > 0 ? origin : null;
}

/** Whether the app is using the in-browser mock client rather than an HTTP backend. */
export function isMockMode(config?: ApiClientConfig): boolean {
  return resolveApiOrigin(config) === null;
}

/** Build either the HTTP client or the in-browser mock client from configuration. */
export function createApiClient(
  config?: ApiClientConfig,
  fetchImpl?: typeof fetch,
): ApiClient {
  const origin = resolveApiOrigin(config);
  return origin !== null
    ? createHttpClient(origin, fetchImpl)
    : createMockClient();
}

let client: ApiClient | null = null;

/** The app's API client: HTTP when `NEXT_PUBLIC_API_ORIGIN` is set, mock otherwise (#33). */
export function getApiClient(): ApiClient {
  client ??= createApiClient();
  return client;
}

/** Clear the cached client instance (for tests). */
export function resetApiClient(): void {
  client = null;
}

export { ApiRequestError } from "@/lib/api/errors";
