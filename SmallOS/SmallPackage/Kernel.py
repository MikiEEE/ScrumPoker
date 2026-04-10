"""
Platform kernels for smallOS.

The scheduler is intentionally written against a tiny interface so the runtime
can stay portable across desktop Python and MicroPython boards. This module
keeps the shared surface in one place, then layers a few practical profiles on
top:
- ``Unix`` for desktop development and testing
- ``MicroPythonKernel`` for the generic portable subset of MicroPython
- thin board profiles like ``ESP32`` and ``PicoW`` for board-flavored defaults

The important design choice is that board profiles do not own scheduling logic.
They only provide capability hints and convenience configuration around the
shared MicroPython transport/time API.
"""


_UNSET = object()


def _import_first(*module_names):
	"""
	Import the first available module name from a list.

	This keeps the rest of the kernel code readable when we want CPython and
	MicroPython fallbacks like ``time``/``utime`` or ``socket``/``usocket``.
	"""
	for module_name in module_names:
		if not module_name:
			continue
		try:
			return __import__(module_name)
		except ImportError:
			continue
	return None


def _portable_shell_split(line):
	"""
	Tokenize one shell command line without relying on desktop-only helpers.

	This parser is intentionally small but still handles the common cases a
	MicroPython-facing shell needs:
	- whitespace-separated tokens
	- single or double quoted strings
	- backslash escaping for the next character
	"""
	tokens = []
	current = []
	quote = None
	escape = False

	for char in line:
		if escape:
			current.append(char)
			escape = False
			continue

		if char == "\\":
			escape = True
			continue

		if quote is not None:
			if char == quote:
				quote = None
			else:
				current.append(char)
			continue

		if char in ("'", '"'):
			quote = char
			continue

		if char.isspace():
			if current:
				tokens.append("".join(current))
				current = []
			continue

		current.append(char)

	if escape:
		current.append("\\")

	if quote is not None:
		raise ValueError("unterminated quoted string")

	if current:
		tokens.append("".join(current))
	return tokens


def detect_micropython_machine_name(sys_mod=None, os_mod=None):
	"""
	Best-effort lookup of the active board/firmware machine name.

	MicroPython commonly exposes a descriptive string through
	``sys.implementation._machine``. When that is unavailable we fall back to
	``os.uname().machine``.
	"""
	if sys_mod is None:
		sys_mod = _import_first('sys')

	if sys_mod is not None:
		implementation = getattr(sys_mod, 'implementation', None)
		machine_name = getattr(implementation, '_machine', None)
		if machine_name:
			return machine_name

	if os_mod is None:
		os_mod = _import_first('os')

	if os_mod is not None and hasattr(os_mod, 'uname'):
		try:
			uname_result = os_mod.uname()
		except TypeError:
			uname_result = os_mod.uname
		machine_name = getattr(uname_result, 'machine', None)
		if machine_name:
			return machine_name

	return ''


def build_micropython_kernel(machine_name=None, **kwargs):
	"""
	Return the best built-in MicroPython kernel profile for the current board.

	This helper is intentionally conservative: it only selects a board-specific
	profile when the firmware string clearly identifies one of the built-in board
	targets. Everything else falls back to the generic ``MicroPythonKernel``.
	"""
	name = (machine_name or detect_micropython_machine_name() or '').lower()

	if 'pico w' in name or 'pico 2 w' in name:
		return PicoW(**kwargs)
	if 'esp32' in name:
		return ESP32(**kwargs)
	if 'esp8266' in name:
		return ESP8266(**kwargs)
	return MicroPythonKernel(**kwargs)


class Kernel:
	'''
	@class Kernel - Soft interface for the platform layer.

	The runtime keeps this surface intentionally small:
	- text output
	- wall-clock time when available
	- scheduler timing built on ticks-style APIs
	- sleeping and I/O readiness waiting
	- generic TCP/TLS stream primitives

	Higher-level protocols such as HTTPS, Redis, MQTT, RabbitMQ/AMQP, and Kafka
	should be built in userland on top of these generic facilities instead of
	requiring protocol-specific kernel methods.
	'''

	def __init__(self):
		self._scheduler_anchor_tick = None
		self._scheduler_elapsed_ms = 0

	def write(self, msg):
		pass

	def shell_split(self, line):
		"""
		Tokenize one shell command line.

		The base implementation uses a small portable parser so shells can work on
		platforms where ``shlex`` is unavailable. Desktop kernels may override
		this with richer parsing behavior.
		"""
		return _portable_shell_split(line)

	def time_epoch(self):
		pass

	def ticks_ms(self):
		pass

	def ticks_add(self, base, delta_ms):
		return base + delta_ms

	def ticks_diff(self, end, start):
		return end - start

	def time_monotonic(self):
		return self.scheduler_now_ms() / 1000

	def scheduler_now_ms(self):
		'''
		Returns a non-decreasing scheduler clock in milliseconds.

		On MicroPython this is derived from ticks_ms/ticks_diff so wrap-around is
		hidden from the scheduler and queue code.
		'''
		current = self.ticks_ms()
		if self._scheduler_anchor_tick is None:
			self._scheduler_anchor_tick = current
			return self._scheduler_elapsed_ms

		delta = self.ticks_diff(current, self._scheduler_anchor_tick)
		if delta < 0:
			delta = 0
		self._scheduler_elapsed_ms += delta
		self._scheduler_anchor_tick = current
		return self._scheduler_elapsed_ms

	def sleep(self, secs):
		self.sleep_ms(int(max(0, secs) * 1000))
		return

	def sleep_ms(self, delay_ms):
		pass

	def io_wait(self, readables, writables, timeout_ms=None):
		return [], []

	def resolve_address(self, host, port):
		return None

	def socket_open(self, address_info):
		return None

	def socket_setblocking(self, sock, flag):
		return

	def socket_connect(self, sock, sockaddr):
		return True

	def socket_connection_error(self, sock):
		return 0

	def socket_send(self, sock, data):
		return 0

	def socket_recv(self, sock, buffer_size):
		return b''

	def socket_close(self, sock):
		return

	def socket_wrap_tls_client(
		self,
		sock,
		server_hostname=None,
		tls_ca_file=None,
		tls_cert_file=None,
		tls_key_file=None,
		tls_verify=True,
	):
		return sock

	def socket_do_handshake(self, sock):
		return

	def _extract_errno(self, exc):
		errno_value = getattr(exc, 'errno', None)
		if errno_value is not None:
			return errno_value
		if getattr(exc, 'args', None):
			return exc.args[0]
		return None

	def socket_needs_read(self, exc):
		return isinstance(exc, BlockingIOError)

	def socket_needs_write(self, exc):
		return False

	def _poll_lookup_key(self, obj):
		"""
		Return the identity key used to map poll events back to registered objects.

		``select.poll()`` typically reports integer file descriptors instead of the
		original socket object, so the kernel needs a stable way to translate poll
		results back into the scheduler's waiter keys.
		"""
		if hasattr(obj, 'fileno'):
			try:
				return obj.fileno()
			except Exception:
				pass
		return obj


class Unix(Kernel):
	'''
	For Unix-like systems.

	The imports used by this implementation are loaded here instead of at module
	import time so alternate kernels can import ``Kernel.py`` without requiring
	desktop-oriented modules such as ``select`` and ``socket`` to exist.
	'''

	def __init__(self):
		super().__init__()
		import errno
		import shlex
		import select
		import socket
		import ssl
		import sys
		import time

		self._errno = errno
		self._shlex = shlex
		self._select = select
		self._socket = socket
		self._ssl = ssl
		self._sys = sys
		self._time = time
		self._poll_factory = getattr(select, 'poll', None)
		return

	def write(self, msg):
		self._sys.stdout.write(msg)
		self._sys.stdout.flush()
		return

	def shell_split(self, line):
		"""Use ``shlex`` on Unix for richer desktop-style shell parsing."""
		return self._shlex.split(line)

	def time_epoch(self):
		return self._time.time()

	def ticks_ms(self):
		return int(self._time.monotonic() * 1000)

	def ticks_add(self, base, delta_ms):
		return base + delta_ms

	def ticks_diff(self, end, start):
		return end - start

	def time_monotonic(self):
		return self._time.monotonic()

	def sleep_ms(self, delay_ms):
		self._time.sleep(max(0, delay_ms) / 1000)
		return

	def io_wait(self, readables, writables, timeout_ms=None):
		if self._poll_factory:
			poller = self._poll_factory()
			mask_by_object = {}
			object_by_key = {}
			read_mask = getattr(self._select, 'POLLIN', 0x001)
			write_mask = getattr(self._select, 'POLLOUT', 0x004)

			for obj in readables:
				mask_by_object[obj] = mask_by_object.get(obj, 0) | read_mask
			for obj in writables:
				mask_by_object[obj] = mask_by_object.get(obj, 0) | write_mask

			for obj, mask in mask_by_object.items():
				object_by_key[self._poll_lookup_key(obj)] = obj
				poller.register(obj, mask)

			timeout = -1 if timeout_ms is None else max(0, int(timeout_ms))
			events = poller.poll(timeout)

			ready_read = []
			ready_write = []
			for obj, mask in events:
				ready_obj = object_by_key.get(obj, obj)
				if mask & read_mask:
					ready_read.append(ready_obj)
				if mask & write_mask:
					ready_write.append(ready_obj)
			return ready_read, ready_write

		timeout = None if timeout_ms is None else max(0, timeout_ms) / 1000
		if not readables and not writables:
			if timeout is not None and timeout > 0:
				self._time.sleep(timeout)
			return [], []
		ready_read, ready_write, _ = self._select.select(readables, writables, [], timeout)
		return ready_read, ready_write

	def resolve_address(self, host, port):
		return self._socket.getaddrinfo(host, port, type=self._socket.SOCK_STREAM)[0]

	def socket_open(self, address_info):
		family, socktype, proto, _, _ = address_info
		return self._socket.socket(family, socktype, proto)

	def socket_setblocking(self, sock, flag):
		sock.setblocking(flag)
		return

	def socket_connect(self, sock, sockaddr):
		err = sock.connect_ex(sockaddr)
		pending = {
			0,
			getattr(self._errno, 'EINPROGRESS', 0),
			getattr(self._errno, 'EWOULDBLOCK', 0),
			getattr(self._errno, 'EALREADY', 0),
			getattr(self._errno, 'EINTR', 0),
			getattr(self._errno, 'EISCONN', 0),
		}
		if err == 0:
			return True
		if err in pending:
			return False
		raise OSError(err, 'socket connect failed')

	def socket_connection_error(self, sock):
		return sock.getsockopt(self._socket.SOL_SOCKET, self._socket.SO_ERROR)

	def socket_send(self, sock, data):
		return sock.send(data)

	def socket_recv(self, sock, buffer_size):
		return sock.recv(buffer_size)

	def socket_close(self, sock):
		sock.close()
		return

	def socket_wrap_tls_client(
		self,
		sock,
		server_hostname=None,
		tls_ca_file=None,
		tls_cert_file=None,
		tls_key_file=None,
		tls_verify=True,
	):
		if tls_verify:
			context = self._ssl.create_default_context(cafile=tls_ca_file)
		else:
			context = self._ssl.SSLContext(self._ssl.PROTOCOL_TLS_CLIENT)
			context.check_hostname = False
			context.verify_mode = self._ssl.CERT_NONE
		if tls_cert_file is not None:
			context.load_cert_chain(certfile=tls_cert_file, keyfile=tls_key_file)
		wrapped = context.wrap_socket(
			sock,
			server_hostname=server_hostname,
			do_handshake_on_connect=False,
		)
		wrapped.setblocking(False)
		return wrapped

	def socket_do_handshake(self, sock):
		sock.do_handshake()
		return

	def socket_needs_read(self, exc):
		return isinstance(exc, (BlockingIOError, self._ssl.SSLWantReadError))

	def socket_needs_write(self, exc):
		return isinstance(exc, self._ssl.SSLWantWriteError)


class MicroPythonKernel(Kernel):
	'''
	Generic kernel targeting the portable subset of MicroPython.

	This class is meant to cover most MicroPython ports without requiring one
	kernel class per board. When board-specific differences appear, prefer small
	subclasses or configuration helpers over full kernel rewrites.
	'''

	def __init__(
		self,
		network_interface=None,
		board_name='Generic MicroPython',
		wifi_country=None,
		wifi_hostname=None,
		wifi_power_management=None,
		modules=None,
	):
		super().__init__()

		modules = modules or {}

		self._time = modules.get('time') or _import_first('time', 'utime')
		self._select = modules.get('select') or _import_first('select', 'uselect')
		self._socket = modules.get('socket') or _import_first('socket', 'usocket')
		self._ssl = modules.get('ssl')
		if self._ssl is None:
			self._ssl = _import_first('ssl', 'ussl')
		self._sys = modules.get('sys') or _import_first('sys')
		self._os = modules.get('os') or _import_first('os')
		self._network = modules.get('network') or _import_first('network')
		self._rp2 = modules.get('rp2') or _import_first('rp2')
		self._machine = modules.get('machine') or _import_first('machine')
		self._poll_factory = getattr(self._select, 'poll', None)
		self.network_interface = network_interface
		self.board_name = board_name
		self.default_wifi_country = wifi_country
		self.default_wifi_hostname = wifi_hostname
		self.default_wifi_power_management = wifi_power_management
		return

	def write(self, msg):
		if self._sys and getattr(self._sys, 'stdout', None):
			self._sys.stdout.write(msg)
			return
		print(msg, end='')

	def time_epoch(self):
		if hasattr(self._time, 'time'):
			return self._time.time()
		return 0

	def ticks_ms(self):
		if hasattr(self._time, 'ticks_ms'):
			return self._time.ticks_ms()
		return int(self.time_epoch() * 1000)

	def ticks_add(self, base, delta_ms):
		if hasattr(self._time, 'ticks_add'):
			return self._time.ticks_add(base, delta_ms)
		return base + delta_ms

	def ticks_diff(self, end, start):
		if hasattr(self._time, 'ticks_diff'):
			return self._time.ticks_diff(end, start)
		return end - start

	def sleep_ms(self, delay_ms):
		if hasattr(self._time, 'sleep_ms'):
			self._time.sleep_ms(max(0, int(delay_ms)))
			return
		if hasattr(self._time, 'sleep'):
			self._time.sleep(max(0, delay_ms) / 1000)
			return

	def io_wait(self, readables, writables, timeout_ms=None):
		if self._poll_factory:
			poller = self._poll_factory()
			read_mask = getattr(self._select, 'POLLIN', 0x001)
			write_mask = getattr(self._select, 'POLLOUT', 0x004)
			mask_by_object = {}
			object_by_key = {}

			for obj in readables:
				mask_by_object[obj] = mask_by_object.get(obj, 0) | read_mask
			for obj in writables:
				mask_by_object[obj] = mask_by_object.get(obj, 0) | write_mask

			for obj, mask in mask_by_object.items():
				object_by_key[self._poll_lookup_key(obj)] = obj
				poller.register(obj, mask)

			timeout = -1 if timeout_ms is None else max(0, int(timeout_ms))
			events = poller.poll(timeout)

			ready_read = []
			ready_write = []
			for obj, mask in events:
				ready_obj = object_by_key.get(obj, obj)
				if mask & read_mask:
					ready_read.append(ready_obj)
				if mask & write_mask:
					ready_write.append(ready_obj)
			return ready_read, ready_write

		if timeout_ms is not None and timeout_ms > 0:
			self.sleep_ms(timeout_ms)
		return [], []

	def resolve_address(self, host, port):
		return self._socket.getaddrinfo(host, port)[0]

	def socket_open(self, address_info):
		family, socktype, proto, _, _ = address_info
		return self._socket.socket(family, socktype, proto)

	def socket_setblocking(self, sock, flag):
		sock.setblocking(flag)
		return

	def socket_connect(self, sock, sockaddr):
		try:
			sock.connect(sockaddr)
			return True
		except Exception as exc:
			err = self._extract_errno(exc)
			# 115 = EINPROGRESS, 11 = EAGAIN on most MicroPython targets
			pending = {
				115, 11,
			}
			if err in pending or self.socket_needs_read(exc) or self.socket_needs_write(exc):
				return False
			raise

	def socket_connection_error(self, sock):
		sol_socket = getattr(self._socket, 'SOL_SOCKET', 1)
		so_error = getattr(self._socket, 'SO_ERROR', 4)
		return sock.getsockopt(sol_socket, so_error)

	def socket_send(self, sock, data):
		return sock.send(data)

	def socket_recv(self, sock, buffer_size):
		return sock.recv(buffer_size)

	def socket_close(self, sock):
		sock.close()
		return

	def socket_wrap_tls_client(
		self,
		sock,
		server_hostname=None,
		tls_ca_file=None,
		tls_cert_file=None,
		tls_key_file=None,
		tls_verify=True,
	):
		if not self._ssl:
			raise NotImplementedError('TLS support is not available on this MicroPython port.')

		kwargs = {}
		if server_hostname is not None:
			kwargs['server_hostname'] = server_hostname
		if tls_cert_file is not None:
			kwargs['cert'] = tls_cert_file
		if tls_key_file is not None:
			kwargs['key'] = tls_key_file
		if not tls_verify and hasattr(self._ssl, 'CERT_NONE'):
			kwargs['cert_reqs'] = self._ssl.CERT_NONE

		# Build a list of progressively simpler kwarg sets to handle ports
		# whose ssl.wrap_socket rejects unknown keyword arguments.  When
		# tls_verify is True we never fall back to a bare call — doing so
		# would silently disable certificate verification.
		attempts = []
		if kwargs:
			attempts.append(kwargs)
		if 'cert' in kwargs or 'key' in kwargs or 'cert_reqs' in kwargs:
			attempts.append({k: v for k, v in kwargs.items() if k == 'server_hostname'})
		if not tls_verify:
			# Only allow the bare fallback when verification is explicitly
			# disabled — falling back here with tls_verify=True would be a
			# silent security downgrade.
			attempts.append({})
		if not attempts:
			attempts.append({})

		last_error = None
		for wrap_kwargs in attempts:
			try:
				wrapped = self._ssl.wrap_socket(sock, **wrap_kwargs)
				break
			except TypeError as exc:
				last_error = exc
		else:
			raise last_error
		if hasattr(wrapped, 'setblocking'):
			wrapped.setblocking(False)
		return wrapped

	def socket_do_handshake(self, sock):
		if hasattr(sock, 'do_handshake'):
			sock.do_handshake()
		return

	def socket_needs_read(self, exc):
		err = self._extract_errno(exc)
		if err in {11, 115}:
			return True
		if self._ssl and hasattr(self._ssl, 'SSLWantReadError') and isinstance(exc, self._ssl.SSLWantReadError):
			return True
		return isinstance(exc, BlockingIOError)

	def socket_needs_write(self, exc):
		if self._ssl and hasattr(self._ssl, 'SSLWantWriteError') and isinstance(exc, self._ssl.SSLWantWriteError):
			return True
		return False

	def machine_name(self):
		'''
		Return the current firmware/board descriptor when the platform exposes one.
		'''
		return detect_micropython_machine_name(sys_mod=self._sys, os_mod=self._os)

	def _configure_wifi_country(self, country):
		'''
		Apply a regulatory country code when the port exposes a hook for it.
		'''
		if not country:
			return False

		if self._rp2 and hasattr(self._rp2, 'country'):
			self._rp2.country(country)
			return True

		if self._network and hasattr(self._network, 'country'):
			self._network.country(country)
			return True

		return False

	def _configure_wifi_hostname(self, nic, hostname):
		'''
		Apply a DHCP or stack hostname using whichever API the port exposes.
		'''
		if not hostname:
			return False

		if self._network and hasattr(self._network, 'hostname'):
			self._network.hostname(hostname)
			return True

		if nic and hasattr(nic, 'config'):
			for kwargs in ({'dhcp_hostname': hostname}, {'hostname': hostname}):
				try:
					nic.config(**kwargs)
					return True
				except Exception:
					continue

		return False

	def _configure_wifi_power_management(self, nic, power_management):
		'''
		Apply a board/port-specific Wi-Fi power management mode when supported.
		'''
		if power_management is None or nic is None or not hasattr(nic, 'config'):
			return False

		try:
			nic.config(pm=power_management)
			return True
		except Exception:
			return False

	def prepare_wifi_station(self, nic=None, country=None, hostname=None, power_management=_UNSET):
		'''
		Apply optional board-flavored Wi-Fi configuration before connecting.

		This is the main extension hook used by board profiles. The generic
		implementation stays conservative and only calls settings when the active
		port exposes the needed API.
		'''
		if country is None:
			country = self.default_wifi_country
		if hostname is None:
			hostname = self.default_wifi_hostname
		if power_management is _UNSET:
			power_management = self.default_wifi_power_management

		self._configure_wifi_country(country)
		self._configure_wifi_hostname(nic, hostname)
		self._configure_wifi_power_management(nic, power_management)
		return nic

	def connect_wifi(
		self,
		ssid,
		password,
		timeout_ms=15000,
		country=None,
		hostname=None,
		power_management=_UNSET,
	):
		'''
		Generic helper for ports that expose ``network.WLAN(STA_IF)``.

		Board profiles reuse this method and simply feed in better defaults for the
		target board, which keeps connection workflow shared across ports.
		'''
		if not self._network or not hasattr(self._network, 'WLAN'):
			raise NotImplementedError('This MicroPython port does not expose network.WLAN.')

		nic = self.network_interface
		if nic is None:
			nic = self._network.WLAN(self._network.STA_IF)
			self.network_interface = nic

		nic.active(True)
		self.prepare_wifi_station(
			nic=nic,
			country=country,
			hostname=hostname,
			power_management=power_management,
		)
		if nic.isconnected():
			return nic

		nic.connect(ssid, password)
		start = self.ticks_ms()
		while not nic.isconnected():
			if self.ticks_diff(self.ticks_ms(), start) > timeout_ms:
				raise TimeoutError('Timed out waiting for Wi-Fi connection.')
			self.sleep_ms(100)
		return nic


class ESP32(MicroPythonKernel):
	'''
	Board profile for ESP32-based MicroPython dev boards.

	ESP32 is one of the most common hobbyist MicroPython targets, so this class
	gives it a friendly name and a place for ESP32-specific defaults without
	fragmenting the shared runtime behavior.
	'''

	def __init__(self, network_interface=None, hostname=None, modules=None):
		super().__init__(
			network_interface=network_interface,
			board_name='ESP32',
			wifi_hostname=hostname,
			modules=modules,
		)


class PicoW(MicroPythonKernel):
	'''
	Board profile for Raspberry Pi Pico W and Pico 2 W.

	The main board-specific convenience here is Wi-Fi setup: callers can supply
	a regulatory country code, hostname, and optional power management mode while
	still using the same shared transport API as every other MicroPython port.
	'''

	def __init__(
		self,
		network_interface=None,
		country=None,
		hostname=None,
		power_management=None,
		modules=None,
	):
		super().__init__(
			network_interface=network_interface,
			board_name='Raspberry Pi Pico W',
			wifi_country=country,
			wifi_hostname=hostname,
			wifi_power_management=power_management,
			modules=modules,
		)


class ESP8266(MicroPythonKernel):
	'''
	Compatibility alias kept for the earlier project structure.
	'''

	def __init__(self, network_interface=None, hostname=None, modules=None):
		super().__init__(
			network_interface=network_interface,
			board_name='ESP8266',
			wifi_hostname=hostname,
			modules=modules,
		)


RaspberryPiPicoW = PicoW
