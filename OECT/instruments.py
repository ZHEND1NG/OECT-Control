from __future__ import annotations

import math
import random
import socket
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from queue import Queue
from typing import Optional

from .models import InstrumentConfig


class InstrumentError(RuntimeError):
    pass


def _validate_gate_voltage(config: InstrumentConfig, voltage: float) -> float:
    value = float(voltage)
    if not math.isfinite(value):
        raise InstrumentError("Gate voltage must be finite.")
    return value


class BaseInstrument:
    def connect(self) -> None:
        raise NotImplementedError

    def configure(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class SimulatedOECTRig:
    def __init__(self, config: InstrumentConfig):
        self.config = config
        self.gate_v = 0.0
        self.drain_v = 0.0
        self.connected = False
        self.t0 = time.monotonic()

    def connect(self) -> None:
        self.connected = True

    def configure(self) -> None:
        self._require_connected()

    def set_drain_voltage(self, voltage: float) -> None:
        self._require_connected()
        self.drain_v = voltage

    def set_gate_voltage(self, voltage: float) -> None:
        self._require_connected()
        self.gate_v = _validate_gate_voltage(self.config, voltage)

    def read(self) -> tuple[float, float, Optional[float]]:
        self._require_connected()
        t = time.monotonic() - self.t0
        gate_effect = 1.0 / (1.0 + math.exp((self.gate_v + 0.35) * 9.0))
        drift = 1.0 + 0.04 * math.sin(t / 25.0)
        noise = random.uniform(-0.035, 0.035)
        drain_i = abs(self.drain_v) * (3e-4 + 2.5e-3 * gate_effect) * drift * (1.0 + noise)
        if self.drain_v < 0:
            drain_i = -drain_i
        gate_i = 1e-8 * self.gate_v + random.uniform(-2e-9, 2e-9)
        vref = self.gate_v * 0.92 + 0.018 * math.sin(t / 9.0) + random.uniform(-0.003, 0.003)
        return drain_i, gate_i, vref if self.config.use_dmm_vref else None

    def close(self) -> None:
        self.connected = False

    def _require_connected(self) -> None:
        if not self.connected:
            raise InstrumentError("Simulated rig is not connected.")


class VisaInstrument:
    def __init__(self, rm, ip: str, timeout_ms: int, retries: int):
        self.rm = rm
        self.ip = ip
        self.timeout_ms = timeout_ms
        self.retries = retries
        self.inst = None

    @property
    def resource_name(self) -> str:
        return f"TCPIP0::{self.ip}::inst0::INSTR"

    def connect(self) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                self.inst = self.rm.open_resource(self.resource_name)
                self.inst.timeout = self.timeout_ms
                self.inst.read_termination = "\n"
                self.inst.write_termination = "\n"
                self.inst.write("*CLS")
                _ = self.inst.query("*IDN?")
                return
            except Exception as exc:
                last_error = exc
                self.close()
                if attempt < self.retries - 1:
                    time.sleep(2.0)
        raise InstrumentError(f"Could not connect to {self.resource_name}: {last_error}")

    def write(self, command: str) -> None:
        if self.inst is None:
            raise InstrumentError(f"{self.resource_name} is not connected.")
        self.inst.write(command)

    def query(self, command: str) -> str:
        if self.inst is None:
            raise InstrumentError(f"{self.resource_name} is not connected.")
        return self.inst.query(command)

    def close(self) -> None:
        if self.inst is not None:
            try:
                self.inst.close()
            finally:
                self.inst = None


class DMM6500(VisaInstrument):
    def __init__(self, rm, config: InstrumentConfig):
        super().__init__(rm, config.dmm_ip, config.timeout_ms, config.retries)
        self.config = config

    def configure(self) -> None:
        self.write("*RST")
        self.write("*CLS")
        self.write('SENS:FUNC "VOLT:DC"')
        self.write("SENS:VOLT:RANG 1.0")
        self.write(f"SENS:VOLT:NPLC {self.config.voltage_nplc}")
        self.write(f"SENS:VOLT:AZER {self.config.autozero}")
        self.write(":SENS:VOLT:INP MANUAL")
        self.write("SENS:VOLT:INP:IMP 10E6")
        self.write("SENS:VOLT:AVER:STAT OFF")

    def read_voltage(self) -> float:
        return float(self.query("READ?").strip())


class Keithley2450(VisaInstrument):
    def __init__(self, rm, ip: str, config: InstrumentConfig, compliance_a: float):
        super().__init__(rm, ip, config.timeout_ms, config.retries)
        self.config = config
        self.compliance_a = compliance_a
        self.current_voltage = 0.0

    def configure(self) -> None:
        self.write("*RST")
        self.write("*CLS")
        self.write("SOUR:FUNC VOLT")
        self.write(f"SOUR:VOLT:ILIM {self.compliance_a}")
        self.write("SENS:FUNC 'CURR'")
        self.write("SENS:CURR:RANG:AUTO ON")
        self.write(f"SENS:CURR:NPLC {self.config.current_nplc}")
        self.write(f"SYST:AZER {self.config.autozero}")
        self.write("OUTP ON")

    def set_voltage(self, voltage: float) -> None:
        self.write(f"SOUR:VOLT {voltage}")
        self.current_voltage = voltage

    def read_current(self) -> float:
        return float(self.query("READ?").strip().split(",")[0])

    def close(self) -> None:
        if self.inst is not None:
            try:
                self.write("OUTP OFF")
            except Exception:
                pass
        super().close()


class TSPRawSocket:
    def __init__(self, ip: str, timeout_ms: int = 5000):
        self.ip = ip
        self.timeout_s = max(0.5, timeout_ms / 1000.0)
        self.sock: Optional[socket.socket] = None
        self.lock = threading.Lock()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.ip, 5025), timeout=self.timeout_s)
        self.sock.settimeout(self.timeout_s)

    def write(self, command: str) -> None:
        if self.sock is None:
            raise InstrumentError("TSP socket is not connected.")
        with self.lock:
            self.sock.sendall((command.rstrip() + "\n").encode("ascii"))

    def query(self, command: str) -> str:
        if self.sock is None:
            raise InstrumentError("TSP socket is not connected.")
        with self.lock:
            self.sock.sendall((command.rstrip() + "\n").encode("ascii"))
            return self._readline()

    def discard_pending(self) -> None:
        if self.sock is None:
            return
        with self.lock:
            old_timeout = self.sock.gettimeout()
            try:
                self.sock.settimeout(0.02)
                while True:
                    try:
                        data = self.sock.recv(4096)
                    except socket.timeout:
                        break
                    if not data:
                        break
            finally:
                self.sock.settimeout(old_timeout)

    def _readline(self) -> str:
        if self.sock is None:
            raise InstrumentError("TSP socket is not connected.")
        chunks = []
        while True:
            data = self.sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        return b"".join(chunks).decode(errors="replace").strip()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


class DMM6500RawSocket:
    def __init__(self, ip: str, timeout_ms: int = 5000, voltage_nplc: float = 1.0, autozero: str = "ON"):
        self.ip = ip
        self.voltage_nplc = voltage_nplc
        self.autozero = autozero
        self.client = TSPRawSocket(ip, timeout_ms)
        self.connected = False

    def connect(self) -> str:
        self.client.connect()
        self.client.discard_pending()
        idn = self.client.query("*IDN?")
        self.client.discard_pending()
        self.connected = True
        return idn

    def configure(self) -> None:
        self._require_connected()
        self.client.write("*RST")
        self.client.write("*CLS")
        self.client.write('SENS:FUNC "VOLT:DC"')
        self.client.write("SENS:VOLT:RANG 1.0")
        self.client.write(f"SENS:VOLT:NPLC {self.voltage_nplc}")
        self.client.write(f"SENS:VOLT:AZER {self.autozero}")
        self.client.write(":SENS:VOLT:INP MANUAL")
        self.client.write("SENS:VOLT:INP:IMP 10E6")
        self.client.write("SENS:VOLT:AVER:STAT OFF")

    def read_voltage(self) -> float:
        self._require_connected()
        last_response = ""
        for _ in range(3):
            response = self.client.query("READ?").strip()
            last_response = response
            try:
                value = float(response.split(",")[0].strip())
            except ValueError:
                self.client.discard_pending()
                continue
            self.client.discard_pending()
            return value
        raise InstrumentError(f"Could not read numeric DMM voltage. Last response was: {last_response!r}")

    def close(self) -> None:
        self.connected = False
        self.client.close()

    def _require_connected(self) -> None:
        if not self.connected:
            raise InstrumentError("DMM6500 is not connected.")


class Keithley2634BDualRig:
    """Keithley 2634B dual-channel TSP backend.

    Default mapping:
    - smua: drain
    - smub: gate
    """

    VALID_CHANNELS = {"smua", "smub"}

    def __init__(self, config: InstrumentConfig):
        self.config = config
        self.client = TSPRawSocket(config.keithley_2634b_ip, config.timeout_ms)
        self.drain_channel = self._validate_channel(config.drain_channel)
        self.gate_channel = self._validate_channel(config.gate_channel)
        if self.drain_channel == self.gate_channel:
            raise InstrumentError("Drain and gate channels must be different on Keithley 2634B.")
        self.dmm = (
            DMM6500RawSocket(config.dmm_ip, config.timeout_ms, config.voltage_nplc, config.autozero)
            if config.use_dmm_vref
            else None
        )
        self.connected = False

    def connect(self) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(self.config.retries):
            try:
                self.client.connect()
                self.client.discard_pending()
                idn = self.client.query("*IDN?")
                if "2634B" not in idn:
                    raise InstrumentError(f"Connected device is not Keithley 2634B: {idn}")
                self.client.discard_pending()
                if self.dmm is not None:
                    self.dmm.connect()
                self.connected = True
                return
            except Exception as exc:
                last_error = exc
                self.client.close()
                if attempt < self.config.retries - 1:
                    time.sleep(1.0)
        raise InstrumentError(f"Could not connect to Keithley 2634B at {self.config.keithley_2634b_ip}: {last_error}")

    def configure(self) -> None:
        self._require_connected()
        self.client.discard_pending()
        self._configure_channel(self.drain_channel, self.config.drain_compliance_a)
        self._configure_channel(self.gate_channel, self.config.gate_compliance_a)
        if self.dmm is not None:
            self.dmm.configure()
        self.client.discard_pending()

    def set_drain_voltage(self, voltage: float) -> None:
        self._set_voltage(self.drain_channel, voltage)

    def set_gate_voltage(self, voltage: float) -> None:
        self._set_voltage(self.gate_channel, _validate_gate_voltage(self.config, voltage))

    def read(self) -> tuple[float, float, float]:
        self._require_connected()
        drain_i = self._measure_current(self.drain_channel)
        gate_i = self._measure_current(self.gate_channel)
        vref = self.dmm.read_voltage() if self.dmm is not None else None
        return drain_i, gate_i, vref

    def close(self) -> None:
        try:
            if self.connected:
                for channel in (self.gate_channel, self.drain_channel):
                    try:
                        self.client.write(f"{channel}.source.output = {channel}.OUTPUT_OFF")
                    except Exception:
                        pass
        finally:
            self.connected = False
            if self.dmm is not None:
                try:
                    self.dmm.close()
                except Exception:
                    pass
            self.client.close()

    def _configure_channel(self, channel: str, compliance_a: float) -> None:
        self.client.write(f"{channel}.source.output = {channel}.OUTPUT_OFF")
        self.client.write(f"{channel}.source.func = {channel}.OUTPUT_DCVOLTS")
        self.client.write(f"{channel}.source.levelv = 0")
        self.client.write(f"{channel}.source.limiti = {compliance_a}")
        self.client.write(f"{channel}.measure.autorangei = {channel}.AUTORANGE_ON")
        self.client.write(f"{channel}.measure.nplc = {self.config.current_nplc}")
        self.client.write(f"{channel}.source.output = {channel}.OUTPUT_ON")

    def _set_voltage(self, channel: str, voltage: float) -> None:
        self._require_connected()
        self.client.write(f"{channel}.source.levelv = {voltage}")

    def _measure_current(self, channel: str) -> float:
        return self._query_float(f"print({channel}.measure.i())", f"{channel} current")

    def _query_float(self, command: str, label: str) -> float:
        last_response = ""
        attempts = max(2, self.config.retries + 1)
        for _ in range(attempts):
            response = self.client.query(command).strip()
            last_response = response
            try:
                value = float(response.split(",")[0].strip())
            except ValueError:
                self.client.discard_pending()
                continue
            self.client.discard_pending()
            return value
        raise InstrumentError(f"Could not read numeric {label}. Last response was: {last_response!r}")

    def _validate_channel(self, channel: str) -> str:
        normalized = (channel or "").strip().lower()
        if normalized not in self.VALID_CHANNELS:
            raise InstrumentError(f"Invalid 2634B channel: {channel}. Use smua or smub.")
        return normalized

    def _require_connected(self) -> None:
        if not self.connected:
            raise InstrumentError("Keithley 2634B is not connected.")


class ADCMT6541SerialQueue:
    """Run every ADCMT 6541 command on one worker thread."""

    def __init__(self, inst):
        self.inst = inst
        self._queue: Queue = Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._worker,
            name="ADCMT6541SerialWorker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, func):
        if not self._running:
            raise InstrumentError("ADCMT 6541 command queue is not running.")
        future: Future = Future()
        self._queue.put((func, future))
        return future.result()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            func, future = item
            try:
                future.set_result(func())
            except Exception as exc:
                future.set_exception(exc)


class ADCMT6541:
    """ADCMT 6541 four-channel voltage source/current monitor."""

    VALID_CHANNELS = {1, 2, 3, 4}

    def __init__(self, rm, config: InstrumentConfig):
        self.rm = rm
        self.config = config
        self.ip = config.adcmt_6541_ip
        self.inst = None
        self.queue: Optional[ADCMT6541SerialQueue] = None

    @property
    def resource_name(self) -> str:
        return f"TCPIP0::{self.ip}::5025::SOCKET"

    def connect(self) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(self.config.retries):
            try:
                self.inst = self.rm.open_resource(self.resource_name)
                self.inst.timeout = self.config.timeout_ms
                self.inst.read_termination = "\n"
                self.inst.write_termination = "\n"
                time.sleep(0.5)
                self.inst.write("*CLS")
                time.sleep(0.2)
                idn = self.inst.query("*IDN?").strip()
                if "6541" not in idn.upper():
                    raise InstrumentError(f"Connected device is not ADCMT 6541: {idn}")
                self.queue = ADCMT6541SerialQueue(self.inst)
                self.queue.start()
                return idn
            except Exception as exc:
                last_error = exc
                self.close()
                if attempt < self.config.retries - 1:
                    time.sleep(1.0)
        raise InstrumentError(f"Could not connect to ADCMT 6541 at {self.ip}: {last_error}")

    def configure(self) -> None:
        self._require_connected()

        def initialize() -> None:
            # Do not send *RST here. On some ADCMT 6541 LAN setups it can
            # terminate the active raw-socket session. Every channel used by
            # this program is configured explicitly below, so a full reset is
            # unnecessary.
            self.inst.write("*CLS")
            time.sleep(0.1)
            self.inst.write("OH0")
            time.sleep(0.1)

        try:
            self.queue.submit(initialize)
        except Exception as exc:
            raise InstrumentError(f"ADCMT 6541 initialization failed: {exc}") from exc

    def configure_channel(self, channel: int, compliance_a: float) -> None:
        channel = self._validate_channel(channel)

        def configure_one() -> None:
            self.inst.write(f"SCH{channel}")
            time.sleep(0.1)
            self.inst.write("M1")
            self.inst.write("VF")
            self.inst.write("F2")
            self.inst.write("R0")
            self.inst.write(f"SOV0,LMI{compliance_a}")
            time.sleep(0.1)
            self.inst.write("OPR")
            time.sleep(0.2)

        self.queue.submit(configure_one)

    def set_voltage(self, channel: int, voltage: float) -> None:
        channel = self._validate_channel(channel)

        def set_one() -> None:
            self.inst.write(f"SCH{channel}")
            self.inst.write(f"SOV{voltage}")

        self.queue.submit(set_one)

    def read_current(self, channel: int) -> float:
        channel = self._validate_channel(channel)

        def read_one() -> float:
            self.inst.write(f"SCH{channel}")
            self.inst.write("*TRG")
            time.sleep(0.2)
            return float(self.inst.query("MON?").strip())

        return self.queue.submit(read_one)

    def output_off(self, channel: int) -> None:
        channel = self._validate_channel(channel)
        if self.queue is None:
            return

        def off_one() -> None:
            self.inst.write(f"SCH{channel}")
            self.inst.write("SBY")

        self.queue.submit(off_one)

    def close(self) -> None:
        if self.queue is not None:
            self.queue.stop()
            self.queue = None
        if self.inst is not None:
            try:
                self.inst.close()
            finally:
                self.inst = None

    def _validate_channel(self, channel: int) -> int:
        try:
            normalized = int(channel)
        except (TypeError, ValueError) as exc:
            raise InstrumentError(f"Invalid ADCMT 6541 channel: {channel}") from exc
        if normalized not in self.VALID_CHANNELS:
            raise InstrumentError("ADCMT 6541 channel must be 1, 2, 3, or 4.")
        return normalized

    def _require_connected(self) -> None:
        if self.inst is None or self.queue is None:
            raise InstrumentError("ADCMT 6541 is not connected.")


@dataclass
class ADCMT6541Rig:
    config: InstrumentConfig
    rm: object
    source: ADCMT6541
    dmm: Optional[DMM6500RawSocket]

    @classmethod
    def create(cls, config: InstrumentConfig) -> "ADCMT6541Rig":
        try:
            import pyvisa
        except ImportError as exc:
            raise InstrumentError(
                "ADCMT 6541 backend requires pyvisa. Install with: python3 -m pip install pyvisa"
            ) from exc

        drain_channel = int(config.adcmt_drain_channel)
        gate_channel = int(config.adcmt_gate_channel)
        if drain_channel == gate_channel:
            raise InstrumentError("Drain and gate channels must be different on ADCMT 6541.")
        if drain_channel not in ADCMT6541.VALID_CHANNELS or gate_channel not in ADCMT6541.VALID_CHANNELS:
            raise InstrumentError("ADCMT 6541 channels must be selected from 1 to 4.")

        rm = pyvisa.ResourceManager()
        source = ADCMT6541(rm, config)
        dmm = (
            DMM6500RawSocket(config.dmm_ip, config.timeout_ms, config.voltage_nplc, config.autozero)
            if config.use_dmm_vref
            else None
        )
        return cls(config=config, rm=rm, source=source, dmm=dmm)

    def connect(self) -> None:
        try:
            self.source.connect()
        except Exception as exc:
            self.close()
            raise InstrumentError(f"ADCMT 6541 connection failed: {exc}") from exc

        if self.dmm is not None:
            try:
                self.dmm.connect()
            except Exception as exc:
                self.close()
                raise InstrumentError(f"DMM Vref connection failed: {exc}") from exc

    def configure(self) -> None:
        try:
            self.source.configure()
        except Exception as exc:
            raise InstrumentError(f"ADCMT 6541 global configuration failed: {exc}") from exc

        try:
            self.source.configure_channel(
                self.config.adcmt_drain_channel,
                self.config.drain_compliance_a,
            )
        except Exception as exc:
            raise InstrumentError(
                f"ADCMT 6541 Drain channel configuration failed: {exc}"
            ) from exc

        try:
            self.source.configure_channel(
                self.config.adcmt_gate_channel,
                self.config.gate_compliance_a,
            )
        except Exception as exc:
            raise InstrumentError(
                f"ADCMT 6541 Gate channel configuration failed: {exc}"
            ) from exc

        if self.dmm is not None:
            try:
                self.dmm.configure()
            except Exception as exc:
                raise InstrumentError(f"DMM Vref configuration failed: {exc}") from exc

    def set_drain_voltage(self, voltage: float) -> None:
        self.source.set_voltage(self.config.adcmt_drain_channel, voltage)

    def set_gate_voltage(self, voltage: float) -> None:
        self.source.set_voltage(
            self.config.adcmt_gate_channel,
            _validate_gate_voltage(self.config, voltage),
        )

    def read(self) -> tuple[float, float, Optional[float]]:
        try:
            drain_i = self.source.read_current(self.config.adcmt_drain_channel)
        except Exception as exc:
            raise InstrumentError(f"ADCMT 6541 Drain current read failed: {exc}") from exc

        try:
            gate_i = self.source.read_current(self.config.adcmt_gate_channel)
        except Exception as exc:
            raise InstrumentError(f"ADCMT 6541 Gate current read failed: {exc}") from exc

        if self.dmm is None:
            vref = None
        else:
            try:
                vref = self.dmm.read_voltage()
            except Exception as exc:
                raise InstrumentError(f"DMM Vref read failed: {exc}") from exc
        return drain_i, gate_i, vref

    def close(self) -> None:
        for channel in (self.config.adcmt_gate_channel, self.config.adcmt_drain_channel):
            try:
                self.source.output_off(channel)
            except Exception:
                pass
        if self.dmm is not None:
            try:
                self.dmm.close()
            except Exception:
                pass
        try:
            self.source.close()
        finally:
            close_rm = getattr(self.rm, "close", None)
            if callable(close_rm):
                try:
                    close_rm()
                except Exception:
                    pass


@dataclass
class RealOECTRig:
    config: InstrumentConfig
    rm: object
    dmm: Optional[DMM6500]
    drain: Keithley2450
    gate: Keithley2450

    @classmethod
    def create(cls, config: InstrumentConfig) -> "RealOECTRig":
        try:
            import pyvisa
        except ImportError as exc:
            raise InstrumentError("Real backend requires pyvisa. Install with: python3 -m pip install pyvisa") from exc

        rm = pyvisa.ResourceManager()
        dmm = DMM6500(rm, config) if config.use_dmm_vref else None
        drain = Keithley2450(rm, config.drain_smu_ip, config, config.drain_compliance_a)
        gate = Keithley2450(rm, config.gate_smu_ip, config, config.gate_compliance_a)
        return cls(config=config, rm=rm, dmm=dmm, drain=drain, gate=gate)

    def connect(self) -> None:
        if self.dmm is not None:
            self.dmm.connect()
        self.drain.connect()
        self.gate.connect()

    def configure(self) -> None:
        if self.dmm is not None:
            self.dmm.configure()
        self.drain.configure()
        self.gate.configure()

    def set_drain_voltage(self, voltage: float) -> None:
        self.drain.set_voltage(voltage)

    def set_gate_voltage(self, voltage: float) -> None:
        self.gate.set_voltage(_validate_gate_voltage(self.config, voltage))

    def read(self) -> tuple[float, float, Optional[float]]:
        drain_i = self.drain.read_current()
        gate_i = self.gate.read_current()
        vref = self.dmm.read_voltage() if self.dmm is not None else None
        return drain_i, gate_i, vref

    def close(self) -> None:
        for inst in (self.gate, self.drain, self.dmm):
            if inst is None:
                continue
            try:
                inst.close()
            except Exception:
                pass
        close_rm = getattr(self.rm, "close", None)
        if callable(close_rm):
            close_rm()


def create_rig(config: InstrumentConfig):
    if config.backend == "keithley_2634b":
        return Keithley2634BDualRig(config)
    if config.backend == "adcmt_6541":
        return ADCMT6541Rig.create(config)
    if config.backend in {"real", "real_2450_dmm"}:
        return RealOECTRig.create(config)
    return SimulatedOECTRig(config)


def query_scpi_idn(ip: str, timeout_ms: int = 3000) -> str:
    client = TSPRawSocket(ip, timeout_ms)
    try:
        client.connect()
        return client.query("*IDN?")
    finally:
        client.close()


def query_adcmt_6541_idn(ip: str, timeout_ms: int = 3000) -> str:
    try:
        import pyvisa
    except ImportError as exc:
        raise InstrumentError(
            "ADCMT 6541 link check requires pyvisa. Install with: python3 -m pip install pyvisa"
        ) from exc

    rm = pyvisa.ResourceManager()
    inst = None
    try:
        inst = rm.open_resource(f"TCPIP0::{ip}::5025::SOCKET")
        inst.timeout = timeout_ms
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        time.sleep(0.5)
        inst.write("*CLS")
        time.sleep(0.2)
        return inst.query("*IDN?").strip()
    finally:
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass
        close_rm = getattr(rm, "close", None)
        if callable(close_rm):
            try:
                close_rm()
            except Exception:
                pass


def check_instrument_links(config: InstrumentConfig) -> dict:
    result = {
        "source_meter": {"required": True, "ok": False, "message": "not checked"},
        "dmm": {"required": bool(config.use_dmm_vref), "ok": False, "message": "not checked"},
    }

    if config.backend == "simulated":
        result["source_meter"] = {"required": True, "ok": True, "message": "simulated"}
        result["dmm"] = {"required": False, "ok": True, "message": "not required"}
        return result

    if config.backend == "keithley_2634b":
        try:
            idn = query_scpi_idn(config.keithley_2634b_ip, config.timeout_ms)
            result["source_meter"] = {
                "required": True,
                "ok": "2634B" in idn,
                "message": idn,
            }
        except Exception as exc:
            result["source_meter"] = {"required": True, "ok": False, "message": str(exc)}

        if config.use_dmm_vref:
            try:
                idn = query_scpi_idn(config.dmm_ip, config.timeout_ms)
                result["dmm"] = {"required": True, "ok": True, "message": idn}
            except Exception as exc:
                result["dmm"] = {"required": True, "ok": False, "message": str(exc)}
        else:
            result["dmm"] = {"required": False, "ok": True, "message": "optional / disabled"}
        return result

    if config.backend == "adcmt_6541":
        try:
            idn = query_adcmt_6541_idn(config.adcmt_6541_ip, config.timeout_ms)
            result["source_meter"] = {
                "required": True,
                "ok": "6541" in idn.upper(),
                "message": idn,
            }
        except Exception as exc:
            result["source_meter"] = {"required": True, "ok": False, "message": str(exc)}

        if config.use_dmm_vref:
            try:
                idn = query_scpi_idn(config.dmm_ip, config.timeout_ms)
                result["dmm"] = {"required": True, "ok": True, "message": idn}
            except Exception as exc:
                result["dmm"] = {"required": True, "ok": False, "message": str(exc)}
        else:
            result["dmm"] = {"required": False, "ok": True, "message": "optional / disabled"}
        return result

    for key, ip in [
        ("source_meter", config.drain_smu_ip),
        ("dmm", config.dmm_ip),
    ]:
        if key == "dmm" and not config.use_dmm_vref:
            result[key] = {"required": False, "ok": True, "message": "disabled"}
            continue
        try:
            result[key] = {"required": True, "ok": True, "message": query_scpi_idn(ip, config.timeout_ms)}
        except Exception as exc:
            result[key] = {"required": True, "ok": False, "message": str(exc)}
    return result
