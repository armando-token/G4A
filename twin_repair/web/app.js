/* BlackboxDC 4-State Web Application Client (Phase F).
 * Populates all metrics, feeder currents, temperatures, and status badges
 * strictly via textContent from result_public.json (zero innerHTML).
 */
(function () {
  "use strict";

  const state = {
    currentState: "incident",
    activeCaseId: "drift_A_to_B",
    activeRunId: "showcase_drift_A_to_B",
    resultCard: null,
    showingAfterRender: true,
    presentMode: false,
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    const el = byId(id);
    if (el) {
      el.textContent = String(value == null ? "" : value);
    }
  }

  function setImgSrc(id, src) {
    const el = byId(id);
    if (el) {
      el.setAttribute("src", src);
    }
  }

  function switchState(targetState) {
    state.currentState = targetState;
    const views = {
      incident: byId("view-incident"),
      running: byId("view-running"),
      result: byId("view-result"),
      evidence: byId("view-evidence"),
    };
    Object.keys(views).forEach(function (k) {
      const v = views[k];
      if (!v) return;
      if (k === targetState) {
        v.classList.add("active");
        v.removeAttribute("hidden");
      } else {
        v.classList.remove("active");
        v.setAttribute("hidden", "hidden");
      }
    });

    const navMap = {
      incident: "nav-state-1",
      running: "nav-state-2",
      result: "nav-state-3",
      evidence: "nav-state-4",
    };
    Object.keys(navMap).forEach(function (k) {
      const btn = byId(navMap[k]);
      if (!btn) return;
      if (k === targetState) {
        btn.classList.add("active");
      } else {
        btn.classList.remove("active");
      }
    });

    if (typeof window !== "undefined" && typeof window.scrollTo === "function") {
      try {
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (_e) {
        window.scrollTo(0, 0);
      }
    }
  }

  function assetUrl(runId, relPath) {
    return "/runs/" + encodeURIComponent(runId) + "/assets/" + relPath;
  }

  function applyResultCard(runRecord) {
    if (!runRecord || !runRecord.result) return;
    const card = runRecord.result;
    state.activeRunId = runRecord.run_id || card.run_id || state.activeRunId;
    state.activeCaseId = runRecord.case_id || card.case_id || state.activeCaseId;
    state.resultCard = card;

    const headlines = card.headlines || {};
    const gs = card.graph_score || {};
    const vis = card.visual_verification || {};
    const roles = card.roles || {};
    const builder = roles.builder || {};
    const critic = roles.critic || {};

    setText("synthetic-badge", headlines.badge || "Synthetic case");
    setText("mode-badge", (card.provider_mode || "mock") + " / " + (card.terminal_state || "PASS"));

    // State 1: Incident
    setText("incident-headline", headlines.incident_headline || "The connector is hot. Is the wiring map still correct?");
    setText("incident-caption", headlines.incident_caption || "");
    setImgSrc("img-incident-sld", assetUrl(state.activeRunId, "current_sld_B.png"));
    setImgSrc("img-incident-render0", assetUrl(state.activeRunId, "render0/render_wiring.png"));
    setImgSrc("img-incident-thermal", assetUrl(state.activeRunId, "old_thermal_rear.png"));

    const switchBtn = byId("btn-switch-control-case");
    if (switchBtn) {
      switchBtn.textContent =
        state.activeCaseId === "drift_A_to_B"
          ? "View no-change control case"
          : "View revision-drift case";
    }

    // State 2: Running provenance
    setText(
      "running-elapsed",
      "Elapsed: " + String(Number(card.total_elapsed_s || runRecord.elapsed_s || 0).toFixed(2)) + "s"
    );
    setText(
      "step-builder-prov",
      "env=" + (builder.environment_id || "n/a") + " | id=" + (builder.interaction_id || builder.session_id || "n/a")
    );
    setText(
      "step-critic-prov",
      "env=" + (critic.environment_id || "n/a") + " | id=" + (critic.interaction_id || critic.session_id || "n/a")
    );

    // State 3: Result
    setText("result-caption", headlines.result_caption || "");
    setText("result-graph-score", "Graph: " + (gs.summary || ""));
    setText("result-terminal-badge", card.terminal_state || "");
    setImgSrc("img-result-sld", assetUrl(state.activeRunId, "current_sld_B.png"));

    updateBeforeAfterRender();

    const oldBanner = byId("banner-old-choice");
    const repBanner = byId("banner-repaired-choice");
    setText("banner-old-choice", headlines.old_choice_banner || "");
    setText("banner-repaired-choice", headlines.repaired_choice_banner || "");

    if (oldBanner && repBanner) {
      oldBanner.className = "choice-banner";
      repBanner.className = "choice-banner";
      if (card.terminal_state === "PASS") {
        oldBanner.classList.add("banner-danger");
        repBanner.classList.add("banner-safe");
      } else if (card.terminal_state === "CONTROL_NO_CHANGE") {
        oldBanner.classList.add("banner-neutral");
        repBanner.classList.add("banner-neutral");
      } else {
        oldBanner.classList.add("banner-amber");
        repBanner.classList.add("banner-amber");
      }
    }

    // State 4: Evidence
    setText("human-review-banner", headlines.approval_banner || "Human review required");
    setText(
      "ev-builder-ids",
      (builder.environment_id || "--") + " / " + (builder.returned_model || builder.agent_id || "--") + " / " + (builder.interaction_id || builder.session_id || "--")
    );
    setText(
      "ev-critic-ids",
      (critic.environment_id || "--") + " / " + (critic.returned_model || critic.agent_id || "--") + " / " + (critic.interaction_id || critic.session_id || "--")
    );
    setText("ev-graph-score", gs.summary || "--");
    setText(
      "ev-visual-delta",
      "cable_pixels_changed=" +
        String(Boolean(vis.cable_pixels_changed)) +
        " | N2=" +
        String(vis.changed_pixels_n2_corridor || 0) +
        " px | N3=" +
        String(vis.changed_pixels_n3_corridor || 0) +
        " px"
    );
    setText(
      "ev-sha256",
      String(vis.render0_wiring_sha256 || "").slice(0, 16) +
        "... -> " +
        String(vis.render1_wiring_sha256 || "").slice(0, 16) +
        "..."
    );
    setText(
      "ev-patch-json",
      JSON.stringify(
        {
          patch: card.patch || {},
          action_summary: card.action_summary || {},
          gates: card.gates || {},
        },
        null,
        2
      )
    );
  }

  function updateBeforeAfterRender() {
    const rel = state.showingAfterRender ? "render1/render_wiring.png" : "render0/render_wiring.png";
    setImgSrc("img-result-render", assetUrl(state.activeRunId, rel));
    setText(
      "result-render-title",
      state.showingAfterRender
        ? "Repaired 3D Twin Wiring Render (render1)"
        : "Stale 3D Twin Wiring Render (render0)"
    );
    const btnBefore = byId("btn-toggle-before");
    const btnAfter = byId("btn-toggle-after");
    if (btnBefore && btnAfter) {
      if (state.showingAfterRender) {
        btnAfter.classList.add("active");
        btnBefore.classList.remove("active");
      } else {
        btnBefore.classList.add("active");
        btnAfter.classList.remove("active");
      }
    }
  }

  async function loadRun(runId) {
    const resp = await fetch("/runs/" + encodeURIComponent(runId), { cache: "no-store" });
    if (!resp.ok) return;
    const data = await resp.json();
    applyResultCard(data);
  }

  async function triggerRepairRun() {
    switchState("running");
    try {
      const resp = await fetch("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          case_id: state.activeCaseId,
          mode: "mock",
        }),
      });
      const data = await resp.json();
      if (resp.ok && data && data.result) {
        applyResultCard(data);
        state.showingAfterRender = true;
        updateBeforeAfterRender();
        switchState("result");
      } else if (data && data.fallback_run_id) {
        await loadRun(data.fallback_run_id);
        switchState("result");
      }
    } catch (_err) {
      await loadRun(state.activeRunId);
      switchState("result");
    }
  }

  function initEvents() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("present") === "1") {
      state.presentMode = true;
      document.body.classList.add("present-mode");
    }

    ["incident", "running", "result", "evidence"].forEach(function (st, idx) {
      const btn = byId("nav-state-" + String(idx + 1));
      if (btn) {
        btn.addEventListener("click", function () {
          switchState(st);
        });
      }
    });

    const repairBtn = byId("btn-repair-twin");
    if (repairBtn) {
      repairBtn.addEventListener("click", function () {
        triggerRepairRun();
      });
    }

    const switchCaseBtn = byId("btn-switch-control-case");
    if (switchCaseBtn) {
      switchCaseBtn.addEventListener("click", function () {
        if (state.activeCaseId === "drift_A_to_B") {
          state.activeCaseId = "control_A_to_A";
          loadRun("showcase_control_A_to_A");
        } else {
          state.activeCaseId = "drift_A_to_B";
          loadRun("showcase_drift_A_to_B");
        }
      });
    }

    const btnBefore = byId("btn-toggle-before");
    const btnAfter = byId("btn-toggle-after");
    if (btnBefore) {
      btnBefore.addEventListener("click", function () {
        state.showingAfterRender = false;
        updateBeforeAfterRender();
      });
    }
    if (btnAfter) {
      btnAfter.addEventListener("click", function () {
        state.showingAfterRender = true;
        updateBeforeAfterRender();
      });
    }

    const evBtn = byId("btn-inspect-evidence");
    if (evBtn) {
      evBtn.addEventListener("click", function () {
        switchState("evidence");
      });
    }

    const backBtn = byId("btn-back-to-result");
    if (backBtn) {
      backBtn.addEventListener("click", function () {
        switchState("result");
      });
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    initEvents();
    loadRun("showcase_drift_A_to_B");
  });
})();
