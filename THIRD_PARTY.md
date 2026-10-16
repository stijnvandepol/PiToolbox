# Bronnen van derden

| Onderdeel | Bron | Versie | Licentie | Gebruik |
|---|---|---|---|---|
| Flipper protobuf-schema's | [flipperdevices/flipperzero-protobuf](https://github.com/flipperdevices/flipperzero-protobuf) | `1c84fa48` (dev, 2024-12-17) | Geen LICENSE-bestand in de repo | Python-code gegenereerd met `scripts/gen_proto.py` naar `src/pitoolbox/core/devices/_flipper_rpc/pb/` |
| RPC-framing en sessie-handshake | [busse/flipperzero-mcp](https://github.com/busse/flipperzero-mcp) | `36948b7f` | MIT | Ideeën overgenomen (nanopb-delimited framing, `start_rpc_session\r` met alleen CR, ping-probe). Herschreven, geen code letterlijk gekopieerd. |
| CLI-commando's en uitvoerformaten | [flipperdevices/flipperzero-firmware](https://github.com/flipperdevices/flipperzero-firmware) | `3fb1d517` (dev, 2026-10-13, na 1.5.1) | GPL-3.0 | Alleen gelezen als referentie voor de commando's en hun `printf`-formaten. Geen code overgenomen. |

Let op: de protobuf-repo heeft geen expliciete licentie. Voor eigen labgebruik is dat geen probleem. Ga je pitoolbox publiek verspreiden, check dit dan eerst.
