# Fase 2: pitoolbox bereikbaar vanuit de Claude-app

Van een werkende fase-1-Pi naar een Claude-connector die ook op je telefoon werkt, via een Cloudflare Tunnel op je eigen domein.

```
Claude-app / claude.ai ──https──▶ Cloudflare ◀──uitgaand── cloudflared (Pi) ──▶ 127.0.0.1:8765 (pitoolbox-mcp)
        ▲                                                                              │
        └── inloggen (wachtwoord + TOTP) in je browser op https://toolbox.example.nl/login ◀┘
```

- De MCP-server luistert **alleen op loopback**. Er staat geen poort open op de Pi; de tunnel maakt een uitgaande verbinding naar Cloudflare en is de enige ingang.
- De MCP-server is zelf ook de OAuth 2.1-autorisatieserver (wachtwoord + TOTP, dynamic client registration, PKCE). Er is geen derde partij voor inloggen.

> **Placeholders.** In dit document en in `deploy/` staat overal **`toolbox.example.nl`** (jouw hostnaam) en **`<TUNNEL-UUID>`** (die krijg je van `cloudflared tunnel create`). Vervang ze. Zie onderaan voor de lijst met punten die niet uit een officiële bron te controleren waren: ze zijn in de tekst gemarkeerd met *(niet geverifieerd)*.

## 0. Wat je nodig hebt

- Een werkende fase-1-installatie op de Pi (Kali ARM64): Flipper en Alfa werken lokaal.
- Een domein waarvan de DNS in je **Cloudflare-account** staat. `cloudflared tunnel login` laat je een zone uit je account kiezen en `route dns` maakt daarin het DNS-record *(dat het domein hiervoor eerst bij Cloudflare moet staan, staat niet expliciet in de gelezen pagina's; niet geverifieerd)*.
- Een authenticator-app (elke app die TOTP met SHA1, 6 cijfers, 30 s ondersteunt).
- Toegang tot de Pi per SSH en een browser op een ander apparaat (voor `cloudflared tunnel login`).
- Een Claude-account. Een custom connector kan op Free (maximaal één), Pro, Max, Team en Enterprise. Bij Team/Enterprise voegt een Owner hem toe ([Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted)).

## 1. cloudflared installeren (officiële apt-repo)

Volgens [pkg.cloudflare.com](https://pkg.cloudflare.com/index.html):

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install cloudflared
cloudflared --version
```

Die pagina noemt `any` als aanbevolen waarde voor elke Debian-afgeleide distro (Kali is er een), maar zegt niets over CPU-architecturen: of de repo `arm64`-pakketten bevat is niet uit die pagina te halen *(niet geverifieerd)*. Werkt `apt` niet op de Pi, gebruik dan het officiële arm64-pakket uit de GitHub-release (`cloudflared-linux-arm64.deb` staat in de lijst van de laatste release, gecontroleerd; de [downloadpagina van Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) linkt ernaar):

```bash
# Download cloudflared-linux-arm64.deb van https://github.com/cloudflare/cloudflared/releases/latest
sudo apt install ./cloudflared-linux-arm64.deb
```

Gebruik **niet** `cloudflared service install`: pitoolbox levert een eigen, aangescherpte unit (`cloudflared-pitoolbox.service`).

## 2. Tunnel aanmaken en DNS koppelen

Commando's uit de [officiële handleiding voor lokale tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/create-local-tunnel/). Doe dit als je gewone gebruiker op de Pi.

```bash
cloudflared tunnel login
```

Dit toont een URL. Open die in een browser (mag op een ander apparaat), kies je domein en autoriseer. Daarna staat `cert.pem` in de standaardmap van cloudflared (`~/.cloudflared/`).

```bash
cloudflared tunnel create pitoolbox
```

Dit schrijft een credentials-bestand `~/.cloudflared/<TUNNEL-UUID>.json` en toont de UUID. **Noteer de UUID.** Het JSON-bestand is het geheim van je tunnel: nooit in git, nooit delen.

```bash
cloudflared tunnel list                                    # controle
cloudflared tunnel route dns pitoolbox toolbox.example.nl  # maakt een CNAME naar <UUID>.cfargotunnel.com
```

`route dns` faalt als er al een DNS-record met die naam bestaat; verwijder dat dan eerst in het Cloudflare-dashboard.

Het `cert.pem` is alleen nodig voor beheeropdrachten (`create`, `route dns`, ...), niet om de tunnel te draaien (de Cloudflare-handleiding laat `run` alleen op de tunnelnaam/UUID en het credentials-bestand steunen). Bewaar het niet onnodig op de Pi; welke rechten het precies geeft is niet gecontroleerd *(niet geverifieerd)*.

## 3. pitoolbox installeren (install.sh)

Vanuit de checkout van de repo:

```bash
sudo deploy/install.sh
```

Het script is idempotent en doet niets met cloudflared-credentials. Het:

1. maakt de systeemgebruiker `pitoolbox` (in `dialout` voor de Flipper, `video` voor `vcgencmd` en `bluetooth` voor de Flipper via BLE) en `cloudflared`;
2. maakt `/opt/pitoolbox` (code), `/etc/pitoolbox` (0750), `/var/lib/pitoolbox` (0700: auth-database, audit-log, captures) en `/etc/cloudflared`;
3. kopieert de code, installeert indien nodig `uv` voor de gebruiker `pitoolbox` (het script toont eerst wat het doet en de SHA-256 van het officiële installatiescript van astral.sh) en draait `uv sync --frozen --no-dev`;
4. maakt `/etc/pitoolbox/pitoolbox.env` (0600, eigenaar `pitoolbox`) en `/etc/pitoolbox/scope.yaml` uit de voorbeelden, **alleen als ze nog niet bestaan**;
5. zet de twee systemd-units neer en doet `daemon-reload`. Er wordt **niets geënabled of gestart**.

Systeempakketten (indien nog niet gedaan in fase 1): `sudo apt install iw iproute2 aircrack-ng libraspberrypi-bin`.

### 3a. Tunnelconfig en credentials neerleggen

```bash
sudo cp /etc/cloudflared/pitoolbox.yml.example /etc/cloudflared/pitoolbox.yml
sudoedit /etc/cloudflared/pitoolbox.yml        # <TUNNEL-UUID> (2x) en toolbox.example.nl (2x) invullen
sudo install -o root -g cloudflared -m 0640 ~/.cloudflared/<TUNNEL-UUID>.json /etc/cloudflared/
```

De ingress-regels in [`deploy/cloudflared/config.yml`](../deploy/cloudflared/config.yml) sturen alleen `/mcp`, `/authorize`, `/token`, `/register`, `/revoke`, `/login` en `/.well-known/*` door naar `http://127.0.0.1:8765`; al het andere krijgt 404. `path:` is volgens de Cloudflare-docs een reguliere expressie op het pad. Controleer je config en de routering zonder iets te starten (de subcommando's `ingress validate` en `ingress rule <URL>` staan in de [documentatie van het configuratiebestand](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/configuration-file/); of `--config` na `tunnel` ook voor deze subcommando's werkt is niet apart gecontroleerd *(niet geverifieerd)*):

```bash
cloudflared tunnel --config /etc/cloudflared/pitoolbox.yml ingress validate
cloudflared tunnel --config /etc/cloudflared/pitoolbox.yml ingress rule https://toolbox.example.nl/mcp      # verwacht: de regel met 127.0.0.1:8765
cloudflared tunnel --config /etc/cloudflared/pitoolbox.yml ingress rule https://toolbox.example.nl/anders   # verwacht: http_status:404
```

### 3b. Configuratie van pitoolbox

```bash
sudoedit /etc/pitoolbox/pitoolbox.env     # PITOOLBOX_PUBLIC_URL=https://toolbox.example.nl
sudoedit /etc/pitoolbox/scope.yaml        # je eigen BSSID's en IR-bestanden
ls -l /etc/pitoolbox/                     # pitoolbox.env: -rw------- pitoolbox pitoolbox
```

`PITOOLBOX_PUBLIC_URL` is **zonder pad en zonder slash** aan het eind. De MCP-resource is `<PUBLIC_URL>/mcp`; de server staat alleen requests toe met precies die hostnaam in de `Host`-header. Zonder deze variabele weigert de server in HTTP-modus te starten.

## 4. Wachtwoord en TOTP instellen (`pitoolbox-auth init`)

Draai dit als gebruiker `pitoolbox`, met dezelfde omgeving als de service, zodat de database (`PITOOLBOX_AUTH_DB`, standaard `/var/lib/pitoolbox/auth.sqlite`) de juiste eigenaar krijgt:

```bash
sudo -u pitoolbox bash -c 'set -a; . /etc/pitoolbox/pitoolbox.env; set +a; exec /opt/pitoolbox/.venv/bin/pitoolbox-auth init'
```

1. Kies een wachtwoord (minimaal 12 tekens, twee keer invoeren). Gebruik een wachtwoordmanager: dit wachtwoord beschermt de toegang tot je Flipper en wifi-adapter.
2. De CLI toont een `otpauth://`-URI en het geheim. Voer het geheim in je authenticator-app in (type: tijdgebaseerd/TOTP, SHA1, 6 cijfers, 30 s).
3. Typ de huidige 6-cijferige code ter bevestiging. Pas daarna wordt alles opgeslagen.

Overige subcommando's: `pitoolbox-auth status` (aantallen en blokkade), `pitoolbox-auth reset-totp` (nieuw TOTP-geheim) en `pitoolbox-auth revoke-all` (zie de beveiligingsnotities). Draai ze op dezelfde manier als hierboven.

## 5. Starten

Eerst alleen de MCP-server, dan lokaal testen, dan pas de tunnel:

```bash
sudo systemctl enable --now pitoolbox-mcp
journalctl -u pitoolbox-mcp -n 30 --no-pager
curl -i http://127.0.0.1:8765/mcp          # verwacht: 401 Unauthorized + WWW-Authenticate
sudo systemctl enable --now cloudflared-pitoolbox
journalctl -u cloudflared-pitoolbox -n 30 --no-pager
```

Controleer ook de rechten van het proces. `CAP_NET_ADMIN` is capability 12 en `CAP_NET_RAW` is 13, dus verwacht `CapAmb: 0000000000003000`:

```bash
pid=$(systemctl show -p MainPID --value pitoolbox-mcp)
sudo grep -E '^Cap(Amb|Bnd)' /proc/$pid/status
sudo systemd-analyze security pitoolbox-mcp --no-pager | tail -n 3     # hardening-score ter info
```

Het samenspel van `NoNewPrivileges=yes` en ambient capabilities voor kindprocessen (`iw`, `airodump-ng`) is uit de man-pages af te leiden maar niet expliciet beschreven *(niet geverifieerd op de Pi)*: test daarom `wifi_scan` en `alfa_set_mode` zodra de service draait. Zie "Problemen oplossen".

## 6. Controleren met curl (van buiten je netwerk)

Doe deze tests vanaf een apparaat dat **niet** op je thuisnetwerk zit (telefoon via mobiel, of een andere locatie), anders test je soms een lokaal DNS-pad.

```bash
# 6.1 Geen redirect en een 401 met resource_metadata:
curl -i https://toolbox.example.nl/mcp
```

Verwacht:

```
HTTP/2 401
www-authenticate: Bearer ... resource_metadata="https://toolbox.example.nl/.well-known/oauth-protected-resource/mcp"
```

Claude start de inlogflow alleen op een **401** (een `WWW-Authenticate` op een 200 negeert Claude) en gebruikt de `resource_metadata`-verwijzing om je autorisatieserver te vinden ([Authentication for connectors](https://claude.com/docs/connectors/building/authentication#serve-discovery-metadata)). Een 3xx naar een andere host is fout: Claude laat bij zo'n redirect de `Authorization`-header vallen ([Troubleshoot your connector](https://claude.com/docs/connectors/building/troubleshooting#your-server-url-redirects-to-a-different-host)).

```bash
# 6.2 Discovery-metadata (JSON, status 200):
curl -s https://toolbox.example.nl/.well-known/oauth-protected-resource/mcp | python3 -m json.tool
curl -s https://toolbox.example.nl/.well-known/oauth-authorization-server | python3 -m json.tool
```

In de eerste moet `resource` **exact** `https://toolbox.example.nl/mcp` zijn en `authorization_servers` jouw eigen issuer (Claude gebruikt alleen het eerste item). In de tweede verwacht je o.a. `issuer`, `authorization_endpoint`, `token_endpoint`, `registration_endpoint` en `"code_challenge_methods_supported": ["S256"]`. Dat zijn de eisen van Claude (zelfde pagina als hierboven).

```bash
# 6.3 Alles wat niet in de ingress staat is 404:
curl -i https://toolbox.example.nl/iets-anders
# 6.4 Host-header-check lokaal (op de Pi): een onbekende hostnaam moet geweigerd worden:
curl -i -H 'Host: kwaad.example' http://127.0.0.1:8765/mcp     # verwacht: 421 Invalid Host header
```

De MCP-SDK antwoordt 421 bij een ongeldige `Host`, 403 bij een ongeldige `Origin` en 400 bij een ongeldige `Content-Type` (gecontroleerd in `mcp/server/transport_security.py` van de geïnstalleerde SDK).

## 7. Testen met de MCP Inspector (op je laptop)

De [MCP Inspector](https://github.com/modelcontextprotocol/inspector) heeft Node 22.19 of nieuwer nodig. Draai hem op je laptop, niet op de Pi: de OAuth-callback gaat naar een loopback-adres en je browser moet er bij kunnen ([Inspector: Authorization](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector/authorization)).

> **Eerst tijdelijk de Inspector-callback toestaan.** De autorisatieserver accepteert standaard **alleen** `https://claude.ai/api/mcp/auth_callback` en `https://claude.com/api/mcp/auth_callback`; de loopback-callback van de Inspector wordt bij registratie geweigerd (`invalid_redirect_uri`). Zet voor de test in `/etc/pitoolbox/pitoolbox.env`:
>
> ```bash
> PITOOLBOX_EXTRA_REDIRECT_URIS=http://127.0.0.1:6274/oauth/callback,http://127.0.0.1:6276/oauth/callback
> ```
>
> en `sudo systemctl restart pitoolbox-mcp`. **Na het testen** de regel weer weghalen, herstarten en `sudo -u pitoolbox /opt/pitoolbox/.venv/bin/pitoolbox-auth revoke-all` draaien. De server logt een waarschuwing zolang extra redirect-URI's actief zijn.

**Webinterface:**

```bash
npx @modelcontextprotocol/inspector --server-url https://toolbox.example.nl/mcp --transport http
```

Open de URL met token die de Inspector print (niet zelf `localhost:6274` intypen). De Inspector krijgt een 401, haalt de metadata op, registreert zichzelf (DCR) en opent de autorisatie-URL in je browser. Log in op de pitoolbox-loginpagina met wachtwoord + TOTP. Daarna zie je het tabblad **Tools** met de pitoolbox-tools; roep `status` aan. De webcallback is standaard `http://127.0.0.1:6274/oauth/callback`.

**CLI (scriptbaar):**

```bash
npx @modelcontextprotocol/inspector --cli https://toolbox.example.nl/mcp --transport http --method tools/list
```

De CLI start zelf de OAuth-flow (callback `http://127.0.0.1:6276/oauth/callback`) en bewaart tokens in de sleutelhanger van je OS, of anders onbeveiligd in `~/.mcp-inspector/secrets.json`. Wis ze na het testen met `--relogin`/"Clear OAuth state". De loopback-callback werkt alleen met de tijdelijke `PITOOLBOX_EXTRA_REDIRECT_URIS` hierboven; de loginpagina toont dan `127.0.0.1` als bestemming.

Werkt de Inspector wel maar Claude niet (of andersom)? Zie "Problemen oplossen".

## 8. De connector toevoegen in Claude

Volgens [Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted) en [Get started with connectors](https://claude.com/docs/connectors/getting-started). De menu-namen verschillen per versie van de app; in de huidige docs heet het **Customize > Connectors** (oudere versies: Settings > Connectors).

1. Ga op **claude.ai** (webbrowser) naar **Customize > Connectors** en kies **Add custom connector**. Op Team/Enterprise doet een Owner dit onder *Organization settings > Connectors* (Add > Custom, type Web) en verbindt elk lid daarna zelf via Customize > Connectors > Connect.
2. **MCP server URL:** `https://toolbox.example.nl/mcp` (HTTPS, precies de waarde van `PITOOLBOX_PUBLIC_URL` plus `/mcp`, zonder slash aan het eind).
3. **Authentication:** *Sign in now*. **OAuth client:** kies *Register automatically* (Dynamic Client Registration), want dat ondersteunt pitoolbox. *Use Claude's published identity* (Client ID Metadata Document) alleen als de autorisatieserver dat adverteert *(niet geverifieerd voor pitoolbox; Claude valt zonder CIMD-advertentie terug op DCR)*. Laat het OAuth-secret leeg. Wijzigen van de authenticatie-instellingen kan niet achteraf: verwijder de connector en voeg hem opnieuw toe.
4. Klik **Add**. Claude stuurt je naar `https://toolbox.example.nl/login`. Controleer dat de hostnaam van de redirect-URI op de pagina `claude.ai` is, vul wachtwoord en TOTP-code in en keur de toegang goed. De callback die Claude gebruikt is `https://claude.ai/api/mcp/auth_callback`.
5. Terug in Customize > Connectors staat de connector onder **Your connectors** als **Connected**. Open de connector en stel onder **Tool permissions** in wat Claude zonder vragen mag: laat tools die zenden (`ir_send`) of capturen (`capture_start`, `alfa_set_mode`) op **Needs approval** (nooit *Always allow*). Dit is bovenop de eigen `confirm_action`-bevestiging van pitoolbox.
6. In een chat: **+ > Connectors** en zet de pitoolbox-connector aan voor dit gesprek. Vraag bijvoorbeeld "Wat is de status van mijn Pi?" en keur de toolaanroep goed (*Allow once*).

### Op de telefoon

De docs zeggen: "A connected connector is available in your conversations on the web, desktop, and mobile" en dat de mobiele app remote connectors ondersteunt. Voeg de connector dus op de **web** toe (stap 1 t/m 5); in de mobiele app staat hij dan onder **+ > Connectors** in een nieuw gesprek en zet je hem daar per gesprek aan. Of je een custom connector ook rechtstreeks in de mobiele app kunt toevoegen staat niet in de gelezen docs *(niet geverifieerd)*. De inlogpagina van pitoolbox is gewone HTML en werkt in de mobiele browser. De verbinding naar je server loopt vanaf de servers van Anthropic, niet vanaf je telefoon (zie het volgende hoofdstuk).

## 9. Beveiliging

- **Alleen loopback.** `pitoolbox-mcp` weigert te binden op iets anders dan `127.0.0.1`/`::1`. Er is geen poortforward en geen openstaande poort op de router nodig; de tunnel is uitgaand. Controleer met `ss -tlnp | grep 8765` (alleen `127.0.0.1:8765`).
- **Alleen de benodigde paden.** De ingress laat alleen de MCP- en OAuth-routes door; de catch-all geeft 404. Een nieuwe route in de code is dus pas bereikbaar nadat je hem in `/etc/cloudflared/pitoolbox.yml` opneemt.
- **Wachtwoord + TOTP**, na 5 foute pogingen volgt een blokkade van 15 minuten (`pitoolbox-auth status` toont het). Een eenmaal geaccepteerde TOTP-code werkt niet nogmaals.
- **Korte tokens.** Access-tokens gelden 15 minuten, refresh-tokens 7 dagen met rotatie (bij elke vernieuwing komt er een nieuw refresh-token). Claude ververst reactief op een 401 en proactief tot vijf minuten voor het verlopen en verwacht `invalid_grant` bij een ongeldig refresh-token ([token refresh](https://claude.com/docs/connectors/building/authentication#token-refresh)).
- **Alles intrekken:** `sudo -u pitoolbox bash -c 'set -a; . /etc/pitoolbox/pitoolbox.env; set +a; exec /opt/pitoolbox/.venv/bin/pitoolbox-auth revoke-all'` verwijdert alle access- en refresh-tokens én alle geregistreerde clients. Daarna moet elke client opnieuw inloggen (in Claude: **Reconnect**). Doe dit bij elk vermoeden van misbruik, na een gestolen telefoon of na `pitoolbox-auth init --force`. Wil je de service direct uitzetten: `sudo systemctl stop cloudflared-pitoolbox` sluit de tunnel meteen.
- **Eigen scope.** `/etc/pitoolbox/scope.yaml` is eigendom van root en alleen leesbaar voor de service: een gecompromitteerde service of LLM kan zijn eigen scope niet verruimen. Zenden (`transmit`) buiten scope wordt altijd geweigerd en binnen scope vraagt pitoolbox altijd een expliciete `confirm_action`.
- **Hardening van de unit.** Geen root, alleen `CAP_NET_ADMIN`/`CAP_NET_RAW`, alleen `/var/lib/pitoolbox` schrijfbaar, geen `/home`, beperkte adresfamilies. Zie de commentaren in `deploy/systemd/pitoolbox-mcp.service`.
- **Geheimen.** `pitoolbox.env` is `0600` en bevat ook de LLM-API-sleutel; `/etc/cloudflared/<UUID>.json` is `0640 root:cloudflared`. De systemd-man-page raadt omgevingsvariabelen voor geheimen af; `LoadCredential=` is het alternatief voor later.
- **Updates.** `cloudflared` komt via apt (de unit gebruikt `--no-autoupdate`); update met `sudo apt update && sudo apt install --only-upgrade cloudflared` en `sudo systemctl restart cloudflared-pitoolbox`.

### Optioneel: Cloudflare WAF-regel

Anthropic belt je server vanaf een vast IPv4-bereik: **`160.79.104.0/21`** ([IP addresses](https://platform.claude.com/docs/en/api/ip-addresses), "Outbound IP addresses"; dit geldt voor MCP-toolaanroepen). Daarmee kun je de paden die alleen Claude gebruikt dichtzetten voor de rest van het internet, terwijl `/authorize` en `/login` open blijven voor je eigen browser (die komt van je eigen IP-adres). `/.well-known/*` laat je open: de [Claude-docs](https://claude.com/docs/connectors/building/authentication#serve-discovery-metadata) zeggen dat ook discovery-aanvragen van dat bereik komen, het is publieke metadata en een blokkade geeft je alleen extra foutkansen.

Dashboard: **Security rules** > **Create rule** > **Custom rules** ([Create a custom rule](https://developers.cloudflare.com/waf/custom-rules/create-dashboard/)). Gebruik de expression editor met (syntax van `in`, `not`, `eq`, `starts_with()` uit de [Cloudflare-operatorreferentie](https://developers.cloudflare.com/ruleset-engine/rules-language/operators/); veldnaam `http.host` niet apart gecontroleerd *(niet geverifieerd)*):

```txt
(http.host eq "toolbox.example.nl"
 and (http.request.uri.path eq "/mcp"
      or starts_with(http.request.uri.path, "/mcp/")
      or http.request.uri.path eq "/token"
      or http.request.uri.path eq "/register"
      or http.request.uri.path eq "/revoke")
 and not ip.src in {160.79.104.0/21})
```

Actie **Block**, dan **Deploy**.

Let op:

- **Test eerst zonder de regel** (stap 6 t/m 8) en voeg de regel pas toe als alles werkt. Valt de connector erna om, zet de regel dan eerst uit.
- Met deze regel werken de **Inspector** en `curl` vanaf je eigen netwerk op `/mcp`, `/token` en `/register` niet meer. Voeg tijdelijk je eigen IP toe (`ip.src in {160.79.104.0/21 <jouw-ip>}`) of schakel de regel uit tijdens het testen.
- Dat `/register` en `/revoke` ook echt vanaf het Anthropic-bereik komen (en niet vanaf je browser) staat niet expliciet in de docs, alleen dat de OAuth-endpoints van Claude binnen 10 seconden moeten antwoorden *(niet geverifieerd)*. Faalt het toevoegen na de regel, haal dan eerst `/register` en `/revoke` uit de expressie.
- Claude Code draait op je eigen machine en komt dus niet vanaf `160.79.104.0/21`.
- Het bereik kan wijzigen; Anthropic zegt dat de adressen niet zonder aankondiging veranderen. Controleer de pagina af en toe.
- IPv6 staat niet in de lijst van uitgaande adressen; de docs zeggen dat connectors IPv4-only zijn en dat de hostnaam een publiek `A`-record moet hebben (een door Cloudflare geproxied record heeft dat).

## 10. Problemen oplossen

Begin altijd hier:

```bash
systemctl status pitoolbox-mcp cloudflared-pitoolbox --no-pager
journalctl -u pitoolbox-mcp -u cloudflared-pitoolbox -n 100 --no-pager
curl -i https://toolbox.example.nl/mcp        # van buiten je netwerk
```

### "Couldn't reach the MCP server" in Claude

Volgens de [troubleshooting-docs](https://claude.com/docs/connectors/building/troubleshooting#couldn%E2%80%99t-reach-the-mcp-server) kan dit meerdere oorzaken hebben:

1. **Tunnel of service staat uit.** `systemctl status cloudflared-pitoolbox pitoolbox-mcp`. De tunnel moet `Registered tunnel connection` loggen (meldingstekst niet geverifieerd) en `curl -i https://toolbox.example.nl/mcp` moet 401 geven. Een Cloudflare-foutpagina (geen JSON van pitoolbox) betekent meestal dat de tunnel niet verbonden is of de service niet draait; welke foutcode dat precies is niet gecontroleerd *(niet geverifieerd)*.
2. **DNS.** `dig +short toolbox.example.nl` vanaf buiten je netwerk moet publieke adressen geven; geen `10.x`, `192.168.x`, `100.64.x`, `127.x`, en niet alleen `AAAA`. Bestaat het CNAME-record niet: `cloudflared tunnel route dns pitoolbox toolbox.example.nl`.
3. **WAF/bot-regel blokkeert Anthropic.** Zoek `403`/`429`-antwoorden in het Cloudflare-dashboard (Security Events). Zet de WAF-regel uit of vergroot de uitzondering naar `160.79.104.0/21`.
4. **Redirect naar een andere host.** `curl -sI https://toolbox.example.nl/mcp` mag geen `3xx` naar een andere host geven (bijvoorbeeld apex naar `www.`).
5. **Discovery faalt.** Beide `curl`-tests uit stap 6.2 moeten 200 met JSON geven. 404 betekent dat het pad niet in de ingress staat of dat `PITOOLBOX_PUBLIC_URL` niet klopt. Check: `cloudflared tunnel --config /etc/cloudflared/pitoolbox.yml ingress rule https://toolbox.example.nl/.well-known/oauth-authorization-server`.
6. **Trage endpoints.** Claude wacht maximaal 10 s op discovery, registratie en token. Een zwaarbelaste Pi (onderspanning! zie `status`) kan te traag zijn.
7. **Lange tool-aanroepen.** Cloudflare wacht standaard maximaal 125 s op een antwoord van de origin (HTTP 524; alleen Enterprise kan dat verhogen, [Error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)). `subghz_rx` kan tot 120 s duren: houd het onder ~100 s. Hoe lang Claude zelf op een toolresultaat wacht is niet gecontroleerd *(niet geverifieerd)*.

### "Authorization with the MCP server failed"

Meestal na het inloggen. Controleer in deze volgorde ([docs](https://claude.com/docs/connectors/building/troubleshooting#authorization-with-the-mcp-server-failed)):

1. De URL in Claude is exact `PITOOLBOX_PUBLIC_URL` + `/mcp`. Claude stuurt die waarde als `resource` mee en de metadata moet er exact mee overeenkomen. Geen `http://`, geen slash aan het eind, geen andere hostnaam, geen verwijzing naar een host die doorverwijst.
2. De pitoolbox-log (`journalctl -u pitoolbox-mcp`) toont of `/register`, `/authorize`, `/login` en `/token` binnenkwamen. Komt er na `/login` geen `/token`, kijk dan naar de WAF of naar `resource`-fouten. Komt er helemaal niets binnen, dan is het een discovery- of bereikbaarheidsprobleem (hierboven).
3. De login zelf: verkeerd wachtwoord of TOTP-code (zie hieronder), of een blokkade na 5 fouten.
4. Na een `pitoolbox-auth revoke-all` of `init --force` zijn alle clients weg; kies in Claude **Reconnect** of verwijder en voeg de connector opnieuw toe.
5. Lukt het niet en toont de URL een `ofid_…`-ID, meld het dan op [anthropics/claude-ai-mcp](https://github.com/anthropics/claude-ai-mcp/issues) met dat ID.

### 421 / 403 / 400 bij een aanvraag (Host-header-controle)

De server controleert `Host` en `Origin` (bescherming tegen DNS-rebinding). Toegestaan zijn de hostnaam uit `PITOOLBOX_PUBLIC_URL` en `127.0.0.1:<poort>`, `localhost:<poort>`, `[::1]:<poort>`. De MCP-SDK geeft **421** bij een foute `Host`, **403** bij een foute `Origin` en **400** bij een foute `Content-Type`.

- **421 via de tunnel:** `PITOOLBOX_PUBLIC_URL` verschilt van de hostnaam waarmee je aanroept (typefout, `www.`, een tweede hostnaam). Pas `pitoolbox.env` aan en herstart: `sudo systemctl restart pitoolbox-mcp`. Zet **niet** `httpHostHeader` in de tunnelconfig om dit te "omzeilen".
- **421 bij testen op de Pi:** gebruik `http://127.0.0.1:8765/mcp` met de poort erbij, niet een ander IP-adres of een andere hostnaam.
- **403 `Invalid Origin`:** een browserclient van een andere herkomst dan je eigen domein of `claude.ai`/`claude.com`. Claude zelf belt server-side zonder `Origin`.

### TOTP-code wordt afgewezen (klokafwijking)

TOTP hangt af van de kloktijd van de Pi. Een Raspberry Pi heeft geen batterijklok (RTC) *(niet geverifieerd voor jouw model/accessoires)*, dus na een stroomuitval kan de tijd tot de NTP-synchronisatie fout zijn.

```bash
timedatectl                       # "System clock synchronized: yes" en "NTP service: active"?
sudo timedatectl set-ntp true     # NTP aanzetten
date -u                           # vergelijk met je telefoon
```

Een code werkt maar één keer. Wacht op de volgende code als je net een geldige code gebruikte. Na 5 foute pogingen is de login 15 minuten geblokkeerd (`pitoolbox-auth status`). Verloren authenticator: `pitoolbox-auth reset-totp` (vraagt een code van het nieuwe geheim ter bevestiging; het oude blijft geldig tot dat lukt, dus je hebt toegang tot de Pi nodig).

### Tunnel valt uit of start niet

- `journalctl -u cloudflared-pitoolbox -n 50`: `permission denied` op het credentials-bestand of de config betekent dat `cloudflared` het niet kan lezen: `sudo chown root:cloudflared /etc/cloudflared/*; sudo chmod 0640 /etc/cloudflared/*.json /etc/cloudflared/pitoolbox.yml`.
- `error parsing`/`ingress`: draai `ingress validate` (stap 3a). Een ontbrekende catch-all of niet-ingevulde `<TUNNEL-UUID>` is de gewone oorzaak.
- `cloudflared tunnel info pitoolbox` toont of er verbindingen zijn. Een dichtgezette uitgaande firewall (uitgaande verbinding naar Cloudflare op poort 7844) blokkeert de tunnel; `--protocol http2` forceren kan helpen als QUIC (UDP) wordt geblokkeerd. Zie de [run-parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/) (`--protocol`, standaard `auto`; poort 7844 niet geverifieerd).
- De unit start niet door de hardening? Haal tijdelijk `ProtectKernelTunables`, `RestrictAddressFamilies` en `SystemCallArchitectures` weg en kijk wat er in de journal verschijnt. De hardening van deze unit is niet op echte hardware getest *(niet geverifieerd)*.

### pitoolbox-mcp start niet

- `PITOOLBOX_PUBLIC_URL ontbreekt`: stel hem in (zie 3b).
- `weiger te binden op ...`: `PITOOLBOX_HTTP_HOST` moet loopback zijn.
- `Permission denied` op `/var/lib/pitoolbox/...`: de service mag alleen daar schrijven (`ReadWritePaths=`). Staat `PITOOLBOX_AUTH_DB`, `PITOOLBOX_AUDIT_LOG` of `PITOOLBOX_CAPTURES_DIR` ergens anders, verplaats het of voeg het pad aan `ReadWritePaths=` toe. Eigenaar moet `pitoolbox` zijn; na een `pitoolbox-auth init` als root: `sudo chown pitoolbox:pitoolbox /var/lib/pitoolbox/auth.sqlite*`.
- Te vaak mislukt: `systemctl reset-failed pitoolbox-mcp`, daarna `systemctl start pitoolbox-mcp` (de unit staat max. 5 starts per 5 minuten toe).

### Wifi of Flipper werkt niet onder systemd

- **`iw`/`airodump-ng`: operation not permitted.** Controleer `CapAmb: 0000000000003000` (stap 5) en `getcap $(command -v iw airodump-ng ip)`. Heeft een binary bestands-capabilities of een setuid-bit, dan wordt de ambient set daarvoor gewist en `NoNewPrivileges` verhindert het alternatief (zie de commentaren in de unit). Laatste redmiddel: `NoNewPrivileges=no` (met een `systemctl edit`-override), de rest van de hardening blijft staan. Test daarna opnieuw.
- **NetworkManager** zet `wlan1` terug naar managed. `sudo deploy/install.sh --onderweg` zet de Alfa op unmanaged; zie [fase5-onderweg.md](fase5-onderweg.md).
- **Flipper niet gevonden:** `ls -l /dev/ttyACM*` (groep `dialout`), `id pitoolbox` toont `dialout` en `video`, en de Flipper mag niet tegelijk in qFlipper of een terminal open staan.

## Bronnen

- Cloudflare: [configuratiebestand](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/configuration-file/), [lokale tunnel aanmaken](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/create-local-tunnel/), [als service op Linux](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/local-management/as-a-service/linux/), [origin-parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/), [run-parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/), [apt-repo](https://pkg.cloudflare.com/index.html), [custom rules](https://developers.cloudflare.com/waf/custom-rules/create-dashboard/), [error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/).
- Anthropic: [Authentication for connectors](https://claude.com/docs/connectors/building/authentication), [Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom/add-unlisted), [Get started with connectors](https://claude.com/docs/connectors/getting-started), [Troubleshoot your connector](https://claude.com/docs/connectors/building/troubleshooting), [Test your connector](https://claude.com/docs/connectors/building/testing), [IP addresses](https://platform.claude.com/docs/en/api/ip-addresses).
- MCP: [Inspector](https://modelcontextprotocol.io/docs/tools/inspector), [Inspector: Authorization](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector/authorization).
- systemd: [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html), [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html), [systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html), [capabilities(7)](https://man7.org/linux/man-pages/man7/capabilities.7.html), [no_new_privs](https://www.kernel.org/doc/html/latest/userspace-api/no_new_privs.html).
