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
  const res = await fetch(url, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new RunStartError(data, res.status);
  return data.run_id;
}

export async function pollRun(runId, { onTick, intervalMs = 1200 } = {}) {
  for (;;) {
    const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    const record = await res.json();
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
