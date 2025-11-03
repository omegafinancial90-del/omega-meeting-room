# app.py
# Streamlit Meeting Room Dashboard (CSV-based, no SQL)
# Rooms: 3 Meeting Rooms + 1 Board Room (Email OTP-gated)
# Views: Now/Next per room, Today timeline, quick booking form
# Timezone: Asia/Kolkata | Business hours: 09:00–19:00 | Slot: 30 min | No overlaps

import streamlit as st
import pandas as pd
from datetime import datetime, date, time, timedelta
from dateutil import tz
import io
import os
import re
import json
import hashlib
import secrets as py_secrets


import smtplib, ssl
from email.message import EmailMessage

# -----------------------------
# CONFIG
# -----------------------------
st.set_page_config(page_title="Room Availability Dashboard", layout="wide")
IST = tz.gettz("Asia/Kolkata")

ROOMS = [
    {"id": "mr1", "name": "Meeting Room 1"},
    {"id": "mr2", "name": "Meeting Room 2"},
    {"id": "mr3", "name": "Meeting Room 3"},
    {"id": "br",  "name": "Board Room"},  # OTP required
]

BOOKINGS_CSV = "bookings.csv"
BUSINESS_HOURS = (time(9, 0), time(19, 0))  # 09:00–19:00
SLOT_MINUTES = 30
STARTING_SOON_MIN = 15

# --- OTP SETTINGS ---
OTP_LENGTH = 6
OTP_TTL_MINUTES = 5
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 45
OTP_DAILY_LIMIT = 50
OTP_RATE_FILE = "otp_rate_limits.json"

# Approver & SMTP from secrets (safer than hard-coding)
APPROVER_EMAIL = st.secrets.get("APPROVER_EMAIL", "")
APPROVER_PIN   = st.secrets.get("APPROVER_PIN", "")

SMTP_HOST = st.secrets.get("SMTP_HOST", "")
SMTP_PORT = int(st.secrets.get("SMTP_PORT", "465"))
SMTP_USER = st.secrets.get("SMTP_USER", "")
SMTP_PASS = st.secrets.get("SMTP_PASS", "")
SMTP_FROM = st.secrets.get("SMTP_FROM", SMTP_USER or "")
SMTP_FROM_NAME = st.secrets.get("SMTP_FROM_NAME", "Room Booking")

# -----------------------------
# STORAGE HELPERS (CSV)
# -----------------------------
def ensure_csv():
    if not os.path.exists(BOOKINGS_CSV):
        df = pd.DataFrame(columns=[
            "room_id", "room_name",
            "date", "start_time", "end_time",
            "booked_by", "title", "created_at"
        ])
        df.to_csv(BOOKINGS_CSV, index=False)

@st.cache_data(show_spinner=False)
def load_bookings() -> pd.DataFrame:
    ensure_csv()
    df = pd.read_csv(BOOKINGS_CSV, dtype=str)
    if df.empty:
        return df
    # normalize
    for col in ["date", "start_time", "end_time", "created_at"]:
        if col in df.columns:
            df[col] = df[col].astype(str)
    return df

def save_bookings(df: pd.DataFrame):
    df.to_csv(BOOKINGS_CSV, index=False)
    load_bookings.clear()  # invalidate cache

# -----------------------------
# TIME UTILS
# -----------------------------
def dt_ist(d: date, t: time) -> datetime:
    return datetime(d.year, d.month, d.day, t.hour, t.minute, tzinfo=IST)

def parse_time_str(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

def fmt_time(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"

def now_ist() -> datetime:
    return datetime.now(IST)

# -----------------------------
# BOOKING LOGIC
# -----------------------------
def overlaps(s1: time, e1: time, s2: time, e2: time) -> bool:
    return (s1 < e2) and (s2 < e1)

def room_status_for_today(df: pd.DataFrame, room_id: str, today: date):
    now = now_ist()
    today_rows = df[(df["room_id"] == room_id) & (df["date"] == today.isoformat())].copy()
    if not today_rows.empty:
        today_rows["start_time_t"] = today_rows["start_time"].apply(parse_time_str)
        today_rows["end_time_t"]   = today_rows["end_time"].apply(parse_time_str)
        today_rows.sort_values(by="start_time_t", inplace=True)

    current_row = None
    next_row = None
    for _, r in today_rows.iterrows():
        s = parse_time_str(r["start_time"])
        e = parse_time_str(r["end_time"])
        sdt = dt_ist(today, s); edt = dt_ist(today, e)
        if sdt <= now < edt:
            current_row = r
            break
        if sdt > now and next_row is None:
            next_row = r

    if current_row is not None:
        label = f"Occupied · {current_row['start_time']}–{current_row['end_time']}"
        return "occupied", label, current_row, next_row

    if next_row is not None:
        label = f"Available · Next: {next_row['start_time']}–{next_row['end_time']}"
        return "available", label, None, next_row

    return "available", "Available all day", None, None

def validate_booking(new_row: dict, df: pd.DataFrame) -> str | None:
    start_t = parse_time_str(new_row["start_time"])
    end_t   = parse_time_str(new_row["end_time"])
    if end_t <= start_t:
        return "End time must be after start time."
    if start_t < BUSINESS_HOURS[0] or end_t > BUSINESS_HOURS[1]:
        return f"Booking must be within business hours {fmt_time(BUSINESS_HOURS[0])}–{fmt_time(BUSINESS_HOURS[1])}."
    for t in (start_t, end_t):
        if (t.minute % SLOT_MINUTES) != 0:
            return f"Time must align with {SLOT_MINUTES}-minute slots (e.g., 09:00, 09:30, 10:00)."

    same_day = df[(df["room_id"] == new_row["room_id"]) & (df["date"] == new_row["date"])]
    for _, r in same_day.iterrows():
        s2 = parse_time_str(r["start_time"])
        e2 = parse_time_str(r["end_time"])
        if overlaps(start_t, end_t, s2, e2):
            return f"Conflict with existing booking {r['start_time']}–{r['end_time']}."
    return None

# -----------------------------
# UI HELPERS
# -----------------------------
def status_pill(state: str, starting_soon: bool) -> str:
    if state == "occupied":
        return '<span class="pill pill-red">Occupied</span>'
    if starting_soon:
        return '<span class="pill pill-amber">Starting Soon</span>'
    return '<span class="pill pill-green">Available</span>'

def inject_css():
    st.markdown(
        """
        <style>
        .pill {
            display:inline-flex; align-items:center; gap:6px;
            border-radius:999px; padding:4px 10px; font-size:12px; 
            border:1px solid rgba(0,0,0,0.08); font-weight:600;
        }
        .pill-green { background:#DCFCE7; color:#065F46; border-color:#A7F3D0; }
        .pill-red   { background:#FFE4E6; color:#9F1239; border-color:#FECDD3; }
        .pill-amber { background:#FEF3C7; color:#92400E; border-color:#FDE68A; }
        .card {
            border:1px solid #eee; border-radius:16px; padding:16px; 
            box-shadow: 0 1px 2px rgba(0,0,0,0.03);
        }
        .muted { color:#6B7280; font-size:13px; }
        .title { font-weight:700; }
        .block { position:absolute; top:50%; transform:translateY(-50%); height:28px; 
                 border:1px solid; border-radius:8px; padding:0 8px; display:flex; align-items:center; font-size:12px;}
        .blk-occupied { background:#FFE4E6; color:#9F1239; border-color:#FECDD3; }
        .blk-open     { background:#DCFCE7; color:#065F46; border-color:#A7F3D0; }
        </style>
        """,
        unsafe_allow_html=True
    )

# -----------------------------
# OTP HELPERS (server-side Email, no OTP shown to booker)
# -----------------------------
def is_email(s: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (s or "").strip()))

def otp_now_utc_ts() -> float:
    return datetime.utcnow().timestamp()

def otp_random() -> str:
    # generate a numeric OTP using Python's secrets module
    return "".join(py_secrets.choice("0123456789") for _ in range(OTP_LENGTH))

def otp_salt() -> str:
    return hashlib.sha1(os.urandom(16)).hexdigest()

def otp_hash(otp: str, salt: str) -> str:
    return hashlib.sha256((salt + ":" + otp).encode("utf-8")).hexdigest()

def otp_load_rate():
    if not os.path.exists(OTP_RATE_FILE):
        return {}
    try:
        return json.loads(open(OTP_RATE_FILE, "r", encoding="utf-8").read())
    except Exception:
        return {}

def otp_save_rate(data: dict):
    with open(OTP_RATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def otp_bucket_key(identifier: str) -> str:
    day = datetime.utcnow().strftime("%Y-%m-%d")
    return f"{identifier}::{day}"

def otp_can_send(identifier: str):
    data = otp_load_rate()
    k = otp_bucket_key(identifier)
    c = int(data.get(k, 0))
    return c < OTP_DAILY_LIMIT, OTP_DAILY_LIMIT - c

def otp_inc_send(identifier: str):
    data = otp_load_rate()
    k = otp_bucket_key(identifier)
    data[k] = int(data.get(k, 0)) + 1
    otp_save_rate(data)

def otp_start_session(booking_row: dict, approver_email: str):
    otp_plain = otp_random()
    salt = otp_salt()
    st.session_state["otp_ctx"] = {
        "salt": salt,
        "hash": otp_hash(otp_plain, salt),
        "expires_at": otp_now_utc_ts() + OTP_TTL_MINUTES * 60,
        "attempts_left": OTP_MAX_ATTEMPTS,
        "resend_at": otp_now_utc_ts() + OTP_RESEND_COOLDOWN_SECONDS,
        "verified": False,
        "approver_email": approver_email,
        "booking_row": booking_row,
    }
    return otp_plain

def otp_resend():
    ctx = st.session_state.get("otp_ctx")
    if not ctx:
        return False, "No OTP session."
    new_plain = otp_random()
    new_salt  = otp_salt()
    ctx.update({
        "salt": new_salt,
        "hash": otp_hash(new_plain, new_salt),
        "expires_at": otp_now_utc_ts() + OTP_TTL_MINUTES * 60,
        "attempts_left": OTP_MAX_ATTEMPTS,
        "resend_at": otp_now_utc_ts() + OTP_RESEND_COOLDOWN_SECONDS,
        "verified": False,
    })
    return True, new_plain

def otp_verify(user_otp: str):
    ctx = st.session_state.get("otp_ctx")
    if not ctx:
        return False, "Session missing."
    if ctx["verified"]:
        return True, "Already verified."
    if otp_now_utc_ts() > ctx["expires_at"]:
        return False, "OTP expired. Please resend."
    if ctx["attempts_left"] <= 0:
        return False, "Too many wrong attempts. Please resend."

    digest = otp_hash(user_otp.strip(), ctx["salt"])
    if digest == ctx["hash"]:
        ctx["verified"] = True
        return True, "Verified."
    else:
        ctx["attempts_left"] -= 1
        return False, f"Incorrect OTP. Attempts left: {ctx['attempts_left']}"

def otp_reset():
    if "otp_ctx" in st.session_state:
        del st.session_state["otp_ctx"]
    st.session_state.pop("approver_pin_ok", None)

# -----------------------------
# EMAIL SENDER — silent to approver
# -----------------------------
def send_otp_email_to_approver(to_email: str, otp_plain: str, brand="Omega Financial") -> tuple[bool, str]:
    """
    Server-side email via SMTP (Gmail app password recommended).
    Returns (ok, msg). OTP not shown to booker.
    """
    SMTP_HOST = st.secrets.get("SMTP_HOST", "")
    SMTP_PORT = int(st.secrets.get("SMTP_PORT", "465"))
    SMTP_USER = st.secrets.get("SMTP_USER", "")
    SMTP_PASS = st.secrets.get("SMTP_PASS", "")
    SMTP_FROM = st.secrets.get("SMTP_FROM", SMTP_USER or "")
    SMTP_FROM_NAME = st.secrets.get("SMTP_FROM_NAME", "Room Booking")

    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS and SMTP_FROM and is_email(to_email)):
        return False, "Email gateway not configured correctly."

    subject = f"{brand} — Board Room OTP"
    body = (
        f"Your One-Time Password is: {otp_plain}\n\n"
        f"This OTP expires in {OTP_TTL_MINUTES} minutes.\n"
        f"Do not share it with anyone.\n"
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
    msg["To"] = to_email
    msg.set_content(body)

    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        return True, "OTP emailed to approver."
    except Exception as e:
        return False, f"Email error: {e}"

# -----------------------------
# ROOM CARD (with Board Room Email-OTP)
# -----------------------------
def room_card(df: pd.DataFrame, room: dict, today: date):
    state, label, now_row, next_row = room_status_for_today(df, room["id"], today)

    starting_soon = False
    if state == "available" and next_row is not None:
        ns = parse_time_str(next_row["start_time"])
        soon_dt = dt_ist(today, ns)
        diff = soon_dt - now_ist()
        starting_soon = diff <= timedelta(minutes=STARTING_SOON_MIN) and diff.total_seconds() > 0

    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.subheader(room["name"])
    st.markdown(status_pill(state, starting_soon), unsafe_allow_html=True)
    st.markdown(f"<div class='muted' style='margin-top:6px;'>{label}</div>", unsafe_allow_html=True)

    col_left, col_right = st.columns([0.6, 0.4])

    with col_left:
        st.caption("Now")
        if state == "occupied" and now_row is not None:
            st.markdown(f"**{now_row['title']}**")
            st.markdown(
                f"<span class='muted'>{now_row['start_time']}–{now_row['end_time']} · Host: {now_row['booked_by']}</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown("<span class='muted'>Free</span>", unsafe_allow_html=True)

        st.caption("Next")
        if next_row is not None:
            st.markdown(f"**{next_row['title']}**")
            st.markdown(
                f"<span class='muted'>{next_row['start_time']}–{next_row['end_time']} · Host: {next_row['booked_by']}</span>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown("<span class='muted'>No upcoming meetings</span>", unsafe_allow_html=True)

    with col_right:
        st.caption("Quick Book")
        form_key = f"quick_book_{room['id']}"
        with st.form(form_key, clear_on_submit=True):
            qb_title = st.text_input("Title / Purpose", placeholder="e.g., Marketing Sync")
            qb_host  = st.text_input("Booked By", placeholder="Your name")
            qb_date  = st.date_input("Date", value=today)
            now_time = datetime.now().time().replace(second=0, microsecond=0)
            start_default = (
                datetime.combine(today, now_time) + timedelta(minutes=SLOT_MINUTES)
            ).time().replace(minute=(0 if now_time.minute < 30 else 30))

            qb_start = st.time_input("Start", value=start_default, step=60 * SLOT_MINUTES)
            qb_end   = st.time_input(
                "End",
                value=(datetime.combine(date.today(), start_default) + timedelta(minutes=SLOT_MINUTES)).time(),
                step=60 * SLOT_MINUTES,
            )
            submitted = st.form_submit_button("Book")

        if submitted:
            if not qb_title or not qb_host:
                st.error("Please provide Title and Booked By.")
            else:
                new_row = {
                    "room_id": room["id"],
                    "room_name": room["name"],
                    "date": qb_date.isoformat(),
                    "start_time": fmt_time(qb_start),
                    "end_time": fmt_time(qb_end),
                    "booked_by": qb_host.strip(),
                    "title": qb_title.strip(),
                    "created_at": datetime.now(IST).isoformat(),
                }
                err = validate_booking(new_row, load_bookings())
                if err:
                    st.error(err)
                else:
                    if room["id"] != "br":
                        df2 = load_bookings().copy()
                        df2 = pd.concat([df2, pd.DataFrame([new_row])], ignore_index=True)
                        save_bookings(df2)
                        st.success(
                            f"Booked {room['name']} · {new_row['date']} {new_row['start_time']}–{new_row['end_time']}"
                        )
                    else:
                        if not is_email(APPROVER_EMAIL):
                            st.error("Approver email not configured correctly on server.")
                        else:
                            ok_limit, _left = otp_can_send(APPROVER_EMAIL)
                            if not ok_limit:
                                st.error("Daily OTP limit reached for approver. Try later.")
                            else:
                                plain = otp_start_session(new_row, APPROVER_EMAIL)
                                ok_mail, msg_mail = send_otp_email_to_approver(APPROVER_EMAIL, plain)
                                if not ok_mail:
                                    otp_reset()
                                    st.error(f"Could not send OTP email: {msg_mail}")
                                else:
                                    otp_inc_send(APPROVER_EMAIL)
                                    st.success("Request received. OTP emailed to approver for confirmation.")
                                    st.caption("Once the approver confirms, this booking will be finalized.")

    st.markdown("</div>", unsafe_allow_html=True)

# -----------------------------
# OTP VERIFICATION (PIN-gated for approver)
# -----------------------------
def otp_verification_panel():
    ctx = st.session_state.get("otp_ctx")
    if not ctx:
        return

    st.divider()
    st.subheader("🔐 Board Room Approval")

    pin_ok = st.session_state.get("approver_pin_ok", False)
    if not pin_ok:
        pin_try = st.text_input("Approver PIN", type="password")
        if st.button("Unlock Approval"):
            if pin_try and pin_try == str(APPROVER_PIN):
                st.session_state["approver_pin_ok"] = True
                st.success("Approval panel unlocked.")
            else:
                st.error("Invalid PIN.")
        return

    st.caption(f"OTP sent to: {ctx['approver_email']} · Expires in ~{max(0, int(ctx['expires_at'] - otp_now_utc_ts()))}s")
    user_otp = st.text_input("Enter 6-digit OTP", max_chars=6)
    col_a, col_b = st.columns([1,1])

    with col_a:
        if st.button("Verify OTP"):
            if not (user_otp and user_otp.isdigit() and len(user_otp) == OTP_LENGTH):
                st.error("Please enter a valid 6-digit numeric OTP.")
            else:
                ok, msg = otp_verify(user_otp)
                if ok:
                    row = ctx["booking_row"]
                    df2 = load_bookings().copy()
                    df2 = pd.concat([df2, pd.DataFrame([row])], ignore_index=True)
                    save_bookings(df2)
                    st.success(f"✅ Approved & Booked: {row['room_name']} · {row['date']} {row['start_time']}–{row['end_time']}")
                    otp_reset()
                else:
                    st.error(msg)

    with col_b:
        can_resend = otp_now_utc_ts() >= ctx["resend_at"]
        if st.button("Resend OTP", disabled=not can_resend):
            if not can_resend:
                st.warning("Please wait before resending.")
            else:
                ok, new_plain = otp_resend()
                if ok:
                    ok_mail, msg_mail = send_otp_email_to_approver(ctx["approver_email"], new_plain)
                    if ok_mail:
                        st.info("New OTP emailed to approver.")
                    else:
                        st.error(f"Resend failed: {msg_mail}")
        st.caption(f"Attempts left: {ctx['attempts_left']}")

# -----------------------------
# TIMELINE
# -----------------------------
def today_timeline(df: pd.DataFrame, today: date):
    st.markdown("#### Today’s Timeline")
    st.caption("09:00 — 19:00")
    for room in ROOMS:
        st.markdown(f"**{room['name']}**")
        timeline = st.container()
        with timeline:
            st.markdown(
                """
                <div style="position:relative;height:40px;border-radius:12px;background:#F3F4F6;overflow:hidden;margin-bottom:8px;"></div>
                """,
                unsafe_allow_html=True
            )
        rows = df[(df["room_id"] == room["id"]) & (df["date"] == today.isoformat())].copy()
        if not rows.empty:
            rows["s"] = rows["start_time"].apply(parse_time_str)
            rows["e"] = rows["end_time"].apply(parse_time_str)
            rows.sort_values(by="s", inplace=True)

        html = io.StringIO()
        html.write('<div style="position:relative;height:40px;border-radius:12px;background:#F3F4F6;overflow:hidden;margin-bottom:16px;">')
        start_dt = dt_ist(today, BUSINESS_HOURS[0])
        end_dt   = dt_ist(today, BUSINESS_HOURS[1])
        for _, r in rows.iterrows():
            sdt = dt_ist(today, r["s"])
            edt = dt_ist(today, r["e"])
            left = max(0.0, (sdt - start_dt).total_seconds() / (end_dt - start_dt).total_seconds()) * 100.0
            width = max(0.0, (edt - sdt).total_seconds() / (end_dt - start_dt).total_seconds()) * 100.0
            html.write(
                f'<div class="block blk-occupied" style="left:{left:.4f}%; width:{width:.4f}%;">{r["title"]}</div>'
            )
        html.write("</div>")
        st.markdown(html.getvalue(), unsafe_allow_html=True)

# -----------------------------
# PAGE
# -----------------------------
inject_css()
today = now_ist().date()
date_str = now_ist().strftime("%A, %d %B %Y")
time_str = now_ist().strftime("%H:%M")

left, right = st.columns([0.7, 0.3])
with left:
    st.title("Room Availability Dashboard")
    st.markdown(f"<span class='muted'>{date_str} · {time_str} IST</span>", unsafe_allow_html=True)
with right:
    view = st.segmented_control("View", options=["Today", "Tomorrow", "Week"], default="Today")

df = load_bookings()

if view == "Today":
    rows = []
    for i in range(0, len(ROOMS), 2):
        rows.append(ROOMS[i:i+2])
    for row_rooms in rows:
        cols = st.columns(len(row_rooms))
        for col, room in zip(cols, row_rooms):
            with col:
                room_card(df, room, today)
    otp_verification_panel()
    st.divider()
    today_timeline(df, today)

elif view == "Tomorrow":
    tomorrow = today + timedelta(days=1)
    st.caption(f"Showing {tomorrow.strftime('%A, %d %B %Y')}")
    rows = []
    for i in range(0, len(ROOMS), 2):
        rows.append(ROOMS[i:i+2])
    for row_rooms in rows:
        cols = st.columns(len(row_rooms))
        for col, room in zip(cols, row_rooms):
            with col:
                room_card(df, room, tomorrow)
    otp_verification_panel()

elif view == "Week":
    st.caption("This week overview (use the Quick Book in each card to add items).")
    for offset in range(0, 7):
        d = today + timedelta(days=offset)
        st.markdown(f"### {d.strftime('%A, %d %B %Y')}")
        rows = []
        for i in range(0, len(ROOMS), 2):
            rows.append(ROOMS[i:i+2])
        for row_rooms in rows:
            cols = st.columns(len(row_rooms))
            for col, room in zip(cols, row_rooms):
                with col:
                    room_card(df, room, d)
        st.divider()
    otp_verification_panel()

# -----------------------------
# Download / Template
# -----------------------------
with st.expander("📄 Download / Upload bookings CSV"):
    st.caption("Columns: room_id, room_name, date (YYYY-MM-DD), start_time (HH:MM), end_time (HH:MM), booked_by, title, created_at (ISO).")
    buf = io.StringIO()
    (load_bookings() if not load_bookings().empty else pd.DataFrame(columns=[
        "room_id","room_name","date","start_time","end_time","booked_by","title","created_at"
    ])).to_csv(buf, index=False)
    st.download_button("Download current bookings.csv", data=buf.getvalue(), file_name="bookings.csv", mime="text/csv")

    up = st.file_uploader("Upload bookings.csv", type=["csv"])
    if up:
        up_df = pd.read_csv(up, dtype=str)
        required = {"room_id","room_name","date","start_time","end_time","booked_by","title","created_at"}
        if not required.issubset(set(up_df.columns)):
            st.error(f"Invalid CSV. Required columns: {', '.join(sorted(required))}")
        else:
            err_msgs = []
            def overlaps(a1, a2, b1, b2):
                return (a1 < b2) and (b1 < a2)
            for rid in [r["id"] for r in ROOMS]:
                sub = up_df[up_df["room_id"] == rid]
                for d in sub["date"].unique():
                    sd = sub[sub["date"] == d].copy()
                    for i, r1 in sd.iterrows():
                        for j, r2 in sd.iterrows():
                            if i >= j: 
                                continue
                            s1 = datetime.strptime(r1["start_time"], "%H:%M").time()
                            e1 = datetime.strptime(r1["end_time"], "%H:%M").time()
                            s2 = datetime.strptime(r2["start_time"], "%H:%M").time()
                            e2 = datetime.strptime(r2["end_time"], "%H:%M").time()
                            if overlaps(s1, e1, s2, e2):
                                err_msgs.append(f"Overlap in {rid} on {d}: {r1['start_time']}-{r1['end_time']} vs {r2['start_time']}-{r2['end_time']}")
            if err_msgs:
                st.error("Upload blocked due to overlaps:\\n- " + "\\n- ".join(err_msgs[:10]) + ("\\n..." if len(err_msgs) > 10 else ""))
            else:
                save_bookings(up_df)
                st.success("Bookings uploaded and saved.")

# Footer
st.caption("Powered by Streamlit · CSV storage · IST timezone · Email OTP for Board Room (server-side, PIN-gated approval)")
