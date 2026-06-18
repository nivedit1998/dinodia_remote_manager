"""Constants for Dinodia Remote Manager."""

DOMAIN = "dinodia_remote_manager"
DATA_STORE_VERSION = 2
DATA_STORE_KEY = f"{DOMAIN}.bindings"
DATA_REMOTE_ROUTER = f"{DOMAIN}.remote_router"
DATA_EVENT_ROUTER = f"{DOMAIN}.event_router"
DATA_TRIGGER_LISTENERS = f"{DOMAIN}.trigger_listeners"
DATA_RUNTIME_TRIGGER_UNSUBSCRIBERS = f"{DOMAIN}.runtime_trigger_unsubscribers"
DATA_RECENT_ROUTED_TRIGGER_EVENTS = f"{DOMAIN}.recent_routed_trigger_events"

EVENT_REMOTE_MANAGER = f"{DOMAIN}_event"

ATTR_BINDING_ID = "binding_id"
ATTR_REMOTE_DEVICE_ID = "remote_device_id"
ATTR_REMOTE_ENTITY_ID = "remote_entity_id"
ATTR_TARGET_DEVICE_ID = "target_device_id"
ATTR_TARGET_ENTITY_ID = "target_entity_id"
ATTR_TARGET_KIND = "target_kind"
ATTR_EVENT_TYPE = "event_type"
ATTR_EVENT_SUBTYPE = "event_subtype"
ATTR_EVENT_COMMAND = "command"
ATTR_EVENT_SOURCE = "source"
ATTR_EVENT_PAYLOAD = "payload"
ATTR_HANDLED_BY_SERVICE = "handled_by_service"

CONF_BINDING_NAME = "binding_name"
CONF_ENABLED = "enabled"

SERVICE_REGISTER_BINDING = "register_binding"
SERVICE_SET_TRIGGER_TARGET = "set_trigger_target"
SERVICE_UPDATE_BINDING = "update_binding"
SERVICE_UNBIND = "unbind"
SERVICE_REMOVE_TENANT_BINDINGS = "remove_tenant_bindings"
SERVICE_REMOVE_TRIGGER_BINDINGS_FOR_DEVICES = "remove_trigger_bindings_for_devices"
SERVICE_RESOLVE_BINDING = "resolve_binding"
SERVICE_LIST_BINDINGS = "list_bindings"
SERVICE_LIST_TRIGGER_DEVICES = "list_trigger_devices"
SERVICE_LIST_TRIGGER_DEVICE_DASHBOARD = "list_trigger_device_dashboard"
SERVICE_SIMULATE_REMOTE_EVENT = "simulate_remote_event"

REMOTE_LABEL_NAME = "Remote"

SUPPORTED_TARGET_DOMAINS = (
    "light",
    "switch",
    "cover",
    "climate",
    "media_player",
    "sensor",
    "binary_sensor",
    "button",
    "fan",
    "lock",
    "vacuum",
    "humidifier",
)

SUPPORTED_ACTIONABLE_TARGET_DOMAINS = (
    "light",
    "switch",
    "cover",
    "climate",
    "media_player",
    "fan",
    "lock",
    "vacuum",
    "humidifier",
)
