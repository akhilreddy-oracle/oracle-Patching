const TOKEN_KEY = "opu-webapp-token";

export function getApiToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setApiToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export function apiHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getApiToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

export async function apiFetch(url, options = {}) {
  const headers = apiHeaders(options.headers || {});
  if (options.body !== undefined && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(url, { ...options, headers });
}
