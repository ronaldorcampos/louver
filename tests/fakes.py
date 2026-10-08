from __future__ import annotations


def Z(data) -> bytes:
    """A feature report with its zero padding removed (tests use this on
    both sides, so a meaningful trailing 0 is dropped consistently)."""
    return bytes(data).rstrip(b"\0")


class FakeTransport:
    """Records every report; answers input reads from a scripted map keyed by
    the last feature report's command bytes."""

    def __init__(self, responses: dict[bytes, bytes] | None = None, fail: bool = False):
        self.features: list[bytes] = []
        self.outputs: list[bytes] = []
        self.log: list[tuple[str, bytes]] = []
        self.responses = responses or {}
        self.fail = fail
        self.closed = False
        self._last = b""

    def send_feature(self, data: bytes) -> None:
        if self.fail:
            raise OSError(5, "EIO")
        # Devices pad reports to descriptor size; compare without the padding.
        self.features.append(Z(data))
        self.log.append(("F", Z(data)))
        self._last = bytes(data[1:3])

    def write(self, data: bytes) -> None:
        if self.fail:
            raise OSError(5, "EIO")
        self.outputs.append(bytes(data))
        self.log.append(("O", bytes(data)))

    def get_input(self, report_id: int, length: int) -> bytes:
        if self.fail:
            raise OSError(5, "EIO")
        return self.responses.get(self._last, b"").ljust(length, b"\0")

    def close(self) -> None:
        self.closed = True


HUB_RESPONSES = {
    bytes([0x50, 0x01]): bytes([0xE0, 0x50, 0x80, 0xC6, 0x12]),
    # 1 leading byte then 4 BE u16: 1200, 800, 0, 1950
    bytes([0x50, 0x00]): bytes([0xF0, 0x04, 0xB0, 0x03, 0x20, 0x00, 0x00, 0x07, 0x9E]),
}

STRIMER_RESPONSES = {
    bytes([0x50, 0x00]): bytes([0xE0, 0x52, 0xFF, 0x40, 0x05]),
    bytes([0x50, 0x01]): bytes([0x04]),
}


class FakeSensors:
    def __init__(self, values: dict[str, float | None]):
        self.values = values

    def read(self, sid: str):
        return self.values.get(sid)

    def missing(self, sid: str):
        parts = sid[4:].split(",") if sid.startswith("max:") else [sid]
        return [p for p in parts if self.values.get(p) is None]

    def list(self):
        return [{"id": k, "label": k, "value": v} for k, v in self.values.items()]
