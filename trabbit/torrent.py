from __future__ import annotations

import hashlib

import bencodepy


def parse_torrent(data: bytes) -> tuple[str, int, str, str]:
    decoded = bencodepy.decode(data)
    if not isinstance(decoded, dict) or b"info" not in decoded:
        raise ValueError("Отримані дані не є валідним torrent-файлом.")

    info = decoded[b"info"]
    if not isinstance(info, dict):
        raise ValueError("Torrent не містить коректний info dictionary.")

    if b"length" in info:
        size = int(info[b"length"])
    else:
        size = sum(int(item[b"length"]) for item in info.get(b"files", []))

    name = info.get(b"name", b"torrent")
    if isinstance(name, bytes):
        name_str = name.decode("utf-8", errors="replace")
    else:
        name_str = str(name)

    encoded_info = bencodepy.encode(info)
    info_hash = hashlib.sha1(encoded_info).hexdigest()
    fingerprint = hashlib.sha256(data).hexdigest()
    return name_str, size, fingerprint, info_hash
