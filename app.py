# Install required libraries
# sudo apt-get install python-crypto python-pip  # for RPi, Linux
# python3 -m pip install pycryptodome            # or pycrypto or Crypto or pyaes
# https://pimylifeup.com/raspberry-pi-flask-web-app/
# Windows:
#  pyinstaller --clean --onefile --add-data "templates*;." --add-data "devices.json;." -n tuyaServer app.py
# Linux:
# .venv/bin/pyinstaller --clean --onefile --add-data "templates*:." --add-data "devices.json:." -n tuyaServer_deb app.py
import logging
import subprocess
import threading
from uuid import uuid4
import webbrowser
from flask import Flask, render_template, request, jsonify, redirect #pip install Flask
from flask_restful import Resource, Api #pip install Flask-RESTful
import json
from datetime import datetime, timedelta, timezone
import os
import sys
import jinja2
import sqlite3
import requests #pip install requests
import paho.mqtt.client as mqtt #pip install paho-mqtt
from waitress import serve #pip install waitress
from apscheduler.schedulers.background import BackgroundScheduler #pip install apscheduler
from threading import Thread
import platform
import shutil
from PIL import Image #pip install pillow

try:
    from pystray import Icon as TrayIcon, MenuItem as item, Menu #pip install pystray
except ValueError:
     subprocess.run(['sudo', 'apt', 'install', '-y', 'libayatana-appindicator3-1', 'gir1.2-ayatanaappindicator3-0.1'])

# ---------- Start Configurations ---------- #
VERSION = "2025.11.27"
print(f"Zigbee Server Version: {VERSION}")
APP_NAME = "ZigbeeServer"

template_loader = ''
if getattr(sys, 'frozen', False):
    # for the case of running in pyInstaller's exe
    bundle_dir = sys._MEIPASS
    #template_loader = jinja2.FileSystemLoader(os.path.join(bundle_dir, 'templates'))
else:
    # for running locally
    #template_loader = jinja2.FileSystemLoader(searchpath="./templates")
    bundle_dir = os.path.dirname(os.path.abspath(__file__))
template_folder = os.path.join(bundle_dir, 'templates')
template_loader = jinja2.FileSystemLoader(template_folder)
print(f"template_loader - {template_folder}")
template_env = jinja2.Environment(loader=template_loader)

app = Flask(__name__)
api = Api(app)
mqtt_client = None
mqtt_connection_error = None
LAST_UPDATE_TIMESTAMP = datetime.now()

# ---------- End Configurations ---------- #
# ---------- Start Classes ---------- #
class On(Resource):
    def get(self, pk):
        try:
            switch = switches.get(pk)
            switch.turn_on()
            logging.info(f"Switch {pk} turned on")
        except:
            return "error"
        return f"{pk} Switch Turned On"

class Off(Resource):
    def get(self, pk):
        try:
            switch = switches.get(pk)
            switch.turn_off()
            logging.info(f"Switch {pk} turned off")
        except:
            return "error"
        return f"{pk} Switch Turned Off"

class Status(Resource):
    def get(self, pk):
        for device in devices:
            if device["id"] == pk: # Assuming 'id' is the friendly_name
                state = device.get("state", "unknown")
                if state is True or str(state).lower() == 'on':
                    status = "On"
                elif state is False or str(state).lower() == 'off':
                    status = "Off"
                else:
                    status = "Offline"
                logging.info(f"Status of {pk} returned as {status}")
                logging.info(f"Request info: {getRequestInfo()}")
                return f"{device['name']} Status: {status}"

#api.add_resource(Items, '/')
api.add_resource(On, '/on/<pk>')
api.add_resource(Off, '/off/<pk>')
api.add_resource(Status, '/status/<pk>')
# ---------- End Classes ---------- #
# ---------- Start Routing Functions ---------- #
@app.route("/")
def index():
    switchInfo = []
    for device in devices:
        switchInfo.append([
            device.get("solution", ""),
            device.get("id", ""), # friendly_name is the new ID
            device.get("state", False),
            device.get("voltage", 0),
            device.get("id", "")
            # The state can be True, False, 'unknown', or 'offline'
        ])
    return render_template("index.html", switches=switchInfo, title=title, minButtonWidth=minButtonWidth, mqtt_error=mqtt_connection_error, last_update=LAST_UPDATE_TIMESTAMP.isoformat())

# Route for toggling a switch
@app.route("/toggle/<pk>")
def toggle_switch(pk):
    print(f"get Toggle input {pk}")
    for device in devices:
        if device["id"] == pk: # pk is the friendly_name
            current_state = device.get("state", False)
            new_state_str = "OFF" if (current_state is True or str(current_state).lower() == 'on') else "ON"
            
            topic = f"{config.get('mqtt_base_topic', 'zigbee2mqtt')}/{pk}/set"
            payload = json.dumps({"state": new_state_str})
            
            if mqtt_client:
                mqtt_client.publish(topic, payload)
                logging.info(f"Published to {topic}: {payload}")
            else:
                logging.error("MQTT client not available, cannot toggle switch.")

            logging.info(f"Request info: {getRequestInfo()}")
            break
    return redirect("/")

@app.route("/all/<action>")
def all_switches(action):
    if action not in ["on", "off"]:
        return "Invalid action", 400

    excluded_devices = config.get("exclude_from_all", [])

    for device in devices:
        if device['id'] in excluded_devices:
            logging.info(f"Skipping device {device['name']} as it is in the exclusion list for 'all' actions.")
            continue
        
        # We only act on devices that are plugs/switches (i.e., support on/off)
        if device.get("power") is not None: # A simple check if it's a controllable switch
            topic = f"{config.get('mqtt_base_topic', 'zigbee2mqtt')}/{device['id']}/set"
            payload = json.dumps({"state": action.upper()})

            if mqtt_client:
                mqtt_client.publish(topic, payload)
                logging.info(f"Published to {topic}: {payload}")
            else:
                logging.error(f"MQTT client not available, cannot control {device['id']}.")

    return redirect("/")

@app.route("/settings", methods=["GET", "POST"])
def settings():
    global config, devices
    db = get_db_conn()
    cursor = db.cursor()
    if request.method == "POST":
        # Reload devices from DB to ensure we have the latest list before processing the form
        devices_from_db = db.execute('SELECT * FROM devices').fetchall()
        current_devices = [dict(row) for row in devices_from_db]

        # Update general settings
        for key in ['title', 'refresh', 'port', 'minButtonWidth', 'autoUpdate', 'autoUpdateURL', 'mqtt_host', 'mqtt_port', 'mqtt_user', 'mqtt_pass', 'mqtt_base_topic', 'z2m_url', 'offline_timeout', 'open_on_startup']:
            if key in request.form:
                # The 'devices' list is reloaded inside load_config_from_db, so we can use it here.
                update_setting(key, request.form[key])

        # Update device solutions
        for device in current_devices:
            solution_key = f"device_solution_{device['id']}"
            if solution_key in request.form:
                #print(f"Updating device {device['id']} solution to {request.form[solution_key]}")
                update_device_solution(device['id'], request.form[solution_key])

        # Update 'exclude_from_all' list
        excluded_ids = request.form.getlist("exclude_from_all")
        cursor.execute('DELETE FROM excluded_devices')
        for device_id in excluded_ids:
            cursor.execute('INSERT INTO excluded_devices (device_id) VALUES (?)', (device_id,))
        db.commit()

        # Reload global config variables
        load_config_from_db()
        # Restart MQTT client with new settings
        init_mqtt_client()
        logging.info("Settings updated successfully")
        return redirect("/")
    
    # GET Request: Ensure we have the latest data from the DB
    load_config_from_db()
    db.close()
    return render_template("settings.html", config=config, devices=devices, title="Settings")

@app.route("/delete_device/<device_id>")
def delete_device(device_id):
    global devices

    # Find and remove the device from the global 'devices' list
    device_to_remove = next((d for d in devices if d.get('id') == device_id), None)
    if device_to_remove:
        devices.remove(device_to_remove)

    # Remove from database
    db = get_db_conn()
    db.execute('DELETE FROM devices WHERE id = ?', (device_id,))
    db.commit()
    db.close()
    return redirect("/settings")

@app.route("/schedule", methods=["GET", "POST"])
def schedule():
    global devices, config
    error_message = None
    if request.method == "POST":
        schedules = []
        schedule_ids = request.form.getlist("schedule_id")
        schedule_names = request.form.getlist("schedule_name")
        schedule_actions = request.form.getlist("schedule_action")
        schedule_times = request.form.getlist("schedule_time")
        timezone_offset_minutes = int(request.form.get('timezone_offset', 0))
        submitted_schedules = [] # To hold the data from the form

        for i in range(len(schedule_ids)):
            sid = schedule_ids[i]
            if not sid:
                sid = str(uuid4())
            name = schedule_names[i]
            action = schedule_actions[i].lower()
            days = request.form.getlist(f"schedule_days_{i}")
            time_str = schedule_times[i]
            device_list = request.form.getlist(f"schedule_devices_{i}")

            # Store submitted data to re-render if validation fails
            submitted_schedules.append({
                "id": sid,
                "name": name,
                "action": action,
                "days": days,
                "time": time_str, # Keep local time for re-rendering
                "devices": device_list
            })

            # Validation for each schedule entry
            if not time_str:
                error_message = f"Validation failed for schedule '{name}': Time is required."
                break
            if not days:
                error_message = f"Validation failed for schedule '{name}': At least one day must be selected."
                break
            if not device_list:
                error_message = f"Validation failed for schedule '{name}': At least one device must be selected."
                break
            if not name:
                name = f"Schedule {i+1}"
            
            # Correctly convert user's local time to UTC using the browser's offset
            local_dt = datetime.strptime(time_str, '%H:%M')
            # getTimezoneOffset() returns a positive value for timezones BEHIND UTC (e.g., Brazil), so we must ADD the offset to get to UTC.
            utc_dt = local_dt + timedelta(minutes=timezone_offset_minutes)
            utc_time_str = utc_dt.strftime('%H:%M') # Use the converted UTC time
            #print(f"Received local time {time_str} with offset {timezone_offset_minutes} mins. Converted to UTC: {utc_time_str}")
            schedules.append({
                "id": sid,
                "name": name,
                "action": action,
                "days": days,
                "time": utc_time_str, # Use the converted UTC time
                "devices": device_list
            })
        
        if error_message:
            # If validation fails, re-render the page with the error
            return render_template("schedule.html", schedules=submitted_schedules, devices=devices, title="Schedule Configuration", error=error_message)

        # Save schedules to DB
        db = get_db_conn()
        cursor = db.cursor()
        cursor.execute('DELETE FROM schedules')
        cursor.execute('DELETE FROM schedule_days')
        cursor.execute('DELETE FROM schedule_devices')
        for s in schedules:
            cursor.execute('INSERT INTO schedules (id, name, action, time) VALUES (?, ?, ?, ?)', (s['id'], s['name'], s['action'], s['time']))
            for day in s['days']:
                cursor.execute('INSERT INTO schedule_days (schedule_id, day) VALUES (?, ?)', (s['id'], day))
            for dev_id in s['devices']:
                cursor.execute('INSERT INTO schedule_devices (schedule_id, device_id) VALUES (?, ?)', (s['id'], dev_id))
        db.commit()
        db.close()

        # Reload config to get the latest schedules before updating the scheduler
        load_config_from_db()
        updateApScheduler()
        return redirect("/")
    
    # GET method: show schedules and devices
    schedules = get_schedules_from_db()
    return render_template("schedule.html", schedules=schedules, devices=devices, title="Schedule Configuration", error=error_message)

@app.route("/check-for-updates")
def check_for_updates():
    """Endpoint for the client to check if a refresh is needed."""
    return jsonify({'last_update': LAST_UPDATE_TIMESTAMP.isoformat()})

# ---------- End Routing Functions ---------- #
# ---------- Start Functions ---------- #

def getRequestInfo():
    # Get IP address
    client_ip = request.remote_addr
    forwarded_ip = request.headers.get('X-Forwarded-For', client_ip)

    # Get browser details
    user_agent = request.user_agent.string

    # Get location data
    #response = requests.get(f"https://ipinfo.io/{forwarded_ip}/json")
    #location_data = response.json()

    return {
        "IP Address": forwarded_ip,
        "Browser": user_agent
    }
    return {
        "IP Address": forwarded_ip,
        "Browser": user_agent,
        "Location": location_data
    }

def updateApScheduler():
    """
    Updates the APScheduler with the current schedules.
    This function should be called whenever schedules are modified.
    """
    scheduler.remove_all_jobs()

    for schedule in config.get("schedules", []):
        days_of_week = schedule.get('days')
        if days_of_week:  # Only add job if days are specified
            scheduler.add_job(
                func=executeSchedule,
                trigger='cron',
                day_of_week=','.join(days_of_week),
                hour=int(schedule['time'].split(':')[0]),
                minute=int(schedule['time'].split(':')[1]),
                args=[schedule['action'], schedule['devices']],
            )
        else:
            logging.warning(f"Skipping schedule '{schedule.get('name')}' because no days are configured.")
    #print(scheduler.get_jobs())
    print("Scheduler updated with new schedules.")
    logging.info("Scheduler updated with new schedules.")

def executeSchedule(action, scheduledDevices):
    """
    Executes the action for the given schedule.
    This function should be called by the scheduler.
    """
    print(f"SCHEDULED: Executing action {action} for devices: {scheduledDevices}")
    for device_id in scheduledDevices:
        # device_id is the friendly_name
        topic = f"{config.get('mqtt_base_topic', 'zigbee2mqtt')}/{device_id}/set"
        payload = json.dumps({"state": action.upper()})
        if mqtt_client:
            mqtt_client.publish(topic, payload)
            logging.info(f"SCHEDULED: Published to {topic}: {payload}")
            print(f"SCHEDULED: Action {action} executed on device {device_id}")
        else:
            logging.error(f"SCHEDULED: MQTT client not available, cannot control {device_id}.")
            print(f"SCHEDULED: Error executing action {action} on device {device_id}: MQTT client not available.")

def check_offline_devices():
    """Checks for devices that have not sent an update recently and marks them as offline."""
    global devices, LAST_UPDATE_TIMESTAMP, config
    now = datetime.now()
    # Use a default of 30 seconds if the setting is missing
    timeout_seconds = int(config.get('offline_timeout', 30))
    for device in devices:
        if now - device.get('last_seen', now) > timedelta(seconds=timeout_seconds):
            if device['state'] != 'offline':
                device['state'] = 'offline'
                LAST_UPDATE_TIMESTAMP = datetime.now()

def sortDevicesByName(newDict):
    """
    Sorts the devices list by the 'solution' key.
    """
    return sorted(newDict, key=lambda x: x.get('solution', '').lower())

# ---------- Database Functions ---------- #

def get_db_conn():
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_conn()
    cursor = conn.cursor()
    # Settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # Populate settings with default values if they don't exist
    default_settings = [
        ('title', 'Zigbee Server'),
        ('refresh', '5'),
        ('port', '5050'),
        ('minButtonWidth', '300'),
        ('autoUpdate', 'True'),
        ('autoUpdateURL', 'https://proj.ydreams.global/ydreams/apps/servers/tuyaServer_deb'),
        ('mqtt_host', 'localhost'),
        ('mqtt_port', '1883'),
        ('mqtt_user', ''),
        ('mqtt_pass', ''),
        ('mqtt_base_topic', 'zigbee2mqtt'),
        ('z2m_url', 'localhost:8080'),
        ('offline_timeout', '30'),
        ('open_on_startup', 'True')
    ]
    cursor.executemany('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', default_settings)

    # Devices table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY, -- friendly_name from Zigbee2MQTT
            name TEXT, -- friendly_name from Zigbee2MQTT
            solution TEXT -- User-defined name/solution
        )
    ''')
    # Excluded devices table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS excluded_devices (
            device_id TEXT PRIMARY KEY,
            FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
        )
    ''')
    # Schedules table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedules (
            id TEXT PRIMARY KEY,
            name TEXT,
            action TEXT,
            time TEXT
        )
    ''')
    # Schedule days mapping
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule_days (
            schedule_id TEXT,
            day TEXT,
            PRIMARY KEY (schedule_id, day),
            FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE
        )
    ''')
    # Schedule devices mapping
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS schedule_devices (
            schedule_id TEXT,
            device_id TEXT,
            PRIMARY KEY (schedule_id, device_id),
            FOREIGN KEY (schedule_id) REFERENCES schedules (id) ON DELETE CASCADE,
            FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
        )
    ''')
    conn.commit()
    conn.close()

def migrate_json_to_db():
    if not os.path.isfile(settingsFile):
        return # No JSON file to migrate

    print("Migrating data from appConfig.json to tuya.db...")
    with open(settingsFile, 'r') as f:
        json_config = json.load(f)

    db = get_db_conn()
    cursor = db.cursor()

    # Migrate settings
    for key, value in json_config.items():
        if key not in ['devices', 'schedules', 'exclude_from_all']:
            cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, str(value)))

    # Migrate devices
    for device in json_config.get('devices', []):
        # This migration is tricky. We assume the old 'name' can be the new 'id' (friendly_name)
        cursor.execute('INSERT OR IGNORE INTO devices (id, name, solution) VALUES (?, ?, ?)', (device['name'], device['name'], device.get('solution', device['name'])))

    # Migrate excluded devices
    for device_id in json_config.get('exclude_from_all', []):
        cursor.execute('INSERT OR REPLACE INTO excluded_devices (device_id) VALUES (?)', (device_id,))

    # Migrate schedules
    for schedule in json_config.get('schedules', []):
        cursor.execute('INSERT OR REPLACE INTO schedules (id, name, action, time) VALUES (?, ?, ?, ?)',
                       (schedule['id'], schedule['name'], schedule['action'], schedule['time']))
        for day in schedule.get('days', []):
            cursor.execute('INSERT OR REPLACE INTO schedule_days (schedule_id, day) VALUES (?, ?)', (schedule['id'], day))
        for dev_id in schedule.get('devices', []):
            cursor.execute('INSERT OR REPLACE INTO schedule_devices (schedule_id, device_id) VALUES (?, ?)', (schedule['id'], dev_id))

    db.commit()
    db.close()
    # Rename old config file to prevent re-migration
    os.rename(settingsFile, settingsFile + '.migrated')
    print("Migration complete. Old config file renamed to appConfig.json.migrated")

def get_all_settings():
    db = get_db_conn()
    settings_dict = {row['key']: row['value'] for row in db.execute('SELECT * FROM settings').fetchall()}
    db.close()
    return settings_dict

def update_setting(key, value):
    conn = get_db_conn()
    conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

def update_device_solution(device_id, solution):
    conn = get_db_conn()
    conn.execute('UPDATE devices SET solution = ? WHERE id = ?', (solution, device_id))
    conn.commit()
    conn.close()

def get_schedules_from_db():
    db = get_db_conn()
    schedules_list = []
    schedules = db.execute('SELECT * FROM schedules').fetchall()
    for s in schedules:
        schedule_dict = dict(s)
        days = db.execute('SELECT day FROM schedule_days WHERE schedule_id = ?', (s['id'],)).fetchall()
        schedule_dict['days'] = [row['day'] for row in days]
        devices_in_schedule = db.execute('SELECT device_id FROM schedule_devices WHERE schedule_id = ?', (s['id'],)).fetchall()
        schedule_dict['devices'] = [row['device_id'] for row in devices_in_schedule]
        schedules_list.append(schedule_dict)
    db.close()
    return schedules_list

# ---------- End Database Functions ---------- #

def get_modified_date(url):
    try:
        response = requests.head(url)  # Use HEAD request to get headers
        if 'Last-Modified' in response.headers:
            return response.headers['Last-Modified']
        else:
            return "No Last-Modified header found."
    except requests.exceptions.RequestException as e:
        return f"An error occurred: {e}"

def check_update(fileURL):
    getFileDate = get_modified_date(fileURL)
    if "An error occurred" in getFileDate or "No Last-Modified header found." in getFileDate:
        print(getFileDate)
        return
    
    newVersionDT = datetime.strptime(getFileDate, "%a, %d %b %Y %H:%M:%S %Z")
    versionDt = datetime.strptime(VERSION, "%Y.%m.%d")
    print(f"Current Version Date: {versionDt}")
    print(f"New Version Date: {newVersionDT}")
    if versionDt.date() < newVersionDT.date():
        logging.info("Update available!")
        print(f"Download link: {fileURL}")
        download_and_replace(fileURL)
    else:
        print("You are using the latest version.")

def download_and_replace(download_url):
    exe_path = sys.argv[0]
    tmp_path = exe_path + ".new"
    print(f"Downloading update from {download_url}...")
    r = requests.get(download_url, stream=True)
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(r.raw, f)
    print("Download complete.")
    # Create a batch file to replace the running exe after exit for windows
    bat_path = exe_path + ".bat"
    with open(bat_path, "w") as bat:
        bat.write(f"""@echo off
ping 127.0.0.1 -n 3 > nul
move /Y "{tmp_path}" "{exe_path}"
start "" "{exe_path}"
del "%~f0"
""")
    print("Restarting with update...")
    os.startfile(bat_path)

    # Create a batch file to replace the running exe after exit for linux
    if OS == "Linux":
        bat_path = exe_path + ".sh"
        with open(bat_path, "w") as bat:
            bat.write(f"""#!/bin/bash
sleep 3
mv -f "{tmp_path}" "{exe_path}"
"./{exe_path}"
""")
        os.chmod(tmp_path, 0o755)
        print("Restarting with update...")
        os.system(f"sh {bat_path}")

    sys.exit(0)

# ---------- MQTT Functions ---------- #

def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connection_error, LAST_UPDATE_TIMESTAMP
    # A reason_code of 0 means success.
    if reason_code == 0:
        print("Connected to MQTT Broker!")
        logging.info("Connected to MQTT Broker!")
        base_topic = config.get('mqtt_base_topic', 'zigbee2mqtt')
        # Subscribe to device status topics
        client.subscribe(f"{base_topic}/+") 
        logging.info(f"Subscribed to device topics: {base_topic}/+")

        mqtt_connection_error = None
        LAST_UPDATE_TIMESTAMP = datetime.now() # Force refresh on connect/reconnect
    else:
        print(f"Failed to connect, return code {reason_code}\n")
        logging.error(f"Failed to connect to MQTT Broker, return code {reason_code}")

def on_message(client, userdata, msg):
    #print(f"Received `{msg.payload.decode()}` from `{msg.topic}` topic")
    topic_parts = msg.topic.split('/')
    global LAST_UPDATE_TIMESTAMP
    base_topic = config.get('mqtt_base_topic', 'zigbee2mqtt')

    # We are interested in topics like 'zigbee2mqtt/DEVICE_NAME'
    # Ignore bridge status and other longer topics for auto-discovery
    if len(topic_parts) == 2 and topic_parts[0] == base_topic:
        friendly_name = topic_parts[1]
        
        # Find the device in our global list
        device = next((d for d in devices if d.get('id') == friendly_name), None)
        
        # If device is not tracked, add it automatically
        if not device:
            # Don't auto-add the bridge device
            if friendly_name == 'bridge':
                return

            logging.info(f"New device '{friendly_name}' detected. Adding to database.")
            db = get_db_conn()
            db.execute('INSERT OR IGNORE INTO devices (id, name, solution) VALUES (?, ?, ?)',
                       (friendly_name, friendly_name, friendly_name))
            db.commit()
            db.close()

            # Add to the runtime list so it appears immediately
            new_device = {'id': friendly_name, 'name': friendly_name, 'solution': friendly_name, 'state': 'unknown', 'voltage': 0, 'power': 0, 'linkquality': 0}
            devices.append(new_device)
            device['last_seen'] = datetime.now()
            LAST_UPDATE_TIMESTAMP = datetime.now() # Trigger refresh for new device
            device = new_device # Use this new device for the rest of the function
        
        try:
            payload = json.loads(msg.payload.decode())
            #print(f"MQTT Message for {friendly_name}: {payload}")
            # Update state
            if 'state' in payload:
                new_state = (payload['state'] == 'ON')
                if device.get('state') != new_state:
                    device['state'] = new_state
                    LAST_UPDATE_TIMESTAMP = datetime.now()
                    device['last_seen'] = datetime.now()
                    #print(f"State of {friendly_name} updated to {device['state']}")
            
            # Update other attributes
            if 'voltage' in payload:
                device['voltage'] = payload['voltage']
            if 'power' in payload:
                device['power'] = payload['power']
            if 'linkquality' in payload:
                device['linkquality'] = payload['linkquality']
            device['last_seen'] = datetime.now()

        except json.JSONDecodeError:
            # Not a JSON payload, might be a simple state string
            payload_str = msg.payload.decode()
            if payload_str.upper() in ['ON', 'OFF']:
                 new_state = (payload_str.upper() == 'ON')
                 if device.get('state') != new_state:
                    LAST_UPDATE_TIMESTAMP = datetime.now()
                    device['last_seen'] = datetime.now()
                    device['state'] = new_state
                    #print(f"State of {friendly_name} updated to {device['state']}")
        except Exception as e:
            logging.error(f"Error processing MQTT message for {friendly_name}: {e}")

def init_mqtt_client():
    global mqtt_client, mqtt_connection_error
    if mqtt_client:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    host = config.get("mqtt_host")
    if not host:
        print("MQTT host not configured. MQTT client will not start.")
        logging.warning("MQTT host not configured. MQTT client will not start.")
        return

    port = int(config.get("mqtt_port", 1883))
    user = config.get("mqtt_user")
    password = config.get("mqtt_pass")

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"tuya_server_{uuid4()}")
    if user and password:
        mqtt_client.username_pw_set(user, password)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    
    try:
        mqtt_client.connect(host, port, 60)
        mqtt_client.loop_start()
        mqtt_connection_error = None # Clear error on successful connection attempt
    except Exception as e:
        mqtt_connection_error = f"Could not connect to MQTT broker at {host}:{port}. Please check settings. Error: {e}"
        print(mqtt_connection_error)
        logging.error(mqtt_connection_error)

# ---------- End MQTT Functions ---------- #

def scan_and_save_new_devices():
    """Scans for new devices and adds them to the database."""
    logging.info("Querying Zigbee2MQTT for devices...")
    z2m_url = config.get("z2m_url")
    if not z2m_url:
        logging.warning("Zigbee2MQTT URL (z2m_url) not configured. Cannot scan for new devices.")
        return
    
    try:
        response = requests.get(f"{z2m_url}/api/devices")
        response.raise_for_status()
        z2m_devices = response.json()['data']
    except Exception as e:
        logging.error(f"Failed to get devices from Zigbee2MQTT API: {e}")
        return

    db = get_db_conn()
    for device in z2m_devices:
        if device.get('friendly_name') and device.get('type') == 'Router': # Most plugs are routers
            friendly_name = device['friendly_name']
            logging.info(f"Found device: {friendly_name}. Adding/updating in DB.")
            db.execute('INSERT OR IGNORE INTO devices (id, name, solution) VALUES (?, ?, ?)',
                       (friendly_name, friendly_name, friendly_name))
    db.commit()
    db.close()
    logging.info("Finished processing devices from scheduled scan.")

def start_scheduler():
    scheduler.start()

def load_config_from_db():
    global config, devices, title, refresh, port, minButtonWidth
    
    db = get_db_conn()
    
    # Load settings
    settings_from_db = {row['key']: row['value'] for row in db.execute('SELECT * FROM settings').fetchall()}    
    
    # Set defaults for all settings to ensure they exist
    title = settings_from_db.get("title", "Zigbee Server")
    refresh = int(settings_from_db.get("refresh", 5))
    port = int(settings_from_db.get("port", 5050))
    minButtonWidth = int(settings_from_db.get("minButtonWidth", 300))
    settings_from_db.setdefault("autoUpdate", "False")
    settings_from_db.setdefault("autoUpdateURL", "")
    settings_from_db.setdefault("mqtt_host", "")
    settings_from_db.setdefault("mqtt_port", "1883")
    settings_from_db.setdefault("mqtt_user", "")
    settings_from_db.setdefault("mqtt_pass", "")
    settings_from_db.setdefault("mqtt_base_topic", "zigbee2mqtt")
    settings_from_db.setdefault("z2m_url", "")
    settings_from_db.setdefault("offline_timeout", "30")
    settings_from_db.setdefault("open_on_startup", "True")

    # Load devices
    devices_from_db = db.execute('SELECT * FROM devices ORDER BY solution COLLATE NOCASE').fetchall()
    devices = [dict(row) for row in devices_from_db]
    # Initialize runtime state for devices
    for device in devices:
        device['state'] = 'unknown'
        device['voltage'] = 0
        device['power'] = 0
        device['linkquality'] = 0
        device['last_seen'] = datetime.now()

    # Load schedules into a config-like dictionary for the scheduler
    schedules = get_schedules_from_db()
    
    # Load excluded devices
    excluded_from_db = db.execute('SELECT device_id FROM excluded_devices').fetchall()
    excluded_devices = [row['device_id'] for row in excluded_from_db]

    config = {
        **settings_from_db,
        "schedules": schedules,
        "exclude_from_all": excluded_devices
    }
    db.close()

def exit_action(icon, item):
    """Function to be called when 'Exit' is clicked."""
    logging.info("Exit command received. Shutting down.")
    icon.stop()
    # A hard exit is the most reliable way to ensure all threads (like waitress) are terminated.
    os._exit(0)

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def open_browser():
    """
    Opens the dashboard URL in a new browser tab.
    """
    dashboard_url = f"http://127.0.0.1:{port}/"
    if sys.platform.startswith('linux'):
        subprocess.Popen(['xdg-open', dashboard_url])
    else:
        webbrowser.open_new_tab(dashboard_url)
        
def run_tray_icon():
    """Creates and runs the system tray icon."""
    try:
        image = Image.open(resource_path("icon.png"))
        menu = (
            item(f"Internet Tester: v_{VERSION}", None, enabled=False),
            Menu.SEPARATOR,
            item('Open Dashboard', open_browser, default=True),
            item('Exit', exit_action)
        )
        tray_icon = TrayIcon(APP_NAME, image, f"{APP_NAME} v{VERSION}", menu)
        logging.info("Starting system tray icon.")
        tray_icon.run()
    except Exception as e:
        logging.error(f"Failed to create system tray icon: {e}", exc_info=True)

def run_web_server():
    """Starts the Flask web server using Waitress."""
    host = "0.0.0.0"
    logging.info(f"Starting server on http://{host}:{port}")
    # The value from DB is a string 'True' or 'False'
    if config.get("open_on_startup", "True") == "True":
        logging.info(f"Dashboard will open automatically at: http://127.0.0.1:{port}/")
        threading.Timer(1, open_browser).start()
    else:
        logging.info(f"Dashboard is available at: http://127.0.0.1:{port}/")
    serve(app, host=host, port=port)
# ---------- End Functions ---------- #

OS = platform.system()
# Get the current working
# directory (CWD)
try:
    this_file = __file__
except NameError:
    this_file = sys.argv[0]
this_file = os.path.abspath(this_file)
if getattr(sys, 'frozen', False):
    cwd = os.path.dirname(sys.executable)
    #copy devices.json to cwd
    devicesFile = os.path.join(cwd, "devices.json")
    if not os.path.isfile(devicesFile):
        devicesFileCopy = os.path.join(bundle_dir, "devices.json")
        if os.path.isfile(devicesFileCopy):
            #copy devices.json to cwd
            #print("Copying devices.json to cwd")
            with open(devicesFileCopy, "r") as f:
                data = f.read()
            with open(devicesFile, "w") as f:
                f.write(data)
else:
    cwd = os.path.dirname(this_file)
    
print("Current working directory:", cwd)
#index file
db_file = os.path.join(cwd, 'tuya.db')
settingsFile = os.path.join(cwd, "appConfig.json")

#templateFolder = os.path.join(cwd, "templates")

#Create logging system anda save to log file
# Create a logger
logging.basicConfig(
    filename=os.path.join(cwd, 'app.log'),
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Initialize DB and migrate if necessary
init_db()
migrate_json_to_db()

# Load configuration from the database
load_config_from_db()

#check if update is available
if config.get("autoUpdate", False):
    check_update(config["autoUpdateURL"])

# Initial scan for devices on startup
#scan_and_save_new_devices()
# Reload devices from DB after scan
load_config_from_db()

# Initialize and start the MQTT client
init_mqtt_client()

#print(devices)
scheduler = BackgroundScheduler(timezone="UTC")
updateApScheduler()
scheduler.add_job(check_offline_devices, 'interval', seconds=10)
# Run the scheduler in a separate thread
Thread(target=start_scheduler).start()
print("Scheduler Started")

if __name__ == '__main__':
    if OS == 'Linux':
        os.environ.setdefault('PYSTRAY_BACKEND', 'appindicator')

    # Run the web server in a separate thread
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    # Run the tray icon in the main thread (pystray must be in the main thread on macOS)
    run_tray_icon()
