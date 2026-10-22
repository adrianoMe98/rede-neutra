import re


def normalize_serial(value):
    serial = value.strip().upper()
    if re.fullmatch(r"[A-Z]{4}-[0-9A-F]{8}", serial):
        return serial.replace("-", "")
    if re.fullmatch(r"[0-9A-F]{16}", serial):
        prefix = bytes.fromhex(serial[:8])
        if all(65 <= byte <= 90 for byte in prefix):
            return prefix.decode("ascii") + serial[8:]
    return serial


def serial_aliases(value):
    serial = normalize_serial(value)
    aliases = {serial}
    if re.fullmatch(r"[A-Z]{4}[0-9A-F]{8}", serial):
        aliases.add(serial[:4].encode("ascii").hex().upper() + serial[4:])
    return aliases
