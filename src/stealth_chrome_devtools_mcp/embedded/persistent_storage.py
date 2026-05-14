import threading
from typing import Any, Dict, Optional

class InMemoryStorage:
    """Thread-safe in-memory storage for browser instance data."""

    def __init__(self):
        """
        Initialize the in-memory storage.

        self: InMemoryStorage - The storage instance.
        """
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {"instances": {}}

    def store_instance(self, instance_id: str, data: Dict[str, Any]):
        """
        Store browser instance data.

        instance_id: str - The unique identifier for the browser instance.
        data: Dict[str, Any] - The data associated with the browser instance.
        """
        with self._lock:
            if 'instances' not in self._data:
                self._data['instances'] = {}
            serializable_data = {
                'instance_id': instance_id,
                'state': data.get('state', 'unknown'),
                'created_at': data.get('created_at', ''),
                'current_url': data.get('current_url', ''),
                'title': data.get('title', ''),
                'tabs': []
            }
            self._data['instances'][instance_id] = serializable_data

    def remove_instance(self, instance_id: str):
        """
        Remove browser instance from storage.

        instance_id: str - The unique identifier for the browser instance to remove.
        """
        with self._lock:
            if 'instances' in self._data and instance_id in self._data['instances']:
                del self._data['instances'][instance_id]

    def get_instance(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """
        Get browser instance data.

        instance_id: str - The unique identifier for the browser instance.
        Returns: Optional[Dict[str, Any]] - The data for the browser instance, or None if not found.
        """
        with self._lock:
            return self._data.get('instances', {}).get(instance_id)

    def list_instances(self) -> Dict[str, Any]:
        """
        List all stored instances.

        Returns: Dict[str, Any] - A copy of all stored instances.
        """
        with self._lock:
            return self._data.copy()

    def clear_all(self):
        """
        Clear all stored data.

        self: InMemoryStorage - The storage instance.
        """
        with self._lock:
            self._data = {"instances": {}}

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get data by key.

        key: str - The key to retrieve from storage.
        default: Any - The default value to return if key is not found.
        Returns: Any - The value associated with the key, or default if not found.
        """
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any):
        """
        Set data by key.

        key: str - The key to set in storage.
        value: Any - The value to associate with the key.
        """
        with self._lock:
            self._data[key] = value

persistent_storage = InMemoryStorage()