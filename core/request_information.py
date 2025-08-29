from dataclasses import dataclass
from typing import Optional


@dataclass
class RequestInformation:
    """
    Information about the request received from the Jarvis Command Center
    
    This object contains details about the voice command and any additional
    context that was provided when the command was selected.
    """
    voice_command: str
    # Future properties can be added here as the JCC endpoint evolves
    # For example: user_id, session_id, timestamp, etc.
