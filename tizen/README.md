# Fluxa for Samsung TV

This Tizen widget is a small launcher for the live Fluxa web application. It
tries the LAN server first and falls back to the public Fluxa URL. Media remains
on the server and is never packaged into the widget.

The LAN server URL is set in `tv-shell.js`. If the server address changes,
update that value and rebuild the widget.

## Build and install

With Samsung TV Extensions and a Samsung TV certificate profile installed:

```sh
tizen build-web -- /var/www/Fluxa/tizen
tizen package -t wgt -s FluxaTV -- /var/www/Fluxa/tizen/.buildResult
tizen install -s 192.168.0.201:26101 --name FluxaTV001.Fluxa-1.0.0.wgt -- /var/www/Fluxa/tizen/.buildResult
tizen run -s 192.168.0.201:26101 -p FluxaTV001.Fluxa
```
