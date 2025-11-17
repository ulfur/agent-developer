"""Helpers for reading Geekworm X1201 UPS telemetry."""

from __future__ import annotations

import datetime as dt
import logging
import struct
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping, Callable

try:  # Optional runtime dependency when telemetry is enabled.
    import smbus2  # type: ignore
except ImportError:  # pragma: no cover - optional feature
    smbus2 = None  # type: ignore

try:  # Optional GPIO dependency for detecting AC power loss.
    import gpiod  # type: ignore
except ImportError:  # pragma: no cover - optional feature
    gpiod = None  # type: ignore


@dataclass(frozen=True)
class BatteryStatus:
    """Snapshot of the UPS state that can be rendered on the aux display."""

    percentage: float | None = None
    voltage: float | None = None
    ac_power: bool | None = None
    state: str | None = None
    low_battery: bool = False
    timestamp: float | None = None


class X1201PowerMonitor:
    """Read battery + AC telemetry from the Geekworm X1201 UPS."""

    VOLTAGE_REGISTER = 0x02
    CAPACITY_REGISTER = 0x04
    DEFAULT_I2C_ADDRESS = 0x36
    LOW_BATTERY_THRESHOLD = 20.0

    def __init__(
        self,
        *,
        i2c_bus: int = 1,
        address: int = DEFAULT_I2C_ADDRESS,
        ac_pin: int | None = 6,
        gpio_chip: str | int = "gpiochip0",
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger(__name__)
        self._bus_id = i2c_bus
        self._address = int(address)
        self._ac_pin = ac_pin
        self._gpio_chip_name = self._normalize_chip_name(gpio_chip)
        self._bus: Any | None = None
        self._gpio_chip: Any | None = None
        self._ac_line: Any | None = None
        self._ac_request: Any | None = None
        self._gpiod_supports_request = bool(gpiod and hasattr(gpiod, "request_lines"))
        self._last_warning: str | None = None

        if smbus2 is None:  # pragma: no cover - guarded by runtime detection
            raise RuntimeError("smbus2 is required for X1201 telemetry support")

    # ------------------------------------------------------------------ public
    def read_status(self) -> BatteryStatus | None:
        """Return the latest battery + AC state or None when unavailable."""
        voltage = self._read_voltage()
        capacity = self._read_capacity()
        ac_state = self._read_ac_state()
        if voltage is None and capacity is None and ac_state is None:
            return None
        percentage = self._clamp_percentage(capacity)
        voltage_estimate = self._estimate_percentage_from_voltage(voltage)
        if percentage is None or percentage < 0 or percentage > 100:
            percentage = voltage_estimate
        elif (
            voltage_estimate is not None
            and percentage is not None
            and (percentage < voltage_estimate - 15 or percentage > voltage_estimate + 15)
        ):
            percentage = voltage_estimate
        state = self._derive_state(ac_state, percentage)
        low_battery = bool(percentage is not None and percentage <= self.LOW_BATTERY_THRESHOLD)
        snapshot = BatteryStatus(
            percentage=percentage,
            voltage=self._round_or_none(voltage, digits=3),
            ac_power=ac_state,
            state=state,
            low_battery=low_battery,
            timestamp=time.time(),
        )
        self._last_warning = None
        return snapshot

    def close(self) -> None:
        """Release any open file descriptors for a clean shutdown."""
        self._close_bus()
        self._release_ac_resources()

    # ----------------------------------------------------------------- helpers
    def _read_voltage(self) -> float | None:
        raw = self._read_register(self.VOLTAGE_REGISTER)
        if raw is None:
            return None
        swapped = struct.unpack("<H", struct.pack(">H", raw))[0]
        return swapped * 1.25 / 1000 / 16

    def _read_capacity(self) -> float | None:
        raw = self._read_register(self.CAPACITY_REGISTER)
        if raw is None:
            return None
        swapped = struct.unpack("<H", struct.pack(">H", raw))[0]
        return swapped / 256.0

    def _read_register(self, register: int) -> int | None:
        bus = self._ensure_bus()
        if bus is None:
            return None
        try:
            return bus.read_word_data(self._address, register)
        except OSError as exc:  # pragma: no cover - hardware specific
            self._log_warning_once(
                f"i2c_register_{register:02x}",
                "Unable to read UPS register 0x%02X: %s",
                register,
                exc,
            )
            self._close_bus()
            return None

    def _ensure_bus(self) -> Any | None:
        if self._bus is not None:
            return self._bus
        if smbus2 is None:  # pragma: no cover - defensive
            return None
        try:
            self._bus = smbus2.SMBus(self._bus_id)
        except FileNotFoundError as exc:  # pragma: no cover - hardware specific
            self._log_warning_once(
                "i2c_not_found",
                "I2C bus %s unavailable for UPS telemetry: %s",
                self._bus_id,
                exc,
            )
            self._bus = None
        except OSError as exc:  # pragma: no cover - hardware specific
            self._log_warning_once(
                "i2c_unavailable",
                "Unable to open I2C bus %s for UPS telemetry: %s",
                self._bus_id,
                exc,
            )
            self._bus = None
        return self._bus

    def _close_bus(self) -> None:
        if self._bus is None:
            return
        try:
            self._bus.close()
        except Exception:  # pragma: no cover - defensive close
            pass
        finally:
            self._bus = None

    def _read_ac_state(self) -> bool | None:
        if self._ac_pin is None or gpiod is None:
            return None
        if self._ac_line is None and self._ac_request is None and not self._setup_ac_line():
            return None
        try:
            if self._ac_request is not None:
                value = self._ac_request.get_value(self._ac_pin)
                return bool(value)
            if self._ac_line is not None:
                return bool(self._ac_line.get_value())
        except OSError as exc:  # pragma: no cover - hardware specific
            self._log_warning_once(
                "gpiod_read_failed",
                "Unable to read UPS PLD pin %s: %s",
                self._ac_pin,
                exc,
            )
            self._release_ac_resources()
        return None

    def _setup_ac_line(self) -> bool:
        if gpiod is None or self._ac_pin is None:
            return False
        target_chip = self._normalize_chip_name(self._gpio_chip_name)
        try:
            if self._gpiod_supports_request:
                from gpiod.line_settings import LineSettings, Direction

                settings = LineSettings(direction=Direction.INPUT)
                self._ac_request = gpiod.request_lines(
                    target_chip,
                    consumer="x1201_pld",
                    config={self._ac_pin: settings},
                )
                return True
            self._gpio_chip = gpiod.Chip(target_chip)
            line = self._gpio_chip.get_line(self._ac_pin)
            line.request(consumer="x1201_pld", type=gpiod.LINE_REQ_DIR_IN)
            self._ac_line = line
            return True
        except (OSError, AttributeError, ImportError) as exc:  # pragma: no cover - hardware specific
            self._log_warning_once(
                "gpiod_init_failed",
                "Unable to access UPS PLD pin %s on %s: %s",
                self._ac_pin,
                self._gpio_chip_name,
                exc,
            )
            self._release_ac_resources()
            return False

    def _release_ac_resources(self) -> None:
        if self._ac_line is not None:
            try:
                self._ac_line.release()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
            self._ac_line = None
        if self._ac_request is not None:
            try:
                self._ac_request.release()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
            self._ac_request = None
        if self._gpio_chip is not None:
            try:
                self._gpio_chip.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
            self._gpio_chip = None

    def _derive_state(self, ac_power: bool | None, percentage: float | None) -> str | None:
        if ac_power is True:
            if percentage is None:
                return "ac"
            if percentage >= 99.0:
                return "charged"
            return "charging"
        if ac_power is False:
            return "battery"
        return None

    def _clamp_percentage(self, value: float | None) -> float | None:
        if value is None:
            return None
        return max(0.0, min(100.0, round(value, 1)))

    def _estimate_percentage_from_voltage(self, voltage: float | None) -> float | None:
        if voltage is None:
            return None
        points = [
            (4.20, 100.0),
            (4.10, 95.0),
            (4.05, 90.0),
            (4.00, 85.0),
            (3.95, 80.0),
            (3.90, 75.0),
            (3.85, 70.0),
            (3.80, 65.0),
            (3.75, 60.0),
            (3.70, 55.0),
            (3.65, 45.0),
            (3.60, 35.0),
            (3.55, 25.0),
            (3.50, 15.0),
            (3.45, 10.0),
            (3.40, 7.0),
            (3.35, 4.0),
            (3.30, 2.0),
            (3.20, 0.0),
        ]
        if voltage >= points[0][0]:
            return points[0][1]
        if voltage <= points[-1][0]:
            return points[-1][1]
        for idx in range(len(points) - 1):
            v_high, p_high = points[idx]
            v_low, p_low = points[idx + 1]
            if v_low <= voltage <= v_high:
                span = v_high - v_low
                if span <= 0:
                    return p_low
                ratio = (voltage - v_low) / span
                return p_low + ratio * (p_high - p_low)
        return None

    def _round_or_none(self, value: float | None, *, digits: int) -> float | None:
        if value is None:
            return None
        return round(value, digits)

    def _normalize_chip_name(self, chip: str | int) -> str:
        if isinstance(chip, str):
            return chip
        try:
            idx = int(chip)
        except (TypeError, ValueError):
            return "gpiochip0"
        return f"gpiochip{idx}"

    def _log_warning_once(self, key: str, message: str, *args: Any) -> None:
        formatted = message % args if args else message
        cache_key = f"{key}:{formatted}"
        if self._last_warning == cache_key:
            return
        self._last_warning = cache_key
        self._logger.warning(formatted)


def _timestamp_to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return None


def normalize_power_payload(snapshot: BatteryStatus | Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a dict representing the telemetry snapshot."""
    if snapshot is None:
        return None
    payload: dict[str, Any]
    if isinstance(snapshot, dict):
        payload = dict(snapshot)
    elif is_dataclass(snapshot):
        payload = asdict(snapshot)
    elif isinstance(snapshot, Mapping):
        payload = dict(snapshot)
    else:
        payload = {}
        for key in ("percentage", "voltage", "ac_power", "state", "low_battery", "timestamp"):
            if hasattr(snapshot, key):
                payload[key] = getattr(snapshot, key)
    if not payload:
        return None
    timestamp = payload.get("timestamp")
    if timestamp is None:
        timestamp = time.time()
    normalized = {
        "percentage": payload.get("percentage"),
        "voltage": payload.get("voltage"),
        "ac_power": payload.get("ac_power"),
        "state": payload.get("state"),
        "low_battery": bool(payload.get("low_battery")),
        "timestamp": timestamp,
    }
    return normalized


class PowerTelemetryCache:
    """Thread-safe cache that stores the most recent power snapshot."""

    def __init__(self, on_update: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._callback = on_update

    def update(self, snapshot: BatteryStatus | Mapping[str, Any] | None) -> dict[str, Any] | None:
        normalized = normalize_power_payload(snapshot)
        if not normalized:
            return None
        normalized["updated_at"] = _timestamp_to_iso(normalized.get("timestamp"))
        with self._lock:
            self._latest = dict(normalized)
            snapshot = dict(self._latest)
        if self._callback:
            try:
                self._callback(snapshot)
            except Exception:
                pass
        return snapshot

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest) if self._latest else None
