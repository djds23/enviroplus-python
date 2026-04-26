#!/usr/bin/env python3

import asyncio
import colorsys
import logging
import sqlite3
import sys
import time

import st7735
from PIL import Image, ImageDraw, ImageFont
from fonts.ttf import RobotoMedium as UserFont
from bme280 import BME280
from enviroplus import gas
from pms5003 import PMS5003, ReadTimeoutError as pmsReadTimeoutError
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
font = ImageFont.truetype(UserFont, 20)

TOP_BAR = 25

VARIABLES = [
    ("temperature", "°C"),
    ("pressure",    "hPa"),
    ("humidity",    "%"),
    ("light",       "Lux"),
    ("oxidised",    "kΩ"),
    ("reduced",     "kΩ"),
    ("nh3",         "kΩ"),
    ("pm1",         "ug/m3"),
    ("pm2_5",       "ug/m3"),
    ("pm10",        "ug/m3"),
]

history = {name: [1] * WIDTH for name, _ in VARIABLES}

# ---------------------------------------------------------------------------
# CPU temp compensation
# ---------------------------------------------------------------------------

COMP_FACTOR      = 2.25
cpu_temp_history = []

def get_cpu_temp() -> float:
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000.0

def compensated_temperature(raw_temp: float) -> float:
    global cpu_temp_history
    cpu_temp_history = (cpu_temp_history + [get_cpu_temp()])[-5:]
    avg_cpu = sum(cpu_temp_history) / len(cpu_temp_history)
    return raw_temp - ((avg_cpu - raw_temp) / COMP_FACTOR)

# ---------------------------------------------------------------------------
# Read all sensors
# ---------------------------------------------------------------------------

def read_all_sensors() -> dict:
    readings = {}

    raw_temp = bme280.get_temperature()
    readings["temperature"] = compensated_temperature(raw_temp)
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
    except SerialTimeoutError:
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

def write_to_supabase(rows: list[dict]):
    try:
        supabase.table("readings").insert(rows).execute()
        logging.info(f"Supabase: wrote {len(rows)} rows")
    except Exception as e:
        logging.error(f"Supabase write failed: {e}")

def write_readings(sensor_readings: dict):
    rows = build_rows(sensor_readings)
    write_to_sqlite(rows)
    write_to_supabase(rows)

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def update_display(variable: str, unit: str, value: float):
    history[variable] = history[variable][1:] + [value]
    vals = history[variable]
    vmin, vmax = min(vals), max(vals)
    colours = [(v - vmin + 1) / (vmax - vmin + 1) for v in vals]

    draw.rectangle((0, 0, WIDTH, HEIGHT), (255, 255, 255))
    for i, c in enumerate(colours):
        r, g, b = [int(x * 255) for x in colorsys.hsv_to_rgb((1.0 - c) * 0.6, 1.0, 1.0)]
        draw.rectangle((i, TOP_BAR, i + 1, HEIGHT), (r, g, b))
        line_y = HEIGHT - (TOP_BAR + (c * (HEIGHT - TOP_BAR))) + TOP_BAR
        draw.rectangle((i, line_y, i + 1, line_y + 1), (0, 0, 0))

    draw.text((0, 0), f"{variable[:4]}: {value:.1f} {unit}", font=font, fill=(0, 0, 0))
    display.display(img)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

mode      = 0
last_tap  = 0
TAP_DELAY = 0.5
last_log  = 0

current_readings = read_all_sensors()

try:
    while True:
        proximity = ltr559.get_proximity()
        now = time.time()

        if proximity > 1500 and now - last_tap > TAP_DELAY:
            mode     = (mode + 1) % len(VARIABLES)
            last_tap = now

        var_name, var_unit = VARIABLES[mode]
        val = current_readings.get(var_name)
        if val is not None:
            update_display(var_name, var_unit, val)

        if now - last_log >= LOG_INTERVAL:
            current_readings = read_all_sensors()
            write_readings(current_readings)
            # check_and_trigger(current_readings)
            last_log = now

        time.sleep(0.5)

except KeyboardInterrupt:
    logging.info("Exiting cleanly")
    sqlite_conn.close()
    sys.exit(0)