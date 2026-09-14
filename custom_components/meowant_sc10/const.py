"""Constants and per-device endpoint builders."""

DOMAIN = "meowant_sc10"

CONF_DEVICE_ID = "device_id"
CONF_ACCESS_ID = "access_id"
CONF_ACCESS_SECRET = "access_secret"
CONF_DATA_CENTER = "data_center"
CONF_MODE = "mode"
CONF_HOST = "host"
CONF_LOCAL_KEY = "local_key"
CONF_PROTOCOL_VERSION = "protocol_version"

MODE_CLOUD = "cloud"
MODE_LOCAL = "local"
MODES = [MODE_LOCAL, MODE_CLOUD]
DEFAULT_MODE = MODE_LOCAL

# Verified on the reference device; the config flow reads the real value from
# the cloud, so this is only a fallback.
DEFAULT_PROTOCOL_VERSION = "3.5"
PROTOCOL_VERSIONS = ["3.1", "3.2", "3.3", "3.4", "3.5"]

# Tuya serves each account from exactly one data center, fixed by the country
# the app account was registered in.
DATA_CENTERS = {
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}
DEFAULT_DATA_CENTER = "us"

TOKEN_PATH = "/v1.0/token?grant_type=1"
DEVICE_LIST_PATH = "/v2.0/cloud/thing/device?page_size=20"


def status_path(device_id: str) -> str:
    """Shadow properties: the only endpoint that returns dp_id values."""
    return f"/v2.0/cloud/thing/{device_id}/shadow/properties"


def command_path(device_id: str) -> str:
    return f"/v1.0/devices/{device_id}/commands"


def device_info_path(device_id: str) -> str:
    return f"/v1.0/devices/{device_id}"


def storage_key(device_id: str) -> str:
    return f"{DOMAIN}_{device_id}"


# Cloud mode polls; local mode is push-driven and only polls as a safety net.
SCAN_INTERVAL = 30
LOCAL_REFRESH_INTERVAL = 300

# Tuya's status endpoint serves a cached shadow copy, so it keeps returning
# values long after the device is unplugged. The online flag on the device
# record is the only thing that reflects reality, and it costs an extra API
# call, so check it every Nth poll: every 4 keeps it under two minutes stale.
# Local mode knows the connection state directly and does not use this.
RECONNECT_CHECK_EVERY = 4

# The primary reboot signal: a power cycle resets several settings in one go,
# and they all land together. On the reference device four settings reset, so
# three gives some margin without being plausible as deliberate changes made
# inside one window.
RESET_DETECTION_THRESHOLD = 3

# platform: switch | number | sensor | time
# category: config | diagnostic | omitted for the main Controls/Sensors sections
# Time datapoints are stored as minutes since midnight (0-1435, 5-minute steps).
#
# DP 7 (excretion_times_day) is deliberately absent: despite its name, the
# device rewrites it as 1 after every visit rather than accumulating, so it
# never reads anything but 1. Confirmed by watching it fire with the same
# value on consecutive visits. Visits Today and Uses Today count DP 102
# instead.
DP_MAPPING = {
    4:   {"name": "Auto Clean",          "platform": "switch", "code": "auto_clean",          "category": "config", "icon": "mdi:autorenew"},
    5:   {"name": "Delay Clean Time",    "platform": "number", "code": "delay_clean_time",    "category": "config", "min": 1, "max": 60, "unit": "min", "icon": "mdi:timer-outline"},
    10:  {"name": "Sleep Mode",          "platform": "switch", "code": "sleep",               "category": "config", "icon": "mdi:sleep"},
    11:  {"name": "Sleep Time Begin",    "platform": "time",   "code": "sleep_start_time",    "category": "config", "icon": "mdi:clock-start"},
    12:  {"name": "Sleep Time End",      "platform": "time",   "code": "sleep_end_time",      "category": "config", "icon": "mdi:clock-end"},
    21:  {"name": "Notification",        "platform": "sensor", "code": "notification",        "category": "diagnostic", "icon": "mdi:bell-outline"},
    22:  {"name": "Fault",               "platform": "sensor", "code": "fault",               "category": "diagnostic", "icon": "mdi:alert-circle-outline"},
    24:  {"name": "Status",              "platform": "sensor", "code": "status",              "icon": "mdi:information-outline"},
    101: {"name": "Motor Current",       "platform": "sensor", "code": "motor_current",       "category": "diagnostic", "unit": "mA", "icon": "mdi:sine-wave"},
    103: {"name": "Child Lock",          "platform": "switch", "code": "child_lock",          "category": "config", "icon": "mdi:lock"},
    104: {"name": "Empty Cycle Status",  "platform": "sensor", "code": "empty",               "icon": "mdi:delete-empty"},
    105: {"name": "Beep",                "platform": "switch", "code": "beep",                "category": "config", "icon": "mdi:volume-high"},
    106: {"name": "Clean Cycle Status",  "platform": "sensor", "code": "clean",               "icon": "mdi:broom"},
    107: {"name": "History Record",      "platform": "sensor", "code": "history_record",      "category": "diagnostic", "icon": "mdi:history"},
    108: {"name": "Kitten Mode",         "platform": "switch", "code": "kitty",               "category": "config", "icon": "mdi:cat"},
    109: {"name": "Indicator Light",     "platform": "switch", "code": "indicator",           "category": "config", "icon": "mdi:led-on"},
    111: {"name": "Deodorizer Reminder", "platform": "switch", "code": "deodorizer_box",      "category": "config", "icon": "mdi:flower"},
}

# The device accepts times in 5-minute increments only.
TIME_STEP_MINUTES = 5
TIME_MAX_MINUTES = 1435

# DP 102 (cat_duration_weight) emits one record per completed visit: bytes 0-1
# are the duration in seconds, bytes 2-3 are weight. The duration was verified
# against observed entry and exit times; weight has only ever read zero.
VISIT_DP = 102
# A visit at least this long counts as a "use" rather than just a visit.
USE_MIN_DURATION_SECONDS = 30

# DP 107 (history_record) reports this value at the moment a clean cycle ends.
HISTORY_DP = 107
CLEAN_DONE_VALUE = "finish_clean"

STORAGE_VERSION = 1

# Human-readable labels for enum datapoints. Raw values not listed here are
# title-cased as a fallback rather than hidden.
VALUE_LABELS = {
    24: {
        "standby": "Idle",
        "waiting": "Waiting",
        "cleaning": "Cleaning",
        "cat_get_in": "Cat Inside",
        "pause": "Paused",
        "pause_2": "Paused",
        "garbage_box_full": "Waste Bin Full",
        "Error": "Error",
        "emptying": "Emptying",
        "empty_done": "Empty Complete",
        "clean_done": "Clean Complete",
    },
    104: {
        "standby1": "Idle",
        "empty": "Emptying",
        "pause1": "Paused",
    },
    106: {
        "standby": "Idle",
        "cleaning": "Cleaning",
        "pause": "Paused",
    },
    107: {
        "enter": "Cat Entered",
        "done": "Visit Ended",
        "finish_clean": "Clean Finished",
        "garbage_full": "Waste Bin Full",
        "E1": "Error E1",
        "E2": "Error E2",
        "E3": "Error E3",
        "E4": "Error E4",
        "E5": "Error E5",
    },
}

# Bitmap datapoints: bit position -> label, low bit first.
BITMAP_LABELS = {
    21: ["Waste Bin Full", "Error E1", "Error E2", "Error E3", "Error E4", "Error E5"],
    22: ["Error E1", "Error E2", "Error E3", "Error E4", "Error E5"],
}
BITMAP_CLEAR_LABEL = "None"

# DP 24 values and DP 21 bit 0 that mean the waste bin is full
BIN_FULL_STATUS = "garbage_box_full"
BIN_FULL_BIT = 1

# Typed challenge guarding destructive buttons.
CONFIRM_PHRASE = "EMPTY"
CONFIRM_TIMEOUT_SECONDS = 60
CONFIRM_MAX_LENGTH = 16
CONFIRMATION_SIGNAL = f"{DOMAIN}_confirmation_updated"

# One-shot buttons. Each fires a single enum value at a datapoint.
# key -> unique_id suffix; dp_id is only used for availability tracking.
# "confirm": True requires the challenge phrase to be typed first.
BUTTONS = {
    "start_clean": {
        "name": "Start Clean Cycle",
        "dp_id": 106,
        "code": "clean",
        "value": "cleaning",
        "icon": "mdi:broom",
    },
    "pause_clean": {
        "name": "Pause Clean Cycle",
        "dp_id": 106,
        "code": "clean",
        "value": "pause",
        "icon": "mdi:pause",
    },
    "start_empty": {
        "name": "Start Empty Cycle",
        "dp_id": 104,
        "code": "empty",
        "value": "empty",
        "icon": "mdi:delete-empty",
        "confirm": True,
    },
}
