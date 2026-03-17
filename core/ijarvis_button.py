"""Shared button abstraction for interactive command responses.

IJarvisButton flows from node commands → CC → notifications → mobile,
providing consistent button rendering across the entire pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class IJarvisButton:
    """A tappable action button attached to a command response.

    Attributes:
        button_text: Display label (e.g. "Send", "Turn On").
        button_action: Action identifier sent back on tap (e.g. "send_click").
        button_type: Controls color/style in the mobile UI.
        button_icon: Optional MaterialCommunityIcons name.
    """

    button_text: str
    button_action: str
    button_type: Literal["primary", "secondary", "destructive"]
    button_icon: str | None = None

    def to_dict(self) -> dict[str, str]:
        d: dict[str, str] = {
            "button_text": self.button_text,
            "button_action": self.button_action,
            "button_type": self.button_type,
        }
        if self.button_icon:
            d["button_icon"] = self.button_icon
        return d
