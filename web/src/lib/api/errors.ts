import type { ApiError } from "@/lib/api/types";

/** A non-2xx API response: its HTTP status and the contract's error object (§ 8). */
export class ApiRequestError extends Error {
  readonly status: number;
  readonly error: ApiError;

  constructor(status: number, error: ApiError) {
    super(error.message);
    this.name = "ApiRequestError";
    this.status = status;
    this.error = error;
  }
}
