// The one typed client for the backend API (docs/architecture/api-contract.md). Components call
// it, never `fetch`; failures are `ApiRequestError`s (`@/lib/api/errors`). Mock mode, the only
// mode until #33 connects the real API, answers from the contract's examples and § 11 scenarios
// in the browser.

import { createMockClient } from "@/lib/api/mock";
import type {
  AnalysisResult,
  CandidateMedia,
  CreateExportRequest,
  CreateJobRequest,
  Export,
  Job,
} from "@/lib/api/types";

export interface ApiClient {
  /** `POST /jobs`: `created` is false when a queued or running job for the same input came back. */
  createJob(request: CreateJobRequest): Promise<{ job: Job; created: boolean }>;
  /** `GET /jobs/{job_id}`. */
  getJob(jobId: string): Promise<Job>;
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
  /** Resolve a root-relative URL from a response against the API origin (§ 1). */
  resolveUrl(url: string): string;
}

/** How often to poll status endpoints while work is queued or running (§ 9.1). */
export const POLL_INTERVAL_MS = 2000;

let client: ApiClient | null = null;

/** The app's API client. Only mock mode exists so far; #33 adds the HTTP client. */
export function getApiClient(): ApiClient {
  client ??= createMockClient();
  return client;
}

export { ApiRequestError } from "@/lib/api/errors";
