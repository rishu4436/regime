// Pick the API implementation by env: VITE_API_MODE=mock (default) | http. VITE_API_URL = backend base URL
// ("" = same origin, which the Vite dev server proxies to :8000).
import { HttpApi } from "./http";
import { MockApi } from "./mock";
import type { ApiClient } from "./types";

export const API_MODE = (import.meta.env.VITE_API_MODE ?? "mock") as "mock" | "http";
export const api: ApiClient = API_MODE === "http" ? new HttpApi(import.meta.env.VITE_API_URL ?? "") : new MockApi();
export type { ApiClient, BacktestRequest } from "./types";
