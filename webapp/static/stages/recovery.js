import { el, badge, classifyStatus } from "../dom.js";
import { apiFetch } from "../api.js";
import { belongsToHost, newestFirst } from "../host_scope.js";
import { helperText } from "../ux.js";
import { backupPolicyChooser, getBackupPolicy } from "../backup_policy.js";

export async function renderRecoveryStage(mount, hostId) {
  mount.innerHTML = "";
  mount.appendChild(
    el("div", { class: "stage-head" }, [
      el("h2", { class: "stage-title", text: "Recovery" }),
      el("p", {
        class: "stage-lead",
        text: "Review recovery preparations, or waive backup if the customer allows patching without it.",
      }),
    ])
  );

  mount.appendChild(backupPolicyChooser(hostId));
  if (getBackupPolicy(hostId) === "waive") {
    mount.appendChild(
      helperText(
        "Backup waived for this host — Recovery evidence is optional. Re-run Readiness evaluate so policy seals require_backup=false.",
        "warn"
      )
    );
  }

  const res = await apiFetch("/api/recovery");
  const data = await res.json();
  const requests = newestFirst(data.requests || []).filter((request) => belongsToHost(request, hostId));

  mount.appendChild(
    el("section", { class: "panel" }, [
      el("h3", { class: "panel-title", text: "Recovery requests" }),
      requests.length
        ? el("table", {}, [
            el("thead", {}, [
              el("tr", {}, [el("th", { text: "Request" }), el("th", { text: "State" }), el("th", { text: "Requester" })]),
            ]),
            el(
              "tbody",
              {},
              requests.map((r) =>
                el("tr", {}, [
                  el("td", {}, [
                    el("a", {
                      class: "back-link",
                      href: `#/recovery/${encodeURIComponent(r.request_id)}`,
                      text: r.request_id,
                    }),
                  ]),
                  el("td", {}, [badge(r.state, classifyStatus(r.state))]),
                  el("td", { text: r.requester || "—" }),
                ])
              )
            ),
          ])
        : helperText("No recovery preparations yet."),
      el("p", { class: "stage-next" }, [
        el("a", { href: "#/recovery", text: "Open All recovery →" }),
      ]),
    ])
  );

  mount.appendChild(
    el("details", { class: "panel lab-only" }, [
      el("summary", { text: "Lab only: TEST_MODE recovery demo" }),
      helperText("Fixture path — does not touch this host’s live estate."),
      el("p", { class: "stage-next" }, [
        el("a", { href: "#/recovery/new", text: "Open demo route →" }),
      ]),
    ])
  );

}
