"""
SMART SCHOOL DASHBOARD
-----------------------
- Login system
- Teachers enter attendance (saved to CSV)
- Live ESP32 canteen data (MQTT - broker.hivemq.com, matches the Arduino firmware topics)
- WhatsApp alerts (via Twilio) when gas leak or waste bin full is detected

SETUP BEFORE RUNNING
---------------------
1. Install dependencies:
     pip install streamlit paho-mqtt pandas twilio

2. Create .streamlit/secrets.toml in your project folder with:

     TWILIO_ACCOUNT_SID = "your_account_sid"
     TWILIO_AUTH_TOKEN  = "your_auth_token"
     TWILIO_WHATSAPP_FROM = "whatsapp:+14155238886"   # Twilio sandbox number

   Get a free Twilio account + WhatsApp Sandbox at:
     https://console.twilio.com  ->  Messaging  ->  Try it out  ->  Send a WhatsApp message
   Sandbox lets you test immediately - each recipient just has to send the
   sandbox's "join <code>" message once to their WhatsApp before they can receive alerts.

3. Run: streamlit run app.py

NOTE ON TESTING WITHOUT HARDWARE
----------------------------------
Since the ESP32 board isn't running yet, use a small publisher script to simulate it,
publishing to the SAME topics this app listens to (see TOPICS dict below), e.g.:

    import paho.mqtt.publish as publish
    publish.single("canteen/gas-status", "LEAK", hostname="broker.hivemq.com")

This lets you test the full dashboard + WhatsApp alert flow before the board exists.
"""

import streamlit as st
import paho.mqtt.client as mqtt
import pandas as pd
import os
import json
import re
from urllib.parse import quote
from datetime import datetime

# ================== CONFIG ==================

MQTT_BROKER = "broker.hivemq.com"
MQTT_PORT = 1883

# Maps short internal keys -> actual topic strings published by the ESP32 firmware
TOPICS = {
    "gas-status": "canteen/gas-status",
    "waste-bin": "canteen/waste-bin",
    "kitchen-health": "canteen/kitchen-health",
    "fan-status": "canteen/fan-status",
    "valve-status": "canteen/valve-status",
    "event-log": "canteen/event-log",
}

BIN_FULL_THRESHOLD = 95  # % - matches firmware's binFull logic

# id : password (swap for a real DB later)
USERS = {
    "teacher1": "pass123",
    "teacher2": "pass123",
    "admin": "admin123",
}
ADMIN_USERS = {"admin"}

ATTENDANCE_FILE = "attendance.csv"
ALERT_STATE_FILE = "alert_state.json"

st.set_page_config(page_title="School Dashboard", page_icon="🏫", layout="wide")

# ================== ALERT STATE PERSISTENCE ==================
# Persisted to disk (not just session_state) so the WhatsApp number and the
# "have we already alerted for this leak" flag survive a page refresh / restart.

def load_alert_state():
    default = {"phone": "", "last_gas_leak": False, "last_bin_full": False}
    if os.path.exists(ALERT_STATE_FILE):
        try:
            with open(ALERT_STATE_FILE, "r") as f:
                data = json.load(f)
                default.update(data)
        except (json.JSONDecodeError, IOError):
            pass
    return default

def save_alert_state(state):
    with open(ALERT_STATE_FILE, "w") as f:
        json.dump(state, f)

if "alert_state" not in st.session_state:
    st.session_state.alert_state = load_alert_state()

# ================== WHATSAPP (Twilio) ==================

def is_valid_whatsapp_number(number: str) -> bool:
    # Expects E.164 format, e.g. +919876543210
    return bool(re.match(r"^\+[1-9]\d{7,14}$", number.strip()))

def whatsapp_web_link(phone: str, message: str) -> str:
    digits_only = re.sub(r"\D", "", phone)
    return f"https://wa.me/{digits_only}?text={quote(message)}"

def send_whatsapp_alert(message: str):
    phone = st.session_state.alert_state.get("phone", "").strip()
    if not phone:
        return False, "No WhatsApp number is configured yet."
    if not is_valid_whatsapp_number(phone):
        return False, "Saved WhatsApp number is invalid. Use international format, e.g. +919876543210."

    try:
        from twilio.rest import Client
        sid = st.secrets["TWILIO_ACCOUNT_SID"]
        token = st.secrets["TWILIO_AUTH_TOKEN"]
        from_number = st.secrets["TWILIO_WHATSAPP_FROM"]
        if sid == "your_account_sid" or token == "your_auth_token":
            return False, "Twilio is not configured yet."
        if not str(from_number).startswith("whatsapp:"):
            return False, "TWILIO_WHATSAPP_FROM must start with whatsapp:, e.g. whatsapp:+14155238886."

        client = Client(sid, token)
        message_result = client.messages.create(
            from_=from_number,
            body=message,
            to=f"whatsapp:{phone}",
        )
        return True, f"Message queued by Twilio. SID: {message_result.sid}"
    except KeyError:
        return False, "Twilio secrets are not configured. Add TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_WHATSAPP_FROM to .streamlit/secrets.toml."
    except Exception as e:
        return False, f"Could not send WhatsApp alert: {e}"

def check_and_send_alerts(data: dict):
    """Edge-triggered alerts: only fires the moment a condition BECOMES true,
    not on every rerun, so the phone isn't spammed every few seconds."""
    state = st.session_state.alert_state

    gas_leak_now = data.get("gas-status") == "LEAK"
    bin_full_now = False
    try:
        bin_full_now = int(data.get("waste-bin", 0)) >= BIN_FULL_THRESHOLD
    except (ValueError, TypeError):
        pass

    changed = False

    if gas_leak_now and not state["last_gas_leak"]:
        ok, message = send_whatsapp_alert("🚨 SCHOOL CANTEEN ALERT: Gas leak detected! Valve is being closed automatically. Please check the dashboard.")
        if not ok:
            st.warning(message)
        changed = True
    if not gas_leak_now and state["last_gas_leak"]:
        changed = True  # reset, no message needed for "all clear" unless you want one

    if bin_full_now and not state["last_bin_full"]:
        ok, message = send_whatsapp_alert(f"🗑️ SCHOOL CANTEEN ALERT: Waste bin is full ({data.get('waste-bin', '?')}%). Please arrange for it to be emptied.")
        if not ok:
            st.warning(message)
        changed = True
    if not bin_full_now and state["last_bin_full"]:
        changed = True

    if changed:
        state["last_gas_leak"] = gas_leak_now
        state["last_bin_full"] = bin_full_now
        save_alert_state(state)

# ================== STYLE / THEME ==================

if "theme" not in st.session_state:
    st.session_state.theme = "dark"

def apply_theme():
    if st.session_state.theme == "dark":
        st.markdown("""
        <style>
        :root {
            color-scheme: dark;
        }
        .stApp {
            background-color: #0d1017;
            color: #f5f7fb;
        }
        [data-testid="stHeader"] {
            background: rgba(13, 16, 23, 0.88);
        }
        section[data-testid="stSidebar"] {
            background-color: #111520;
            border-right: 1px solid #242b3a;
        }
        section[data-testid="stSidebar"] * {
            color: #f5f7fb;
        }
        .stMarkdown, p, label, h1, h2, h3, h4, h5, h6,
        [data-testid="stMarkdownContainer"] {
            color: #f5f7fb !important;
        }
        div[data-testid="stTextInput"] input,
        div[data-testid="stNumberInput"] input,
        textarea,
        select {
            background-color: #151a26 !important;
            color: #f5f7fb !important;
            border-color: #2f384b !important;
        }
        div[data-testid="stDataFrame"],
        div[data-testid="stTable"] {
            color: #f5f7fb;
        }
        div[data-testid="stAlert"] {
            background-color: #1a1d29;
            color: #f5f7fb;
        }
        button[kind="primary"],
        div[data-testid="stButton"] button {
            background-color: #202737;
            color: #ffffff;
            border-color: #354158;
            border-radius: 8px;
        }
        div[data-testid="stButton"] button:hover {
            border-color: #7ca7ff;
            color: #ffffff;
        }
        div[data-testid="stDownloadButton"] button,
        div[data-testid="stLinkButton"] a {
            border-radius: 8px;
        }
        .metric-card {
            background: #151a26;
            border-radius: 8px;
            padding: 18px;
            text-align: center;
            border: 1px solid #263047;
        }
        .metric-card h3 { color: #9aa4c7; font-size: 14px; margin-bottom: 6px; }
        .metric-card h1 { color: #ffffff; font-size: 28px; margin: 0; }
        .status-ok { color: #3ddc97; }
        .status-bad { color: #ff5c5c; }
        .login-box {
            max-width: 380px;
            margin: 60px auto;
            padding: 30px;
            border-radius: 8px;
            background: #151a26;
            border: 1px solid #263047;
        }
        .page-kicker, .muted-text { color: #99a6bd !important; }
        .soft-panel {
            background: #151a26;
            border: 1px solid #263047;
            border-radius: 8px;
            padding: 18px;
        }
        .role-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border: 1px solid #31405d;
            border-radius: 999px;
            background: #1b2333;
            color: #dce7ff;
            font-size: 12px;
            font-weight: 600;
        }
        .sidebar-brand {
            font-size: 19px;
            font-weight: 750;
            margin-bottom: 4px;
        }
        </style>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <style>
        :root {
            color-scheme: light;
        }
        .stApp {
            background-color: #f6f7f9;
            color: #212529;
        }
        [data-testid="stHeader"] {
            background: rgba(246, 247, 249, 0.88);
        }
        section[data-testid="stSidebar"] {
            background-color: #ffffff;
            border-right: 1px solid #dee2e6;
        }
        section[data-testid="stSidebar"] * {
            color: #212529;
        }
        div[data-testid="stTextInput"] input,
        div[data-testid="stNumberInput"] input,
        textarea,
        select {
            background-color: #ffffff !important;
            color: #212529 !important;
            border-color: #ced4da !important;
        }
        div[data-testid="stAlert"] {
            background-color: #ffffff;
            color: #212529;
        }
        div[data-testid="stButton"] button {
            background-color: #ffffff;
            color: #212529;
            border-color: #ced4da;
            border-radius: 8px;
        }
        div[data-testid="stButton"] button:hover {
            border-color: #868e96;
            color: #212529;
        }
        .metric-card {
            background: #ffffff;
            border-radius: 8px;
            padding: 18px;
            text-align: center;
            border: 1px solid #dee2e6;
            box-shadow: 0 8px 20px rgba(15,23,42,0.05);
        }
        .metric-card h3 { color: #495057; font-size: 14px; margin-bottom: 6px; }
        .metric-card h1 { color: #212529; font-size: 28px; margin: 0; }
        .status-ok { color: #2b8a3e; }
        .status-bad { color: #c92a2a; }
        .login-box {
            max-width: 380px;
            margin: 60px auto;
            padding: 30px;
            border-radius: 8px;
            background: #ffffff;
            border: 1px solid #dee2e6;
            box-shadow: 0 10px 15px rgba(0,0,0,0.05);
        }
        .stMarkdown, p, label, h1, h2, h3, h4, h5, h6 { color: #212529 !important; }
        div[data-testid="stDownloadButton"] button,
        div[data-testid="stLinkButton"] a {
            border-radius: 8px;
        }
        .page-kicker, .muted-text { color: #667085 !important; }
        .soft-panel {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 18px;
            box-shadow: 0 8px 20px rgba(15,23,42,0.04);
        }
        .role-pill {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border: 1px solid #d0d5dd;
            border-radius: 999px;
            background: #f9fafb;
            color: #344054;
            font-size: 12px;
            font-weight: 600;
        }
        .sidebar-brand {
            font-size: 19px;
            font-weight: 750;
            margin-bottom: 4px;
        }
        </style>
        """, unsafe_allow_html=True)

apply_theme()

# ================== SESSION STATE ==================

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = ""

def is_admin_user():
    return st.session_state.username in ADMIN_USERS

def page_title(title: str, subtitle: str = ""):
    st.markdown(f"<div class='page-kicker'>Smart School Dashboard</div>", unsafe_allow_html=True)
    st.title(title)
    if subtitle:
        st.markdown(f"<p class='muted-text'>{subtitle}</p>", unsafe_allow_html=True)

def compact_card(label: str, value: str, note: str = ""):
    note_html = f"<p class='muted-text' style='margin:6px 0 0'>{note}</p>" if note else ""
    st.markdown(
        f"""
        <div class="metric-card">
            <h3>{label}</h3>
            <h1>{value}</h1>
            {note_html}
        </div>
        """,
        unsafe_allow_html=True,
    )

def role_label():
    return "Admin" if is_admin_user() else "Teacher"

# ================== MQTT (cached, one connection per server process) ==================

@st.cache_resource
def get_mqtt_data():
    data = {key: "—" for key in TOPICS}

    def on_connect(client, userdata, flags, rc):
        for key, topic in TOPICS.items():
            client.subscribe(topic)

    def on_message(client, userdata, msg):
        for key, topic in TOPICS.items():
            if msg.topic == topic:
                data[key] = msg.payload.decode()
                break

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
    except Exception as e:
        st.warning(f"Could not connect to MQTT broker: {e}")
    return data

# ================== LOGIN PAGE ==================

def login_page():
    st.markdown("<h1 style='text-align:center;'>🏫 Smart School Dashboard</h1>", unsafe_allow_html=True)
    st.markdown("<p class='muted-text' style='text-align:center;'>Attendance, canteen monitoring, and alerts in one simple workspace.</p>", unsafe_allow_html=True)
    st.markdown("<div class='login-box'>", unsafe_allow_html=True)
    st.subheader("Login")
    uid = st.text_input("ID")
    pwd = st.text_input("Password", type="password")
    if st.button("Login", use_container_width=True):
        if uid in USERS and USERS[uid] == pwd:
            st.session_state.logged_in = True
            st.session_state.username = uid
            st.rerun()
        else:
            st.error("Invalid ID or password")
    st.markdown("</div>", unsafe_allow_html=True)

# ================== ATTENDANCE PAGE ==================

def attendance_page():
    page_title("Attendance", "Record class attendance and review daily patterns.")

    with st.container():
        st.markdown("<div class='soft-panel'>", unsafe_allow_html=True)
        with st.form("attendance_entry_form", clear_on_submit=True):
            col1, col2, col3 = st.columns([1.4, 1, 1])
            with col1:
                class_name = st.text_input("Class", placeholder="e.g. 10-A")
            with col2:
                present = st.number_input("Present", min_value=0, step=1)
            with col3:
                total = st.number_input("Total students", min_value=0, step=1)

            submitted = st.form_submit_button("Submit Attendance", use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

    if submitted:
        if class_name.strip() == "":
            st.error("Enter a class name")
        elif present > total:
            st.error("Present students cannot be more than total students")
        elif total == 0:
            st.error("Total students must be greater than 0")
        else:
            new_row = pd.DataFrame([{
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "teacher": st.session_state.username,
                "class": class_name.strip(),
                "present": present,
                "total": total,
            }])
            if os.path.exists(ATTENDANCE_FILE):
                new_row.to_csv(ATTENDANCE_FILE, mode="a", header=False, index=False)
            else:
                new_row.to_csv(ATTENDANCE_FILE, index=False)
            st.success(f"Attendance saved for {class_name.strip()}")

    st.divider()

    if os.path.exists(ATTENDANCE_FILE):
        df = pd.read_csv(ATTENDANCE_FILE)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df["present"] = pd.to_numeric(df["present"], errors="coerce").fillna(0)
        df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)

        csv_data = df.to_csv(index=False).encode("utf-8")

        daily_df = df.dropna(subset=["date"]).copy()
        daily_df["day"] = daily_df["date"].dt.date
        daily_summary = daily_df.groupby("day", as_index=False).agg(
            present=("present", "sum"),
            total=("total", "sum"),
        )
        daily_summary = daily_summary[daily_summary["total"] > 0]

        overview_tab, records_tab = st.tabs(["Overview", "Records"])

        with overview_tab:
            total_present = int(df["present"].sum())
            total_students = int(df["total"].sum())
            overall_percent = (total_present / total_students * 100) if total_students else 0

            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Overall Attendance", f"{overall_percent:.2f}%")
            with col2:
                st.metric("Students Present", total_present)
            with col3:
                st.metric("Records Saved", len(df))

            st.progress(min(overall_percent / 100, 1.0), text=f"Overall attendance: {overall_percent:.2f}%")

        if not daily_summary.empty:
            daily_summary["attendance_percent"] = (
                daily_summary["present"] / daily_summary["total"] * 100
            ).round(2)
            top_day = daily_summary.loc[daily_summary["attendance_percent"].idxmax()]
            least_day = daily_summary.loc[daily_summary["attendance_percent"].idxmin()]

            with overview_tab:
                col1, col2 = st.columns(2)
                with col1:
                    st.metric(
                        "Top Attendance Day",
                        str(top_day["day"]),
                        f'{top_day["attendance_percent"]:.2f}%',
                    )
                with col2:
                    st.metric(
                        "Least Attendance Day",
                        str(least_day["day"]),
                        f'{least_day["attendance_percent"]:.2f}%',
                    )

                chart_df = daily_summary.sort_values("day").set_index("day")[["attendance_percent"]]
                st.line_chart(chart_df, height=260)

                with st.expander("Daily Attendance Summary"):
                    st.dataframe(
                        daily_summary.sort_values("day", ascending=False),
                        use_container_width=True,
                    )
        else:
            with overview_tab:
                st.info("Add records with total students greater than 0 to see top and least attendance days.")

        with records_tab:
            st.download_button(
                "Download Attendance CSV",
                data=csv_data,
                file_name="attendance.csv",
                mime="text/csv",
                use_container_width=True,
            )
            st.dataframe(df.sort_values("date", ascending=False), use_container_width=True)
    else:
        st.info("No attendance records yet.")

# ================== CANTEEN DASHBOARD PAGE ==================

def canteen_page():
    if not is_admin_user():
        st.error("Only admin users can access the canteen dashboard.")
        return

    page_title("Canteen Live Status", "Monitor safety signals from the ESP32 canteen system.")
    data = get_mqtt_data()

    # Run alert check every time this page loads / refreshes
    check_and_send_alerts(data)

    gas_status = data.get("gas-status", "—")
    waste_raw = data.get("waste-bin", "0")
    try:
        waste_percent = max(0, min(int(waste_raw), 100))
    except (ValueError, TypeError):
        waste_percent = 0

    col1, col2, col3 = st.columns(3)
    with col1:
        compact_card("Gas Status", gas_status, "Valve closes automatically on leak")
    with col2:
        compact_card("Waste Bin", f"{data.get('waste-bin', '—')}%", f"Full alert at {BIN_FULL_THRESHOLD}%")
        st.progress(waste_percent / 100, text=f"Waste level: {waste_percent}%")
    with col3:
        compact_card("Kitchen Health", data.get("kitchen-health", "—"), "Live MQTT feed")

    col4, col5, col6 = st.columns(3)
    with col4:
        compact_card("Fan", data.get("fan-status", "—"))
    with col5:
        compact_card("Valve", data.get("valve-status", "—"))
    with col6:
        compact_card("Last Event", data.get("event-log", "—"))

    st.caption("Live data via MQTT (broker.hivemq.com). Use refresh if the board has just published new data.")
    if st.button("Refresh Live Data", use_container_width=True):
        st.rerun()

# ================== ALERT SETTINGS PAGE ==================

def alert_settings_page():
    if not is_admin_user():
        st.error("Only admin users can access alert settings.")
        return

    page_title("Alert Settings", "Configure the phone number used for canteen safety alerts.")

    current = st.session_state.alert_state.get("phone", "")
    phone_input = st.text_input(
        "WhatsApp number (with country code, e.g. +919876543210)",
        value=current,
    )

    if st.button("Save Number"):
        if is_valid_whatsapp_number(phone_input):
            st.session_state.alert_state["phone"] = phone_input.strip()
            save_alert_state(st.session_state.alert_state)
            st.success("Number saved. Alerts will be sent here.")
        else:
            st.error("Enter a valid number in international format, e.g. +919876543210")

    if current:
        st.info(f"Currently configured: {current}")

    st.divider()
    st.caption(
        "Automatic WhatsApp sending needs Twilio credentials. Without Twilio, the test button "
        "will open WhatsApp with the message pre-filled so you can send it manually."
    )

    if st.button("Send Test Alert"):
        phone_to_test = phone_input.strip()
        test_message = "✅ Test alert from Smart School Dashboard. WhatsApp alerts are working."
        if phone_to_test and phone_to_test != st.session_state.alert_state.get("phone", ""):
            if is_valid_whatsapp_number(phone_to_test):
                st.session_state.alert_state["phone"] = phone_to_test
                save_alert_state(st.session_state.alert_state)
            else:
                st.error("Enter a valid WhatsApp number before sending the test alert.")
                return

        ok, message = send_whatsapp_alert(test_message)
        if ok:
            st.success(message)
        else:
            saved_phone = st.session_state.alert_state.get("phone", "").strip()
            if saved_phone and is_valid_whatsapp_number(saved_phone):
                st.warning(f"{message} Use the manual WhatsApp link below instead.")
                st.link_button("Open WhatsApp Test Message", whatsapp_web_link(saved_phone, test_message))
            else:
                st.error(message)

# ================== MAIN APP ==================

def main_app():
    with st.sidebar:
        st.markdown("<div class='sidebar-brand'>Smart School</div>", unsafe_allow_html=True)
        st.markdown(
            f"<span class='role-pill'>{role_label()}</span>",
            unsafe_allow_html=True,
        )
        st.caption(f"Signed in as {st.session_state.username}")
        st.divider()
        pages = ["Attendance"]
        if is_admin_user():
            pages.extend(["Canteen Dashboard", "Alert Settings"])

        if st.session_state.get("page") not in pages:
            st.session_state.page = "Attendance"

        page = st.radio("Navigate", pages, key="page")
        st.divider()

        is_dark = st.toggle("🌙 Dark Mode", value=(st.session_state.theme == "dark"))
        if is_dark != (st.session_state.theme == "dark"):
            st.session_state.theme = "dark" if is_dark else "light"
            st.rerun()

        st.divider()
        if st.button("Logout"):
            st.session_state.logged_in = False
            st.session_state.username = ""
            st.rerun()

    if page == "Attendance":
        attendance_page()
    elif page == "Canteen Dashboard":
        canteen_page()
    else:
        alert_settings_page()

# ================== ENTRY POINT ==================

if st.session_state.logged_in:
    main_app()
else:
    login_page()
