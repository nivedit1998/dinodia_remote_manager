"""Constants for Dinodia Remote Manager."""

DOMAIN = "dinodia_remote_manager"
DATA_STORE_VERSION = 1
DATA_STORE_KEY = f"{DOMAIN}.bindings"

ATTR_BINDING_ID = "binding_id"
ATTR_REMOTE_DEVICE_ID = "remote_device_id"
ATTR_TARGET_DEVICE_ID = "target_device_id"
ATTR_TARGET_ENTITY_ID = "target_entity_id"
ATTR_TARGET_KIND = "target_kind"

CONF_BINDING_NAME = "binding_name"
CONF_ENABLED = "enabled"

SERVICE_REGISTER_BINDING = "register_binding"
SERVICE_UNBIND = "unbind"
SERVICE_RESOLVE_BINDING = "resolve_binding"

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
)

SUPPORTED_ACTIONABLE_TARGET_DOMAINS = (
    "light",
    "switch",
    "cover",
    "climate",
    "media_player",
)

