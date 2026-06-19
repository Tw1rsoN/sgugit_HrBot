function toast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(window.__toastT);
  window.__toastT = setTimeout(() => el.classList.remove("show"), 2200);
}

function norm(x) {
  return String(x || "").toLowerCase().trim();
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}


async function apiGetVacancies() {
  const r = await fetch("/admin/api/vacancies", { credentials: "same-origin" });
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("application/json")) throw new Error("not_json");
  return await r.json();
}

async function apiAddVacancy(payload) {
  const r = await fetch("/admin/api/vacancies", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(payload),
  });
  return await r.json();
}

async function apiDeleteVacancy(id) {
  const r = await fetch(`/admin/api/vacancies/${encodeURIComponent(id)}`, {
    method: "DELETE",
    credentials: "same-origin",
  });
  return await r.json();
}

async function apiGetResponses(vacId) {
  const r = await fetch(`/admin/api/vacancies/${encodeURIComponent(vacId)}/responses`, {
    credentials: "same-origin",
  });
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("application/json")) throw new Error("not_json");
  return await r.json();
}

function getTags() {
  return Array.isArray(window.__tags) ? window.__tags.slice() : [];
}

function setTags(tags) {
  const clean = (Array.isArray(tags) ? tags : [])
    .map(t => String(t || "").trim())
    .filter(t => t.length > 0)
    .slice(0, 60);

  const seen = new Set();
  const uniq = [];
  for (const t of clean) {
    const k = t.toLowerCase();
    if (seen.has(k)) continue;
    seen.add(k);
    uniq.push(t);
  }

  window.__tags = uniq;
  renderTags();
}

function addTag(tag) {
  const t = String(tag || "").trim();
  if (!t) return;
  const next = getTags();
  next.push(t);
  setTags(next);
}

function removeTag(tag) {
  const t = String(tag || "").trim().toLowerCase();
  const next = getTags().filter(x => x.toLowerCase() !== t);
  setTags(next);
}

function removeLastTag() {
  const next = getTags();
  next.pop();
  setTags(next);
}

function renderTags() {
  const row = document.getElementById("tagsRow");
  const input = document.getElementById("tagInput");
  if (!row || !input) return;

  Array.from(row.querySelectorAll(".tagChip")).forEach(n => n.remove());

  const tags = getTags();
  for (const t of tags) {
    const chip = document.createElement("div");
    chip.className = "tagChip";
    chip.innerHTML = `
      <span>${escapeHtml(t)}</span>
      <button type="button" aria-label="Удалить">×</button>
    `;
    chip.querySelector("button").addEventListener("click", () => removeTag(t));
    row.insertBefore(chip, input);
  }
}

function tagsToArray(tags) {
  if (Array.isArray(tags)) return tags;
  if (tags == null) return [];
  const s = String(tags).trim();
  if (!s) return [];
  try {
    const parsed = JSON.parse(s);
    if (Array.isArray(parsed)) return parsed;
  } catch (_) {}
  return s.split(",").map(x => x.trim()).filter(Boolean);
}

function fmtTs(ts) {
  const n = Number(ts || 0);
  if (!n) return "";
  const d = new Date(n * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/* ===== modal ===== */
function openModal(vac) {
  const modal = document.getElementById("vacModal");
  const title = document.getElementById("modalTitle");
  const meta = document.getElementById("modalMeta");
  const body = document.getElementById("modalBody");

  title.textContent = vac.title || "Вакансия";
  meta.textContent = [
    vac.specialization ? `Специализация: ${vac.specialization}` : "",
    vac.experience ? `Опыт: ${vac.experience}` : "",
    (tagsToArray(vac.tags).length ? `Теги: ${tagsToArray(vac.tags).join(", ")}` : "")
  ].filter(Boolean).join(" · ");

  body.textContent = (vac.description || "").trim() || "Описание не заполнено.";

  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
}

function closeModal() {
  const modal = document.getElementById("vacModal");
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
}

/* ===== render ===== */
function setStats(vacancies, totalResponses) {
  document.getElementById("stVac").textContent = String(vacancies.length);
  document.getElementById("stApps").textContent = String(totalResponses);
}

function renderVacancies(vacancies) {
  const body = document.getElementById("vacBody");
  body.innerHTML = "";

  if (!vacancies.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="7" style="padding:18px; color: rgba(234,240,255,.65);">Пока нет вакансий</td>`;
    body.appendChild(tr);
    return;
  }

  for (const v of vacancies) {
    const tr = document.createElement("tr");

    const tagsArr = tagsToArray(v.tags);
    const tagsHtml = tagsArr.length
      ? `<div class="tagsInline">${
          tagsArr.slice(0, 10).map(t => `<span class="tagPill">${escapeHtml(t)}</span>`).join("")
        }${tagsArr.length > 10 ? `<span class="tagPill">+${tagsArr.length - 10}</span>` : ""}</div>`
      : `<span style="color: rgba(234,240,255,.55);">—</span>`;

    const title = (v.title || "").trim();
    const spec = (v.specialization || "").trim();
    const exp = (v.experience || "").trim();
    const cnt = Number(v.responses_count || 0);

    tr.innerHTML = `
      <td class="td-id">${v.id}</td>
      <td>
        <div style="font-weight: 900; color: rgba(234,240,255,.95);">${escapeHtml(title)}</div>
        <div style="margin-top:6px;">
          <button class="linkBtn" type="button" data-act="details" data-id="${v.id}">Подробнее</button>
        </div>
      </td>
      <td>${spec ? escapeHtml(spec) : "—"}</td>
      <td>${exp ? escapeHtml(exp) : "—"}</td>
      <td>${tagsHtml}</td>
      <td><span class="badgeMini blue">${cnt}</span></td>
      <td>
        <button class="btn-outline" data-act="select" data-id="${v.id}">Отклики</button>
        <button class="btn-outline" data-act="delete" data-id="${v.id}">Удалить</button>
      </td>
    `;

    body.appendChild(tr);
  }

  body.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const act = btn.getAttribute("data-act");
      const id = btn.getAttribute("data-id");

      if (act === "select") {
        await selectVacancy(id);
        return;
      }

      if (act === "details") {
        const vac = (window.__vacancies || []).find(x => String(x.id) === String(id));
        if (vac) openModal(vac);
        return;
      }

      if (act === "delete") {
        if (!confirm("Удалить вакансию? Отклики по ней тоже удалятся.")) return;
        try {
          const res = await apiDeleteVacancy(id);
          if (res?.ok) {
            toast("Вакансия удалена");
            if (window.__selectedVacId === String(id)) {
              window.__selectedVacId = null;
              window.__responses = [];
              updateSelectedLine();
              renderResponses([]);
              document.getElementById("appsRefresh").disabled = true;
            }
            await refreshVacancies();
          } else {
            toast(res?.error || "Ошибка удаления");
          }
        } catch {
          toast("Ошибка удаления");
        }
      }
    });
  });
}

function updateSelectedLine() {
  const el = document.getElementById("selectedVacLine");
  if (!window.__selectedVacId) {
    el.textContent = "Выбери вакансию в таблице сверху";
    return;
  }
  const v = (window.__vacancies || []).find(x => String(x.id) === String(window.__selectedVacId));
  el.textContent = v ? `Вакансия: ${v.title || v.id}` : `Вакансия ID: ${window.__selectedVacId}`;
}

function renderResponses(responses) {
  const body = document.getElementById("appsBody");
  body.innerHTML = "";

  if (!window.__selectedVacId) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6" style="padding:18px; color: rgba(234,240,255,.65);">Выбери вакансию выше</td>`;
    body.appendChild(tr);
    return;
  }

  if (!responses.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="6" style="padding:18px; color: rgba(234,240,255,.65);">Пока нет откликов</td>`;
    body.appendChild(tr);
    return;
  }

  for (const r of responses) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="td-id">${r.telegram_id || ""}</td>
      <td>${escapeHtml(r.student_first_name || "")}</td>
      <td>${escapeHtml(r.student_last_name || "")}</td>
      <td>${escapeHtml(r.student_group || "")}</td>
      <td>${escapeHtml(r.study_specialization || "")}</td>
      <td>${escapeHtml(fmtTs(r.applied_at))}</td>
    `;
    body.appendChild(tr);
  }
}

/* ===== filters ===== */
function applyVacancyFilter() {
  const q = norm(document.getElementById("vacSearch")?.value || "");
  const list = window.__vacancies || [];
  if (!q) return renderVacancies(list);

  const filtered = list.filter(v => {
    const tagsArr = tagsToArray(v.tags);
    return (
      norm(v.title).includes(q) ||
      norm(v.description).includes(q) ||
      norm(v.experience).includes(q) ||
      norm(v.specialization).includes(q) ||
      norm(v.id).includes(q) ||
      tagsArr.some(t => norm(t).includes(q))
    );
  });

  renderVacancies(filtered);
}

function applyResponseFilter() {
  const q = norm(document.getElementById("appSearch")?.value || "");
  const list = window.__responses || [];
  if (!q) return renderResponses(list);

  const filtered = list.filter(r => {
    const tg = norm(r.telegram_id);
    const fn = norm(r.student_first_name);
    const ln = norm(r.student_last_name);
    const fio1 = (fn + " " + ln).trim();
    const fio2 = (ln + " " + fn).trim();
    const grp = norm(r.student_group);
    const spec = norm(r.study_specialization);
    const dt = norm(fmtTs(r.applied_at));

    return tg.includes(q) || fn.includes(q) || ln.includes(q) || fio1.includes(q) || fio2.includes(q)
      || grp.includes(q) || spec.includes(q) || dt.includes(q);
  });

  renderResponses(filtered);
}

/* ===== refresh ===== */
async function refreshVacancies() {
  try {
    const data = await apiGetVacancies();
    const vacancies = Array.isArray(data.vacancies) ? data.vacancies : [];
    window.__vacancies = vacancies;

    const totalResponses = vacancies.reduce((acc, v) => acc + Number(v.responses_count || 0), 0);
    setStats(vacancies, totalResponses);

    applyVacancyFilter();
  } catch {
    toast("Не удалось загрузить вакансии");
    window.__vacancies = [];
    setStats([], 0);
    renderVacancies([]);
  }
}

async function selectVacancy(vacId) {
  window.__selectedVacId = String(vacId);
  updateSelectedLine();
  document.getElementById("appsRefresh").disabled = false;
  await refreshResponses();
}

async function refreshResponses() {
  if (!window.__selectedVacId) return;

  try {
    const data = await apiGetResponses(window.__selectedVacId);
    const responses = Array.isArray(data.responses) ? data.responses : [];
    window.__responses = responses;
    applyResponseFilter();
  } catch {
    toast("Не удалось загрузить отклики");
    window.__responses = [];
    renderResponses([]);
  }
}

/* ===== boot ===== */
document.addEventListener("DOMContentLoaded", () => {
  if (!window.ADMIN_BOOT) return;

  window.__vacancies = [];
  window.__responses = [];
  window.__selectedVacId = null;

  // modal binds
  document.getElementById("modalClose").addEventListener("click", closeModal);
  document.getElementById("modalBackdrop").addEventListener("click", closeModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  // tags init
  window.__tags = [];
  renderTags();

  const form = document.getElementById("vacForm");
  const titleEl = document.getElementById("vacTitle");
  const expEl = document.getElementById("vacExp");
  const specEl = document.getElementById("vacSpec");
  const descEl = document.getElementById("vacDesc");
  const tagInput = document.getElementById("tagInput");

  tagInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addTag(tagInput.value);
      tagInput.value = "";
      return;
    }
    if (e.key === "Backspace") {
      if (!tagInput.value && getTags().length) {
        e.preventDefault();
        removeLastTag();
      }
    }
  });

  tagInput.addEventListener("paste", (e) => {
    const text = (e.clipboardData || window.clipboardData).getData("text");
    if (text && text.includes(",")) {
      e.preventDefault();
      const parts = text.split(",").map(x => x.trim()).filter(Boolean);
      for (const p of parts) addTag(p);
      tagInput.value = "";
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    const title = (titleEl.value || "").trim();
    const experience = (expEl.value || "").trim();
    const specialization = (specEl.value || "").trim();
    const description = (descEl.value || "").trim();
    const tags = getTags();

    if (!title) return toast("Заполни название вакансии");
    if (!specialization) return toast("Заполни специализацию");

    try {
      const res = await apiAddVacancy({
        title,
        description,
        experience,
        specialization,
        tags,
        is_active: true
      });

      if (res?.ok) {
        toast("Вакансия добавлена");
        titleEl.value = "";
        expEl.value = "";
        specEl.value = "";
        descEl.value = "";
        setTags([]);
        await refreshVacancies();
      } else {
        toast(res?.error || "Ошибка добавления");
      }
    } catch {
      toast("Ошибка добавления");
    }
  });

  document.getElementById("vacRefresh").addEventListener("click", refreshVacancies);
  document.getElementById("vacSearch").addEventListener("input", applyVacancyFilter);

  document.getElementById("appsRefresh").addEventListener("click", refreshResponses);
  document.getElementById("appSearch").addEventListener("input", applyResponseFilter);

  refreshVacancies();
  updateSelectedLine();
  renderResponses([]);
});
