#!/usr/bin/env python3

import logging
import sqlite3
import sys
import time

import st7735
from PIL import Image, ImageDraw, ImageFont
from fonts.ttf import RobotoMedium as UserFont
from bme280 import BME280
from enviroplus import gas
from pms5003 import PMS5003, ReadTimeoutError as pmsReadTimeoutError, SerialTimeoutError
from supabase import create_client

from config import (
    SUPABASE_URL, SUPABASE_KEY,
    TAPO_IP, TAPO_EMAIL, TAPO_PASS,
    PM25_THRESHOLD, LOG_INTERVAL, SQLITE_PATH
)

try:
    from ltr559 import LTR559
    ltr559 = LTR559()
except ImportError:
    import ltr559

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)-8s %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S"
)

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self):
        self.current_readings: dict = {}
        self.last_sync_time: float | None = None
        self.last_written: dict[str, float] = {}
        self.cpu_temp_history: list[float] = []

# ---------------------------------------------------------------------------
# Sensor + client init
# ---------------------------------------------------------------------------

bme280   = BME280()
pms5003  = PMS5003()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------------------------------------------------------
# SQLite init
# ---------------------------------------------------------------------------

def init_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        create table if not exists readings (
            id          integer primary key autoincrement,
            recorded_at text    default (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            label       text    not null,
            unit        text    not null,
            value       real    not null
        )
    """)
    conn.execute("create index if not exists idx_readings_label on readings (label)")
    conn.execute("create index if not exists idx_readings_time  on readings (recorded_at)")
    conn.commit()
    return conn

sqlite_conn = init_sqlite(SQLITE_PATH)

# ---------------------------------------------------------------------------
# Display init
# ---------------------------------------------------------------------------

display = st7735.ST7735(
    port=0, cs=1, dc="GPIO9",
    backlight="GPIO12", rotation=270,
    spi_speed_hz=10000000
)
display.begin()

WIDTH  = display.width
HEIGHT = display.height

img  = Image.new("RGB", (WIDTH, HEIGHT), color=(0, 0, 0))
draw = ImageDraw.Draw(img)
font = ImageFont.truetype(UserFont, 11)

CELL_W = WIDTH  // 2
CELL_H = HEIGHT // 4

DISPLAY_SENSORS = {
    "temperature": ("tmp", "°C"),
    "pressure":    ("prs", "hPa"),
    "humidity":    ("hum", "%"),
    "nh3":         ("nh3", "kΩ"),
    "pm1":         ("pm1", "ug/m3"),
    "pm2_5":       ("p25", "ug/m3"),
    "pm10":        ("p10", "ug/m3"),
}

GRID_CELLS = ["sync", "temperature", "pressure", "humidity", "nh3", "pm1", "pm2_5", "pm10"]

# ---------------------------------------------------------------------------
# CPU temp compensation
# ---------------------------------------------------------------------------

COMP_FACTOR = 2.25

def get_cpu_temp() -> float:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000.0

def compensated_temperature(raw_temp: float, state: AppState) -> float:
    state.cpu_temp_history = (state.cpu_temp_history + [get_cpu_temp()])[-5:]
    avg_cpu = sum(state.cpu_temp_history) / len(state.cpu_temp_history)
    return raw_temp - ((avg_cpu - raw_temp) / COMP_FACTOR)

# ---------------------------------------------------------------------------
# Read all sensors
# ---------------------------------------------------------------------------

def read_all_sensors(state: AppState) -> dict:
    readings = {}

    raw_temp = bme280.get_temperature()
    readings["temperature"] = compensated_temperature(raw_temp, state)
    readings["pressure"]    = bme280.get_pressure()
    readings["humidity"]    = bme280.get_humidity()

    proximity = ltr559.get_proximity()
    readings["light"] = ltr559.get_lux() if proximity < 10 else 1.0

    gas_data = gas.read_all()
    readings["oxidised"] = gas_data.oxidising / 1000
    readings["reduced"]  = gas_data.reducing  / 1000
    readings["nh3"]      = gas_data.nh3        / 1000

    try:
        pms_data = pms5003.read()
        readings["pm1"]   = float(pms_data.pm_ug_per_m3(1.0))
        readings["pm2_5"] = float(pms_data.pm_ug_per_m3(2.5))
        readings["pm10"]  = float(pms_data.pm_ug_per_m3(10))
    except (pmsReadTimeoutError, SerialTimeoutError):
        logging.warning("PMS5003 read timeout — skipping particulates")
        readings["pm1"] = readings["pm2_5"] = readings["pm10"] = None

    return readings

# ---------------------------------------------------------------------------
# Shared row builder
# ---------------------------------------------------------------------------

SENSOR_META = {
    "temperature": ("temperature", "c"),
    "pressure":    ("pressure",    "hPa"),
    "humidity":    ("humidity",    "%"),
    "light":       ("light",       "lux"),
    "oxidised":    ("oxidised",    "kO"),
    "reduced":     ("reduced",     "kO"),
    "nh3":         ("nh3",         "kO"),
    "pm1":         ("pm1",         "ug/m3"),
    "pm2_5":       ("pm2_5",       "ug/m3"),
    "pm10":        ("pm10",        "ug/m3"),
}

def build_rows(sensor_readings: dict) -> list[dict]:
    return [
        {"label": label, "unit": unit, "value": sensor_readings[key]}
        for key, (label, unit) in SENSOR_META.items()
        if sensor_readings.get(key) is not None
    ]

# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def write_to_sqlite(rows: list[dict]):
    try:
        sqlite_conn.executemany(
            "insert into readings (label, unit, value) values (:label, :unit, :value)",
            rows
        )
        sqlite_conn.commit()
        logging.info(f"SQLite: wrote {len(rows)} rows")
    except Exception as e:
        logging.error(f"SQLite write failed: {e}")

def write_to_supabase(rows: list[dict], state: AppState):
    try:
        supabase.table("readings").insert(rows).execute()
        state.last_sync_time = time.time()
        logging.info(f"Supabase: wrote {len(rows)} rows")
    except Exception as e:
        logging.error(f"Supabase write failed: {e}")

def write_readings(sensor_readings: dict, state: AppState):
    all_rows = build_rows(sensor_readings)
    changed = [r for r in all_rows if round(r["value"], 1) != state.last_written.get(r["label"])]
    if not changed:
        logging.info("No sensor values changed — skipping write")
        return
    for r in changed:
        state.last_written[r["label"]] = round(r["value"], 1)
    write_to_sqlite(changed)
    write_to_supabase(changed, state)

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def render_dashboard(state: AppState):
    draw.rectangle((0, 0, WIDTH, HEIGHT), (0, 0, 0))

    for i, key in enumerate(GRID_CELLS):
        x = (i % 2) * CELL_W
        y = (i // 2) * CELL_H

        if key == "sync":
            label = "sync"
            if state.last_sync_time is not None:
                value_text = time.strftime("%H:%M", time.localtime(state.last_sync_time))
            else:
                value_text = "--:--"
            value_color = (255, 255, 255)
        else:
            label, unit = DISPLAY_SENSORS[key]
            val = state.current_readings.get(key)
            if val is None:
                value_text  = "ERR"
                value_color = (255, 0, 0)
            elif key == "pm2_5" and val > PM25_THRESHOLD:
                value_text  = f"{val:.1f} {unit}"
                value_color = (255, 0, 0)
            else:
                value_text  = f"{val:.1f} {unit}"
                value_color = (255, 255, 255)

        draw.text((x + 2, y + 1),  label,      font=font, fill=(180, 180, 180))
        draw.text((x + 2, y + 12), value_text, font=font, fill=value_color)

    display.display(img)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

state    = AppState()
last_log = 0

state.current_readings = read_all_sensors(state)

try:
    while True:
        now = time.time()

        if now - last_log >= LOG_INTERVAL:
            state.current_readings = read_all_sensors(state)
            write_readings(state.current_readings, state)
            last_log = now

        render_dashboard(state)
        time.sleep(0.5)

except KeyboardInterrupt:
    logging.info("Exiting cleanly")
    sqlite_conn.close()
    sys.exit(0)