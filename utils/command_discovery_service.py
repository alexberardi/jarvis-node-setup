import importlib
import pkgutil
import threading
import time
from typing import Dict, List, Optional
from functools import lru_cache

from core.ijarvis_command import IJarvisCommand
from utils.config_service import Config


class CommandDiscoveryService:
    def __init__(self, refresh_interval: int = 20):
        self.refresh_interval = refresh_interval
        self._commands_cache: Dict[str, IJarvisCommand] = {}
        self._last_refresh = 0
        self._lock = threading.Lock()
        
        # Start background refresh thread
        self._refresh_thread = threading.Thread(target=self._background_refresh, daemon=True)
        self._refresh_thread.start()

    def _background_refresh(self):
        """Background thread that refreshes commands every refresh_interval seconds"""
        while True:
            time.sleep(self.refresh_interval)
            try:
                self._discover_commands()
                print(f"[CommandDiscovery] Refreshed {len(self._commands_cache)} commands")
            except Exception as e:
                print(f"[CommandDiscovery] Error refreshing commands: {e}")

    def _discover_commands(self):
        """Discover all IJarvisCommand implementations"""
        import commands
        
        new_commands = {}
        
        for _, module_name, _ in pkgutil.iter_modules(commands.__path__):
            try:
                module = importlib.import_module(f"commands.{module_name}")
                
                for attr in dir(module):
                    cls = getattr(module, attr)
                    
                    if (isinstance(cls, type) and 
                        issubclass(cls, IJarvisCommand) and 
                        cls is not IJarvisCommand):
                        
                        instance = cls()
                        new_commands[instance.command_name] = instance
                        
            except Exception as e:
                print(f"[CommandDiscovery] Error loading module {module_name}: {e}")
        
        with self._lock:
            self._commands_cache = new_commands
            self._last_refresh = time.time()

    def get_command(self, command_name: str) -> Optional[IJarvisCommand]:
        """Get a specific command by name"""
        with self._lock:
            return self._commands_cache.get(command_name)

    def get_all_commands(self) -> Dict[str, IJarvisCommand]:
        """Get all discovered commands"""
        with self._lock:
            return self._commands_cache.copy()

    def get_available_commands_schema(self) -> List[Dict]:
        """Get the schema for all available commands (for LLM)"""
        with self._lock:
            return [cmd.get_command_schema() for cmd in self._commands_cache.values()]

    def refresh_now(self):
        """Force an immediate refresh of commands"""
        self._discover_commands()


# Global instance
_command_discovery_service = None


def get_command_discovery_service() -> CommandDiscoveryService:
    """Get the global command discovery service instance"""
    global _command_discovery_service
    if _command_discovery_service is None:
        _command_discovery_service = CommandDiscoveryService()
    return _command_discovery_service 