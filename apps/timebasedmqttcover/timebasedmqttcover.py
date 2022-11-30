"""
Time Based MQTT Covers
This add support for position and also reversing the motor.

Args:
covers:            M: List of covers to publish
 - parent:         M: Entity_id of existing "dumb" cover
   frendly_name:   M: A friendly name for this cover
   unique_id:      O: An optional id to use for the MQTT topics
   time_to_close:  M: How many seconds it takes to close the cover
   time_to_open:   M: How many seconds it takes to open the cover
   reaction_time:  O: How long time it takes for the device to react
DEBUG:             O: yes | no (activate debug logging)

When having a cover/curtain from the top of a window:

time_to_open is the total time for the curtain to
roll all the way up to uncover the window

time_to_close is the total time for the curtain to
roll all the way down to cover the window
"""

import appdaemon.plugins.hass.hassapi as hass
import appdaemon.plugins.mqtt.mqttapi as mqtt
import json
import re
import time

VERSION = 0.76
MANUFACTURER = "Valitron AB"
MODEL = "Time Based MQTT Cover"

# CLOSED = Curtain covering window (going down)
# OPEN = Curtain rolled in (fully up) (going up)
OPEN_NUMBER = 10000
CLOSED_NUMBER = 0

PUBLISH_MOVING_POSITION_EVERY = 1

# When fully opening or closing a cover, add this much time
EXTRA_TIME_FOR_FULL_RUN = 1.5

# Store all attributes every day to disk
STORE_COVER_STATE_EVERY = 60 * 60 * 24

MAX_UUID_TRIES = 100
UUID_PREFIX = "tbmqttc_"

TOPIC_PREFIX = "tbmqttcover/"

# Main cover attributes
COVER_DATA = {
    "command_topic": "~set_cover",
    "position_topic": "~position",
    "json_attributes_topic": "~attributes",
    "set_position_topic": "~set_cover_position",
    "position_closed": CLOSED_NUMBER,
    "position_open": OPEN_NUMBER,
}

# Invert switch attributes
INVERT_DATA = {
    "command_topic": "~set_invert",
    "state_topic": "~invert_state",
}

# Invert switch attributes
MOTOR_STATUS_DATA = {
    "state_topic": "~motor_on_off_state",
}


class TimeBasedMQTTCover(mqtt.Mqtt, hass.Hass):
    def initialize(self):
        # initialize variables
        self.covers = {}
        self.publish_posision_timer = None

        # These are read from file to get last state
        stored_covers = {}

        self.hass = self.get_plugin_api("HASS")

        # This is the file where we store current states
        # between restarts for this app
        self.persistance_file = (
            __file__.rsplit("/", 1)[0] + f"/{self.name}.json"
        )

        self.debug(f"DB FILE: {self.persistance_file}")
        self.load_persistance_file()

        self.set_namespace("mqtt")

        self.listen_event(self.handle_mqtt_message, "MQTT_MESSAGE")

        self.debug("Starting to register all covers")
        # Parse and register all configured covers
        self.parse_and_register()

        self.debug("Will publish current positions...")

        # Publish current initial state
        self.schedule_publish_position(0)

        # Save the current state of all covers every STORE_COVER_STATE_EVERY
        # seconds

        self.hass.run_every(
            callback=self.save_persistance_file,
            start=f"now+{STORE_COVER_STATE_EVERY}",
            interval=STORE_COVER_STATE_EVERY,
        )

    def terminate(self):
        try:
            for entity_id, cover in self.covers.items():
                topic = cover["topic_subscription"]
                self.debug(f"Unsubscribing to topic {topic}")
                self.call_service("mqtt/unsubscribe", topic=topic)

        except Exception as e:
            self.error(f"Unexpected Exception when unsubscribing {e}")

        self.save_persistance_file()
        self.debug("Finished clean up process, bye bye...")

    def debug(self, text):
        if "DEBUG" in self.args and self.args["DEBUG"]:
            self.log(f"DEBUG: {text}")

    def load_persistance_file(self):
        try:
            with open(self.persistance_file, "r") as f:
                self.stored_covers = json.load(f)

        except Exception as e:
            self.error(f"Exception when loading persistance file: {e}")

    def save_persistance_file(self, kwargs=None):
        try:
            with open(self.persistance_file, "w") as f:
                f.write(json.dumps(self.covers, indent=4))

            self.log(f"Persistance entries written to {self.persistance_file}")

        except Exception as e:
            self.error(f"Exception when storing persistance file: {e}")
            return False

    def remove_timer(self, th):
        """Wrapper to cancel_timer with sanity checks

        Parameters
        ----------
        th : str
            Timer Handle from run_in

        Returms boolean
        """
        if th is not None and self.hass.timer_running(th):
            self.hass.cancel_timer(th)
            self.debug(f"Cancelled the timer with handle: {th}")
            return True
        else:
            return False

    def schedule_publish_position(self, delay):
        self.debug(f"=== Scheduling publishing of positions in {delay}s")

        # Just make it easier to read below
        th = self.publish_posision_timer

        # Only schedule if its not already scheduled by another cover
        if th is None or not self.hass.timer_running(th):
            self.publish_posision_timer = self.hass.run_in(
                callback=self.publish_position_worker, delay=delay
            )

    def publish_position_worker(self, kwargs):
        self.debug("=== Trying to find moving covers...")

        # The number of covers we find that are not stopped
        moving_covers = 0
        for device_id, cover in self.covers.items():
            if cover["status"] != "stopped":
                moving_covers += 1
                self.calculate_position(cover)
                if cover["moving_position"] != -1:
                    self.publish_position(
                        cover, cover["moving_position"]
                    )

        self.debug(f"=== Number of moving covers: {moving_covers}")

        # If we found covers moving, run again in
        # PUBLISH_MOVING_POSITION_EVERY seconds
        if moving_covers > 0:
            self.schedule_publish_position(PUBLISH_MOVING_POSITION_EVERY)

        self.debug("Done with the publish handling for now...")

    def get_cover_from_parent_id(self, parent_id):
        for cover in self.covers:
            if "parent" in cover and cover["parent"] == parent_id:
                return cover

        return None

    def get_unique_device_id(self, cover):
        """Gets a unique id based on the friendly_name inside cover config

        Parameters
        ----------
        cover_config : dict
            The complete dict of a cover config from apps.yaml

        Returns
        -------
        str
            A string containing the unique id
        """
        suffix = 1

        # Use suggested unique_id from config instead of friendly_name
        if "unique_id" in cover:
            base_id = cover["unique_id"]
        else:
            base_id = re.sub("[^a-z0-9]+", "_", cover["friendly_name"].lower())

        # Loop until we find a unique id or we run out of tries
        for i in range(MAX_UUID_TRIES):
            device_id = base_id if suffix <= 1 else f"{base_id}_{suffix}"
            if device_id in self.covers:
                suffix += 1
                continue
            else:
                break

        self.debug(f"Calculated an unique ID: {device_id}")
        return device_id

    def get_last(self, device_id, attribute, default):
        """Get last stored state on file

        Parameters
        ----------
        device_id : str
            The cover id to get data from
        attribute : atr
            The attribute to get
        default : str|int|float
            The default value to return if attribute not found

        Returns
        str|int|float
            The last state or the default value
        """
        if device_id in self.stored_covers:
            cover = self.stored_covers[device_id]
            if attribute in cover:
                return cover[attribute]

        return default

    def parse_and_register(self):
        for cover in self.args["covers"]:
            if "friendly_name" not in cover:
                self.error(f"Missing friendly name...")
                continue

            if "parent" not in cover:
                self.error(f"Missing parent...")
                continue

            self.log(f"Adding Cover {cover['friendly_name']}")
            parent_id = cover["parent"]

            if self.get_cover_from_parent_id(parent_id) is not None:
                log.error(f"Parent {parent_id} duplicated, skipping")
                continue

            device_id = self.get_unique_device_id(cover)
            topic_base = f"{TOPIC_PREFIX}{device_id}/"

            base_config = {
                "~": topic_base,
                "device": {
                    "identifiers": [f"{UUID_PREFIX}{device_id}"],
                    "name": cover["friendly_name"],
                    "sw_version": str(VERSION),
                    "manufacturer": MANUFACTURER,
                    "model": MODEL,
                },
            }

            # The cover
            #
            config = dict(base_config)
            config["name"] = cover["friendly_name"]
            config["unique_id"] = f"{UUID_PREFIX}{device_id}_cover"
            config.update(COVER_DATA)

            self.call_service(
                "mqtt/publish",
                topic=f"homeassistant/cover/{device_id}_cover/config",
                payload=json.dumps(config),
                retain=False,
            )

            self.debug(f"Published config for COVER: {device_id}")

            # The invert switch
            #
            config = dict(base_config)
            config["name"] = f"Invert Direction of Motor"
            config["icon"] = "mdi:directions"
            config["unique_id"] = f"{UUID_PREFIX}{device_id}_invert"
            config.update(INVERT_DATA)

            self.call_service(
                "mqtt/publish",
                topic=f"homeassistant/switch/{device_id}_inversed/config",
                payload=json.dumps(config),
                retain=False,
            )

            self.debug(f"Published config for INVERT Switch: {device_id}")

            # The is motor running sensor
            #
            config = dict(base_config)
            config["name"] = f"{cover['friendly_name']} Motor On/Off"
            config["device_class"] = "moving"
            config["unique_id"] = f"{UUID_PREFIX}{device_id}_motor_on_off"
            config.update(MOTOR_STATUS_DATA)

            self.call_service(
                "mqtt/publish",
                topic=f"homeassistant/binary_sensor/{device_id}_motor_on_off/config",
                payload=json.dumps(config),
                retain=False,
            )

            self.debug(
                f"Published config for Motor On/Off Sensor: {device_id}"
            )

            self.covers[device_id] = {
                "id": device_id,
                "topic_base": topic_base,
                "topic_subscription": f"{topic_base}#",
                "parent_id": parent_id,
                "friendly_name": cover["friendly_name"],
                "position": self.get_last(device_id, "position", 0),
                "moving_position": self.get_last(
                    device_id, "moving_position", -1
                ),
                "last_motor_start": self.get_last(
                    device_id, "last_motor_start", 0
                ),
                "last_motor_stop": self.get_last(
                    device_id, "last_motor_stop", 0
                ),
                "run_motor_for": self.get_last(
                    device_id, "run_motor_for", 0
                ),
                "motor_ran_for": self.get_last(
                    device_id, "motor_ran_for", 0
                ),
                "direction": self.get_last(device_id, "direction", 0),
                "status": self.get_last(device_id, "status", "stopped"),
                "invert": self.get_last(device_id, "invert", False),
                "time_to_open": cover["time_to_open"],
                "time_to_close": cover["time_to_close"],
                "reaction_time": cover["reaction_time"]
                if "reaction_time" in cover
                else 0,
                "movement_timer_id": -1,
            }

            self.publish_status(self.covers[device_id])
            self.publish_position(self.covers[device_id])

            self.call_service("mqtt/subscribe", topic=f"{topic_base}#")
            self.debug(f"Subscribed to: {topic_base}#")

    def publish_status(self, cover):
        # attributes
        self.call_service(
            "mqtt/publish",
            topic=f"{cover['topic_base']}attributes",
            payload=json.dumps(
                {
                    # "hubba_bubba": cover["position"],
                    "status": cover["status"],
                    "parent": cover["parent_id"],
                    "last_motor_start": cover["last_motor_start"],
                    "last_motor_stop": cover["last_motor_stop"],
                    "run_motor_for": cover["run_motor_for"],
                    "motor_ran_for": cover["motor_ran_for"],
                    "direction": cover["direction"],
                    "time_to_open": cover["time_to_open"],
                    "time_to_close": cover["time_to_close"],
                    "invert": "Yes" if cover["invert"] else "No",
                }
            ),
            retain=False,
        )

        # state for switch invert
        self.call_service(
            "mqtt/publish",
            topic=f"{cover['topic_base']}invert_state",
            payload="ON" if cover["invert"] else "OFF",
            retain=False,
        )

        # state for sensor motor running
        self.call_service(
            "mqtt/publish",
            topic=f"{cover['topic_base']}motor_on_off_state",
            payload="OFF" if cover["status"] == "stopped" else "ON",
            retain=False,
        )

    def publish_position(self, cover, position=None):
        if position is None:
            position = cover["position"]

        self.debug(
            f"Publishing position {position} for {cover['friendly_name']}"
        )

        # position
        self.call_service(
            "mqtt/publish",
            topic=f"{cover['topic_base']}position",
            payload=position,
            retain=False,
        )

    def handle_mqtt_message(self, callback, event, kwargs):
        TOPIC_HANDLERS = {
            "~set_invert": TimeBasedMQTTCover.handle_set_invert,
            "~set_cover_position": TimeBasedMQTTCover.handle_set_position,
            "~set_cover": TimeBasedMQTTCover.handle_set_cover,
        }

        full_topic = event["topic"]
        if not full_topic.startswith(TOPIC_PREFIX):
            return

        for cid in self.covers:
            prefix = f"{TOPIC_PREFIX}{cid}/"

            if full_topic.startswith(prefix):
                topic = full_topic.replace(prefix, "~")
                if topic in TOPIC_HANDLERS:
                    if cid in self.covers:
                        cover = self.covers[cid]
                        TOPIC_HANDLERS[topic](self, event, cover)
                    else:
                        self.error(f"Cover: {cid} not found for {topic}")

                break

    def handle_set_cover(self, event, cover):
        self.debug(f"handle_set_cover: {cover['friendly_name']}")

        if event["payload"] == "STOP":
            self.stop_cover(cover)
        elif event["payload"] == "OPEN":
            self.set_position(cover, OPEN_NUMBER)
        else:
            self.set_position(cover, CLOSED_NUMBER)

    def handle_set_invert(self, event, cover):
        payload = event["payload"]

        cover["invert"] = True if payload == "ON" else False

        self.publish_status(cover)
        self.debug(
            f"handle_set_invert: updated cover {cover['friendly_name']}"
        )

    def handle_set_position(self, event, cover):
        self.debug(f"INSIDE handle_set_position {event}")
        self.set_position(cover, int(event["payload"]))

    def set_position(self, cover, position):
        self.debug(f"INSIDE_set_position {cover['friendly_name']} {position}")

        if cover["status"] != "stopped":
            self.stop_cover(cover)

        if position == OPEN_NUMBER:
            self.start_open(cover)
            time_to_run = cover["time_to_open"] + EXTRA_TIME_FOR_FULL_RUN
        elif position == CLOSED_NUMBER:
            self.start_close(cover)
            time_to_run = cover["time_to_close"] + EXTRA_TIME_FOR_FULL_RUN
        else:
            if position < cover["position"]:
                delta = cover["position"] - position
                time_to_run = cover["time_to_close"] / OPEN_NUMBER * delta
                self.debug(f"Time to run for closing: {time_to_run}")
                self.start_close(cover)
            else:
                delta = position - cover["position"]
                time_to_run = cover["time_to_open"] / OPEN_NUMBER * delta
                self.debug(f"Time to run for opening: {time_to_run}")
                self.start_open(cover)

        self.cancel_cover_timer(cover)
        cover["movement_timer_id"] = self.hass.run_in(
            callback=self.stop_cover_callback, delay=time_to_run, **cover
        )

        cover["run_motor_for"] = time_to_run

    def calculate_position(self, cover):
        if cover["status"] == "stopped":
            run_time = cover["last_motor_stop"] - cover["last_motor_start"]
            cover["motor_ran_for"] = run_time
        else:
            run_time = time.time() - cover["last_motor_start"]
            cover["motor_ran_for"] = 0
            if (
                run_time > cover["time_to_open"] + EXTRA_TIME_FOR_FULL_RUN
                and run_time > cover["time_to_close"] + EXTRA_TIME_FOR_FULL_RUN
            ):
                self.log(
                    "Maximum run time exceeded, stopping cover "
                    f"{cover['friendly_name']}"
                )

                cover["last_motor_stop"] = time.time()
                cover["status"] = "stopped"

        self.debug(f"STATUS {cover['friendly_name']} {cover['status']}")

        time_to_destination = (
            cover["time_to_open"]
            if cover["direction"] == "opening"
            else cover["time_to_close"]
        )

        distance = run_time / time_to_destination * OPEN_NUMBER
        if cover["direction"] == "opening":
            position = cover["position"] + round(distance)
        else:
            position = cover["position"] - round(distance)

        if position > OPEN_NUMBER:
            position = OPEN_NUMBER
        elif position < CLOSED_NUMBER:
            position = CLOSED_NUMBER

        if cover["status"] == "stopped":
            cover["position"] = position
            cover["moving_position"] = -1
        else:
            cover["moving_position"] = position

        self.debug(f"Motor run time: {run_time} seconds")
        self.debug(f"Distance travelled {distance}")
        self.debug(f"New position {position}")

    def stop_cover_callback(self, cover_copy):
        try:
            self.stop_cover(self.covers[cover_copy["id"]])
        except Exception as e:
            self.error(f"Something went wrong when stopping cover {e}")

    def cancel_cover_timer(self, cover):
        if self.remove_timer(cover["movement_timer_id"]):
            self.debug(f"Successfully stopped timer {cover['friendly_name']}")

    def stop_cover(self, cover):
        self.cancel_cover_timer(cover)

        if cover["status"] == "stopped":
            self.log(f"Cover '{cover['friendly_name']}' was already stopped.")
        else:
            self.debug(f"Stopping {cover['friendly_name']}")

            cover["last_motor_stop"] = time.time()
            cover["status"] = "stopped"

            self.call_service(
                "cover/stop_cover",
                entity_id=cover["parent_id"],
                namespace="default",
            )

        self.calculate_position(cover)
        self.publish_position(cover)
        self.publish_status(cover)

    def start_close(self, cover):
        self.debug(f"--- Start CLOSE COVER {cover['friendly_name']}")
        cover["last_motor_start"] = time.time() + cover["reaction_time"]
        cover["status"] = "moving"
        cover["direction"] = "closing"

        if cover["invert"]:
            self.call_service(
                "cover/open_cover",
                entity_id=cover["parent_id"],
                namespace="default",
            )
        else:
            self.call_service(
                "cover/close_cover",
                entity_id=cover["parent_id"],
                namespace="default",
            )

        self.publish_status(cover)
        self.schedule_publish_position(0)

    def start_open(self, cover):
        self.debug(f"--- Start OPEN COVER {cover['friendly_name']}")
        cover["last_motor_start"] = time.time() + cover["reaction_time"]
        cover["status"] = "moving"
        cover["direction"] = "opening"

        if cover["invert"]:
            self.call_service(
                "cover/close_cover",
                entity_id=cover["parent_id"],
                namespace="default",
            )
        else:
            self.call_service(
                "cover/open_cover",
                entity_id=cover["parent_id"],
                namespace="default",
            )

        self.publish_status(cover)
        self.schedule_publish_position(0)
