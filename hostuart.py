#!/usr/bin/env python3
"""Client for the Windows local-only UART bridge."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import socket
import subprocess
import sys
import termios
import time
import tty

DEFAULT_HOST = os.environ.get('HOSTUART_HOST', '127.0.0.1')
DEFAULT_PORT = int(os.environ.get('HOSTUART_TUNNEL_PORT', '15039'))
BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400,
         460800, 500000, 576000, 921600, 1000000, 1152000, 1500000,
         2000000, 2500000, 3000000, 3500000, 4000000)
DEFAULT_DEVICE_REGISTRY = Path(os.environ.get('HOSTUART_DEVICE_REGISTRY',
                                               '~/.config/hostuart/devices.json')).expanduser()

class BridgeError(RuntimeError): pass

class LatestLog:
    """Keep only the newest UART boot/log period in one bounded file."""
    def __init__(self, path, idle_seconds, max_bytes):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.idle_seconds, self.max_bytes = idle_seconds, max_bytes
        self.last_data, self.size = None, 0
        self.path.write_bytes(b'')

    def write(self, data):
        now = time.monotonic()
        replace = (self.last_data is None or now - self.last_data >= self.idle_seconds
                   or self.size + len(data) > self.max_bytes)
        mode = 'wb' if replace else 'ab'
        with self.path.open(mode) as output: output.write(data)
        self.size = len(data) if replace else self.size + len(data)
        self.last_data = now

def endpoint(args): return args.host, args.tunnel_port

def connect(args):
    try: return socket.create_connection(endpoint(args), timeout=args.connect_timeout)
    except OSError as exc: raise BridgeError(f'No Windows UART bridge at {args.host}:{args.tunnel_port}: {exc}') from exc

def send_json(sock, message): sock.sendall(json.dumps(message, separators=(',', ':')).encode() + b'\n')

def read_json(sock):
    data = bytearray()
    while len(data) < 65536:
        try:
            value = sock.recv(1)
        except socket.timeout as exc:
            raise BridgeError(
                'UART bridge did not reply to the open request before the timeout. '
                'Close any old terminal/MobaXterm session using COM3, then restart the Windows UART bridge.'
            ) from exc
        if not value: raise BridgeError('UART bridge closed before replying.')
        if value == b'\n': break
        data.extend(value)
    try: reply = json.loads(data)
    except json.JSONDecodeError as exc: raise BridgeError(f'Invalid UART bridge reply: {data!r}') from exc
    if not reply.get('ok'): raise BridgeError('UART bridge: ' + reply.get('error', 'unknown error'))
    return reply

def link(args):
    return {'port': args.port, 'baudrate': args.baudrate, 'bytesize': args.bytesize,
            'parity': args.parity, 'stopbits': args.stopbits, 'flow': args.flow}

def payload(args):
    try: data = bytes.fromhex(args.data) if args.hex else args.data.encode(args.encoding)
    except ValueError as exc: raise SystemExit(f'invalid hexadecimal data: {exc}') from exc
    return data + (b'\r\n' if args.crlf else b'\n' if args.newline else b'')

def render(data, hex_mode): return data.hex(' ') if hex_mode else data.decode('utf-8', errors='replace')

def control(args, op, **extra):
    with connect(args) as sock:
        send_json(sock, {'op': op, **extra}); return read_json(sock)

def do_list(args):
    reply = control(args, 'list')
    if args.json: print(json.dumps(reply['ports'], ensure_ascii=False, indent=2))
    elif reply['ports']:
        for port in reply['ports']:
            print(f"{port['port']:<8} {port.get('name', '')}")
            if port.get('pnp_id'): print(f"         pnp_id: {port['pnp_id']}")
    else: print('Windows reports no serial ports.')

def do_probe(args):
    reply = control(args, 'probe', **link(args))
    print(f"{reply['port']} opened successfully: {reply['settings']}")

def do_info(args):
    reply = control(args, 'info', port=args.port)
    if args.json: print(json.dumps(reply, ensure_ascii=False, indent=2)); return
    print(f"{reply['port']}: {reply.get('name', '')}\nbridge: reachable")
    if reply.get('pnp_id'): print(f"pnp_id: {reply['pnp_id']}")

def port_matches(port, value):
    needle = value.casefold()
    return any(needle in str(port.get(field, '')).casefold()
               for field in ('port', 'name', 'pnp_id', 'description', 'manufacturer'))

def load_registry(path):
    if not path.exists(): return {'boards': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise BridgeError(f'Cannot read device registry {path}: {exc}') from exc
    if not isinstance(data, dict) or not isinstance(data.get('boards', {}), dict):
        raise BridgeError(f'Invalid device registry {path}: expected an object with boards.')
    return data

def save_registry(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def board_pnp_id(args):
    if not args.board: return args.pnp_id
    record = load_registry(args.device_registry)['boards'].get(args.board)
    if not record or not record.get('pnp_id'):
        raise BridgeError(f'Unknown board {args.board!r}. Enroll it with hostuart enroll {args.board} --pnp-id <id>.')
    return record['pnp_id']

def do_resolve(args):
    ports = control(args, 'list')['ports']
    expected = args.expected_port.upper() if args.expected_port else None
    pnp_id = board_pnp_id(args)
    if pnp_id:
        candidates = [port for port in ports if port.get('pnp_id', '').casefold() == pnp_id.casefold()]
    elif args.match:
        candidates = [port for port in ports if port_matches(port, args.match)]
    else:
        candidates = [port for port in ports if port.get('port', '').upper() == expected]
    if not candidates:
        criterion = f"PNP ID {pnp_id!r}" if pnp_id else f"match {args.match!r}" if args.match else expected
        raise BridgeError(f'No Windows COM port matches {criterion}. Run hostuart list --json to inspect current identities.')
    if len(candidates) != 1:
        names = ', '.join(port['port'] for port in candidates)
        raise BridgeError(f'Multiple Windows COM ports match; refine --pnp-id or --match: {names}')
    result = {'expected_port': expected, 'selected': candidates[0],
              'port_mismatch': bool(expected and candidates[0]['port'].upper() != expected)}
    if args.json: print(json.dumps(result, ensure_ascii=False, indent=2)); return
    print(f"selected: {result['selected']['port']}  {result['selected'].get('name', '')}")
    if result['selected'].get('pnp_id'): print(f"pnp_id: {result['selected']['pnp_id']}")
    if expected: print(f"expected: {expected}; port_mismatch: {'yes' if result['port_mismatch'] else 'no'}")

def do_enroll(args):
    registry = load_registry(args.device_registry)
    boards = registry.setdefault('boards', {})
    existing = boards.get(args.board)
    if existing and existing.get('pnp_id') != args.pnp_id and not args.replace:
        raise BridgeError(f'Board {args.board!r} is already enrolled with another PNP ID; rerun with --replace to change it.')
    boards[args.board] = {'pnp_id': args.pnp_id}
    save_registry(args.device_registry, registry)
    print(f'Enrolled {args.board}: {args.pnp_id}\nregistry: {args.device_registry}')

def session(args):
    sock = connect(args); send_json(sock, {'op': 'open', **link(args)}); read_json(sock)
    sock.settimeout(args.timeout); return sock

def do_monitor(args):
    print(f"Listening on Windows {args.port}; automatic reconnect is enabled; Ctrl+C to stop.", file=sys.stderr)
    deadline = None if args.duration is None else time.monotonic() + args.duration
    latest_log = LatestLog(args.log_file, args.new_log_after_idle, args.max_log_bytes) if args.log_file else None
    if latest_log: print(f'Latest log: {latest_log.path}', file=sys.stderr)
    try:
        while deadline is None or time.monotonic() < deadline:
            try:
                with session(args) as sock:
                    while deadline is None or time.monotonic() < deadline:
                        try: data = sock.recv(4096)
                        except socket.timeout: continue
                        if not data: raise BridgeError('UART session ended')
                        if latest_log: latest_log.write(data)
                        prefix = time.strftime('%H:%M:%S ') if args.timestamps else ''
                        print(prefix + render(data, args.hex), end='\n' if args.hex else '', flush=True)
            except (BridgeError, OSError) as exc:
                if deadline is not None and time.monotonic() >= deadline: break
                print(f'UART disconnected ({exc}); retrying in {args.retry_delay:g}s...', file=sys.stderr)
                time.sleep(args.retry_delay)
    except KeyboardInterrupt: pass

def do_send(args):
    with session(args) as sock: sock.sendall(payload(args))
    print(f'Wrote {len(payload(args))} byte(s) to Windows {args.port}.')

def do_request(args):
    with session(args) as sock:
        sock.sendall(payload(args)); end = time.monotonic() + args.read_for; chunks = []
        while time.monotonic() < end:
            try: data = sock.recv(4096)
            except socket.timeout: continue
            if not data: break
            chunks.append(data)
    print(render(b''.join(chunks), args.hex))

def do_terminal(args):
    """Relay a local TTY to an opened Windows serial port."""
    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
    if not os.isatty(stdin_fd) or not os.isatty(stdout_fd):
        raise BridgeError('terminal requires an interactive Linux TTY.')
    old_terminal = termios.tcgetattr(stdin_fd)
    try:
        with session(args) as sock:
            print(f'Connected to Windows {args.port}; press Ctrl+] to disconnect.', file=sys.stderr)
            tty.setraw(stdin_fd)
            while True:
                readable, _, _ = select.select((stdin_fd, sock), (), ())
                if sock in readable:
                    data = sock.recv(4096)
                    if not data:
                        raise BridgeError('UART session ended')
                    os.write(stdout_fd, data)
                if stdin_fd in readable:
                    data = os.read(stdin_fd, 4096)
                    if not data:
                        return
                    escape = data.find(b'\x1d')  # Ctrl+], handled locally.
                    if escape >= 0:
                        if escape:
                            sock.sendall(data[:escape])
                        return
                    sock.sendall(data)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_terminal)

def do_zsend(args):
    """Send one file with ZMODEM through the existing UART bridge."""
    source = Path(args.file).expanduser()
    if not source.is_file():
        raise BridgeError(f'File not found: {source}')
    if '\n' in args.remote_dir or '\r' in args.remote_dir:
        raise BridgeError('remote directory must not contain a newline')
    if not shutil.which('sz'):
        raise BridgeError('sz is required on Linux for ZMODEM transfer')
    remote_command = f'cd {shlex.quote(args.remote_dir)} && exec rz -y\r'.encode()
    print(f'Sending {source.name} ({source.stat().st_size} bytes) to Windows {args.port}:{args.remote_dir}', file=sys.stderr)
    with session(args) as sock:
        sock.sendall(remote_command)
        # Wait for rz's CAN-prefixed ZMODEM greeting.  Starting sz before this
        # point feeds shell command echo into sz and makes it abort as a cancel.
        deadline = time.monotonic() + 3
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError('Timed out waiting for board rz ZMODEM greeting')
            readable, _, _ = select.select((sock,), (), (), remaining)
            if not readable:
                continue
            greeting = sock.recv(4096)
            if not greeting:
                raise BridgeError('UART session ended before board rz started')
            can_start = greeting.find(b'\x18')
            if can_start >= 0:
                # Keep the receiver's ZRINIT.  It is the first protocol frame
                # sz must consume; only shell echo before it is discarded.
                # ZMODEM's binary header starts with "**<CAN>", so retain
                # the two asterisks when they are present.
                protocol_start = greeting.rfind(b'**', 0, can_start)
                greeting = greeting[protocol_start if protocol_start >= 0 else can_start:]
                break
        process = subprocess.Popen(['sz', '-b', '-y', str(source)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.stdin.write(greeting)
            process.stdin.flush()
            while process.poll() is None:
                readable, _, _ = select.select((sock, process.stdout), (), (), 1)
                if sock in readable:
                    data = sock.recv(4096)
                    if not data:
                        raise BridgeError('UART session ended during ZMODEM transfer')
                    process.stdin.write(data)
                    process.stdin.flush()
                if process.stdout in readable:
                    data = os.read(process.stdout.fileno(), 4096)
                    if data:
                        sock.sendall(data)
            stderr = process.stderr.read().decode('utf-8', errors='replace').strip()
            if process.returncode:
                raise BridgeError(f'ZMODEM send failed (sz exit {process.returncode}): {stderr}')
            print(f'ZMODEM transfer completed: {source.name}', file=sys.stderr)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2)

def add_link(parser):
    parser.add_argument('port', help='Windows COM port, for example COM3')
    parser.add_argument('-b', '--baudrate', type=int, choices=BAUDS, default=115200)
    parser.add_argument('--bytesize', type=int, choices=(5, 6, 7, 8), default=8)
    parser.add_argument('--parity', choices=('none', 'even', 'odd'), default='none')
    parser.add_argument('--stopbits', type=int, choices=(1, 2), default=1)
    parser.add_argument('--flow', choices=('none', 'rtscts', 'xonxoff'), default='none')

def parser():
    root = argparse.ArgumentParser(prog='hostuart', description='Use Windows COM ports through a secure SSH reverse tunnel.')
    root.add_argument('--host', default=DEFAULT_HOST); root.add_argument('--tunnel-port', type=int, default=DEFAULT_PORT); root.add_argument('--connect-timeout', type=float, default=3)
    sub = root.add_subparsers(dest='command', required=True)
    p = sub.add_parser('list'); p.add_argument('--json', action='store_true'); p.set_defaults(func=do_list)
    p = sub.add_parser('info'); p.add_argument('port'); p.add_argument('--json', action='store_true'); p.set_defaults(func=do_info)
    p = sub.add_parser('resolve', help='resolve a current COM port from a stable Windows device identity')
    selector = p.add_mutually_exclusive_group(required=True)
    selector.add_argument('--expected-port', help='expected COM port; verifies only this current port')
    selector.add_argument('--pnp-id', help='exact Windows PNPDeviceID from hostuart list --json')
    selector.add_argument('--board', help='enrolled board name resolved through its PNPDeviceID')
    selector.add_argument('--match', help='case-insensitive match against port name, PNP ID, description, or manufacturer')
    p.add_argument('--device-registry', type=Path, default=DEFAULT_DEVICE_REGISTRY)
    p.add_argument('--json', action='store_true'); p.set_defaults(func=do_resolve)
    p = sub.add_parser('enroll', help='store a confirmed board-to-PNP-device identity mapping')
    p.add_argument('board', help='local board label, for example rk3576-console')
    p.add_argument('--pnp-id', required=True, help='exact Windows PNPDeviceID from hostuart list --json')
    p.add_argument('--device-registry', type=Path, default=DEFAULT_DEVICE_REGISTRY)
    p.add_argument('--replace', action='store_true', help='replace an existing board mapping')
    p.set_defaults(func=do_enroll)
    p = sub.add_parser('probe', help='open and close without transmitting'); add_link(p); p.set_defaults(func=do_probe)
    p = sub.add_parser('configure', aliases=('config',), help='validate settings by opening the port; does not transmit'); add_link(p); p.set_defaults(func=do_probe)
    for name, func in [('send', do_send), ('request', do_request)]:
        p = sub.add_parser(name); add_link(p); p.add_argument('data'); p.add_argument('--hex', action='store_true'); p.add_argument('--encoding', default='utf-8'); p.add_argument('--newline', action='store_true'); p.add_argument('--crlf', action='store_true'); p.set_defaults(func=func)
        if name == 'request': p.add_argument('--read-for', type=float, default=1.0)
    p = sub.add_parser('monitor', aliases=('read',)); add_link(p); p.add_argument('--duration', type=float); p.add_argument('--retry-delay', type=float, default=1.0); p.add_argument('--log-file', help='overwrite this file with the latest UART log period'); p.add_argument('--new-log-after-idle', type=float, default=2.0, help='seconds of silence before new data replaces the log'); p.add_argument('--max-log-bytes', type=int, default=1024 * 1024); p.add_argument('--hex', action='store_true'); p.add_argument('--timestamps', action='store_true'); p.set_defaults(func=do_monitor)
    p = sub.add_parser('terminal', aliases=('shell',), help='interactive bidirectional serial terminal; Ctrl+] disconnects locally')
    add_link(p); p.set_defaults(func=do_terminal)
    p = sub.add_parser('zsend', help='send one file with ZMODEM; starts rz -y on the board')
    add_link(p); p.add_argument('file', help='local file to send'); p.add_argument('--remote-dir', default='/test', help='existing board directory for the received file')
    p.set_defaults(func=do_zsend)
    return root

def main(argv=None):
    try:
        args = parser().parse_args(argv); args.timeout = 0.25; args.func(args); return 0
    except BridgeError as exc:
        print(f'hostuart: {exc}', file=sys.stderr); return 2
if __name__ == '__main__': raise SystemExit(main())
