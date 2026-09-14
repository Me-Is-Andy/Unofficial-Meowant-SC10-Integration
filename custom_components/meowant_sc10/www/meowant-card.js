const CARD_TAG = "meowant-litter-box-card";
const EDITOR_TAG = "meowant-litter-box-card-editor";
const INTEGRATION = "meowant_sc10";

// Entities are matched by entity_id suffix within the chosen device. Renaming
// an entity_id in the registry will break its row; the config accepts an
// explicit override for that case.
const MATCHERS = {
  status: (id) => /_status$/.test(id) && !/cycle/.test(id),
  visits: (id) => /_visits_today$/.test(id),
  uses: (id) => /_uses_today$/.test(id),
  lastClean: (id) => /_last_clean_elapsed$/.test(id),
  binFull: (id) => /_waste_bin_full$/.test(id),
  cleanButton: (id) => /_start_clean_cycle$/.test(id),
};

const BUTTON_STYLES = `
  .meowant-action {
    width: 100%;
    padding: 10px 16px;
    font-size: 14px;
    font-family: inherit;
    font-weight: 500;
    color: var(--text-primary-color, #fff);
    background: var(--primary-color, #03a9f4);
    border: none;
    border-radius: var(--ha-card-border-radius, 12px);
    cursor: pointer;
  }
  .meowant-action:hover:not(:disabled) { opacity: 0.9; }
  .meowant-action:disabled { opacity: 0.4; cursor: not-allowed; }
`;

const relative = (iso) => {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const seconds = Math.round((then - Date.now()) / 1000);
  const units = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
    ["second", 1],
  ];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return formatter.format(Math.round(seconds / size), unit);
    }
  }
  return null;
};

const absolute = (iso) => {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
};

const isUsable = (state) =>
  state && !["unknown", "unavailable", ""].includes(state.state);

class MeowantLitterBoxCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(EDITOR_TAG);
  }

  static getStubConfig(hass) {
    const entities = hass.entities || {};
    const match = Object.values(entities).find(
      (entry) => entry.platform === INTEGRATION && entry.device_id
    );
    return { device_id: match ? match.device_id : "", title: "Litter Box" };
  }

  setConfig(config) {
    this._config = { title: "Litter Box", ...config };
    this._built = false;
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 4;
  }

  _resolve() {
    const found = {};
    const entities = this._hass.entities || {};
    const deviceId = this._config.device_id;

    for (const [entityId, entry] of Object.entries(entities)) {
      if (entry.platform !== INTEGRATION) continue;
      if (deviceId && entry.device_id !== deviceId) continue;
      for (const [key, matches] of Object.entries(MATCHERS)) {
        if (!found[key] && matches(entityId)) found[key] = entityId;
      }
    }

    for (const key of Object.keys(MATCHERS)) {
      if (this._config[key]) found[key] = this._config[key];
    }
    return found;
  }

  _build() {
    this.innerHTML = "";
    const card = document.createElement("ha-card");
    card.style.padding = "16px";

    const style = document.createElement("style");
    style.textContent = BUTTON_STYLES;
    card.appendChild(style);

    const header = document.createElement("div");
    header.style.cssText =
      "display:flex;align-items:center;gap:10px;margin-bottom:12px;";

    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", "mdi:cat");
    icon.style.cssText = "color:var(--secondary-text-color);";

    const title = document.createElement("span");
    title.style.cssText = "font-size:16px;font-weight:500;";
    title.textContent = this._config.title;

    const badge = document.createElement("span");
    badge.style.cssText =
      "margin-left:auto;font-size:12px;padding:3px 10px;border-radius:12px;" +
      "background:var(--secondary-background-color);color:var(--primary-text-color);";

    header.append(icon, title, badge);

    const rows = document.createElement("div");
    rows.style.cssText = "display:flex;flex-direction:column;gap:2px;";

    const makeRow = (iconName, label) => {
      const row = document.createElement("div");
      row.style.cssText =
        "display:flex;align-items:baseline;gap:10px;padding:6px 0;";
      const rowIcon = document.createElement("ha-icon");
      rowIcon.setAttribute("icon", iconName);
      rowIcon.style.cssText =
        "--mdc-icon-size:18px;color:var(--secondary-text-color);";
      const rowLabel = document.createElement("span");
      rowLabel.style.cssText =
        "font-size:14px;color:var(--secondary-text-color);";
      rowLabel.textContent = label;
      const value = document.createElement("span");
      value.style.cssText = "margin-left:auto;font-size:14px;text-align:right;";
      row.append(rowIcon, rowLabel, value);
      rows.appendChild(row);
      return { row, value };
    };

    this._els = {
      card,
      badge,
      visit: makeRow("mdi:cat", "Last visit"),
      clean: makeRow("mdi:broom", "Last clean"),
      today: makeRow("mdi:counter", "Today"),
    };

    const actions = document.createElement("div");
    actions.style.cssText = "margin-top:14px;";

    // A native button rather than one of HA's internal components: those are
    // renamed between frontend versions, and an unregistered custom element
    // renders as an invisible empty tag.
    const button = document.createElement("button");
    button.className = "meowant-action";
    button.type = "button";
    button.textContent = "Start clean cycle";
    button.addEventListener("click", () => this._press());
    actions.appendChild(button);
    this._els.button = button;

    card.append(header, rows, actions);
    this.appendChild(card);
    this._built = true;
  }

  _press() {
    const ids = this._resolve();
    if (!ids.cleanButton) return;
    this._hass.callService("button", "press", { entity_id: ids.cleanButton });
  }

  _update() {
    if (!this._hass || !this._config) return;
    if (!this._built) this._build();

    const ids = this._resolve();
    const get = (key) => (ids[key] ? this._hass.states[ids[key]] : undefined);

    const status = get("status");
    const binFull = get("binFull");
    if (binFull && binFull.state === "on") {
      this._els.badge.textContent = "Bin full";
      this._els.badge.style.background = "var(--error-color)";
      this._els.badge.style.color = "var(--text-primary-color)";
    } else {
      this._els.badge.textContent = isUsable(status) ? status.state : "Unknown";
      this._els.badge.style.background = "var(--secondary-background-color)";
      this._els.badge.style.color = "var(--primary-text-color)";
    }

    const visits = get("visits");
    const visitAt = visits && visits.attributes.last_visit_at;
    this._els.visit.value.innerHTML = visitAt
      ? `${absolute(visitAt)}<br><span style="font-size:12px;color:var(--secondary-text-color)">${relative(visitAt)}</span>`
      : "None recorded";

    const clean = get("lastClean");
    this._els.clean.value.innerHTML = isUsable(clean)
      ? `${absolute(clean.state)}<br><span style="font-size:12px;color:var(--secondary-text-color)">${relative(clean.state)}</span>`
      : "None recorded";

    // Plain ASCII only: this file has been mangled by an encoding mismatch
    // before, and non-ASCII punctuation is not worth the risk.
    const uses = get("uses");
    const visitCount = isUsable(visits) ? visits.state : "-";
    const useCount = isUsable(uses) ? uses.state : "-";
    this._els.today.value.textContent = `${visitCount} visits, ${useCount} uses`;

    this._els.button.disabled = !ids.cleanButton;
  }
}

const EDITOR_SCHEMA = [
  { name: "title", selector: { text: {} } },
  { name: "device_id", selector: { device: { integration: INTEGRATION } } },
];

class MeowantLitterBoxCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) =>
        schema.name === "device_id" ? "Device" : "Title";
      this._form.addEventListener("value-changed", (ev) => {
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: ev.detail.value },
            bubbles: true,
            composed: true,
          })
        );
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = this._config;
    this._form.schema = EDITOR_SCHEMA;
  }
}

if (!customElements.get(CARD_TAG)) {
  customElements.define(CARD_TAG, MeowantLitterBoxCard);
}
if (!customElements.get(EDITOR_TAG)) {
  customElements.define(EDITOR_TAG, MeowantLitterBoxCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === CARD_TAG)) {
  window.customCards.push({
    type: CARD_TAG,
    name: "Meowant SC10 Litter Box",
    description: "Status, last visit, last clean cycle, and a manual clean button.",
    preview: true,
  });
}
