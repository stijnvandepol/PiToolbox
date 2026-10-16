"""De PiToolbox-FAP via RPC op de Flipper zetten (`install_app_file`, alleen voor `ui-install`).

Regels en volgorde: zie "De FAP installeren" in flipper.py.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from pitoolbox.core.devices._fap import FAP_APP_NAME, FAP_MAX_BYTES, inspect_fap
from pitoolbox.core.devices._flipper_app_channel import _APP_PATH_RE, FlipperAppChannel
from pitoolbox.core.devices._flipper_rpc.client import RpcClient, RpcError
from pitoolbox.core.devices._flipper_rpc.pb import flipper_pb2
from pitoolbox.core.devices._flipper_types import AppLocked, _Attempt
from pitoolbox.core.errors import DeviceError

# Loggernaam van flipper.py: alle logregels van FlipperDevice onder één naam.
log = logging.getLogger("pitoolbox.core.devices.flipper")

# Grootte van één blok bij storage_write_request: flipperzero-protobuf `storage.options`
# ("PB_Storage.File.data max_size:512", "not affected by nanopb, so server & client should keep in
# mind these max values"); de officiële client gebruikt hetzelfde (flipper_storage.py rpc_write).
# Eén frame (blok + pad + kop) blijft zo ruim onder RPC_BUFFER_SIZE = 1024 (rpc.h, mntm-012).
STORAGE_CHUNK = 512
# Fouten van storage_mkdir_request die "bestaat al" betekenen (rpc_storage.c: FSE_EXIST).
_MKDIR_EXISTS = "ERROR_STORAGE_EXIST"


@dataclass(frozen=True)
class AppInstall:
    """Resultaat van `install_app_file`."""

    path: str
    size: int
    md5: str
    skipped: bool  # stond er al identiek (zelfde MD5); er is niets geschreven
    seconds: float


ProgressFn = Callable[[int, int], None]


class FlipperInstaller(FlipperAppChannel):
    """Schrijft de FAP in blokken, controleert de MD5 en hernoemt pas daarna."""

    def install_app_file(
        self,
        path: str,
        data: bytes,
        *,
        progress: ProgressFn | None = None,
        skip_if_same: bool = True,
    ) -> AppInstall:
        """Zet de PiToolbox-FAP op `path` (onder /ext/apps, eindigt op .fap).

        Weigert (`AppLocked`) als de gekoppelde app draait of de loader bezet is: een draaiende
        FAP overschrijven is niet veilig, en welke app open is ziet de Pi niet. Volgorde:
        bestaande MD5 vergelijken (`skip_if_same`), mappen maken, schrijven naar `<path>.part`
        in blokken van `STORAGE_CHUNK`, MD5 van de Flipper vergelijken, hernoemen naar `path`
        (storage_common_rename verwijdert een bestaand doel eerst, storage_external_api.c), en
        de MD5 op `path` nog één keer controleren. `progress(bytes_verstuurd, totaal)`.

        Raises:
            ValueError: ongeldig pad, of `data` is geen geldige PiToolbox-FAP.
            AppLocked: er is een app open.
            DeviceError: schrijven of controleren mislukt (de oude app blijft dan staan).
        """
        if not _APP_PATH_RE.fullmatch(path) or ".." in path:
            raise ValueError(f"ongeldig app-pad: {path!r}")
        if not 0 < len(data) <= FAP_MAX_BYTES:
            raise ValueError(f"FAP van {len(data)} bytes; toegestaan 1..{FAP_MAX_BYTES}")
        info = inspect_fap(data)  # FapError is een ValueError
        if info.name != FAP_APP_NAME:
            raise ValueError(f"dit is niet de PiToolbox-app maar {info.name!r}")
        expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
        chunks = [data[i : i + STORAGE_CHUNK] for i in range(0, len(data), STORAGE_CHUNK)]
        part = f"{path}.part"

        def op(gen: int, attempt: _Attempt) -> AppInstall:
            started = time.monotonic()
            if self._rpc_app_state != "off":
                raise AppLocked("de PiToolbox-app draait; sluit hem eerst (Back op de Flipper)")
            client = self._ensure_rpc(gen)
            cancel = self._cancel(gen)
            if self._loader_locked(client):
                raise AppLocked(
                    "er is een app open op de Flipper; sluit die eerst (de Pi overschrijft geen "
                    "app die mogelijk draait, en sluit nooit een app van jou)"
                )
            if skip_if_same and self._md5sum(client, path, cancel) == expected:
                return AppInstall(path, len(data), expected, True, time.monotonic() - started)
            self._mkdir_parents(client, path, cancel)
            attempt.committed = True  # vanaf hier geen stille herhaling na een reconnect
            try:
                self._write_chunks(client, part, chunks, cancel, progress, len(data))
                written = self._md5sum(client, part, cancel)
                if written != expected:
                    raise DeviceError(
                        f"MD5 op de Flipper ({written or 'geen'}) wijkt af van het bestand "
                        f"({expected}); de oude app blijft staan"
                    )
                rename = flipper_pb2.Main()
                rename.storage_rename_request.old_path = part
                rename.storage_rename_request.new_path = path
                client.request(rename, self._timing.rpc_timeout_s, cancel=cancel)
            except BaseException:
                self._delete_quietly(client, part)
                raise
            final = self._md5sum(client, path, cancel)
            if final != expected:
                raise DeviceError(f"MD5 na hernoemen klopt niet ({final or 'geen'})")
            return AppInstall(path, len(data), expected, False, time.monotonic() - started)

        return self._run("install_app_file", op)

    def _md5sum(self, client: RpcClient, path: str, cancel: Callable[[], bool]) -> str | None:
        """MD5 (kleine letters) van een bestand op de Flipper; None als het niet bestaat.
        storage_md5sum_request/-response (storage.proto; md5sum max 32 tekens, storage.options)."""
        main = flipper_pb2.Main()
        main.storage_md5sum_request.path = path
        try:
            frames = client.request(main, self._timing.rpc_timeout_s, cancel=cancel)
        except RpcError as exc:
            if exc.status_name == "ERROR_STORAGE_NOT_EXIST":
                return None
            raise
        return str(frames[0].storage_md5sum_response.md5sum).strip().lower()

    def _mkdir_parents(self, client: RpcClient, path: str, cancel: Callable[[], bool]) -> None:
        """Maak de mappen tussen /ext en het bestand (mkdir maakt één niveau; FSE_EXIST is ok)."""
        parts = path.split("/")[2:-1]  # ["", "ext", "apps", "Tools", "x.fap"] -> apps, Tools
        current = "/ext"
        for name in parts:
            current = f"{current}/{name}"
            main = flipper_pb2.Main()
            main.storage_mkdir_request.path = current
            try:
                client.request(main, self._timing.rpc_timeout_s, cancel=cancel)
            except RpcError as exc:
                if exc.status_name != _MKDIR_EXISTS:
                    raise

    def _write_chunks(
        self,
        client: RpcClient,
        path: str,
        chunks: list[bytes],
        cancel: Callable[[], bool],
        progress: ProgressFn | None,
        total: int,
    ) -> None:
        mains = []
        for chunk in chunks:
            main = flipper_pb2.Main()
            main.storage_write_request.path = path
            main.storage_write_request.file.data = chunk
            mains.append(main)

        def on_chunk(done: int, _count: int) -> None:
            if progress is not None:
                progress(min(done * STORAGE_CHUNK, total), total)

        client.request_chunked(mains, self._timing.rpc_timeout_s, cancel=cancel, on_chunk=on_chunk)

    def _delete_quietly(self, client: RpcClient, path: str) -> None:
        """Opruimen na een mislukte upload; nooit een fout (de link kan al weg zijn)."""
        transport = self._transport
        if transport is None or not transport.is_open or self._client is not client:
            return
        try:
            main = flipper_pb2.Main()
            main.storage_delete_request.path = path
            client.request(main, self._timing.rpc_timeout_s)
        except Exception as exc:  # noqa: BLE001 - opruimen mag de echte fout niet verhullen
            log.debug("%s opruimen: %s", path, exc)
