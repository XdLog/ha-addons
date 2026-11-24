"""Green'Up Netatmo : lit la borne Legrand Green'Up Home (module NLEVSE) via l'API
Netatmo et publie les valeurs dans Mosquitto avec découverte MQTT Home Assistant.

Unités observées sur la borne (relevé du 22/11/2026) :
  current_charge_instant_consumption : cW  (707351 -> 7073 W)
  current_total_charge_consumption   : Wh, remis à zéro à chaque session
  current_charge_duration            : minutes (supposé)
  charging_station_status            : 0 = au repos, 3 = en charge (autres codes inconnus)
"""

import json
import logging
import os
import re
import signal
import sys
import time
from urllib.parse import unquote

import paho.mqtt.client as mqtt
import requests

OPTIONS_FILE = "/data/options.json"
STATE_FILE = "/data/state.json"
COOKIES_FILE = "/data/cookies.json"
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
        code = opts.get("auth_code") or ""
        if code and state.get("used_auth_code") != code:
            self.exchange_code(code, opts.get("redirect_uri") or "https://localhost")
            state["used_auth_code"] = code
            state["seed_refresh_token"] = opts.get("refresh_token") or ""
            save_json(STATE_FILE, state)
            return
        if state.get("used_auth_code") and state.get("refresh_token"):
            return  # token issu d'un code d'autorisation : on le garde
        seed = opts["refresh_token"]
        # Netatmo fait tourner le refresh token : on garde le dernier reçu, sauf si
        # l'utilisateur a collé un nouveau token dans les options.
        if state.get("seed_refresh_token") != seed:
            state["seed_refresh_token"] = seed
            state["refresh_token"] = seed
            state.pop("access_token", None)
            state["expires_at"] = 0
            save_json(STATE_FILE, state)

    def exchange_code(self, code, redirect_uri):
        log.info("Échange du code d'autorisation Netatmo")
        r = requests.post(
            f"{API}/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "scope": "read_magellan write_magellan",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise AuthError(f"Échec de l'échange du code ({r.status_code}) : {r.text}")
        tok = r.json()
        log.info("Token obtenu, scopes : %s", tok.get("scope"))
        self.state["access_token"] = tok["access_token"]
        self.state["refresh_token"] = tok["refresh_token"]
        self.state["expires_at"] = time.time() + int(tok.get("expires_in", 10800)) - 120

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
        """Retourne (home_id, module). Sans filtre gateway_types, homesdata ne renvoie
        pas les modules Legrand : on le précise, puis on se rabat sur homestatus."""
        if home_id and module_id:
            log.info("home_id et module_id fournis : pas de recherche")
            return home_id, {"id": module_id, "type": "NLEVSE", "name": "Borne recharge"}
        data = self.get("/api/homesdata", {"gateway_types": ["NLEVSE", "NLG"]})
        log.debug("homesdata brut : %s", json.dumps(data))
        homes = [h for h in data["body"].get("homes", []) if not home_id or h["id"] == home_id]
        for home in homes:
            for mod in home.get("modules", []):
                if (module_id and mod["id"] == module_id) or (not module_id and mod.get("type") == "NLEVSE"):
                    return home["id"], mod
        for home in homes:
            status = self.get("/api/homestatus", {"home_id": home["id"]})
            log.debug("homestatus brut : %s", json.dumps(status))
            for mod in status["body"].get("home", {}).get("modules", []):
                if (module_id and mod["id"] == module_id) or (not module_id and mod.get("type") == "NLEVSE"):
                    return home["id"], dict(mod, name="Borne recharge")
        raise RuntimeError("Aucune borne NLEVSE trouvée (vérifiez les scopes du token, ou renseignez home_id et module_id)")


class NetatmoWeb(Netatmo):
    """Mode « compte » : se connecte comme le portail dev.netatmo.com avec l'e-mail et
    le mot de passe Netatmo, et récupère le cookie netatmocomaccess_token (~3 h).
    Non officiel : lecture seule, à utiliser avec une fréquence raisonnable."""

    LOGIN_PAGE = "https://auth.netatmo.com/fr-fr/access/login"
    LOGIN_POST = "https://auth.netatmo.com/access/postlogin"
    KEYCHAIN = "https://auth.netatmo.com/fr-fr/access/keychain"
    NEXT_URL = "https://dev.netatmo.com/"
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

    def __init__(self, opts, state):  # pylint: disable=super-init-not-called
        self.email = opts["netatmo_email"]
        self.password = opts["netatmo_password"]
        self.manual = opts.get("session_token") or ""
        self.state = state
        if self.manual and state.get("manual_token") != self.manual:
            state["manual_token"] = self.manual
            state["access_token"] = self.manual
            state["expires_at"] = time.time() + 3 * 3600 - 300
            save_json(STATE_FILE, state)

    def refresh(self):
        sess = requests.Session()
        sess.headers.update({"User-Agent": self.UA, "Accept-Language": "fr-FR,fr;q=0.9"})
        self._load_cookies(sess)
        old = self.state.get("access_token")
        # Session « rester connecté » (cookie authnetatmocomrefresh_token) : la page
        # keychain redélivre un token sans nouvelle connexion, donc sans e-mail d'alerte.
        if any(c.name == "authnetatmocomrefresh_token" for c in sess.cookies):
            for url in (self.KEYCHAIN, self.LOGIN_PAGE):
                r0 = sess.get(url, params={"next_url": self.NEXT_URL}, timeout=20)
                token = self._access_cookie(sess)
                log.debug("Renouvellement par session via %s : HTTP %s, URL finale %s, token %s",
                          url, r0.status_code, r0.url, "nouveau" if token and token != old else "absent")
                if token and token != old:
                    self._store(sess, token)
                    log.info("Token renouvelé via la session existante (sans reconnexion)")
                    return
        page = sess.get(self.LOGIN_PAGE, params={"next_url": self.NEXT_URL}, timeout=20)
        log.info("Connexion au compte Netatmo (mode web)")
        # La page récupère le jeton CSRF via /access/csrf puis soumet le formulaire.
        csrf = sess.get(
            "https://auth.netatmo.com/access/csrf",
            headers={"Referer": page.url, "X-Requested-With": "XMLHttpRequest"},
            timeout=20,
        )
        try:
            token_csrf = csrf.json()["token"]
        except (ValueError, KeyError):
            raise AuthError(f"Jeton CSRF introuvable (HTTP {csrf.status_code})")
        r = sess.post(
            self.LOGIN_POST,
            params={"next_url": self.NEXT_URL},
            data={
                "email": self.email,
                "password": self.password,
                "stay_logged": "on",
                "website": "",
                "_token": token_csrf,
            },
            headers={"Referer": page.url, "Origin": "https://auth.netatmo.com"},
            timeout=20,
            allow_redirects=True,
        )
        names = sorted({c.name for c in sess.cookies})
        log.debug("Connexion web : HTTP %s, URL finale %s, cookies %s", r.status_code, r.url, names)
        token = self._access_cookie(sess)
        if not token:
            raise AuthError(
                f"Connexion refusée (HTTP {r.status_code}, cookies reçus : {names}). "
                "Vérifiez e-mail et mot de passe ; une double authentification ou un captcha bloque ce mode"
            )
        self._store(sess, token)
        log.info("Connexion web réussie")

    @staticmethod
    def _access_cookie(sess):
        val = next((c.value for c in sess.cookies if c.name == "netatmocomaccess_token"), None)
        return unquote(val) if val else None

    def _store(self, sess, token):
        self.state["access_token"] = token
        self.state["expires_at"] = time.time() + 3 * 3600 - 300
        save_json(STATE_FILE, self.state)
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
             "expires": c.expires, "secure": c.secure}
            for c in sess.cookies if c.name != "netatmocomaccess_token"
        ]
        save_json(COOKIES_FILE, cookies)

    @staticmethod
    def _load_cookies(sess):
        for c in load_json(COOKIES_FILE, []):
            if c.get("expires") and c["expires"] < time.time():
                continue
            sess.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"],
                             expires=c.get("expires"), secure=c.get("secure", True))


# --------------------------------------------------------------------------- MQTT
def mqtt_settings(opts):
    """Options manuelles si mqtt_host est renseigné, sinon service MQTT du Supervisor."""
    if opts.get("mqtt_host"):
        log.info("MQTT : options manuelles (%s)", opts["mqtt_host"])
        return {
            "host": opts["mqtt_host"],
            "port": opts.get("mqtt_port") or 1883,
            "username": opts.get("mqtt_user") or "",
            "password": opts.get("mqtt_password") or "",
        }
    token = os.environ.get("SUPERVISOR_TOKEN")
    r = requests.get(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Service MQTT du Supervisor indisponible ({r.status_code} : {r.text}). "
            "Renseignez mqtt_host, mqtt_user et mqtt_password dans les options."
        )
    log.info("MQTT : service du Supervisor")
    return r.json()["data"]


def connect_mqtt(opts, avail_topic):
    cfg = mqtt_settings(opts)
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


# --------------------------------------------------------------------------- déclencheur HA
def ha_state(entity_id):
    """État d'une entité HA via l'API Core du Supervisor (homeassistant_api)."""
    r = requests.get(
        f"http://supervisor/core/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN')}"},
        timeout=10,
    )
    r.raise_for_status()
    return str(r.json().get("state", "")).lower()


def wait_or_trigger(seconds, entity_id, trigger_states, last):
    """Attend `seconds`, mais rend la main dès que l'entité du véhicule passe dans un
    état « en charge ». Retourne le dernier état lu."""
    if not entity_id:
        time.sleep(seconds)
        return last
    end = time.time() + seconds
    while True:
        try:
            cur = ha_state(entity_id)
        except Exception as e:  # HA indisponible : on ne bloque pas le relevé
            log.debug("Lecture de %s impossible : %s", entity_id, e)
            cur = last
        if cur in trigger_states and last not in trigger_states and last is not None:
            log.info("%s est passé à « %s » : relevé immédiat", entity_id, cur)
            return cur
        last = cur
        remaining = end - time.time()
        if remaining <= 0:
            return last
        time.sleep(min(15, remaining))


# --------------------------------------------------------------------------- boucle
def main():
    opts = load_json(OPTIONS_FILE, {})
    logging.basicConfig(
        level=logging.DEBUG if opts.get("debug") else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    web = bool(opts.get("netatmo_email"))
    if web:
        required = ("netatmo_email", "netatmo_password")
    elif opts.get("auth_code"):
        required = ("client_id", "client_secret")
    else:
        required = ("client_id", "client_secret", "refresh_token")
    for key in required:
        if not opts.get(key):
            log.error("Option manquante : %s", key)
            sys.exit(1)

    state = load_json(STATE_FILE, {})
    api = NetatmoWeb(opts, state) if web else Netatmo(opts, state)
    log.info("Mode d'authentification : %s", "compte Netatmo (web)" if web else "application OAuth")
    while True:
        try:
            home_id, mod = api.find_charger(opts.get("home_id") or "", opts.get("module_id") or "")
            break
        except Exception as e:
            log.error("%s. Nouvel essai dans 5 minutes.", e)
            time.sleep(300)
    log.info("Borne trouvée : %s (%s) dans le home %s", mod.get("name"), mod["id"], home_id)

    state_topic = f"{BASE_TOPIC}/{mod['id'].replace(':', '').lower()}/state"
    avail_topic = f"{BASE_TOPIC}/availability"
    while True:
        try:
            client = connect_mqtt(opts, avail_topic)
            break
        except Exception as e:
            log.error("Connexion MQTT impossible : %s. Nouvel essai dans 5 minutes.", e)
            time.sleep(300)
    publish_discovery(client, mod, state_topic, avail_topic)
    client.publish(avail_topic, "online", retain=True)

    def stop(*_):
        client.publish(avail_topic, "offline", retain=True)
        client.loop_stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)

    interval_charging = int(opts.get("scan_interval", 60))
    interval_idle = int(opts.get("scan_interval_idle", 300))
    interval = interval_idle
    auth_failures = 0
    trigger_entity = (opts.get("trigger_entity") or "").strip()
    trigger_states = {x.strip().lower() for x in (opts.get("trigger_states") or "on,charging").split(",") if x.strip()}
    if trigger_entity:
        log.info("Déclencheur : %s (états %s)", trigger_entity, ", ".join(sorted(trigger_states)))
    trigger_last = None
    while True:
        try:
            data = api.get("/api/homestatus", {"home_id": home_id})
            log.debug("homestatus brut : %s", json.dumps(data))
            m = next((x for x in data["body"]["home"].get("modules", []) if x["id"] == mod["id"]), None)
            if m is None:
                log.warning("Borne absente de homestatus (token sans accès à la borne ?)")
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
                    "power_w": round(int(m.get("current_charge_instant_consumption", 0)) / 100),
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
                # En charge : relevé rapproché ; au repos : relevé espacé.
                interval = interval_charging if status == 3 else interval_idle
            auth_failures = 0
        except AuthError as e:
            auth_failures += 1
            wait = min(600 * 2 ** (auth_failures - 1), 3600)
            log.error("%s. Nouvel essai dans %d min.", e, wait // 60)
            client.publish(avail_topic, "offline", retain=True)
            time.sleep(wait)
            continue
        except Exception as e:  # réseau, API indisponible...
            log.warning("Erreur pendant la lecture : %s", e)
            interval = interval_idle
        # Au repos, on surveille l'entité du véhicule pour basculer sans attendre.
        if interval == interval_idle:
            trigger_last = wait_or_trigger(interval, trigger_entity, trigger_states, trigger_last)
        else:
            time.sleep(interval)


if __name__ == "__main__":
    main()
