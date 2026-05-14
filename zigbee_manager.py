import asyncio
import threading
import logging
import json
from datetime import datetime
from zigpy.application import ControllerApplication
import zigpy.types as t
from zigpy.zcl import clusters
import serial.tools.list_ports
import os
import platform

# Import radio libraries
try:
    import zigpy_znp.zigbee.application as znp
except ImportError:
    znp = None

try:
    import bellows.zigbee.application as ezsp
except ImportError:
    ezsp = None

try:
    import zigpy_deconz.zigbee.application as deconz
except ImportError:
    deconz = None

try:
    import zigpy_xbee.zigbee.application as xbee
except ImportError:
    xbee = None

try:
    import zigpy_zigate.zigbee.application as zigate
except ImportError:
    zigate = None

class ZigbeeManager:
    def __init__(self, config, state_callback=None):
        self.config = config
        self.state_callback = state_callback
        self.app = None
        self.loop = None
        self.thread = None
        self.running = False
        self.logger = logging.getLogger("ZigbeeManager")

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._init_zigbee())
        self.loop.run_forever()

    async def _init_zigbee(self):
        radio_type = self.config.get("zigbee_radio_type", "znp").lower()
        port = self.config.get("zigbee_port", "auto")
        
        if port.lower() == "auto" or not port:
            self.logger.info("Zigbee port set to 'auto', attempting detection...")
            detected_port = self.detect_port()
            if detected_port:
                port = detected_port
                self.logger.info(f"Auto-detected Zigbee port: {port}")
            else:
                self.logger.error("Could not auto-detect Zigbee dongle. Please specify port in settings.")
                self.running = False
                return

        db_path = self.config.get("zigbee_database", "zigbee.db")

        app_config = {
            "database_path": db_path,
            "device": {
                "path": port,
            }
        }

        self.logger.info(f"Initializing Zigbee with radio {radio_type} on {port}")

        try:
            if radio_type == "znp" and znp:
                self.app = await znp.ControllerApplication.new(app_config)
            elif radio_type == "ezsp" and ezsp:
                self.app = await ezsp.ControllerApplication.new(app_config)
            elif radio_type == "deconz" and deconz:
                self.app = await deconz.ControllerApplication.new(app_config)
            elif radio_type == "xbee" and xbee:
                self.app = await xbee.ControllerApplication.new(app_config)
            elif radio_type == "zigate" and zigate:
                self.app = await zigate.ControllerApplication.new(app_config)
            else:
                raise ValueError(f"Unsupported or missing radio library for: {radio_type}")

            await self.app.startup(auto_form=True)
            self.app.add_listener(self)
            self.logger.info("Zigbee stack started successfully")
            
            # Initial device scan
            for device in self.app.devices.values():
                self._handle_device_update(device)

        except Exception as e:
            self.logger.error(f"Failed to start Zigbee stack: {e}", exc_info=True)
            self.running = False

    def _handle_device_update(self, device):
        if not self.state_callback:
            return

        # Simplify device info for the app
        # Most plugs use OnOff cluster (0x0006)
        state = "unknown"
        ieee = str(device.ieee)
        model = device.model
        
        # Try to find the OnOff cluster in any endpoint
        for endpoint in device.endpoints.values():
            if 0x0006 in endpoint.in_clusters:
                # We don't have the current state yet, but we've found a switch
                # In a real app, we'd query it or wait for a report
                pass
        
        self.state_callback(ieee, model, device)

    # Zigbee application listeners
    def device_initialized(self, device):
        self.logger.info(f"Device initialized: {device.ieee} ({device.model})")
        self._handle_device_update(device)

    def device_joined(self, device):
        self.logger.info(f"Device joined: {device.ieee}")

    def attribute_updated(self, device, cluster, attrid, value):
        self.logger.debug(f"Attribute updated: {device.ieee} cluster {cluster} attr {attrid} value {value}")
        # 0x0006 is OnOff, 0x0000 is Attribute ID for OnOff
        if cluster == 6 and attrid == 0:
            new_state = bool(value)
            self.logger.info(f"Device {device.ieee} state changed to {new_state}")
            if self.state_callback:
                self.state_callback(str(device.ieee), device.model, device, state=new_state)

    # Command methods
    def set_state(self, ieee_str, state):
        if not self.app or not self.running:
            return False
        
        asyncio.run_coroutine_threadsafe(self._async_set_state(ieee_str, state), self.loop)
        return True

    async def _async_set_state(self, ieee_str, state):
        try:
            ieee = t.EUI64.convert(ieee_str)
            device = self.app.get_device(ieee)
            
            # Find OnOff cluster
            for endpoint in device.endpoints.values():
                if 0x0006 in endpoint.in_clusters:
                    cluster = endpoint.in_clusters[0x0006]
                    if state:
                        await cluster.on()
                    else:
                        await cluster.off()
                    self.logger.info(f"Successfully turned {'on' if state else 'off'} device {ieee_str}")
                    return
            self.logger.warning(f"Device {ieee_str} does not support OnOff cluster")
        except Exception as e:
            self.logger.error(f"Error setting state for {ieee_str}: {e}")

    def permit_joins(self, duration=60):
        if not self.app or not self.running:
            return False
        asyncio.run_coroutine_threadsafe(self.app.permit(duration), self.loop)
        return True

    def get_devices(self):
        if not self.app:
            return {}
        return self.app.devices

    @staticmethod
    def detect_port():
        """
        Attempts to detect a Zigbee dongle on serial ports.
        Focuses on Linux paths but also uses cross-platform pyserial detection.
        """
        # 1. Try /dev/serial/by-id/ (Linux specific, very reliable)
        if platform.system() == "Linux":
            by_id_path = "/dev/serial/by-id/"
            if os.path.exists(by_id_path):
                try:
                    devices = os.listdir(by_id_path)
                    for dev in devices:
                        # Common keywords in Zigbee dongle names
                        if any(keyword.lower() in dev.lower() for keyword in ["Sonoff_Zigbee", "Zigbee", "ConBee", "Sonoff", "CP210x", "znp", "ezsp"]):
                            full_path = os.path.join(by_id_path, dev)
                            return full_path
                except Exception:
                    pass

        # 2. Try pyserial list_ports (Cross-platform)
        try:
            ports = serial.tools.list_ports.comports()
            for port in ports:
                # Check description and hwid
                desc = port.description.lower()
                hwid = port.hwid.lower()
                
                # Keywords to look for
                keywords = ["sonoff_zigbee", "sonoff", "zigbee", "conbee", "cp210", "ch340", "ti usb", "skyconnect"]
                if any(k in desc for k in keywords):
                    return port.device
                
                # Common VIDs:PIDs for Zigbee dongles
                # 10c4:ea60 - CP210x (Sonoff, SkyConnect, etc.)
                # 1a86:7523 - CH34x (Older dongles)
                # 0403:6001 - FTDI (Some others)
                vids_pids = ["10c4:ea60", "1a86:7523", "0403:6001"]
                if any(k in hwid for k in vids_pids):
                    return port.device
        except Exception:
            pass

        return None
