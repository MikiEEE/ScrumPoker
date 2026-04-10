"""
Helpers for reading client defaults from the active smallOS runtime config.

Each client still accepts explicit constructor arguments, but when a task is
already attached to a runtime these helpers let the protocol layer inherit
project-wide defaults from ``SmallOSConfig.client_defaults``.
"""


MISSING = object()


def runtime_client_defaults(task, section):
    """Return merged config defaults for one client section."""
    runtime = getattr(task, "OS", None)
    config = getattr(runtime, "config", None)
    if config is None:
        return {}
    if hasattr(config, "client_defaults_for"):
        return config.client_defaults_for(section)

    client_defaults = getattr(config, "client_defaults", None)
    if not isinstance(client_defaults, dict):
        return {}

    defaults = dict(client_defaults.get("stream", {}))
    section_defaults = client_defaults.get(section, {})
    if isinstance(section_defaults, dict):
        defaults.update(section_defaults)
    return defaults


def resolve_client_setting(task, section, key, explicit_value, fallback):
    """Prefer an explicit value, otherwise inherit from runtime config."""
    if explicit_value is not MISSING:
        return explicit_value
    return runtime_client_defaults(task, section).get(key, fallback)
