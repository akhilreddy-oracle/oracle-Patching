/** Local display guard only. The native controller enforces the sealed window. */
export function executionWindow(plan, now = Date.now()) {
  const bounds = plan?.maintenance_window;
  const utc = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
  const parse = value => {
    if (typeof value !== "string" || !utc.test(value)) return NaN;
    const stamp = Date.parse(value);
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 19) === value.slice(0, 19) ? stamp : NaN;
  };
  const start = parse(bounds?.start);
  const end = parse(bounds?.end);
  if (![start, end, now].every(Number.isFinite) || end <= start) {
    return { open: false, status: "unknown", detail: "The sealed maintenance window is missing or invalid. Reload the plan and inspect its evidence before continuing." };
  }
  if (now < start) return { open: false, status: "upcoming", detail: `The maintenance window opens at ${bounds.start}. Execution and retry are unavailable until then.` };
  if (now >= end) return { open: false, status: "closed", detail: `The maintenance window closed at ${bounds.end}. Execution and retry are unavailable. Preserve completed tasks and arrange an approved recovery or continuation procedure; this plan's sealed window cannot be edited.` };
  if (end - now < 30000) return { open: false, status: "closing", detail: "Less than 30 seconds remain in the maintenance window. There is not enough time to claim another task." };
  return { open: true, status: "open", detail: `Maintenance window open until ${bounds.end}. The controller verifies the window and retry eligibility for every action.` };
}
