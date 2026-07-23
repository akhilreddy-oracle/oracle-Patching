import { apiFetch } from "./api.js";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class RunStartError extends Error {
  constructor(data, status) {
    super(data.message || "Failed to start run");
    this.data = data;
    this.status = status;
  }
}

export async function startRun(url, body) {
  const res = await apiFetch(url, {
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new RunStartError(data, res.status);
  return data.run_id;
}

export async function pollRun(runId, { onTick, intervalMs = 1200 } = {}) {
  for (;;) {
    const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}`);
    let record;
    try {
      record = await res.json();
    } catch (err) {
      throw new Error(`run poll returned non-JSON for ${runId}: ${err}`);
    }
    if (!res.ok) {
      throw new Error(record.message || `run poll failed (${res.status}) for ${runId}`);
    }
    if (onTick) onTick(record);
    if (record.status === "succeeded" || record.status === "failed") return record;
    await sleep(intervalMs);
  }
}

/** Convenience: start a run and poll it to completion, reporting progress via onTick. */
export async function runToCompletion(url, body, opts) {
  const runId = await startRun(url, body);
  return pollRun(runId, opts);
}
