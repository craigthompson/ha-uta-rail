# UTA Rail for Home Assistant

Live positions for Utah Transit Authority FrontRunner and TRAX (Blue, Red and Green Line) trains.

UTA's realtime feed is binary and can't be read directly from a browser, so this integration fetches it inside Home Assistant. It needs no API key or settings. It is not affiliated with UTA.

Route and departure data provided by permission of UTA.

## Install

1. In HACS, add this repository as a custom repository with the **Integration** category, then download it.
2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration** and choose **UTA Rail**.

## What you get

- `sensor.uta_rail_trains`: the number of trains currently reporting a position.
- `GET /api/uta_rail/data`: an authenticated API returning each train's position, speed, and stops, plus the track line and stations for each rail line. Dashboard cards can call it with `hass.callApi("GET", "uta_rail/data")`.

Train data is served from the API instead of sensor attributes so that it isn't pushed to every connected dashboard each time the trains move.

## How it works

Vehicle positions and arrival estimates refresh every 30 seconds. The schedule refreshes daily, and is used to identify rail trips because UTA's feed has trip IDs but no route IDs.

## Limitations

- UTA only publishes arrival estimates for some trips. Other trains have a position and schedule, but no delay.
- FrontRunner has no Sunday service, so it shows no trains then.
