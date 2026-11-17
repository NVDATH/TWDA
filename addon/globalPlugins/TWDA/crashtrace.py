# -*- coding: utf-8 -*-
# NOT TRANSLATED ON PURPOSE. Developer-only file, like diagnostics.py: the release build leaves it out. The hooks that
# call it (marked TEMP) are written so that its absence changes nothing.
#
# crashtrace.py: finds out what the add-on and NVDA were doing when NVDA froze or crashed. Two files in the TWDA folder
# of NVDA's configuration (next to the cache):
#   trace.log  what the add-on does, one line each, flushed at every line so that a crash cannot take it away: jobs
#              start and end, the swap of modules (see utils.isolated_modules), background checks, the main window
#              opening and closing, threads that start and end (the ones of other add-ons too), exceptions in
#              threads. When the main thread of NVDA does not answer for a few seconds, the Python stack of every
#              thread is written (NVDA's own watchdog writes the stack of the main thread only).
#   fault.log  written by faulthandler when the process dies of a native error (an access violation): the Python
#              stack of every thread at that moment.
# Nothing here changes what the add-on does. start() once, trace() from anywhere (a no-op before start()).

import faulthandler
import os
import sys
import threading
import time
import traceback
from collections import Counter

import globalVars
import wx

FOLDER = os.path.join(globalVars.appArgs.configPath, "TWDA")
TRACE_FILE = os.path.join(FOLDER, "trace.log")
FAULT_FILE = os.path.join(FOLDER, "fault.log")
MAX_BYTES = 1024 * 1024    # a file bigger than this is moved to <name>.old when the add-on starts
BEAT_SECONDS = 1.0         # how often the main thread is asked to answer
STALL_SECONDS = 4.0        # no answer for this long: the main thread is frozen
STALL_REPEAT = 10.0        # while it stays frozen, the stacks are written again this often
HEARTBEAT_SECONDS = 60.0   # a line that says the add-on is alive

_lock = threading.Lock()
_trace = None
_fault = None  # kept open for as long as the process runs: faulthandler writes to its descriptor
_stop = threading.Event()
_last_beat = 0.0
_old_thread_hook = None
_old_unraisable_hook = None


def _stamp():
	now = time.time()
	return "%s.%03d" % (time.strftime("%m-%d %H:%M:%S", time.localtime(now)), int(now * 1000) % 1000)


def _open(path):
	os.makedirs(FOLDER, exist_ok=True)
	try:
		if os.path.getsize(path) > MAX_BYTES:
			os.replace(path, path + ".old")
	except OSError:
		pass
	return open(path, "a", encoding="utf-8", buffering=1)


def _write(text):
	f = _trace
	if f is None:
		return
	with _lock:
		try:
			f.write(text)
			f.flush()
		except (OSError, ValueError):
			pass


def trace(event, **info):
	"""One line: the time, the thread, what happened, and the details."""
	if _trace is None:
		return
	_write("%s [%s] %s%s\n" % (_stamp(), threading.current_thread().name, event, "".join(" %s=%s" % kv for kv in info.items())))


def dump_threads(reason):
	"""The Python stack of every thread, in the trace."""
	names = {t.ident: t.name for t in threading.enumerate()}
	frames = sys._current_frames()
	parts = ["%s [%s] === %s: stacks of %d threads ===" % (_stamp(), threading.current_thread().name, reason, len(frames))]
	for ident, frame in frames.items():
		parts.append("--- thread %s (%s)%s" % (
			names.get(ident, "?"), ident, " MAIN" if ident == threading.main_thread().ident else "",
		))
		parts.append("".join(traceback.format_stack(frame)).rstrip())
	_write("\n".join(parts) + "\n=== end of the stacks ===\n")


def _beat():
	global _last_beat
	_last_beat = time.time()


def _watch():
	"""Asks the main thread to answer every BEAT_SECONDS (a wx callback) and writes the stacks when it does not."""
	stalled_since = None
	last_dump = last_heartbeat = time.time()
	known = Counter(t.name for t in threading.enumerate())
	while not _stop.wait(BEAT_SECONDS):
		try:
			wx.CallAfter(_beat)
		except Exception:
			pass
		now = time.time()
		gap = now - _last_beat
		if gap > STALL_SECONDS:
			if stalled_since is None:
				stalled_since = now - gap
				trace("MAIN THREAD STALLED", seconds="%.1f" % gap)
				dump_threads("main thread stalled")
				last_dump = now
			elif now - last_dump > STALL_REPEAT:
				dump_threads("main thread still stalled after %.0f s" % (now - stalled_since))
				last_dump = now
		elif stalled_since is not None:
			trace("main thread answers again", after="%.1f s" % (now - stalled_since))
			stalled_since = None
		current = Counter(t.name for t in threading.enumerate())
		if current != known:
			started, ended = sorted((current - known).elements()), sorted((known - current).elements())
			trace("threads", started=",".join(started[:8]) or "-", ended=",".join(ended[:8]) or "-", now=sum(current.values()))
			known = current
		if now - last_heartbeat >= HEARTBEAT_SECONDS:
			last_heartbeat = now
			trace("alive", threads=sum(current.values()))


def _thread_hook(args):
	trace("exception in a thread", thread=args.thread.name if args.thread else "?", error=repr(args.exc_value)[:300])
	_write("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
	if _old_thread_hook is not None:
		_old_thread_hook(args)


def _unraisable_hook(args):
	trace("unraisable exception", where=repr(args.object)[:200], error=repr(args.exc_value)[:300])
	if _old_unraisable_hook is not None:
		_old_unraisable_hook(args)


def start():
	global _trace, _fault, _old_thread_hook, _old_unraisable_hook, _last_beat
	if _trace is not None:
		return
	_trace = _open(TRACE_FILE)
	_fault = _open(FAULT_FILE)
	was_on = faulthandler.is_enabled()
	_write("%s [%s] === session start %s: pid %d, Python %s, faulthandler was on: %s ===\n" % (
		_stamp(), threading.current_thread().name, time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
		sys.version.split()[0], was_on,
	))
	_fault.write("=== session start %s (pid %d) ===\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
	_fault.flush()
	faulthandler.enable(file=_fault, all_threads=True)
	_old_thread_hook, threading.excepthook = threading.excepthook, _thread_hook
	_old_unraisable_hook, sys.unraisablehook = sys.unraisablehook, _unraisable_hook
	_last_beat = time.time()
	_stop.clear()
	threading.Thread(target=_watch, name="TWDATraceWatchdog", daemon=True).start()


def stop():
	"""The add-on ends (NVDA exits or reloads its add-ons). faulthandler and its file stay as they are until the
	process ends; start() can run again."""
	global _trace
	if _trace is None:
		return
	trace("=== session end (the add-on terminated) ===")
	_stop.set()
	if threading.excepthook is _thread_hook:
		threading.excepthook = _old_thread_hook
	if sys.unraisablehook is _unraisable_hook:
		sys.unraisablehook = _old_unraisable_hook
	with _lock:
		f, _trace = _trace, None
		try:
			f.close()
		except OSError:
			pass
