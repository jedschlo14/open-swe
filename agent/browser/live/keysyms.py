"""X keysyms for browser key events, which is what the display's keyboard input takes.

Characters outside ASCII have no keycode on the display, so they are not mapped here;
the relay inserts them as text instead.
"""

_NAMED_KEYS = {
    "Enter": 0xFF0D,
    "Backspace": 0xFF08,
    "Tab": 0xFF09,
    "Escape": 0xFF1B,
    "Delete": 0xFFFF,
    "Insert": 0xFF63,
    "Home": 0xFF50,
    "End": 0xFF57,
    "PageUp": 0xFF55,
    "PageDown": 0xFF56,
    "ArrowLeft": 0xFF51,
    "ArrowUp": 0xFF52,
    "ArrowRight": 0xFF53,
    "ArrowDown": 0xFF54,
    "CapsLock": 0xFFE5,
    "ContextMenu": 0xFF67,
    **{f"F{n}": 0xFFBD + n for n in range(1, 13)},
}
_MODIFIER_CODES = {
    "ShiftLeft": 0xFFE1,
    "ShiftRight": 0xFFE2,
    "ControlLeft": 0xFFE3,
    "ControlRight": 0xFFE4,
    "AltLeft": 0xFFE9,
    "AltRight": 0xFFEA,
    "MetaLeft": 0xFFEB,
    "MetaRight": 0xFFEC,
}
_FIRST_NON_ASCII = 0x80


def keysym_for(key: str, code: str) -> int | None:
    """The X keysym a browser key event stands for, or ``None`` when it has none."""
    if code in _MODIFIER_CODES:
        return _MODIFIER_CODES[code]
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) == 1 and ord(key) < _FIRST_NON_ASCII:
        return ord(key)
    return None
