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

export interface Candidate {
  candidate_id: string;
  rank: number;
  score: number;
  start_s: number;
  end_s: number;
  source: CandidateSource;
  n_viewer_clips: number;
  top_signals: string[];
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

export type Layout = "center_crop" | "configured_region";

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
  duration_s: number | null;
  created_at: string;
  started_at: string | null;
  updated_at: string;
  finished_at: string | null;
}

export interface Health {
  status: "ok";
  schema_version: string;
  job_runner: "mock" | "pipeline";
}
