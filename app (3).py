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
    "Principal": "principal123456789##",
}

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

def send_whatsapp_alert(message: str):
    phone = st.session_state.alert_state.get("phone", "")
    if not phone:
        return  # no number configured yet, silently skip

    try:
        from twilio.rest import Client
        sid = st.secrets["TWILIO_ACCOUNT_SID"]
        token = st.secrets["TWILIO_AUTH_TOKEN"]
        from_number = st.secrets["TWILIO_WHATSAPP_FROM"]

        client = Client(sid, token)
        client.messages.create(
            from_=from_number,
            body=message,
            to=f"whatsapp:{phone}",
        )
    except KeyError:
        st.warning("Twilio secrets not configured - see setup instructions at the top of app.py")
    except Exception as e:
        st.warning(f"Could not send WhatsApp alert: {e}")

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
        send_whatsapp_alert("🚨 SCHOOL CANTEEN ALERT: Gas leak detected! Valve is being closed automatically. Please check the dashboard.")
        changed = True
    if not gas_leak_now and state["last_gas_leak"]:
        changed = True  # reset, no message needed for "all clear" unless you want one

    if bin_full_now and not state["last_bin_full"]:
        send_whatsapp_alert(f"🗑️ SCHOOL CANTEEN ALERT: Waste bin is full ({data.get('waste-bin', '?')}%). Please arrange for it to be emptied.")
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
        .main { background-color: #0f1116; }
        .metric-card {
            background: linear-gradient(135deg, #1e2130, #262b3d);
            border-radius: 14px;
            padding: 18px;
            text-align: center;
            border: 1px solid #333a52;
        }
        .metric-card h3 { color: #9aa4c7; font-size: 14px; margin-bottom: 6px; }
        .metric-card h1 { color: #ffffff; font-size: 28px; margin: 0; }
        .status-ok { color: #3ddc97; }
        .status-bad { color: #ff5c5c; }
        .login-box {
            max-width: 380px;
            margin: 60px auto;
            padding: 30px;
            border-radius: 16px;
            background: #1a1d29;
            border: 1px solid #333a52;
        }
        </style>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <style>
        .main { background-color: #f8f9fa; }
        .metric-card {
            background: linear-gradient(135deg, #ffffff, #f1f3f5);
            border-radius: 14px;
            padding: 18px;
            text-align: center;
            border: 1px solid #dee2e6;
            box-shadow: 0 4px 6px rgba(0,0,0,0.05);
        }
        .metric-card h3 { color: #495057; font-size: 14px; margin-bottom: 6px; }
        .metric-card h1 { color: #212529; font-size: 28px; margin: 0; }
        .status-ok { color: #2b8a3e; }
        .status-bad { color: #c92a2a; }
        .login-box {
            max-width: 380px;
            margin: 60px auto;
            padding: 30px;
            border-radius: 16px;
            background: #ffffff;
            border: 1px solid #dee2e6;
            box-shadow: 0 10px 15px rgba(0,0,0,0.05);
        }
        .stMarkdown, p, label, h1, h2, h3, h4, h5, h6 { color: #212529 !important; }
        </style>
        """, unsafe_allow_html=True)

apply_theme()

# ================== SESSION STATE ==================

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = ""

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
    st.header("📋 Attendance Entry")

    col1, col2, col3 = st.columns(3)
    with col1:
        class_name = st.text_input("Class (e.g. 10-A)")
    with col2:
        present = st.number_input("Present", min_value=0, step=1)
    with col3:
        total = st.number_input("Total students", min_value=0, step=1)

    if st.button("Submit Attendance"):
        if class_name.strip() == "":
            st.error("Enter a class name")
        else:
            new_row = pd.DataFrame([{
                "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "teacher": st.session_state.username,
                "class": class_name,
                "present": present,
                "total": total,
            }])
            if os.path.exists(ATTENDANCE_FILE):
                new_row.to_csv(ATTENDANCE_FILE, mode="a", header=False, index=False)
            else:
                new_row.to_csv(ATTENDANCE_FILE, index=False)
            st.success(f"Attendance saved for {class_name}")

    st.subheader("Today's Records")
    if os.path.exists(ATTENDANCE_FILE):
        df = pd.read_csv(ATTENDANCE_FILE)
        st.dataframe(df.sort_values("date", ascending=False), use_container_width=True)
    else:
        st.info("No attendance records yet.")

# ================== CANTEEN DASHBOARD PAGE ==================

def canteen_page():
    st.header("🍽️ Canteen Live Status")
    data = get_mqtt_data()

    # Run alert check every time this page loads / refreshes
    check_and_send_alerts(data)

    cols = st.columns(3)
    labels = {
        "gas-status": ("⚠️ Gas Status", cols[0]),
        "waste-bin": ("🗑️ Waste Bin (%)", cols[1]),
        "kitchen-health": ("💚 Kitchen Health", cols[2]),
        "fan-status": ("🌀 Fan Status", cols[0]),
        "valve-status": ("🔧 Valve Status", cols[1]),
        "event-log": ("📝 Last Event", cols[2]),
    }

    for feed, (label, col) in labels.items():
        with col:
            st.markdown(f"""
            <div class="metric-card">
                <h3>{label}</h3>
                <h1>{data.get(feed, "—")}</h1>
            </div>
            """, unsafe_allow_html=True)

    st.caption("Live data via MQTT (broker.hivemq.com) — updates automatically when the ESP32 publishes.")
    if st.button("🔄 Refresh"):
        st.rerun()

# ================== ALERT SETTINGS PAGE ==================

def alert_settings_page():
    st.header("📱 WhatsApp Alert Settings")
    st.write("Enter the WhatsApp number that should receive gas leak and waste bin full alerts.")

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
        "Note: this uses Twilio's WhatsApp Sandbox for testing. "
        "The recipient must first send the sandbox's join code to the Twilio WhatsApp "
        "number once, from their own WhatsApp, before they can receive messages."
    )

    if st.button("Send Test Alert"):
        send_whatsapp_alert("✅ Test alert from Smart School Dashboard. WhatsApp alerts are working.")
        st.success("Test message sent (check WhatsApp).")

# ================== MAIN APP ==================

def main_app():
    with st.sidebar:
        st.markdown(f"### 👋 {st.session_state.username}")
        page = st.radio("Navigate", ["Attendance", "Canteen Dashboard", "Alert Settings"])
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
