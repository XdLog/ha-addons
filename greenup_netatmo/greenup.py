"""Green'Up Netatmo : lit la borne Legrand Green'Up Home (module NLEVSE) via l'API
Netatmo et publie les valeurs dans Mosquitto avec découverte MQTT Home Assistant.

Unités observées sur la borne (relevé du 22/11/2026) :
  current_charge_instant_consumption : mW  (707351 -> 7,07 kW)
  current_total_charge_consumption   : Wh, remis à zéro à chaque session
  current_charge_duration            : minutes (supposé)
  charging_station_status            : 0 = au repos, 3 = en charge (autres codes inconnus)
"""

import json
import logging
import os
import signal
import sys
import time

import paho.mqtt.client as mqtt
import requests

OPTIONS_FILE = "/data/options.json"
STATE_FILE = "/data/state.json"
API = "https://api.netatmo.com"
DISCOVERY_PREFIX = "homeassistant"
BASE_TOPIC = "greenup_netatmo"
STATUS_LABELS = {0: "Au repos", 3: "En charge"}

log = logging.getLogger("greenup")


# --------------------------------------------------------------------------- état
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Netatmo
class AuthError(Exception):
    pass


class Netatmo:
    def __init__(self, opts, state):
        self.client_id = opts["client_id"]
        self.client_secret = opts["client_secret"]
        self.state = state
        seed = opts["refresh_token"]
        # Netatmo fait tourner le refresh token : on garde le dernier reçu, sauf si
        # l'utilisateur a collé un nouveau token dans les options.
        if state.get("seed_refresh_token") != seed:
            state["seed_refresh_token"] = seed
            state["refresh_token"] = seed
            state.pop("access_token", None)
            state["expires_at"] = 0
            save_json(STATE_FILE, state)

    def refresh(self):
        log.info("Renouvellement du token Netatmo")
        r = requests.post(
            f"{API}/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.state["refresh_token"],
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise AuthError(f"Échec du renouvellement ({r.status_code}) : {r.text}")
        tok = r.json()
        self.state["access_token"] = tok["access_token"]
        self.state["refresh_token"] = tok.get("refresh_token", self.state["refresh_token"])
        self.state["expires_at"] = time.time() + int(tok.get("expires_in", 10800)) - 120
        save_json(STATE_FILE, self.state)

    def get(self, path, params):
        if not self.state.get("access_token") or time.time() >= self.state.get("expires_at", 0):
            self.refresh()
        for attempt in (1, 2):
            r = requests.get(
                f"{API}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.state['access_token']}"},
                timeout=20,
            )
            if r.status_code in (401, 403) and attempt == 1:
                self.refresh()
                continue
            r.raise_for_status()
            return r.json()
        raise AuthError("Accès refusé après renouvellement du token")

    def find_charger(self, home_id, module_id):
        data = self.get("/api/homesdata", {})
        for home in data["body"].get("homes", []):
            if home_id and home["id"] != home_id:
                continue
            for mod in home.get("modules", []):
                if (module_id and mod["id"] == module_id) or (not module_id and mod.get("type") == "NLEVSE"):
                    return home["id"], mod
        raise RuntimeError("Aucune borne NLEVSE trouvée dans homesdata (vérifiez les scopes du token)")


# --------------------------------------------------------------------------- MQTT
def mqtt_settings():
    token = os.environ.get("SUPERVISOR_TOKEN")
    r = requests.get(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["data"]


def connect_mqtt(avail_topic):
    cfg = mqtt_settings()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="greenup_netatmo")
    if cfg.get("username"):
        client.username_pw_set(cfg["username"], cfg.get("password"))
    client.will_set(avail_topic, "offline", retain=True)
    client.connect(cfg["host"], int(cfg.get("port", 1883)), keepalive=60)
    client.loop_start()
    return client


def publish_discovery(client, mod, state_topic, avail_topic):
    uid = mod["id"].replace(":", "").lower()
    device = {
        "identifiers": [f"greenup_{uid}"],
        "name": mod.get("name") or "Borne Green'Up",
        "manufacturer": "Legrand",
        "model": "Green'Up Home (NLEVSE)",
    }
    common = {"state_topic": state_topic, "availability_topic": avail_topic, "device": device}
    entities = {
        ("sensor", "power"): {
            "name": "Puissance",
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "value_template": "{{ value_json.power_w }}",
        },
        ("sensor", "session_energy"): {
            "name": "Énergie session",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "suggested_display_precision": 2,
            "value_template": "{{ value_json.session_kwh }}",
        },
        ("sensor", "total_energy"): {
            "name": "Énergie totale",
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
            "suggested_display_precision": 2,
            "value_template": "{{ value_json.total_kwh }}",
        },
        ("sensor", "duration"): {
            "name": "Durée de charge",
            "unit_of_measurement": "min",
            "device_class": "duration",
            "value_template": "{{ value_json.duration_min }}",
        },
        ("sensor", "status"): {
            "name": "Statut",
            "icon": "mdi:ev-station",
            "value_template": "{{ value_json.status_label }}",
            "json_attributes_topic": state_topic,
            "json_attributes_template": "{{ {'code': value_json.status, 'erreur': value_json.error} | tojson }}",
        },
        ("binary_sensor", "charging"): {
            "name": "En charge",
            "device_class": "battery_charging",
            "value_template": "{{ 'ON' if value_json.status == 3 else 'OFF' }}",
        },
        ("sensor", "error"): {
            "name": "Code erreur",
            "icon": "mdi:alert-circle-outline",
            "entity_category": "diagnostic",
            "value_template": "{{ value_json.error }}",
        },
    }
    for (component, key), cfg in entities.items():
        payload = dict(common, **cfg)
        payload["unique_id"] = f"greenup_{uid}_{key}"
        payload["object_id"] = f"greenup_{key}"
        client.publish(f"{DISCOVERY_PREFIX}/{component}/greenup_{uid}/{key}/config", json.dumps(payload), retain=True)


# --------------------------------------------------------------------------- boucle
def main():
    opts = load_json(OPTIONS_FILE, {})
    logging.basicConfig(
        level=logging.DEBUG if opts.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    for key in ("client_id", "client_secret", "refresh_token"):
        if not opts.get(key):
            log.error("Option manquante : %s", key)
            sys.exit(1)

    state = load_json(STATE_FILE, {})
    api = Netatmo(opts, state)
    home_id, mod = api.find_charger(opts.get("home_id") or "", opts.get("module_id") or "")
    log.info("Borne trouvée : %s (%s) dans le home %s", mod.get("name"), mod["id"], home_id)

    state_topic = f"{BASE_TOPIC}/{mod['id'].replace(':', '').lower()}/state"
    avail_topic = f"{BASE_TOPIC}/availability"
    client = connect_mqtt(avail_topic)
    publish_discovery(client, mod, state_topic, avail_topic)
    client.publish(avail_topic, "online", retain=True)

    def stop(*_):
        client.publish(avail_topic, "offline", retain=True)
        client.loop_stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)

    interval = int(opts.get("scan_interval", 60))
    while True:
        try:
            data = api.get("/api/homestatus", {"home_id": home_id})
            log.debug("homestatus brut : %s", json.dumps(data))
            m = next((x for x in data["body"]["home"].get("modules", []) if x["id"] == mod["id"]), None)
            if m is None:
                log.warning("Borne absente de homestatus")
                client.publish(avail_topic, "offline", retain=True)
            else:
                session_wh = int(m.get("current_total_charge_consumption", 0))
                # Compteur cumulé persistant : ajoute la progression de la session,
                # ou la session entière si elle a été remise à zéro.
                last = state.get("last_session_wh", 0)
                delta = session_wh - last if session_wh >= last else session_wh
                state["total_wh"] = state.get("total_wh", 0) + delta
                state["last_session_wh"] = session_wh
                save_json(STATE_FILE, state)

                status = int(m.get("charging_station_status", -1))
                payload = {
                    "power_w": round(int(m.get("current_charge_instant_consumption", 0)) / 1000),
                    "session_kwh": round(session_wh / 1000, 3),
                    "total_kwh": round(state["total_wh"] / 1000, 3),
                    "duration_min": int(m.get("current_charge_duration", 0)),
                    "status": status,
                    "status_label": STATUS_LABELS.get(status, f"Code {status}"),
                    "error": int(m.get("charging_station_error", 0)),
                }
                client.publish(state_topic, json.dumps(payload), retain=True)
                client.publish(avail_topic, "online", retain=True)
                log.debug("Publié : %s", payload)
        except AuthError as e:
            log.error("%s. Régénérez un refresh token et collez-le dans les options.", e)
            client.publish(avail_topic, "offline", retain=True)
            time.sleep(600)
            continue
        except Exception as e:  # réseau, API indisponible...
            log.warning("Erreur pendant la lecture : %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
