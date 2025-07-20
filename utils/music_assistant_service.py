import asyncio
import json
import websockets
import threading
import time
from typing import Any, Dict, Optional, List
from utils.config_service import Config


class MusicAssistantService:
    def __init__(self) -> None:
        self.uri: Optional[str] = Config.get_str('music_assistant_url')
        self.player_id: Optional[str] = Config.get_str('music_assistant_player_id')
        self.ws: Optional[Any] = None  # websockets.WebSocketServerProtocol
        self.lock: threading.Lock = threading.Lock()
        self.connected: bool = False
        self.message_id: int = 0
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self.player_cache: Dict[str, Dict[str, Any]] = {}

        threading.Thread(target=self._start_event_loop, daemon=True).start()

    def _start_event_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    async def _connect(self) -> None:
        try:
            if not self.uri:
                raise ValueError("music_assistant_url not configured")
            self.ws = await websockets.connect(self.uri)
            self.connected = True
            await self._get_players_async()
        except Exception as e:
            print(f"[MA] Connection failed: {e}")
            self.connected = False
            await asyncio.sleep(5)
            await self._connect()

    async def _get_players_async(self) -> None:
        """Async version of get_players for internal use"""
        result: Optional[Dict[str, Any]] = await self._send_command("players/all")
        if result and "result" in result:
            for p in result["result"]:
                self.player_cache[p["name"]] = p

    async def _send_command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if not self.ws or self.ws.closed:
            print("[MA] WebSocket not open. Reconnecting...")
            await self._connect()

        self.message_id += 1
        message: Dict[str, Any] = {
                "message_id": self.message_id,
                "command": command
        }

        if payload:
            message["payload"] = payload

        try:
            if self.ws:
                await self.ws.send(json.dumps(message))
                response: str = await self.ws.recv()
                return json.loads(response)
        except Exception as e:
            print(f"[MA] Command failed: {e}")
            self.connected = False
            return None

    def _run_async(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def get_players(self) -> Dict[str, Dict[str, Any]]:
        result: Optional[Dict[str, Any]] = self._run_async(self._send_command("players/all"))

        if result and "result" in result:
            for p in result["result"]:
                self.player_cache[p["name"]] = p
        return self.player_cache

    def pause_player(self, player_name: str) -> Optional[Dict[str, Any]]:
        player: Optional[Dict[str, Any]] = self.player_cache.get(player_name)
        if not player:
            self.get_players()
            player = self.player_cache.get(player_name)

        if not player:
            print(f"[MA] Player '{player_name}' not found.")
            return None

        payload: Dict[str, Any] = {
                "player_id": player["player_id"],
                "command": "pause"
        }

        return self._run_async(self._send_command("players/cmd/pause", payload))


class DummyMusicAssistantService:
    def pause(self, *args: Any) -> None:
        pass

    def is_playing(self, *args: Any) -> bool:
        return False
