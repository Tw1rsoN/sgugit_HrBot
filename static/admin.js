function toast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(window.__toastT);
  window.__toastT = setTimeout(() => el.classList.remove("show"), 2200);
}

async function apiGetUsers() {
  const r = await fetch("/admin/api/users?limit=200", { credentials: "same-origin" });
  const ct = r.headers.get("content-type") || "";
  if (!ct.includes("application/json")) throw new Error("not_json");
  return await r.json();
}

async function apiGrant(tgId) {
  const r = await fetch("/admin/api/grant", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ telegram_id: tgId })
  });
  return await r.json();
}

async function apiRevoke(tgId) {
  const r = await fetch("/admin/api/revoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ telegram_id: tgId })
  });
  return await r.json();
}

function computeStats(users) {
  const total = users.length;
  const allowed = users.filter(u => u.is_allowed === 1).length;
  const requested = users.filter(u => u.pending_action === "access_requested").length;
  return { total, allowed, requested };
}

function norm(x) {
  return String(x || "").toLowerCase().trim();
}

function renderUsers(users) {
  const body = document.getElementById("usersBody");
  if (!body) return;

  body.innerHTML = "";

  for (const u of users) {
    const tr = document.createElement("tr");

    const pill = (u.is_allowed === 1)
      ? `<span class="pill ok">ДА</span>`
      : `<span class="pill no">НЕТ</span>`;

    const req = (u.pending_action === "access_requested")
      ? `<span class="pill">запрос</span>`
      : ``;

    const hh = u.has_hh ? "✓" : "";

    tr.innerHTML = `
      <td class="td-id">${u.telegram_id}</td>
      <td>${u.student_first_name || ""}</td>
      <td>${u.student_last_name || ""}</td>
      <td>${u.student_group || ""}</td>
      <td>${u.study_specialization || ""}</td>
      <td>${pill}</td>
      <td>${req}</td>
      <td>${hh}</td>
      <td>
        <button class="btn-outline" data-act="grant" data-id="${u.telegram_id}">Выдать</button>
        <button class="btn-outline" data-act="revoke" data-id="${u.telegram_id}">Забрать</button>
      </td>
    `;

    body.appendChild(tr);
  }

  body.querySelectorAll("button[data-act]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const tg = parseInt(btn.getAttribute("data-id"), 10);
      const act = btn.getAttribute("data-act");

      try {
        const res = (act === "grant") ? await apiGrant(tg) : await apiRevoke(tg);
        if (res && res.ok) {
          toast(act === "grant" ? "Доступ выдан" : "Доступ забран");
          await refresh();
        } else {
          toast(res?.error || "Ошибка");
        }
      } catch (e) {
        toast("Ошибка запроса");
      }
    });
  });
}

function applyFilter() {
  const q = norm(document.getElementById("search")?.value || "");
  const users = window.__users || [];

  if (!q) {
    renderUsers(users);
    return;
  }

  const filtered = users.filter(u => {
    const tg = norm(u.telegram_id);
    const fn = norm(u.student_first_name);
    const ln = norm(u.student_last_name);
    const fio1 = (fn + " " + ln).trim();
    const fio2 = (ln + " " + fn).trim();
    const grp = norm(u.student_group);
    const spec = norm(u.study_specialization);

    return (
      tg.includes(q) ||
      fn.includes(q) ||
      ln.includes(q) ||
      fio1.includes(q) ||
      fio2.includes(q) ||
      grp.includes(q) ||
      spec.includes(q)
    );
  });

  renderUsers(filtered);
}

function parseTgIdFromInput(el) {
  const v = (el?.value || "").trim();
  if (!v || !/^\d+$/.test(v)) return null;
  return parseInt(v, 10);
}

async function refresh() {
  try {
    const data = await apiGetUsers();
    const users = Array.isArray(data.users) ? data.users : [];

    window.__users = users;

    const st = computeStats(users);
    const stTotal = document.getElementById("stTotal");
    const stAllowed = document.getElementById("stAllowed");
    const stReq = document.getElementById("stReq");
    if (stTotal) stTotal.textContent = st.total;
    if (stAllowed) stAllowed.textContent = st.allowed;
    if (stReq) stReq.textContent = st.requested;

    applyFilter();
  } catch (e) {
    toast("Не удалось загрузить пользователей");
    renderUsers([]);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  if (!window.ADMIN_BOOT) return;

  document.getElementById("refresh")?.addEventListener("click", refresh);
  document.getElementById("search")?.addEventListener("input", applyFilter);

  const tgInp = document.getElementById("tgIdInput");

  document.getElementById("btnGrant")?.addEventListener("click", async () => {
    const tg = parseTgIdFromInput(tgInp);
    if (!tg) return toast("Неверный Telegram ID");
    const res = await apiGrant(tg);
    if (res?.ok) { toast("Доступ выдан"); await refresh(); }
    else toast(res?.error || "Ошибка");
  });

  document.getElementById("btnRevoke")?.addEventListener("click", async () => {
    const tg = parseTgIdFromInput(tgInp);
    if (!tg) return toast("Неверный Telegram ID");
    const res = await apiRevoke(tg);
    if (res?.ok) { toast("Доступ забран"); await refresh(); }
   else toast(res?.error || "Ошибка");
  });

  refresh();
});
