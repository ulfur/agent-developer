"""Minimal IT8591/IT8951 e-ink driver that relies on lgpio for GPIO + SPI.

The implementation mirrors the public Waveshare IT8951 C driver (MIT licence)
but is trimmed down for the 7.8\" HAT use-case: 4bpp full-frame updates that
can be triggered from Python code.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Iterable, List, Sequence

from PIL import Image


LOG = logging.getLogger(__name__)


class DisplayUnavailable(RuntimeError):
    """Raised when the hardware stack cannot be initialised."""


# IT8951 command/register constants (subset)
IT8951_TCON_SYS_RUN = 0x0001
IT8951_TCON_STANDBY = 0x0002
IT8951_TCON_SLEEP = 0x0003
IT8951_TCON_REG_RD = 0x0010
IT8951_TCON_REG_WR = 0x0011
IT8951_TCON_LD_IMG = 0x0020
IT8951_TCON_LD_IMG_AREA = 0x0021
IT8951_TCON_LD_IMG_END = 0x0022
USDEF_I80_CMD_DPY_AREA = 0x0034
USDEF_I80_CMD_DPY_BUF_AREA = 0x0037
USDEF_I80_CMD_VCOM = 0x0039
USDEF_I80_CMD_GET_DEV_INFO = 0x0302

DISPLAY_REG_BASE = 0x1000
UP1SR = DISPLAY_REG_BASE + 0x138
BGVR = DISPLAY_REG_BASE + 0x250
LUTAFSR = DISPLAY_REG_BASE + 0x224
I80CPCR = 0x0004
LISAR = 0x0208

IT8951_ROTATE_0 = 0
IT8951_ROTATE_90 = 1
IT8951_ROTATE_180 = 2
IT8951_ROTATE_270 = 3
IT8951_LDIMG_L_ENDIAN = 0
IT8951_PIXEL_4BPP = 2
IT8951_PIXEL_8BPP = 3
DU_MODE = 1    # fast monochrome refresh
GC16_MODE = 2  # 16-level grayscale refresh


@dataclass(frozen=True)
class IT8591Config:
    """Pin + timing configuration for the display HAT."""

    width: int = 1872
    height: int = 1404
    spi_device: int = 0  # /dev/spidev<device>.<channel>
    spi_channel: int = 0
    spi_hz: int = 24_000_000
    gpio_chip: int | str = 0
    rst_pin: int = 17
    busy_pin: int = 24
    cs_pin: int = 8
    vcom_mv: int = 1800
    rotate: int = IT8951_ROTATE_180


class IT8591DisplayDriver:
    """Thin wrapper around the IT8591/IT8951 command set."""

    def __init__(self, config: IT8591Config, logger: logging.Logger | None = None):
        self._config = config
        self._logger = logger or LOG
        self._lgpio = self._import_lgpio()
        self._gpio_handle = None
        self._claimed_pins: set[int] = set()
        self._spi_handle = None
        self._lock = threading.Lock()
        self._dev_info: dict[str, int | str] | None = None
        self._frame_addr = 0
        self.width = config.width
        self.height = config.height
        self._initialise()

    # ------------------------------------------------------------------ public
    def display_image(self, image: Image.Image, *, mode: int = GC16_MODE) -> None:
        """Send the provided PIL image (converted to 4bpp grayscale) to the HAT."""
        if self._spi_handle is None or self._gpio_handle is None:
            raise DisplayUnavailable("display driver not initialised")

        with self._lock:
            prepared = self._prepare_frame(image)
            self._wait_for_display_ready()
            self._write_frame_4bpp(prepared, 0, 0, self.width, self.height)
            self._display_area(0, 0, self.width, self.height, mode)
            self._wait_for_display_ready()

    def display_region(
        self,
        image: Image.Image,
        bounds: tuple[int, int, int, int],
        *,
        mode: int = DU_MODE,
    ) -> None:
        if self._spi_handle is None or self._gpio_handle is None:
            raise DisplayUnavailable("display driver not initialised")
        x, y, w, h = self._normalize_bounds(bounds)
        if w <= 0 or h <= 0:
            return
        self._logger.debug(
            "display_region bounds=%s,%s,%s,%s",
            x,
            y,
            w,
            h,
        )
        with self._lock:
            region = self._ensure_region_size(image, w, h)
            prepared = self._pack_grayscale(region, w)
            self._wait_for_display_ready()
            self._write_frame_4bpp(prepared, x, y, w, h)
            # Some firmware revisions ignore USDEF_I80_CMD_DPY_AREA when rotation is enabled;
            # fall back to full-frame refresh for stability when partial rendering fails.
            try:
                self._display_area(x, y, w, h, mode)
            except Exception:
                self._logger.warning(
                    "display_region fallback to full-frame refresh x=%s y=%s size=%sx%s",
                    x,
                    y,
                    w,
                    h,
                )
                self._display_area(0, 0, self.width, self.height, mode)
            self._wait_for_display_ready()

    def clear(self) -> None:
        """Fill the panel with white pixels."""
        blank = Image.new("L", (self.width, self.height), color=0xFF)
        self.display_image(blank)

    def close(self) -> None:
        """Release GPIO/SPI handles to leave the bus in a clean state."""
        if self._spi_handle is not None:
            try:
                self._lgpio.spi_close(self._spi_handle)
            except Exception:  # pragma: no cover - defensive close
                pass
            self._spi_handle = None
        if self._gpio_handle is not None:
            for pin in list(self._claimed_pins):
                try:
                    self._lgpio.gpio_free(self._gpio_handle, pin)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
            self._claimed_pins.clear()
            try:
                self._lgpio.gpiochip_close(self._gpio_handle)
            except Exception:  # pragma: no cover - defensive close
                pass
            self._gpio_handle = None

    # ------------------------------------------------------------ init helpers
    def _import_lgpio(self):
        try:
            import lgpio  # type: ignore
        except ImportError as exc:
            raise DisplayUnavailable("lgpio module missing; install python3-lgpio") from exc
        return lgpio

    def _initialise(self) -> None:
        try:
            self._setup_gpio()
            self._setup_spi()
            self._reset()
            self._write_command(IT8951_TCON_SYS_RUN)
            self._dev_info = self._read_dev_info()
            if self._dev_info:
                self.width = int(self._dev_info.get("width", self.width))
                self.height = int(self._dev_info.get("height", self.height))
                self._frame_addr = int(self._dev_info.get("memory_addr", 0))
            self._write_register(I80CPCR, 0x0001)  # enable packed writes
            if self._config.vcom_mv is not None:
                self._set_vcom(self._config.vcom_mv)
        except Exception as exc:
            self.close()
            raise DisplayUnavailable(f"failed to initialise IT8591 HAT: {exc}") from exc

    def _setup_gpio(self) -> None:
        chip = self._config.gpio_chip
        handle = self._lgpio.gpiochip_open(chip)
        if handle < 0:
            raise DisplayUnavailable(f"Unable to open GPIO chip {chip}")
        self._gpio_handle = handle
        self._lgpio.gpio_claim_input(self._gpio_handle, self._config.busy_pin)
        self._claimed_pins.add(self._config.busy_pin)
        self._lgpio.gpio_claim_output(self._gpio_handle, self._config.rst_pin, level=1)
        self._claimed_pins.add(self._config.rst_pin)
        self._lgpio.gpio_claim_output(self._gpio_handle, self._config.cs_pin, level=1)
        self._claimed_pins.add(self._config.cs_pin)

    def _setup_spi(self) -> None:
        handle = self._lgpio.spi_open(
            self._config.spi_device,
            self._config.spi_channel,
            self._config.spi_hz,
            0,
        )
        if handle < 0:
            raise DisplayUnavailable(f"Unable to open SPI device {self._config.spi_device}.{self._config.spi_channel}")
        self._spi_handle = handle

    # -------------------------------------------------------------- primitives
    def _reset(self) -> None:
        self._digital_write(self._config.rst_pin, 1)
        time.sleep(0.2)
        self._digital_write(self._config.rst_pin, 0)
        time.sleep(0.01)
        self._digital_write(self._config.rst_pin, 1)
        time.sleep(0.2)

    def _digital_write(self, pin: int, value: int) -> None:
        self._lgpio.gpio_write(self._gpio_handle, pin, value)

    def _digital_read(self, pin: int) -> int:
        return self._lgpio.gpio_read(self._gpio_handle, pin)

    def _spi_write_word(self, value: int) -> None:
        data = bytes([(value >> 8) & 0xFF, value & 0xFF])
        self._lgpio.spi_write(self._spi_handle, data)

    def _spi_read_word(self) -> int:
        _, data = self._lgpio.spi_read(self._spi_handle, 2)
        return (data[0] << 8) | data[1]

    def _wait_ready(self) -> None:
        while self._digital_read(self._config.busy_pin) == 0:
            time.sleep(0.001)

    def _write_command(self, command: int) -> None:
        self._wait_ready()
        self._digital_write(self._config.cs_pin, 0)
        self._spi_write_word(0x6000)
        self._wait_ready()
        self._spi_write_word(command)
        self._digital_write(self._config.cs_pin, 1)

    def _write_data(self, data: int) -> None:
        self._wait_ready()
        self._digital_write(self._config.cs_pin, 0)
        self._spi_write_word(0x0000)
        self._wait_ready()
        self._spi_write_word(data)
        self._digital_write(self._config.cs_pin, 1)

    def _write_multi(self, command: int, values: Sequence[int]) -> None:
        self._write_command(command)
        for value in values:
            self._write_data(value)

    def _read_data(self) -> int:
        self._wait_ready()
        self._digital_write(self._config.cs_pin, 0)
        self._spi_write_word(0x1000)
        self._wait_ready()
        self._spi_read_word()  # dummy
        self._wait_ready()
        result = self._spi_read_word()
        self._digital_write(self._config.cs_pin, 1)
        return result

    def _read_multi_words(self, count: int) -> List[int]:
        self._wait_ready()
        self._digital_write(self._config.cs_pin, 0)
        self._spi_write_word(0x1000)
        self._wait_ready()
        self._spi_read_word()  # dummy
        self._wait_ready()
        words: List[int] = []
        for _ in range(count):
            words.append(self._spi_read_word())
        self._digital_write(self._config.cs_pin, 1)
        return words

    def _write_register(self, address: int, value: int) -> None:
        self._write_command(IT8951_TCON_REG_WR)
        self._write_data(address)
        self._write_data(value)

    def _read_register(self, address: int) -> int:
        self._write_command(IT8951_TCON_REG_RD)
        self._write_data(address)
        return self._read_data()

    # ------------------------------------------------------------- high-level
    def _read_dev_info(self) -> dict[str, int | str]:
        self._write_command(USDEF_I80_CMD_GET_DEV_INFO)
        words = self._read_multi_words(20)
        info = {
            "width": words[0],
            "height": words[1],
            "memory_addr": (words[3] << 16) | words[2],
            "fw_version": "".join(chr(w >> 8) + chr(w & 0xFF) for w in words[4:12]).strip("\x00"),
            "lut_version": "".join(chr(w >> 8) + chr(w & 0xFF) for w in words[12:20]).strip("\x00"),
        }
        self._logger.info(
            "IT8591 panel detected: %sx%s px, FW=%s LUT=%s",
            info["width"],
            info["height"],
            info["fw_version"],
            info["lut_version"],
        )
        return info

    def _set_target_memory(self, addr: int) -> None:
        high = (addr >> 16) & 0xFFFF
        low = addr & 0xFFFF
        self._write_register(LISAR + 2, high)
        self._write_register(LISAR, low)

    def _load_img_area_start(self, pixel_format: int, x: int, y: int, w: int, h: int) -> None:
        args = [
            (IT8951_LDIMG_L_ENDIAN << 8) | (pixel_format << 4) | self._config.rotate,
            x,
            y,
            w,
            h,
        ]
        self._write_multi(IT8951_TCON_LD_IMG_AREA, args)

    def _load_img_end(self) -> None:
        self._write_command(IT8951_TCON_LD_IMG_END)

    def _wait_for_display_ready(self) -> None:
        while self._read_register(LUTAFSR) != 0:
            time.sleep(0.01)

    def _display_area(self, x: int, y: int, w: int, h: int, mode: int) -> None:
        self._write_multi(USDEF_I80_CMD_DPY_AREA, [x, y, w, h, mode])

    def _set_vcom(self, millivolts: int) -> None:
        if millivolts < 0:
            millivolts = abs(millivolts)
        self._write_command(USDEF_I80_CMD_VCOM)
        self._write_data(0x0001)
        self._write_data(millivolts)
        self._logger.info("Configured e-ink VCOM to -%.02fV", millivolts / 1000)

    # ------------------------------------------------------------ frame upload
    def _prepare_frame(self, image: Image.Image) -> bytearray:
        if image.size != (self.width, self.height):
            image = image.resize((self.width, self.height))
        return self._pack_grayscale(image, self.width)

    def _ensure_region_size(self, image: Image.Image, w: int, h: int) -> Image.Image:
        if image.size == (w, h):
            return image
        resized = image.resize((w, h))
        return resized

    def _pack_grayscale(self, image: Image.Image, stride: int) -> bytearray:
        gray = image.convert("L")
        pixels = gray.tobytes()
        packed = bytearray()
        for row_start in range(0, len(pixels), stride):
            row = pixels[row_start : row_start + stride]
            nibble = -1
            current_byte = 0
            for value in row:
                four_bit = value // 17  # map 0-255 -> 0-15
                if nibble == -1:
                    current_byte = four_bit << 4
                    nibble = 0
                else:
                    current_byte |= four_bit & 0x0F
                    packed.append(current_byte)
                    nibble = -1
                    current_byte = 0
            if nibble == 0:
                packed.append(current_byte)
        if len(packed) % 2:
            packed.append(0x00)
        return packed

    def _write_frame_4bpp(self, packed_bytes: bytearray, x: int, y: int, w: int, h: int) -> None:
        self._set_target_memory(self._frame_addr)
        self._load_img_area_start(IT8951_PIXEL_4BPP, x, y, w, h)
        buf = packed_bytes
        data_count = len(buf) // 2
        idx = 0
        self._wait_ready()
        self._digital_write(self._config.cs_pin, 0)
        self._spi_write_word(0x0000)
        self._wait_ready()
        for _ in range(data_count):
            word = (buf[idx] << 8) | buf[idx + 1]
            self._spi_write_word(word)
            idx += 2
        self._digital_write(self._config.cs_pin, 1)
        self._load_img_end()

    def _normalize_bounds(self, bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, w, h = bounds
        x = max(0, min(self.width - 1, x))
        y = max(0, min(self.height - 1, y))
        if x % 2:
            x = max(0, x - 1)
            w += 1
        w = max(1, min(self.width - x, w))
        # align to 4-pixel (2-byte) boundaries for 4bpp writes
        if x % 4:
            shift = x % 4
            x = max(0, x - shift)
            w += shift
        remainder = w % 4
        if remainder:
            pad = 4 - remainder
            if x + w + pad <= self.width:
                w += pad
            else:
                x = max(0, x - pad)
                w = min(self.width - x, w + pad)
        h = max(1, min(self.height - y, h))
        return x, y, w, h

    def _transform_coordinates(self, x: int, y: int, w: int, h: int) -> tuple[int, int]:
        rotate = self._config.rotate
        if rotate == IT8951_ROTATE_0:
            return x, y
        if rotate == IT8951_ROTATE_90:
            hw_x = y
            hw_y = self.width - (x + w)
            return hw_x, hw_y
        if rotate == IT8951_ROTATE_180:
            hw_x = self.width - (x + w)
            hw_y = self.height - (y + h)
            return hw_x, hw_y
        if rotate == IT8951_ROTATE_270:
            hw_x = self.height - (y + h)
            hw_y = x
            return hw_x, hw_y
        return x, y
