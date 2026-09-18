# Unofficial Meowant SC10 Integration

A Home Assistant integration for the Meowant SC10 self-cleaning litter box, with local network control or Tuya cloud control.

Not affiliated with or endorsed by Meowant or Tuya.

## Why this exists

The SC10 pairs with Home Assistant through the built-in Tuya integration, but that integration does not expose the manual clean cycle, the empty cycle, or the per-visit data the device records. This one does, and adds a few things the device itself gets wrong.

## Local or Cloud

Local control is the default and is recommended. It talks to the device directly over your network, so updates arrive the moment something happens rather than on a polling interval, it keeps working when your internet does not, and it uses no Tuya API quota.

Cloud control remains available and works the same way from Home Assistant's point of view, polling every 30 seconds.

Either way you need a Tuya IoT Platform cloud project: local control uses it once during setup to read the device's local key. Only the Meowant SC10 (product id `wyvu1hlo3s9weqt9`) is listed for selection; other devices in the same cloud project are filtered out.

## What you get

**Controls**

- Start, pause, and empty cycle buttons
- A typed confirmation guarding the empty cycle, so an accidental tap cannot dump the litter
- A Reset Deodorizer button, with its own separate typed confirmation, for after a cartridge swap

**Sensors**

- Current status, clean cycle status, empty cycle status
- Waste bin full
- Visits Today: every cat visit, however brief
- Uses Today: visits over 30 seconds, which is a better proxy for actual use
- Last clean time, both as a timestamp and as elapsed time
- Deodorizer Percentage: see [Deodorizer percentage tracking](#deodorizer-percentage-tracking) below

**Settings**

Auto clean, delay clean time, sleep mode and its start and end times, child lock, beep, kitten mode, indicator light, deodorizer reminder.

**A dashboard card**

Registered automatically; no HACS frontend resource to configure. Find it in the card picker as "Meowant SC10 Litter Box".

## Helpful additions

**Settings survive a power cycle.** The SC10 forgets several settings when it loses power. This integration remembers what they were and puts them back when it notices the device has restarted.

**Local control survives an IP change.** If the device moves to a different address, the integration finds it again on the network and saves the new one.

## Deodorizer percentage tracking

The percentage shown here is calculated locally, the same way the manufacturer's own app does it, from a stored reference date rather than from the device.

- **At initial setup**, you're asked for the cartridge's current percentage. Leave it blank to start fresh at 100% as of that day, or enter what the Smart Life/Meowant app shows to sync to an already-partial cartridge.
- **After replacing the cartridge**, use the Reset Deodorizer button (type "Reset" into its confirmation box first) to set the counter back to 100% as of today.
- **To recalibrate at any time** without deleting the integration, go to Settings → Devices & Services → Meowant SC10 → Configure, and enter the current percentage from the app.
- The percentage ticks down once per day at local midnight, and is independent of the device's own activation date.

## Requirements

- Home Assistant 2024.8 or newer
- A Tuya IoT Platform cloud project, which is free
- Your litter box already set up in the Smart Life or Tuya app
- For local control: the litter box and Home Assistant on the same network, and a static or reserved IP address for the box

## Setting up Tuya

1. Create an account at [iot.tuya.com](https://iot.tuya.com) and log in.
2. Go to **Cloud** > **Development** and click **Create Cloud Project**. Choose the data center matching where your Smart Life account is registered — the app shows this under **Me** > **Settings** > **Account and Security** > **Region**. The wrong data center will fail to authenticate.
3. Once created, note the **Access ID** and **Access Secret** on the project's Overview tab.
4. Go to the **Devices** tab, then **Link App Account**, and scan the QR code with the Smart Life app (**Me** > the scan icon, top right).
5. On the **Service API** tab, confirm **IoT Core** is subscribed. It is free, but the trial expires periodically and needs renewing — an expired subscription is the most common cause of the integration failing and losing communication with the device.

## Installation

### HACS

1. HACS > three-dot menu > **Custom repositories**
2. Add `https://github.com/Me-Is-Andy/Unofficial-Meowant-SC10-Integration` as an **Integration**
3. Install, then restart Home Assistant

### Manual

Copy `custom_components/meowant_sc10` into your `config/custom_components/` directory and restart.

## Configuration

**Settings** > **Devices & Services** > **Add Integration** > **Meowant SC10**.

Enter the Access ID, Access Secret and data center, and choose local or cloud control. The integration then lists the devices in your cloud project so you can pick the litter box by name — only Meowant SC10 devices appear, even if your cloud project has other Tuya devices linked.

You'll then be asked for the deodorizer cartridge's current percentage (optional — see [Deodorizer percentage tracking](#deodorizer-percentage-tracking)).

For local control there is one more step: confirm the device's address on your network. It is found automatically where possible, and the local key is filled in from the cloud.

To change the deodorizer baseline later, or any of the above, revisit the integration's **Configure** option from Settings → Devices & Services.

## Notes and limitations

**Local control benefits from a fixed address.** Give the litter box a DHCP reservation on your router if you can. The integration recovers if the address changes, but a reservation avoids the interruption.

**Cloud mode polls.** The Tuya Cloud API has no push mechanism, so cloud mode reads state every 30 seconds and uses roughly 3,600 API calls a day. Tuya's free tier is generous enough for one device, but several devices on one cloud project will add up. Local control has neither limitation.

**Restart detection is heuristic.** A power cycle is detected by several settings changing at once. Changing three or more settings by hand in the vendor app within a short window would be misread as a restart, and your saved values would be reverted.

**One firmware.** The datapoint map was derived from one SC10. Other units very likely match, but a different firmware could differ, and the labels for the error bitmaps are inferred from Tuya's datapoint specification rather than observed. The 4%/day deodorizer depletion rate is likewise observed on one unit rather than documented by Tuya.

## Troubleshooting

Turn on debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.meowant_sc10: debug
```

**Setup fails with "Could not reach the device on your network".** Check the IP address is right and that Home Assistant is on the same network as the litter box. If the device was set up recently, try protocol version 3.4 or 3.3.

**Entities unavailable in local mode.** The connection dropped. The log will show reconnection attempts; if the device changed address, the integration scans for it after a few failures.

**Entities unavailable in cloud mode, log shows `1010` or `token invalid`.** The credentials were rejected. Usually an expired IoT Core subscription, occasionally a rotated Access Secret.

**Entities unavailable in cloud mode, log shows `1004` or `sign invalid`.** The Access Secret is wrong, or the data center does not match the account.

**The card does not appear in the picker.** Hard-refresh the browser (Ctrl+Shift+R). If that fails, clear the site's service worker under the browser's developer tools, Application tab.

**Deodorizer Percentage shows Unknown.** The integration hasn't been given a starting point yet. Go to Settings → Devices & Services → Meowant SC10 → Configure, and enter the current percentage from the app.

## Credits

Built by trial and error after the Tuya integration didn't do what I wanted. Claude was used in the making of this integration.

## License

MIT
