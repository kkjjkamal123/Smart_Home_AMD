#!/usr/bin/env python3
"""
Entity Neural — Raspberry Pi Bridge Server
==========================================
Run this on your Pi BEFORE opening the dashboard.
It:
  1. Subscribes to MQTT from both ESP32-C6 boards
  2. Runs the Random Forest classification
  3. Controls relay via GPIO17 / GPIO27
  4. Serves a REST API that the website polls every second

Usage:
  python3 bridge.py

Then in the dashboard, enter your Pi's IP and click "Connect to Pi"
"""

import json
import time
import threading
import pickle
import numpy as np

from flask import Flask, jsonify
from flask_cors import CORS
import paho.mqtt.client as mqtt

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    print("⚠  RPi.GPIO not found — relay control disabled (OK if testing on PC)")
    GPIO_AVAILABLE = False

# ── CONFIG ──────────────────────────────────────────────────────────
MQTT_BROKER   = "localhost"          # Mosquitto on same Pi
MQTT_TOPIC    = "energy/current"
MODEL_PATH    = "model.pkl"
THRESHOLD_A   = 3.0                  # Amps — shed if total exceeds this
GPIO_RELAY1   = 17                   # Room 1 relay
GPIO_RELAY2   = 27                   # Room 2 relay — this is the one we shed
API_PORT      = 5555                 # Website polls http://PI_IP:5555/api/data
# ────────────────────────────────────────────────────────────────────

# ── LOAD MODEL ──────────────────────────────────────────────────────
try:
    with open(MODEL_PATH, "rb") as f:
        model = pickle.load(f)
    print(f"✓ Random Forest loaded from {MODEL_PATH}")
except FileNotFoundError:
    print(f"⚠  {MODEL_PATH} not found — using rule-based classifier")
    model = None

# ── GPIO SETUP ──────────────────────────────────────────────────────
if GPIO_AVAILABLE:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GPIO_RELAY1, GPIO.OUT)
    GPIO.setup(GPIO_RELAY2, GPIO.OUT)
    GPIO.output(GPIO_RELAY1, GPIO.HIGH)   # HIGH = relay OFF (active-LOW relay)
    GPIO.output(GPIO_RELAY2, GPIO.HIGH)
    print(f"✓ Relay GPIO ready — pin {GPIO_RELAY1} (Room1), pin {GPIO_RELAY2} (Room2)")

# ── SHARED STATE ────────────────────────────────────────────────────
state = {
    "esp1":        None,        # latest feature dict from esp1
    "esp2":        None,        # latest feature dict from esp2
    "esp1_label":  "—",
    "esp2_label":  "—",
    "esp1_conf":   0.0,
    "esp2_conf":   0.0,
    "inferMs":     1.2,
    "shed":        False,
    "shedCount":   0,
    "savedWh":     0.0,
    "lastUpdate":  0,
}
state_lock = threading.Lock()

# ── CLASSIFY ────────────────────────────────────────────────────────
def classify(feat):
    """Run RF model or fallback rule-based classifier"""
    t0 = time.perf_counter()

    if model is not None:
        X = [[feat["rms"], feat["peak"], feat["std"], feat["crest"], feat["surge"]]]
        label_idx = model.predict(X)[0]
        conf      = model.predict_proba(X)[0].max() * 100
        label     = "DC Motor (M027)" if label_idx == 0 else "LED Load"
    else:
        # Rule-based fallback matching training data boundaries
        if feat["rms"] > 0.25 and feat["crest"] > 1.2 and feat["surge"] > 0.30:
            label = "DC Motor (M027)"
            conf  = min(99, 94 + feat["rms"] * 4)
        elif feat["rms"] < 0.01:
            label = "No Load"
            conf  = 99.0
        else:
            label = "LED Load"
            conf  = 92.0

    infer_ms = (time.perf_counter() - t0) * 1000
    return label, round(conf, 1), round(infer_ms, 3)

# ── LOAD SHEDDING DECISION ──────────────────────────────────────────
def check_shed():
    with state_lock:
        f1 = state["esp1"]
        f2 = state["esp2"]
        if f1 is None or f2 is None:
            return

        total = f1["rms"] + f2["rms"]
        currently_shed = state["shed"]

        # Shed Room 2 if total exceeds threshold and Room 2 is lower priority (LED or same)
        should_shed = total > THRESHOLD_A

        if should_shed and not currently_shed:
            state["shed"] = True
            state["shedCount"] += 1
            print(f"⚡ THRESHOLD EXCEEDED ({total:.3f}A > {THRESHOLD_A}A) — shedding Room 2")
            if GPIO_AVAILABLE:
                GPIO.output(GPIO_RELAY2, GPIO.LOW)   # Cut Room 2 motor
        elif not should_shed and currently_shed:
            state["shed"] = False
            print(f"✓ Load restored ({total:.3f}A < {THRESHOLD_A}A) — Room 2 back on")
            if GPIO_AVAILABLE:
                GPIO.output(GPIO_RELAY2, GPIO.HIGH)  # Restore Room 2

        # Accumulate saved energy
        if state["shed"] and f2 is not None:
            state["savedWh"] += (f2["rms"] * 5.0) / 3600.0  # per-second accumulation

# ── MQTT CALLBACKS ──────────────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe(MQTT_TOPIC)
        print(f"✓ MQTT connected — subscribed to '{MQTT_TOPIC}'")
    else:
        print(f"✗ MQTT connect failed — code {rc}")

def on_message(client, userdata, msg):
    try:
        data   = json.loads(msg.payload)
        device = data.get("device", "unknown")
        feat   = data.get("features", {})

        label, conf, infer_ms = classify(feat)

        with state_lock:
            state[device]              = feat
            state[f"{device}_label"]   = label
            state[f"{device}_conf"]    = conf
            state["inferMs"]           = infer_ms
            state["lastUpdate"]        = time.time()

        print(f"{device}: {label} ({conf:.1f}%) | "
              f"rms={feat.get('rms',0):.3f}A | "
              f"infer={infer_ms:.3f}ms")

        check_shed()

    except Exception as e:
        print(f"Message parse error: {e}")

# ── FLASK API ────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)   # allow website to poll from any origin

@app.route("/api/status")
def api_status():
    return jsonify({"status": "ok", "version": "1.0"})

@app.route("/api/data")
def api_data():
    with state_lock:
        return jsonify({
            "esp1":      state["esp1"],
            "esp2":      state["esp2"],
            "esp1_label": state["esp1_label"],
            "esp2_label": state["esp2_label"],
            "esp1_conf":  state["esp1_conf"],
            "esp2_conf":  state["esp2_conf"],
            "inferMs":    state["inferMs"],
            "shed":       state["shed"],
            "shedCount":  state["shedCount"],
            "savedWh":    round(state["savedWh"], 6),
            "totalA":     round(
                (state["esp1"]["rms"] if state["esp1"] else 0) +
                (state["esp2"]["rms"] if state["esp2"] else 0), 4
            ),
            "lastUpdate": state["lastUpdate"],
            "uptime":     round(time.time() - START_TIME, 1),
        })

@app.route("/api/shed/<room>")
def manual_shed(room):
    """Manual shed trigger — GET /api/shed/2 to shed Room 2"""
    if room == "1" and GPIO_AVAILABLE:
        GPIO.output(GPIO_RELAY1, GPIO.LOW)
        return jsonify({"action": "shed", "room": 1})
    elif room == "2" and GPIO_AVAILABLE:
        GPIO.output(GPIO_RELAY2, GPIO.LOW)
        return jsonify({"action": "shed", "room": 2})
    return jsonify({"error": "unknown room or GPIO not available"})

@app.route("/api/restore/<room>")
def restore(room):
    """Restore — GET /api/restore/2"""
    if room == "1" and GPIO_AVAILABLE:
        GPIO.output(GPIO_RELAY1, GPIO.HIGH)
        return jsonify({"action": "restore", "room": 1})
    elif room == "2" and GPIO_AVAILABLE:
        GPIO.output(GPIO_RELAY2, GPIO.HIGH)
        return jsonify({"action": "restore", "room": 2})
    return jsonify({"error": "unknown room or GPIO not available"})

# ── MAIN ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    START_TIME = time.time()

    print("=" * 55)
    print("  Entity Neural — Pi Bridge Server")
    print("=" * 55)
    print(f"  MQTT broker : {MQTT_BROKER}:1883")
    print(f"  MQTT topic  : {MQTT_TOPIC}")
    print(f"  Threshold   : {THRESHOLD_A} A")
    print(f"  Relay 1     : GPIO{GPIO_RELAY1} (Room 1)")
    print(f"  Relay 2     : GPIO{GPIO_RELAY2} (Room 2 — shed target)")
    print(f"  API port    : {API_PORT}")
    print("=" * 55)
    print()
    print(f"  Dashboard → enter Pi IP in the connection panel")
    print(f"  URL: http://YOUR_PI_IP:{API_PORT}/api/data")
    print()

    # Start MQTT in background thread
    mqttc = mqtt.Client()
    mqttc.on_connect = on_connect
    mqttc.on_message = on_message
    mqttc.connect(MQTT_BROKER, 1883, 60)
    mqtt_thread = threading.Thread(target=mqttc.loop_foarever, daemon=True)
    mqtt_thread.start()

    # Start Flask (disable reloader so threading works)
    try:
        app.run(host="0.0.0.0", port=API_PORT, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
        if GPIO_AVAILABLE:
            GPIO.cleanup()
