import { getActor } from "./actor.js";

const TOKEN_KEY = "opu-webapp-token";
export const TOKEN_EVENT = "opu-api-token-change";
let readSignal;

export function getApiToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setApiToken(token) {
  const value = String(token || "").trim();
  if (value === getApiToken()) return;
  if (value) localStorage.setItem(TOKEN_KEY, value);
  else localStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new Event(TOKEN_EVENT));
}

export function setReadSignal(signal) { readSignal = signal; }
export function getReadSignal() { return readSignal; }

export class ApiError extends Error {
  constructor(data, status) {
    super(status === 401
      ? "Authentication required. Enter a valid API token in Session."
      : data.message || `Request failed (${status}).`);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

export function apiHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getApiToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

export async function apiFetch(url, options = {}) {
  const { acceptedStatuses = [], ...request } = options;
  const headers = apiHeaders(request.headers || {});
  if ((request.method || "GET").toUpperCase() !== "GET" && getActor()) headers["X-OPU-Actor"] = getActor();
  if (request.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  // Cancel reads when their page is replaced. Submitted operations continue on
  // the server and can be inspected from their run or plan after navigation.
  const signal = request.signal ?? ((request.method || "GET").toUpperCase() === "GET" ? readSignal : undefined);
  const response = await fetch(url, { ...request, headers, signal });
  if (!response.ok && !acceptedStatuses.includes(response.status)) {
    let data;
    try { data = await response.json(); } catch { data = {}; }
    throw new ApiError(data && typeof data === "object" ? data : {}, response.status);
  }
  return response;
}
