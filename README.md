# Unofficial Meowant SC10 Integration

A Home Assistant integration for the Meowant SC10 self-cleaning litter box, built on the Tuya Cloud API.

Not affiliated with or endorsed by Meowant or Tuya.

## Why this exists

The SC10 pairs with Home Assistant through the built-in Tuya integration, but that integration does not expose the manual clean cycle, the empty cycle, or the per-visit data the device records. This one does, and adds a few things the device itself gets wrong.

## What you get

**Controls**

- Start, pause, and empty cycle buttons
- A typed confirmation guarding the empty cycle, so an accidental tap cannot dump the litter

**Sensors**

- Current status, clean cycle status, empty cycle status
- Waste bin full
- Visits Today: every cat visit, however brief
- Uses Today: visits over 30 seconds, which is a better proxy for actual use
- Last clean time, both as a timestamp and as elapsed time

**Settings**

Auto clean, delay clean time, sleep mode and its start and end times, child lock, beep, kitten mode, indicator light, deodorizer reminder.

**A dashboard card**

Registered automatically; no HACS frontend resource to configure. Find it in the card picker as "Meowant SC10 Litter Box".

## Helpful additions

**Settings survive a power cycle.** The SC10 forgets several settings when it loses power. This integration remembers what they were and puts them back when it notices the device has restarted.

## Requirements

- Home Assistant 2024.8 or newer
- A Tuya IoT Platform cloud project, which is free
- Your litter box already set up in the Smart Life or Tuya app

## Setting up Tuya

This is the fiddly part. Budget fifteen minutes.

1. Create an account at [iot.tuya.com](https://iot.tuya.com) and log in.
2. Go to **Cloud** > **Development** and click **Create Cloud Project**. Choose the data center matching where your Smart Life account is registered — the app shows this under **Me** > **Settings** > **Account and Security** > **Region**. The wrong data center will fail to authenticate.
3. Once created, note the **Access ID** and **Access Secret** on the project's Overview tab.
4. Go to the **Devices** tab, then **Link App Account**, and scan the QR code with the Smart Life app (**Me** > the scan icon, top right).
5. Still on the **Devices** tab, find your litter box and copy its **Device ID**.
6. On the **Service API** tab, confirm **IoT Core** is subscribed. It is free, but the trial expires periodically and needs renewing — an expired subscription is the most common cause of the integration failing and losing communication with the device.

## Installation

### HACS

1. HACS > three-dot menu > **Custom repositories**
2. Add `https://github.com/Me-Is-Andy/Unofficial-Meowant-SC10-Integration` as an **Integration**
3. Install, then restart Home Assistant

### Manual

Copy `custom_components/meowant_sc10` into your `config/custom_components/` directory and restart.

## Configuration

**Settings** > **Devices & Services** > **Add Integration** > **Meowant SC10**.

Enter the device ID, access ID, access secret, and data center from the Tuya setup above. The integration validates all four before saving, so an error at this point means one of them is wrong rather than something failing later.

## Notes and limitations

**Polling.** The Tuya Cloud API has no push mechanism, so state is polled every 30 seconds. A visit shorter than that is still counted, because visits are read from a record the device writes rather than from live state, but Home Assistant will not see the cat enter in real time.

**API quota.** Roughly 3,600 calls a day at the default interval. Tuya's free tier is generous enough for one device, but several devices on one cloud project will add up.

**Restart detection is heuristic.** A power cycle is detected by several settings changing in the same poll. Changing three or more settings by hand in the vendor app inside 30 seconds would be misread as a restart, and your saved values would be reverted.

**One firmware.** The datapoint map was derived from one SC10. Other units very likely match, but a different firmware could differ, and the labels for the error bitmaps are inferred from Tuya's datapoint specification rather than observed.

## Troubleshooting

Turn on debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.meowant_sc10: debug
```

**Entities unavailable, log shows `1010` or `token invalid`.** The credentials were rejected. Usually an expired IoT Core subscription, occasionally a rotated Access Secret.

**Entities unavailable, log shows `1004` or `sign invalid`.** The Access Secret is wrong, or the data center does not match the account.

**The card does not appear in the picker.** Hard-refresh the browser (Ctrl+Shift+R). If that fails, clear the site's service worker under the browser's developer tools, Application tab.

## Credits

Built by trial and error after the Tuya integration didn't do what I wanted. Claude was used in the making of this integration.

## License

MIT
