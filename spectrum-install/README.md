# Spectrum install bundle

Configures a DX NetOps Spectrum instance to turn `appneta-ia-bridge` traps into alarms. Run it once
per Spectrum instance, using `intelligent_alarms.sh` from the top of this repository.

## Modes

Install exactly one mode per Spectrum instance. Set the destination's
`TRAP_DESTINATION_<N>_SCHEME` on the bridge to the same value.

| Mode | Use when | Traps | Result in Spectrum |
|---|---|---|---|
| `general` | Spectrum has no AppNeta (VNA) path models (the common case) | 8 OIDs, `.126`–`.133` under `apnNotifBranch`: raised and cleared for each severity | One alarm per Critical or Major incident on the `AppNeta Intelligent Alarms` model; Minor and Warning are logged as events only |
| `vna` | Spectrum has `NetworkPath`/`WebPath` models carrying `AppNetaPathIdentifier` (`0x673009f`) | 4 OIDs, `.122`–`.125`: alarm and event tiers, raised and cleared, for every rule | An alarm titled with the rule name, raised on the matching path model; Warning is logged as an event only |

## What the installer changes

| Item | Location (under `$SPECTRUM_HOME`) | Action |
|---|---|---|
| Trap decoding | `SS/CsVendor/SDN_INTEG/AppNetaAdmin/AlertMap` | Appends one marked block |
| Event handling | `SS/CsVendor/SDN_INTEG/AppNetaAdmin/EventDisp` | Appends one marked block |
| Severity map (`vna`) | `SS/CsVendor/SDN_INTEG/SeverityMaps/IntelligentAlarmSeverity` | New file |
| Path-model alarm rules (`vna`) | `SS/CsVendor/SDN_INTEG/NetworkPath/EventDisp`, `.../WebPath/EventDisp` | New files |
| Alarm text | `SG-Support/CsPCause/Prob*_en_US`, `SG-Support/CsEvFormat/Event*_en_US` | New files |
| State record | `SS/CsVendor/SDN_INTEG/AppNetaAdmin/.intelligent-alarms-state` | Records mode and checksums |

Uninstall uses the state record to remove exactly this, and refuses to change anything if
`AlertMap` or `EventDisp` were edited after the install. Installing a different mode first
uninstalls the current one. The installer never touches the MIB database and never restarts
anything; those steps are below.

## Split install: SpectroSERVER and OneClick on separate hosts

Run the installer on both hosts, with the same mode. Each host gets only what it uses:

| Host | Command | Installs |
|---|---|---|
| SpectroSERVER | `./intelligent_alarms.sh --spectroserver --<mode> install` | Everything in the table above |
| OneClick web server | `./intelligent_alarms.sh --oneclick --<mode> install` | `SG-Support/CsPCause/Prob*_en_US` and `SG-Support/CsEvFormat/Event*_en_US` only, plus `SG-Support/.intelligent-alarms-oneclick-state` |

OneClick formats alarm and event text from its own host's `SG-Support`, live. If these files are
missing there, the alarm's probable-cause page says `file Prob... was not found on this server` and
the event log says `...for which no event format file exists`. The logic files (`AlertMap`,
`EventDisp`, severity map, path-model rules) are read by SpectroSERVER only. The MIB compile happens
on the OneClick host, run by `--oneclick install` itself. Create the `AppNetaAdmin` model and restart SpectroSERVER as in the steps below.
`--oneclick status` and `--oneclick uninstall` work the same way as on a single
host. Run the plain `--spectroserver` command on a OneClick-only host and it stops with a pointer to
`--oneclick`.

## Prerequisites

Do all of this as the account that owns the Spectrum installation (the owner of its `SS`
directory; usually `spectrum`, not root). The installer stops and prints the corrected command if
you run it as another account.

1. **Become the owner and find the install directory.**
   ```bash
   ps -eo user,args | grep '[S]pectroSERVER'     # first column: the account SpectroSERVER runs as
   sudo -u spectrum -i
   # Only if spectrum80.env is not at /opt/SPECTRUM/spectrum80.env:
   #   export SPECTRUM_ENV_FILE=/path/to/spectrum80.env
   export SPECTRUM_HOME=$(sed -n 's/^SPECROOT=//p' "${SPECTRUM_ENV_FILE:-/opt/SPECTRUM/spectrum80.env}" | head -1)
   echo "SPECTRUM_HOME=$SPECTRUM_HOME"           # must print your Spectrum directory
   ls -ld "$SPECTRUM_HOME/SS"                    # owner must be the account you are now
   ```
   `intelligent_alarms.sh` finds the same directory itself and prints `Using SPECTRUM_HOME=...`;
   confirm it matches. Export `SPECTRUM_HOME` again in any new shell.
2. **Check file ownership.** Files the installer writes must keep the owner, group and `rw-rw----`
   mode of the existing ones; SpectroSERVER ignores unreadable files without an error.
   ```bash
   ls -la "$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin/"
   ```
3. **Check SpectroSERVER is running and the database is clean.** The `SpectroSERVER` process must
   be running, and `$SPECTRUM_HOME/SS/.VNMDB.LOCK` must be absent or present only while
   SpectroSERVER is running. If the database is dirty, recover it first with your normal procedure
   (Broadcom KB 191684, "How to reload the SpectroSERVER Database (SSdb)"; KB 113595, "Spectrum
   crashed, database was not gracefully closed") — that is a database recovery action; confirm with
   whoever owns the instance before running it.
4. **Take two backups.**
   - Database (online, SpectroSERVER keeps running):
     ```bash
     "$SPECTRUM_HOME/SS-Tools/StartOnlineBackup" -lh <landscape_handle>
     ls -la "$SPECTRUM_HOME"/SS-DB-Backup/*.SSdb.gz | tail -3   # note the new file's name
     ```
     The landscape handle is in `$SPECTRUM_HOME/SS/VNM.OUT` ("loading landscape 0x... at
     precedence ...").
   - The two files the installer appends to:
     ```bash
     for f in AlertMap EventDisp; do
       cp "$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin/$f" \
          "$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG/AppNetaAdmin/$f.pre-appneta-ia-bridge.bak"
     done
     ```

## Step-by-step install

1. **Copy the bundle to the Spectrum server**, keeping the directories side by side:
   ```bash
   scp -r intelligent_alarms.sh lib spectrum-install spectrum-install-general mibs \
       spectrum-host:/tmp/appneta-ia-bridge-install/
   ```
2. **Install**, in the owner-account shell from the prerequisites:
   ```bash
   cd /tmp/appneta-ia-bridge-install
   ./intelligent_alarms.sh --spectroserver --general install     # or: --spectroserver --vna install
   ./intelligent_alarms.sh --spectroserver status                # "Spectrum: general installed"
   ```
3. **Compile the MIBs.** The installer in step 2 already did this; it exits `1` if a MIB fails to
   compile and skips the compile on a host with no `MibDatabase`. To compile by hand:
   `APPNETA-INTELLIGENT-ALARM-MIB.mib` imports `APPNETA-MIB`, and the
   compiler looks for that module as a file next to it, so copy both (a copy of `APPNETA-MIB.mib`
   is included in `mibs/`; use your own newer copy in its place if you have one):
   ```bash
   mkdir -p /tmp/mib-compile
   cp /tmp/appneta-ia-bridge-install/mibs/*.mib /tmp/mib-compile/
   bash "$SPECTRUM_HOME/MibDatabase/scripts/BulkMibCompile.sh" -d /tmp/mib-compile -f "*.mib"
   ```
   Both files must compile with no `MIB failed to compile` line. The tool's exit code is `0` even on
   failure, so check the output.
4. **Restart SpectroSERVER.** The new files are read only at start.
   ```bash
   (cd "$SPECTRUM_HOME/bin" && ./stopSS.pl)
   pgrep -f SpectroSERVER                      # prints nothing once stopped
   (cd "$SPECTRUM_HOME/bin" && ./startSS.pl)
   ```
   If SpectroSERVER runs under a process supervisor with automatic start disabled, run `startSS.pl`
   explicitly. If you later edit an installed `Prob*` or `Event*` file, restart the OneClick web
   application as well as SpectroSERVER.
5. **Point the bridge at this instance.** Set `TRAP_DESTINATION_<N>_HOST`, `_PORT` (default `162`)
   and `_COMMUNITY` to match this Spectrum's trap listener, and `_SCHEME` to the installed mode.
6. **Create the `AppNetaAdmin` model**, once per bridge instance. Spectrum needs it to attribute
   incoming traps.

   In OneClick: **Locator** → **Universe** → **Topology** tab → **Create New Model by Type** →
   `AppNetaAdmin`, then fill in:
   - **Name:** shown on every alarm from this integration, for example `AppNeta Intelligent
     Alarms`. Add a suffix such as ` - <site>` if you run several bridges.
   - **Monitoring Point IP Address:** the address the bridge's traps are attributed to, not
     Spectrum's own address. With the v0.1.3 sender-identity claim, the bridge no longer needs to be
     the real sender of this address: set `TRAP_DESTINATION_<N>_SOURCE_ADDRESS` to this same IP.
     Do not use the IP of another real device already modeled in Spectrum.
   - **Security String:** leave blank.

   Then enable IP-header trap mapping, which is off by default and without which traps are not
   matched to the model: select the new model, open **Component Detail** → **Attributes**, filter
   for attribute ID `0x3dc000b` (`map_traps_to_this_model_using_IP_header`), double-click its value,
   choose **Yes** and **OK**. It takes effect on the next trap without a restart.

   Equivalent REST calls:

   | Field | Attribute ID | Note |
   |---|---|---|
   | Model type handle (`mtypeid`) | `0x6730043` | |
   | Name | `0x1006e` | |
   | `Monitoring Point IP Address` | `0x3dc000f` | Not the standard `Network_Address` attributes |
   | `map_traps_to_this_model_using_IP_header` | `0x3dc000b` | Set to `true` explicitly |

   ```bash
   curl -k -H "Authorization: Bearer <token>" -X POST \
     "https://<spectrum-host>:8080/spectrum/restful/model?landscapeid=<landscape_handle>&mtypeid=0x6730043&attr=0x1006e&val=AppNeta%20Intelligent%20Alarms&attr=0x3dc000f&val=<bridge-ip>"
   curl -k -H "Authorization: Bearer <token>" -X PUT \
     "https://<spectrum-host>:8080/spectrum/restful/model/<new_model_handle>?attr=0x3dc000b&val=true"
   ```
   To remove the model, delete it in OneClick or call `DELETE /spectrum/restful/model?mh=<handle>`.

## Verifying the install

You need a Spectrum REST token for the alarm queries (OneClick **Administration** → **Token
Configure**). The bridge itself never calls Spectrum's REST API. Port `8080` is OneClick's usual
HTTP port; use yours if it differs.

Send test alarms through the bridge, as in step 7 of [`INSTALLATION.md`](../INSTALLATION.md#7-verify),
and work through these layers in order. For a `vna` destination, the test payload must name a real
path (`"itemType"` and `"itemId"` from your AppNeta organization); otherwise no path model matches and
layers 1 and 2 pass but no alarm is raised.

1. **The trap arrives.** On the Spectrum server, capture while you send a test alarm:
   ```bash
   sudo tcpdump -i any -n udp port 162 -w /tmp/trap-capture.pcap &
   # send the test alarm now
   sleep 2; kill %1
   tcpdump -r /tmp/trap-capture.pcap -n -v | head -20
   ```
   You should see one UDP packet from the bridge host with the trap's varbinds decoded. If not,
   check `docker logs appneta-ia-bridge` and the network path.
2. **An alarm exists at the right severity.**
   ```bash
   curl -H "Authorization: Bearer <token>" -H "Accept: application/json" \
     "http://<spectrum-host>:8080/spectrum/restful/alarms?throttlesize=5"
   ```
   Look for the `alarmId` you sent. If the trap arrived but there is no alarm, re-check the
   `AppNetaAdmin` model: a missing model, the wrong Monitoring Point IP Address, or
   `map_traps_to_this_model_using_IP_header` left at `No` is the usual cause.
3. **Warning severity creates no alarm.** Send `"alarmSeverity":"WARNING"` with a new `alarmId`; it
   must not appear in the alarm list (an event is logged).
4. **Cleared resolves the alarm.** Send the same `alarmId` with `"state":"CLEARED"`; the alarm from
   step 2 clears and no second alarm appears.

## Backout plan

Use the lightest level that fixes the problem.

**Level 1: remove the files (one SpectroSERVER restart).**
```bash
/tmp/appneta-ia-bridge-install/intelligent_alarms.sh --spectroserver uninstall
```
The dispatcher removes exactly what it installed. If it refuses because `AlertMap` or `EventDisp`
were edited, or the state record is missing, restore manually:
```bash
D="$SPECTRUM_HOME/SS/CsVendor/SDN_INTEG"
mv "$D/AppNetaAdmin/AlertMap.pre-appneta-ia-bridge.bak"  "$D/AppNetaAdmin/AlertMap"
mv "$D/AppNetaAdmin/EventDisp.pre-appneta-ia-bridge.bak" "$D/AppNetaAdmin/EventDisp"
rm -f "$D/SeverityMaps/IntelligentAlarmSeverity" "$D/NetworkPath/EventDisp" "$D/WebPath/EventDisp"
rm -f "$SPECTRUM_HOME"/SG-Support/CsPCause/Prob067321[5-8]*_en_US
rm -f "$SPECTRUM_HOME"/SG-Support/CsEvFormat/Event067321[5-8]*_en_US
(cd "$SPECTRUM_HOME/bin" && ./stopSS.pl && ./startSS.pl)
```
This does not undo the MIB compile; the compiled MIB stays in the database but is inert once the
`AlertMap` and `EventDisp` entries are gone.

**Level 2: full database restore (last resort).** Use it only if the MIB compile itself damaged
state. It returns Spectrum to the moment of the backup in prerequisite 4, discarding any model or
topology changes made since.
```bash
(cd "$SPECTRUM_HOME/bin" && ./stopSS.pl)                       # pkill -9 SpectroSERVER only if it hangs
"$SPECTRUM_HOME/lib/SDPM/processd" --stop                      # mandatory before SSdbload
rm -f "$SPECTRUM_HOME/SS/.VNMDB.LOCK" "$SPECTRUM_HOME/SS/DDM/.DDMDB.LOCK"
gunzip -k "$SPECTRUM_HOME/SS-DB-Backup/<backup-file>.SSdb.gz"
cd "$SPECTRUM_HOME/SS" && echo Y | "$SPECTRUM_HOME/SS-Tools/SSdbload" -il \
  "$SPECTRUM_HOME/SS-DB-Backup/<backup-file>.SSdb"             # must run from $SPECTRUM_HOME/SS
"$SPECTRUM_HOME/lib/SDPM/processd" --start
(cd "$SPECTRUM_HOME/bin" && ./startSS.pl)
```
Run `SSdbload -version <file>` first to confirm the backup matches your Spectrum version; a
mismatch fails with `SPC-DBS-23007` and must not be forced.

## Events list shows "Error processing event"

Spectrum writes its own generic `0x3dc0000` "Error processing event" line immediately before every
event on an `AppNetaAdmin` model, successful or not. It is not an error and cannot be edited. To hide
it, save an Events-list filter in OneClick that excludes `Type = 0x3dc0000` or includes only types
starting `0x67321`.

## Checking the OIDs

After any change to these files, confirm the bridge, the MIB and the fragments agree:

```bash
python3 -c "
from alarm_bridge.mapping import (OID, TRAP_OID_ALARM_RAISED, TRAP_OID_ALARM_CLEARED,
                                  TRAP_OID_EVENT_RAISED, TRAP_OID_EVENT_CLEARED)
for name in ('alarmId', 'description', 'tags', 'itemId32', 'monitoringPointId32',
             'alarmSeverityLvl', 'fieldNineTarget'):
    print(name, OID[name])
print(TRAP_OID_ALARM_RAISED, TRAP_OID_ALARM_CLEARED, TRAP_OID_EVENT_RAISED, TRAP_OID_EVENT_CLEARED)
"
```

Compare with `apnNotifBranch` in `mibs/APPNETA-INTELLIGENT-ALARM-MIB.mib` and the trap OIDs in
`AlertMap.appneta-ia.fragment`. Varbind indices in the fragments start at `300` in declaration
order, so they do not equal the OID's last number (for example `alarmSeverityLvl`, `.23`, is index
`322`).
