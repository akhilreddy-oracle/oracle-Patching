/** Resource identity comes from the API's evidence-bound target, not its name. */
export function belongsToHost(record, hostId) {
  return Boolean(hostId && record && record.host_id === hostId);
}
export const planBelongsToHost = belongsToHost;

export function newestFirst(records = []) {
  const at = (record) => {
    const value = Date.parse(record.created_at || "");
    return Number.isFinite(value) ? value : -Infinity;
  };
  return [...records].sort((left, right) => {
    const a = at(left), b = at(right);
    if (a !== b) return a > b ? -1 : 1;
    return String(left.plan_id || left.request_id || "").localeCompare(String(right.plan_id || right.request_id || ""));
  });
}
