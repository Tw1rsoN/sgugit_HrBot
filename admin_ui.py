import os
import sqlite3
from functools import wraps
from flask import Flask, request, session, redirect, render_template, jsonify
import json
import time


DB_PATH = os.getenv("DB_PATH", "hh_bot.db")
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "change-me-please")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = SECRET_KEY


REQUIRED_COLS = {
    "telegram_id": "INTEGER PRIMARY KEY",
    "is_allowed": "INTEGER NOT NULL DEFAULT 0",
    "pending_action": "TEXT DEFAULT ''",
    "hh_token": "TEXT",
    "resume_id": "TEXT",
    "student_first_name": "TEXT DEFAULT ''",
    "student_last_name": "TEXT DEFAULT ''",
    "student_group": "TEXT DEFAULT ''",
    "study_specialization": "TEXT DEFAULT ''",
}


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    return c


def ensure_schema():
    con = _conn()
    cur = con.cursor()

    cur.execute("CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY)")

    for col, col_type in REQUIRED_COLS.items():
        if col == "telegram_id":
            continue
        if not _column_exists(cur, "users", col):
            cur.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type}")



    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            experience TEXT NOT NULL DEFAULT '',
            specialization TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        )
        """
    )

    if not _column_exists(cur, "vacancies", "description"):
        cur.execute("ALTER TABLE vacancies ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if not _column_exists(cur, "vacancies", "experience"):
        cur.execute("ALTER TABLE vacancies ADD COLUMN experience TEXT NOT NULL DEFAULT ''")
    if not _column_exists(cur, "vacancies", "specialization"):
        cur.execute("ALTER TABLE vacancies ADD COLUMN specialization TEXT NOT NULL DEFAULT ''")
    if not _column_exists(cur, "vacancies", "tags_json"):
        cur.execute("ALTER TABLE vacancies ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
    if not _column_exists(cur, "vacancies", "is_active"):
        cur.execute("ALTER TABLE vacancies ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if not _column_exists(cur, "vacancies", "created_at"):
        cur.execute("ALTER TABLE vacancies ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")


    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS vacancy_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vacancy_id INTEGER NOT NULL,
            telegram_id INTEGER NOT NULL,
            applied_at INTEGER NOT NULL,
            UNIQUE(vacancy_id, telegram_id)
        )
        """
    )

    con.commit()
    con.close()


def _column_exists(cur, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any((r[1] == col) for r in cur.fetchall())


def login_required(fn):
    @wraps(fn)
    def wrap(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin")
        return fn(*args, **kwargs)
    return wrap


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    ensure_schema()
    if request.method == "POST":
        login = (request.form.get("login") or "").strip()
        password = (request.form.get("password") or "").strip()
        if login == ADMIN_LOGIN and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin/panel")
        return render_template("admin_login.html", error="Неверный логин или пароль.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect("/admin")


@app.route("/admin/panel")
@login_required
def admin_panel():
    return render_template("admin_panel.html")


@app.route("/admin/api/users")
@login_required
def admin_api_users():
    ensure_schema()
    con = _conn()
    cur = con.cursor()
    cur.execute(
        """
        SELECT telegram_id,
               is_allowed,
               pending_action,
               hh_token,
               resume_id,
               student_first_name,
               student_last_name,
               student_group,
               study_specialization
        FROM users
        ORDER BY telegram_id DESC
        """
    )
    users = []
    for r in cur.fetchall():
        users.append({
            "telegram_id": int(r["telegram_id"]),
            "is_allowed": int(r["is_allowed"] or 0),
            "pending_action": (r["pending_action"] or ""),
            "has_hh": 1 if (r["hh_token"] or "").strip() else 0,
            "has_resume": 1 if (r["resume_id"] or "").strip() else 0,
            "student_first_name": (r["student_first_name"] or ""),
            "student_last_name": (r["student_last_name"] or ""),
            "student_group": (r["student_group"] or ""),
            "study_specialization": (r["study_specialization"] or ""),
        })
    con.close()
    return jsonify({"users": users})


def _set_access(telegram_id: int, allow: int):
    ensure_schema()
    con = _conn()
    cur = con.cursor()
    cur.execute("UPDATE users SET is_allowed = ?, pending_action = '' WHERE telegram_id = ?", (allow, telegram_id))
    if cur.rowcount == 0:
        cur.execute("INSERT INTO users (telegram_id, is_allowed, pending_action) VALUES (?, ?, '')", (telegram_id, allow))
    con.commit()
    con.close()


@app.route("/admin/api/grant", methods=["POST"])
@login_required
def admin_api_grant():
    data = request.get_json(force=True, silent=True) or {}
    tg = int(data.get("telegram_id") or 0)
    if not tg:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    _set_access(tg, 1)
    return jsonify({"ok": True})


@app.route("/admin/api/revoke", methods=["POST"])
@login_required
def admin_api_revoke():
    data = request.get_json(force=True, silent=True) or {}
    tg = int(data.get("telegram_id") or 0)
    if not tg:
        return jsonify({"ok": False, "error": "telegram_id required"}), 400
    _set_access(tg, 0)
    return jsonify({"ok": True})

def register_admin_ui(flask_app, db_path="hh_bot.db"):
    global DB_PATH
    DB_PATH = db_path

    ensure_schema()

    def _safe_add(rule, endpoint, view_func, methods=None):
        if endpoint in flask_app.view_functions:
            return
        flask_app.add_url_rule(rule, endpoint=endpoint, view_func=view_func, methods=methods)

    # базовые
    _safe_add("/admin", "admin_ui_login", admin_login, methods=["GET", "POST"])
    _safe_add("/admin/logout", "admin_ui_logout", admin_logout)
    _safe_add("/admin/panel", "admin_ui_panel", admin_panel)

    # users api
    _safe_add("/admin/api/users", "admin_ui_api_users", admin_api_users)
    _safe_add("/admin/api/grant", "admin_ui_api_grant", admin_api_grant, methods=["POST"])
    _safe_add("/admin/api/revoke", "admin_ui_api_revoke", admin_api_revoke, methods=["POST"])

    # vacancies page + api
    _safe_add("/admin/vacancies", "admin_ui_vacancies_page", admin_vacancies_page)
    _safe_add("/admin/api/vacancies", "admin_ui_api_vacancies", admin_api_vacancies, methods=["GET", "POST"])
    _safe_add("/admin/api/vacancies/<int:vacancy_id>", "admin_ui_api_vacancy_delete", admin_api_vacancy_delete, methods=["DELETE"])
    _safe_add("/admin/api/vacancies/<int:vacancy_id>/responses", "admin_ui_api_vacancy_responses", admin_api_vacancy_responses, methods=["GET"])

    return flask_app

@app.route("/admin/vacancies")
@login_required
def admin_vacancies_page():
    ensure_schema()
    return render_template("admin_vacancies.html")


@app.route("/admin/api/vacancies", methods=["GET", "POST"])
@login_required
def admin_api_vacancies():
    ensure_schema()
    con = _conn()
    cur = con.cursor()

    if request.method == "GET":
        cur.execute(
            """
            SELECT
                v.id,
                v.title,
                v.description,
                v.experience,
                v.specialization,
                v.tags_json,
                v.is_active,
                v.created_at,
                (SELECT COUNT(1) FROM vacancy_responses r WHERE r.vacancy_id = v.id) AS responses_count
            FROM vacancies v
            ORDER BY v.created_at DESC
            """
        )
        rows = cur.fetchall()
        con.close()

        out = []
        for r in rows:
            try:
                tags = json.loads(r["tags_json"] or "[]")
                if not isinstance(tags, list):
                    tags = []
            except Exception:
                tags = []

            out.append({
                "id": int(r["id"]),
                "title": r["title"] or "",
                "description": r["description"] or "",
                "experience": r["experience"] or "",
                "specialization": r["specialization"] or "",
                "tags": tags,
                "is_active": int(r["is_active"] or 0),
                "created_at": int(r["created_at"] or 0),
                "responses_count": int(r["responses_count"] or 0),
            })

        return jsonify({"vacancies": out})

    # POST
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    experience = (data.get("experience") or "").strip()
    specialization = (data.get("specialization") or "").strip()
    tags = data.get("tags") or []
    is_active = 1 if data.get("is_active", True) else 0

    if isinstance(tags, str):
        tags = [x.strip() for x in tags.split(",") if x.strip()]
    if not isinstance(tags, list):
        tags = []
    tags = [str(x).strip() for x in tags if str(x).strip()]

    if not title:
        con.close()
        return jsonify({"ok": False, "error": "title required"}), 400

    cur.execute(
        """
        INSERT INTO vacancies (title, description, experience, specialization, tags_json, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (title, description, experience, specialization, json.dumps(tags, ensure_ascii=False), is_active, int(time.time()))
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()

    return jsonify({"ok": True, "id": int(new_id)})


@app.route("/admin/api/vacancies/<int:vacancy_id>", methods=["DELETE"])
@login_required
def admin_api_vacancy_delete(vacancy_id: int):
    ensure_schema()
    con = _conn()
    cur = con.cursor()

    cur.execute("DELETE FROM vacancy_responses WHERE vacancy_id = ?", (vacancy_id,))
    cur.execute("DELETE FROM vacancies WHERE id = ?", (vacancy_id,))
    con.commit()
    con.close()

    return jsonify({"ok": True})


@app.route("/admin/api/vacancies/<int:vacancy_id>/responses", methods=["GET"])
@login_required
def admin_api_vacancy_responses(vacancy_id: int):
    ensure_schema()
    con = _conn()
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            r.telegram_id,
            r.applied_at,
            u.student_first_name,
            u.student_last_name,
            u.student_group,
            u.study_specialization
        FROM vacancy_responses r
        LEFT JOIN users u ON u.telegram_id = r.telegram_id
        WHERE r.vacancy_id = ?
        ORDER BY r.applied_at DESC
        """,
        (vacancy_id,)
    )
    rows = cur.fetchall()
    con.close()

    out = []
    for r in rows:
        out.append({
            "telegram_id": int(r["telegram_id"]),
            "applied_at": int(r["applied_at"] or 0),
            "student_first_name": (r["student_first_name"] or ""),
            "student_last_name": (r["student_last_name"] or ""),
            "student_group": (r["student_group"] or ""),
            "study_specialization": (r["study_specialization"] or ""),
        })

    return jsonify({"responses": out})


if __name__ == "__main__":
    ensure_schema()
    app.run(host="0.0.0.0", port=int(os.getenv("ADMIN_PORT", "8000")), debug=False)
