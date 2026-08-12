import os
import yaml
from typing import Any, Optional

class ConfigRegistry:
    _instance: Optional["ConfigRegistry"] = None

    def __init__(self, config_path: str):
        with open(config_path, "r", encoding="utf-8") as f:
            self._data = yaml.safe_load(f)
        self._apply_env_overrides()

    @classmethod
    def init(cls, config_path: str = "config.yaml") -> "ConfigRegistry":
        cls._instance = cls(config_path)
        return cls._instance

    @classmethod
    def get(cls, key_path: str, default: Any = None) -> Any:
        keys = key_path.split(".")
        value = cls._instance._data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    @classmethod
    def override(cls, key_path: str, value: Any) -> None:
        keys = key_path.split(".")
        target = cls._instance._data
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value

    def _apply_env_overrides(self) -> None:
        for key, val in os.environ.items():
            if key.startswith("RAG_"):
                config_key = key[4:].lower().replace("__", ".")
                self._set_nested(self._data, config_key, val)

    @staticmethod
    def _set_nested(data: dict, key_path: str, value: str) -> None:
        keys = key_path.split(".")
        for k in keys[:-1]:
            if k not in data:
                data[k] = {}
            data = data[k]
        data[keys[-1]] = yaml.safe_load(value) if value.isdigit() else value
