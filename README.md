# Windows UART bridge

Windows owns the `COMx` device. The Windows service only listens on `127.0.0.1`; its SSH reverse tunnel exposes the same port only as Linux `127.0.0.1:15039`.

On Windows, copy `windows-safe` to a trusted folder and run:

```powershell
.\Start-SafeUartBridge.cmd
```

On Linux:

```bash
ln -sf /home/<user>/uart-host-bridge/hostuart ~/.local/bin/hostuart
hostuart list
hostuart list --json
hostuart resolve --pnp-id 'USB\\VID_XXXX&PID_XXXX\\SERIAL'
hostuart enroll rk3576-console --pnp-id 'USB\\VID_XXXX&PID_XXXX\\SERIAL'
hostuart resolve --board rk3576-console
hostuart probe COM3 -b 115200
hostuart monitor COM3 -b 115200 --timestamps
```

`probe` opens and closes the port but transmits no data. `monitor` is passive. `send` and `request` transmit data and should only be used with an explicitly approved payload.

For an interactive Linux console similar to a serial-terminal application, use:

```bash
hostuart terminal COM3 -b 115200
```

This relays keyboard input to the board and board output back to the local terminal. Press `Ctrl+]` to disconnect locally; that key is not transmitted to the board.

Windows can renumber a USB serial adapter after re-enumeration. `hostuart list --json` exposes the Windows `PNPDeviceID`; record that stable identity and use `hostuart resolve --pnp-id ...` to find its current `COMx`. Do not assume an old COM number remains valid. The Windows launcher keeps retrying its SSH reverse tunnel after a network interruption.

For a board that is used repeatedly, confirm its PNP identity once and enroll it under a local name. `hostuart resolve --board <name>` then identifies its current COM port before any probe, monitor, send, or request opens the port. The registry defaults to `~/.config/hostuart/devices.json`; it stores only local board labels and PNP IDs.

If port `15039` is occupied, choose another unused value in both Windows `-RemotePort` and Linux `--tunnel-port` (or `HOSTUART_TUNNEL_PORT`).
