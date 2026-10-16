"""Regenerate the Flipper protobuf modules.

Bron: https://github.com/flipperdevices/flipperzero-protobuf (branch dev).
Vastgepinde commit staat in PROTO_COMMIT; werk die bij wanneer je opnieuw genereert.

Gebruik:
    git clone https://github.com/flipperdevices/flipperzero-protobuf /tmp/fzpb
    git -C /tmp/fzpb checkout <PROTO_COMMIT>
    uv run python scripts/gen_proto.py /tmp/fzpb

protoc schrijft `import storage_pb2` (top-level). Dat werkt niet binnen een package,
dus we herschrijven die regels naar relatieve imports.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from grpc_tools import protoc

PROTO_COMMIT = "1c84fa48919cbb71d1cc65236fc0ee36740e24c6"
OUT = Path(__file__).resolve().parent.parent / "src/pitoolbox/core/devices/_flipper_rpc/pb"
_IMPORT_RE = re.compile(r"^import (\w+_pb2) as (\w+)$", re.MULTILINE)
_PYI_IMPORT_RE = re.compile(r"^import (\w+_pb2) as (\w+)$", re.MULTILINE)


def main(src: str) -> int:
    src_dir = Path(src)
    protos = sorted(str(p) for p in src_dir.glob("*.proto"))
    if not protos:
        print(f"geen .proto-bestanden in {src_dir}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    rc = protoc.main(["protoc", f"-I{src_dir}", f"--python_out={OUT}", f"--pyi_out={OUT}", *protos])
    if rc != 0:
        return int(rc)
    for f in list(OUT.glob("*_pb2.py")) + list(OUT.glob("*_pb2.pyi")):
        text = f.read_text(encoding="utf-8")
        text = _IMPORT_RE.sub(r"from . import \1 as \2", text)
        f.write_text(text, encoding="utf-8")
    (OUT / "__init__.py").write_text(
        f'"""Gegenereerd uit flipperzero-protobuf @ {PROTO_COMMIT}. Niet handmatig wijzigen."""\n',
        encoding="utf-8",
    )
    print(f"ok: {len(protos)} proto's -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
