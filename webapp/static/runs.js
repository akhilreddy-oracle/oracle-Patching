import { apiFetch, getReadSignal } from "./api.js";

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

export class RunOutcomeUnknownError extends Error {
  constructor(runId, record) {
    super(`Run ${runId} has an unknown outcome. Inspect and reconcile it before starting more work.`);
    this.name = "RunOutcomeUnknownError";
    this.runId = runId;
    this.record = record;
  }
}

export async function startRun(url, body) {
  const res = await apiFetch(url, {
    method: "POST",
    body: body !== undefined ? JSON.stringify(body) : undefined,
    acceptedStatuses: [409],
  });
  const data = await res.json();
  // Join an in-flight run instead of treating a dedupe conflict as failure
  // (estate refresh overlapping a host-page discovery is the common case).
  if (res.status === 409 && data.error === "run_in_progress" && data.run_id) {
    return data.run_id;
  }
  if (!res.ok) throw new RunStartError(data, res.status);
  return data.run_id;
}

export async function pollRun(runId, { onTick, intervalMs = 1200, signal = getReadSignal() } = {}) {
  for (;;) {
    signal?.throwIfAborted();
    const res = await apiFetch(`/api/runs/${encodeURIComponent(runId)}`, { signal });
    let record;
    try {
      record = await res.json();
    } catch (err) {
      throw new Error(`run poll returned non-JSON for ${runId}: ${err}`);
    }
    signal?.throwIfAborted();
    if (!res.ok) {
      throw new Error(record.message || `run poll failed (${res.status}) for ${runId}`);
    }
    if (onTick) onTick(record);
    if (record.status === "unknown") {
      throw new RunOutcomeUnknownError(runId, record);
    }
    if (record.status === "succeeded" || record.status === "failed") return record;
    await sleep(intervalMs);
  }
}

/** Convenience: start a run and poll it to completion, reporting progress via onTick. */
export async function runToCompletion(url, body, opts) {
  const signal = opts?.signal ?? getReadSignal();
  const runId = await startRun(url, body);
  return pollRun(runId, { ...opts, signal });
}
