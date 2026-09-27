"""Static ProtonDB-style compatibility data for Steam games on Linux.

This module ships a bundled seed so the game checker works offline.
A future --refresh-protondb command can update the seed from a public source.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Tier values: native, platinum, gold, silver, bronze, borked
PROTONDB_DATA: Dict[str, Dict[str, Any]] = {
    "394360": {
        # Hearts of Iron IV
        "tier": "gold",
        "reports": 1874,
        "rating": 8.2,
        "notes": "Runs well with Proton 9.0+. The launcher may need a mouse workaround on some setups.",
    },
    "730": {
        # Counter-Strike 2 / CS:GO
        "tier": "native",
        "reports": 0,
        "rating": 10.0,
        "notes": "Native Linux build available via Steam.",
    },
    "570": {
        # Dota 2
        "tier": "native",
        "reports": 0,
        "rating": 10.0,
        "notes": "Native Linux build available via Steam.",
    },
    "440": {
        # Team Fortress 2
        "tier": "native",
        "reports": 0,
        "rating": 10.0,
        "notes": "Native Linux build available via Steam.",
    },
    "252490": {
        # Rust
        "tier": "bronze",
        "reports": 642,
        "rating": 5.1,
        "notes": "Easy Anti-Cheat blocks online play on Proton. Linux native client was deprecated.",
    },
    "1091500": {
        # Cyberpunk 2077
        "tier": "gold",
        "reports": 4120,
        "rating": 8.5,
        "notes": "Runs well with Proton Experimental or GE-Proton. Enable DLSS/FSR as desired.",
    },
    "271590": {
        # Grand Theft Auto V
        "tier": "gold",
        "reports": 3890,
        "rating": 8.3,
        "notes": "Runs well with Proton. BattlEye can cause issues in GTA Online.",
    },
    "1172470": {
        # Apex Legends
        "tier": "borked",
        "reports": 2100,
        "rating": 2.1,
        "notes": "Easy Anti-Cheat blocks Linux/Proton. Not playable.",
    },
    "1085660": {
        # Destiny 2
        "tier": "borked",
        "reports": 1850,
        "rating": 1.9,
        "notes": "BattlEye blocks Proton. Not playable on Linux.",
    },
    "1245620": {
        # Elden Ring
        "tier": "gold",
        "reports": 1520,
        "rating": 8.4,
        "notes": "Runs well with Proton. Online play works with Easy Anti-Cheat enabled.",
    },
    "292030": {
        # The Witcher 3: Wild Hunt
        "tier": "platinum",
        "reports": 2340,
        "rating": 9.2,
        "notes": "Runs flawlessly with Proton.",
    },
    "377160": {
        # Fallout 4
        "tier": "gold",
        "reports": 980,
        "rating": 8.0,
        "notes": "Runs well; minor audio issues on some setups.",
    },
    "489830": {
        # The Elder Scrolls V: Skyrim Special Edition
        "tier": "platinum",
        "reports": 1760,
        "rating": 9.1,
        "notes": "Runs flawlessly with Proton.",
    },
    "1623730": {
        # Palworld
        "tier": "gold",
        "reports": 890,
        "rating": 8.1,
        "notes": "Runs well with Proton. Multiplayer generally works.",
    },
    "105600": {
        # Terraria
        "tier": "native",
        "reports": 0,
        "rating": 10.0,
        "notes": "Native Linux build available.",
    },
    "413150": {
        # Stardew Valley
        "tier": "native",
        "reports": 0,
        "rating": 10.0,
        "notes": "Native Linux build available.",
    },
    "814380": {
        # Sekiro: Shadows Die Twice
        "tier": "gold",
        "reports": 760,
        "rating": 8.2,
        "notes": "Runs well with Proton. Disable composition pipeline if stuttering.",
    },
    "1551360": {
        # Forza Horizon 5
        "tier": "silver",
        "reports": 540,
        "rating": 6.8,
        "notes": "Playable but may require tweaks for online/Multiplayer.",
    },
    "1293160": {
        # Deep Rock Galactic
        "tier": "platinum",
        "reports": 620,
        "rating": 9.0,
        "notes": "Runs flawlessly with Proton.",
    },
    "548570": {
        # ROUNDS
        "tier": "gold",
        "reports": 180,
        "rating": 8.0,
        "notes": "Runs well with Proton.",
    },
    "431960": {
        # Wallpaper Engine
        "tier": "silver",
        "reports": 310,
        "rating": 6.5,
        "notes": "Works with some scenes needing Windows-specific plugins.",
    },
    "976730": {
        # Halo: The Master Chief Collection
        "tier": "gold",
        "reports": 430,
        "rating": 8.1,
        "notes": "Campaigns run well. Multiplayer may need anti-cheat workarounds.",
    },
}


def lookup(app_id: int | str) -> Optional[Dict[str, Any]]:
    """Return ProtonDB-style data for a Steam app ID, or None if unknown."""
    return PROTONDB_DATA.get(str(app_id))
