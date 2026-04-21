
"""
MQTT Module for Hardware Communication
Handles communication with parking sensors, gates, and IoT devices.
"""

import os
import json
import logging
import threading
import asyncio
from decimal import Decimal
from datetime import datetime
from typing import Callable, Dict, Any, List, Optional

import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
load_dotenv()

def _json_default(value: Any):
    """Serialize common Oracle/Python values for MQTT JSON payloads."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _to_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default)                           

class ParkingMQTTClient:
    
    def __init__(
        self, 
        broker_host: str = "localhost",
        broker_port: int = 1883,
        client_id: str = "parking_system_service",
    ):
        self.broker_host = broker_host
        self.broker_port = broker_port
        
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
            )
        else:
            self.client = mqtt.Client(client_id=client_id)
        
        self._lock = threading.Lock()
        
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        
        self.message_handlers: Dict[str, Callable] = {}
        self.connected = False
        
        self.TOPICS = {
            "spot_status": "parking/spots/status",
            "alerts": "parking/system/alerts",
            "commands": "parking/spots/cmd"
        }
    
        self.latest_spot_status: Dict[str, Dict[str, Any]] = {}

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            self.connected = True
            logger.info(f"Connected to MQTT Broker: {self.broker_host}")
            # Resubscribe on reconnect
            for topic_pattern in self.TOPICS.values():
                self.client.subscribe(topic_pattern)
        else:
            logger.error(f"Connection failed: {reason_code}")

    def _on_disconnect(self, client, userdata, disconnect_flags_or_rc, reason_code=None, properties=None):
        self.connected = False
        disconnect_reason = reason_code if reason_code is not None else disconnect_flags_or_rc
        logger.warning(f"Disconnected from broker (Reason: {disconnect_reason})")

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode('utf-8')
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"raw_payload": payload}

            # 1. Internal Logic Handlers
            if msg.topic == self.TOPICS["spot_status"]:
                self._handle_spot_status(data)
            
            # 2. Dynamic Registered Handlers
            for pattern, handler in self.message_handlers.items():
                if mqtt.topic_matches_sub(pattern, msg.topic):
                    handler(msg.topic, data)
                    
        except Exception as e:
            logger.error(f"Error processing message on {msg.topic}: {e}", exc_info=True)

    def _handle_spot_status(self, data: Dict[str, Any]):
        spot_id = data.get("spot_id")
        if not spot_id:
            return

        is_occupied = self._to_bool(data.get("status"))
        
        with self._lock:
            self.latest_spot_status[str(spot_id)] = {
                "occupied": is_occupied,
                "last_updated": datetime.utcnow().isoformat(),
                "battery": data.get("battery_level")
            }
        logger.debug(f"Spot {spot_id} updated: {'Occupied' if is_occupied else 'Free'}")

    def connect(self):
        try:
            self.client.connect_async(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start() 
        except Exception as e:
            logger.error(f"Critical MQTT connection error: {e}")

    def publish(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False):
        if not self.connected:
            logger.warning(f"Attempted to publish to {topic} while disconnected.")
            return False
        
        try:
            result = self.client.publish(topic, _to_json(payload), qos=qos, retain=retain)
            return result.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception as e:
            logger.error(f"Publishing error: {e}")
            return False

    def publish_single(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False):
        try:
            json_payload = _to_json(payload)
            publish.single(
                topic,
                payload=json_payload,
                qos=qos,
                retain=retain,
                hostname=self.broker_host,
                port=self.broker_port
            )
            logger.debug(f"Published to {topic} via single publish")
            return True
        except Exception as e:
            logger.error(f"Single publish error: {e}")
            return False

    def publish_single(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False):
        try:
            json_payload = _to_json(payload)
            publish.single(
                topic,
                payload=json_payload,
                qos=qos,
                retain=retain,
                hostname=self.broker_host,
                port=self.broker_port
            )
            logger.debug(f"Published to {topic} via single publish")
            return True
        except Exception as e:
            logger.error(f"Single publish error: {e}")
            return False

    def _to_bool(self, value: Any) -> bool:
        if isinstance(value, bool): return value
        if isinstance(value, (int, float)): return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "occupied", "busy", "on"}
        return False

    def get_spot_info(self, spot_id: str) -> Optional[Dict]:
        with self._lock:
            return self.latest_spot_status.get(spot_id)

    async def async_publish(self, topic: str, payload: Dict[str, Any], qos: int = 1, retain: bool = False) -> bool:
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self.publish(topic, payload, qos, retain)
            )
            return result
        except Exception as e:
            logger.error(f"Async publish error on {topic}: {e}")
            return False

    async def async_publish_single(self, topic: str, payload: List) -> bool:
        ch= "?"
        try:
            print
            loop = asyncio.get_event_loop()
            ch += ",".join(map(str, payload)) + ","
            await loop.run_in_executor(
                None,
                lambda: publish.single(
                    topic,
                    payload=ch,
                    hostname=self.broker_host,
                    port=self.broker_port
                )
            )
            logger.debug(f"Published to {topic} via async single publish")
            return True
        except Exception as e:
            logger.error(f"Async single publish error on {topic}: {e}")
            return False
    async def async_publish_single2(self, topic: str, payload: Dict[str, Any]) -> bool:
        try:
        
            loop = asyncio.get_event_loop()

            payload1 =_to_json(payload)
            print(f"Async publishing to {topic} with payload: {payload1}")

            await loop.run_in_executor(
                None,
                lambda: publish.single(
                    topic,
                    payload=payload1,
                    hostname=self.broker_host,
                    port=self.broker_port
                )
            )
            logger.debug(f"Published to {topic} via async single publish")
            return True
        except Exception as e:
            logger.error(f"Async single publish error on {topic}: {e}")
            return False

    async def wait_for_connection(self, timeout: int = 10) -> bool:
        start_time = datetime.utcnow()
        while not self.connected:
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            if elapsed > timeout:
                logger.error(f"MQTT connection timeout after {timeout} seconds")
                return False
            await asyncio.sleep(0.1)
        logger.info("MQTT connection established")
        return True

_mqtt_instance = None

def get_mqtt_singleton() -> ParkingMQTTClient:
    """Get or create the MQTT singleton instance"""
    global _mqtt_instance
    if _mqtt_instance is None:
        _mqtt_instance = ParkingMQTTClient(
            broker_host=os.getenv("MQTT_BROKER_HOST", "localhost"),
            broker_port=int(os.getenv("MQTT_BROKER_PORT", 1883)),
            client_id=os.getenv("MQTT_CLIENT_ID", "parking_system_main")
        )
        _mqtt_instance.connect()
    return _mqtt_instance


def publish_single_message(
    topic: str,
    payload: Dict[str, Any],
    hostname: Optional[str] = None,
    port: Optional[int] = None
) -> bool:
    """
    Convenience function for simple one-off MQTT publishes.

    Args:
        topic: MQTT topic to publish to
        payload: Data to send (will be JSON-encoded)
        qos: Quality of Service level (0, 1, or 2)
        retain: Whether to retain the message on the broker
        hostname: MQTT broker host (uses env var if not provided)
        port: MQTT broker port (uses env var if not provided)

    Returns:
        True if publish succeeded, False otherwise
    """
    try:
        if hostname is None:
            hostname = os.getenv("MQTT_BROKER_HOST", "localhost")
        if port is None:
            port = int(os.getenv("MQTT_BROKER_PORT", 1883))
        print(f"Publishing to {hostname}:{port} on topic '{topic}' with payload: {payload}")
        publish.single(
            topic,
            payload=_to_json(payload),
            hostname=hostname,
            port=port
        )
        return True
    except Exception as e:
        logger.error(f"Single publish error on {topic}: {e}")
        return False