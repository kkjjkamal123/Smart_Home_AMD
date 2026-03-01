from machine import ADC, Pin
from umqtt.simple import MQTTClient
import network
import time
import ujson
import math

# Configuration
SSID = "KAMAL_LOQ"
PASSWORD = "lol12345"
MQTT_BROKER = "192.168.137.174"  # Pi IP
TOPIC = b"sensor/data"
DEVICE_NAME = "esp1"  # Change to "esp2" on second board

# Simulation parameters for M027 geared motor
MOTOR_STEADY_CURRENT = 0.42  # Amps (typical M027 steady state)
MOTOR_STARTUP_PEAK = 2.3     # Amps (3-5x surge during startup)
STARTUP_DURATION = 8         # Seconds of startup phase
NOISE_LEVEL = 0.03           # Random fluctuation

# Connect to WiFi
print("Connecting to WiFi...")
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(SSID, PASSWORD)

while not wlan.isconnected():
    print(".", end="")
    time.sleep(1)
print(f"\nWiFi connected: {wlan.ifconfig()[0]}")

# Connect to MQTT
client = MQTTClient(DEVICE_NAME, MQTT_BROKER)
client.connect()
print(f"MQTT connected to {MQTT_BROKER}")

# Simulation state
tick = 0
phase = "startup"  # startup, steady, off
phase_start = 0

def generate_motor_features():
    """Generate realistic motor current features"""
    global tick, phase, phase_start
    
    # Phase transitions
    if phase == "startup" and (tick - phase_start) > STARTUP_DURATION:
        phase = "steady"
        phase_start = tick
    
    # Simulate different phases
    if phase == "startup":
        # Exponential decay from peak to steady
        progress = (tick - phase_start) / STARTUP_DURATION
        current = MOTOR_STARTUP_PEAK * math.exp(-3 * progress) + MOTOR_STEADY_CURRENT
        # High variability during startup
        noise = (random() - 0.5) * NOISE_LEVEL * 4
    elif phase == "steady":
        # Stable with small fluctuations
        current = MOTOR_STEADY_CURRENT
        noise = (random() - 0.5) * NOISE_LEVEL
    else:  # off
        current = 0.0
        noise = 0.0
    
    current += noise
    
    # Generate feature vector (simulating what real sensor would produce)
    rms = max(0.0, current)
    peak = max(0.0, current * (1.4 if phase == "startup" else 1.2))
    std = abs(noise) * 2
    crest = peak / (rms + 0.001)  # Avoid division by zero
    surge = peak if phase == "startup" else rms
    
    return {
        "rms": round(rms, 4),
        "peak": round(peak, 4),
        "std": round(std, 4),
        "crest": round(crest, 2),
        "surge": round(surge, 4)
    }

def random():
    """Simple random number generator"""
    return (tick * 9301 + 49297) % 233280 / 233280.0

# Main loop
print("Starting motor simulation...")
try:
    while True:
        features = generate_motor_features()
        
        payload = {
            "device": DEVICE_NAME,
            "features": features,
            "phase": phase,
            "tick": tick
        }
        
        client.publish(TOPIC, ujson.dumps(payload))
        print(f"Published: RMS={features['rms']:.3f}A, Peak={features['peak']:.3f}A, Phase={phase}")
        
        tick += 1
        time.sleep(1)
        
except KeyboardInterrupt:
    print("\nStopped by user")
except Exception as e:
    print(f"Error: {e}")
finally:
    client.disconnect()
    wlan.disconnect()
