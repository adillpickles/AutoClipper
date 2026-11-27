// HTTP implementation of `ApiClient`, talking to `autoclipper serve` (docs/architecture/api-contract.md).
// Every endpoint lives under `/api/v1`, non-2xx responses carry the contract's `ErrorResponse`
// envelope (§ 8), and returned file URLs are resolved against the configured API origin (§ 1, § 9.4).

import type { ApiClient } from "@/lib/api/client";
import { ApiRequestError } from "@/lib/api/errors";
import type {
  AnalysisResult,
  CandidateMedia,
  ChannelVodsResponse,
  CreateExportRequest,
  CreateHighlightsRequest,
  CreateJobRequest,
  DeleteJobResponse,
  ErrorResponse,
  Export,
  Health,
  HighlightsVideo,
  Job,
  JobListResponse,
  JobSummary,
  ListJobsParams,
  PreflightReport,
  ToolSetupRequest,
  ToolSetupStatus,
} from "@/lib/api/types";

const API_PREFIX = "/api/v1";

function isErrorResponse(body: unknown): body is ErrorResponse {
  if (typeof body !== "object" || body === null || !("error" in body)) {
    return false;
  }
  const error = (body as { error: unknown }).error;
  return (
    typeof error === "object" &&
    error !== null &&
    "code" in error &&
    typeof (error as { code: unknown }).code === "string" &&
    "message" in error &&
    typeof (error as { message: unknown }).message === "string"
  );
}

/** Normalize an API origin string (scheme, host and optional port, no trailing slash). */
export function normalizeOrigin(origin: string): string {
  const trimmed = origin.trim();
  const parsed = new URL(trimmed);
  return parsed.origin;
}

export function createHttpClient(
  origin: string,
  fetchImpl: typeof fetch = globalThis.fetch,
): ApiClient {
  const baseOrigin = normalizeOrigin(origin);

  async function request<T>(
    path: string,
    init?: RequestInit,
  ): Promise<{ status: number; data: T }> {
    const url = new URL(`${API_PREFIX}${path}`, baseOrigin).toString();
    const headers = new Headers(init?.headers);
    if (init?.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const response = await fetchImpl(url, {
      ...init,
      headers,
      cache: "no-store",
    });

    if (!response.ok) {
      let parsed: unknown = null;
      try {
        parsed = await response.json();
      } catch {
        // Plain-text or empty error body (e.g. proxy error): fall back to a generic contract error.
      }
      if (isErrorResponse(parsed)) {
        throw new ApiRequestError(response.status, {
          code: parsed.error.code,
          message: parsed.error.message,
          details: parsed.error.details ?? null,
        });
      }
      throw new ApiRequestError(response.status, {
        code: "internal_error",
        message: `Server returned HTTP ${response.status}.`,
        details: null,
      });
    }

    const data = (await response.json()) as T;
    return { status: response.status, data };
  }

  return {
    async getHealth(): Promise<Health> {
      const { data } = await request<Health>("/health");
      return data;
    },

    async getPreflight(): Promise<PreflightReport> {
      const { data } = await request<PreflightReport>("/preflight");
      return data;
    },

    async getToolSetup(): Promise<ToolSetupStatus> {
      const { data } = await request<ToolSetupStatus>("/tools/setup");
      return data;
    },

    async startToolSetup(
      body?: ToolSetupRequest,
    ): Promise<{ setup: ToolSetupStatus; started: boolean }> {
      const init: RequestInit =
        body !== undefined
          ? { method: "POST", body: JSON.stringify(body) }
          : { method: "POST" };
      const { status, data } = await request<ToolSetupStatus>(
        "/tools/setup",
        init,
      );
      return { setup: data, started: status === 202 };
    },

    async getChannelVods(
      channel: string,
      limit?: number,
    ): Promise<ChannelVodsResponse> {
      const params = new URLSearchParams({ channel });
      if (limit !== undefined) {
        params.set("limit", String(limit));
      }
      const { data } = await request<ChannelVodsResponse>(
        `/channels/vods?${params.toString()}`,
      );
      return data;
    },
    async listJobs(params?: ListJobsParams): Promise<JobListResponse> {
      const query = new URLSearchParams();
      if (params?.limit !== undefined) {
        query.set("limit", String(params.limit));
      }
      if (params?.offset !== undefined) {
        query.set("offset", String(params.offset));
      }
      if (params?.before !== undefined) {
        query.set("before", params.before);
      }
      if (params?.q !== undefined && params.q.trim() !== "") {
        query.set("q", params.q.trim());
      }
      if (params?.state !== undefined) {
        query.set("state", params.state);
      }
      const qs = query.toString();
      const { data } = await request<JobListResponse>(
        qs ? `/jobs?${qs}` : "/jobs",
      );
      return data;
    },

    async getJobStatuses(jobIds) {
      const { data } = await request<JobSummary[]>("/jobs/status", {
        method: "POST",
        body: JSON.stringify({ job_ids: jobIds }),
      });
      return data;
    },

    async createJob(
      body: CreateJobRequest,
    ): Promise<{ job: Job; created: boolean }> {
      const { status, data } = await request<Job>("/jobs", {
        method: "POST",
        body: JSON.stringify(body),
      });
      return { job: data, created: status === 201 };
    },

    async getJob(jobId: string): Promise<Job> {
      const { data } = await request<Job>(`/jobs/${encodeURIComponent(jobId)}`);
      return data;
    },

    async deleteJob(
      jobId: string,
      options?: { deleteExports?: boolean },
    ): Promise<DeleteJobResponse> {
      const qs = options?.deleteExports ? "?delete_exports=true" : "";
      const { data } = await request<DeleteJobResponse>(
        `/jobs/${encodeURIComponent(jobId)}${qs}`,
        { method: "DELETE" },
      );
      return data;
    },

    async getResult(jobId: string): Promise<AnalysisResult> {
      const { data } = await request<AnalysisResult>(
        `/jobs/${encodeURIComponent(jobId)}/result`,
      );
      return data;
    },

    async getCandidateMedia(
      jobId: string,
      candidateId: string,
    ): Promise<CandidateMedia> {
      const { data } = await request<CandidateMedia>(
        `/jobs/${encodeURIComponent(jobId)}/candidates/${encodeURIComponent(candidateId)}/media`,
      );
      return data;
    },

    async createExport(
      body: CreateExportRequest,
    ): Promise<{ export: Export; created: boolean }> {
      const { status, data } = await request<Export>("/exports", {
        method: "POST",
        body: JSON.stringify(body),
      });
      return { export: data, created: status === 201 };
    },

    async getExport(exportId: string): Promise<Export> {
      const { data } = await request<Export>(
        `/exports/${encodeURIComponent(exportId)}`,
      );
      return data;
    },

    async createHighlights(
      body: CreateHighlightsRequest,
    ): Promise<{ highlights: HighlightsVideo; created: boolean }> {
      const { status, data } = await request<HighlightsVideo>("/highlights", {
        method: "POST",
        body: JSON.stringify(body),
      });
      return { highlights: data, created: status === 201 };
    },

    async getHighlights(highlightsId: string): Promise<HighlightsVideo> {
      const { data } = await request<HighlightsVideo>(
        `/highlights/${encodeURIComponent(highlightsId)}`,
      );
      return data;
    },

    resolveUrl(url: string): string {
      return new URL(url, baseOrigin).toString();
    },
  };
}
