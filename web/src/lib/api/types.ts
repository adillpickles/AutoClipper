// Types of the backend API, hand-written from docs/architecture/api-contract.md. Field names are
// the contract's snake_case. VOD positions are seconds (`*_s`), wall-clock times UTC ISO-8601
// strings (`*_at`), IDs strings. Every field is always present; optional values are `null`.

export type State = "queued" | "running" | "done" | "failed";
export interface ApiError {
  code: string; // open enum
  message: string;
  details: Record<string, unknown> | null;
}

export interface ErrorResponse {
  error: ApiError;
}

export interface Progress {
  stage: string; // open enum
  stage_index: number;
  stage_count: number;
  stage_fraction: number | null;
  message: string;
}

export interface AnalysisWarning {
  code: string; // open enum
  message: string;
}

export interface CreateJobRequest {
  vod: string;
  video_path?: string | null;
}

export interface Job {
  job_id: string;
  video_id: string;
  video_path: string | null;
  state: State;
  progress: Progress | null;
  warnings: AnalysisWarning[];
  error: ApiError | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
}

export interface JobRenderSummary {
  render_id: string;
  kind: "export" | "highlights";
  candidate_id: string | null;
  state: State;
  file_available: boolean;
  download_url: string | null;
  duration_s: number | null;
  created_at: string;
  finished_at: string | null;
}

export interface JobSummary {
  job_id: string;
  video_id: string;
  video_path: string | null;
  state: State;
  progress: Progress | null;
  title: string | null;
  broadcaster_id: string | null;
  broadcaster_display_name: string | null;
  duration_s: number | null;
  thumbnail_url: string | null;
  candidate_count: number | null;
  result_available: boolean;
  export_count: number;
  missing_export_count: number;
  highlights_count: number;
  missing_highlights_count: number;
  exports: JobRenderSummary[];
  highlights: JobRenderSummary[];
  warning_count: number;
  error: ApiError | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
}

export interface ListJobsParams {
  limit?: number;
  offset?: number;
  before?: string;
  q?: string;
  state?: State;
}

export interface JobListResponse {
  items: JobSummary[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface DeleteJobResponse {
  job_id: string;
  deleted: true;
  deleted_files: number;
}

export interface VodSummary {
  video_id: string;
  title: string;
  broadcaster_id: string;
  broadcaster_display_name: string;
  duration_s: number;
  created_at: string;
}

export interface ScorerInfo {
  kind: "heuristic" | "trained_model" | "mock";
  name: string;
  run_id: string | null;
}

export type CandidateSource = "viewer_clips" | "discovered" | "both";

export interface SignalDetail {
  kind: string; // open enum
  label: string;
  detail: string;
}

export interface Candidate {
  candidate_id: string;
  rank: number;
  score: number;
  start_s: number;
  end_s: number;
  source: CandidateSource;
  n_viewer_clips: number;
  top_signals: string[];
  title?: string | null;
  reason?: string | null;
  signal_details?: SignalDetail[];
}

export interface AnalysisResult {
  schema_version: string;
  generated_at: string;
  vod: VodSummary;
  scorer: ScorerInfo;
  n_viewer_clips_total: number | null;
  warnings: AnalysisWarning[];
  candidates: Candidate[];
}

export interface PreviewMedia {
  status: "available" | "preparing" | "unavailable";
  kind: string | null; // open enum: "video_file"
  url: string | null;
  media_start_s: number | null;
  media_end_s: number | null;
  reason: string | null;
  message: string | null;
}

export interface ExportAvailability {
  status: "available" | "unavailable";
  reason: string | null;
  message: string | null;
  min_start_s: number | null;
  max_end_s: number | null;
  max_duration_s: number | null;
  layouts: string[];
}

export interface CandidateMedia {
  job_id: string;
  candidate_id: string;
  video_id: string;
  preview: PreviewMedia;
  export: ExportAvailability;
}

export type Layout = "center_crop" | "configured_region" | "stacked";

export interface CreateExportRequest {
  job_id: string;
  candidate_id: string;
  start_s: number;
  end_s: number;
  layout: Layout;
  captions: boolean;
}

export interface Export {
  export_id: string;
  job_id: string;
  candidate_id: string;
  video_id: string;
  start_s: number;
  end_s: number;
  layout: string;
  captions: boolean;
  state: State;
  progress: Progress | null;
  error: ApiError | null;
  download_url: string | null;
  filename?: string | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
}

export interface HighlightsSegment {
  candidate_id: string;
  start_s: number;
  end_s: number;
}

export interface CreateHighlightsRequest {
  job_id: string;
  segments?: HighlightsSegment[] | null;
  count?: number;
  order?: "chronological" | "rank";
  captions?: boolean;
}

export interface HighlightsVideo {
  highlights_id: string;
  job_id: string;
  video_id: string;
  segments: HighlightsSegment[];
  order: "chronological" | "rank";
  captions: boolean;
  state: State;
  progress: Progress | null;
  error: ApiError | null;
  download_url: string | null;
  filename?: string | null;
  duration_s: number | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
}

// A job's ZIP download (§ 7.6): what it would contain right now.
export type ArchiveKind = "clips" | "highlights";

export interface ArchiveOmission {
  render_id: string;
  candidate_id: string | null;
  rank: number | null;
  reason: "queued" | "running" | "failed" | "file_missing";
}

export interface ArchiveStatus {
  kind: ArchiveKind;
  ready_count: number;
  omitted: ArchiveOmission[];
  filename: string;
  download_url: string | null;
}

export interface Health {
  status: "ok";
  schema_version: string;
  job_runner: "mock" | "pipeline";
  captions_available: boolean;
}

export type PreflightStatus = "ok" | "warn" | "fail";

export interface PreflightCheck {
  name: string; // open enum
  label: string;
  status: PreflightStatus;
  message: string;
  fix: string | null;
  source?: string | null; // open enum: "config" | "managed" | "path" | "mixed" | null
}

export interface PreflightReport {
  status: PreflightStatus;
  checks: PreflightCheck[];
}

export type ToolSetupName = "ffmpeg" | "twitch_downloader";
export type ToolSetupState = "idle" | "running" | "done" | "failed";

export interface ToolSetupRequest {
  tools?: ToolSetupName[] | null;
  force?: boolean;
}

export interface ToolSetupItem {
  tool: ToolSetupName;
  label: string;
  stage: string; // open enum: queued, checking, downloading, verifying, extracting, done, skipped, failed
  message: string;
  bytes_downloaded: number | null;
  bytes_total: number | null;
  fraction: number | null;
  error: ApiError | null;
}

export interface ToolSetupStatus {
  state: ToolSetupState;
  items: ToolSetupItem[];
  error: ApiError | null;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
}

export interface ChannelInfo {
  broadcaster_id: string;
  login: string;
  display_name: string;
  profile_image_url: string | null;
}

export interface ChannelVod {
  video_id: string;
  title: string;
  created_at: string;
  duration_s: number;
  thumbnail_url: string | null;
  url: string;
  view_count: number;
  analyzed: boolean;
  last_job_id: string | null;
}

export interface ChannelVodsResponse {
  channel: ChannelInfo;
  vods: ChannelVod[];
  truncated: boolean;
}
