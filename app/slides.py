from __future__ import annotations

import concurrent.futures
import os
import queue
import threading
import time
from typing import Callable

import pythoncom
import win32api
import win32com.client
import win32con
import win32gui
import win32process

from app.config import cfg


class SlideControllerError(Exception):
    pass


class SlideController:
    """Owns the PowerPoint COM object on its own dedicated thread (§4.3).

    All public methods run on the calling thread, submit work to the COM
    thread, and block on a concurrent.futures.Future with a hard timeout.
    A dead PowerPoint does not always raise — GotoSlide can block forever —
    so the timeout, not an exception from COM, is what detects the fault.
    """

    def __init__(self) -> None:
        self._commands: queue.Queue = queue.Queue()
        self._app = None
        self._presentation = None
        self._slideshow_window = None
        threading.Thread(target=self._run, daemon=True).start()

    def open(self, pptx_path: str) -> None:
        """Opens the deck and starts the slideshow, full-screen on the
        configured monitor (slides.display in config.yaml)."""
        self._submit(lambda: self._do_open(pptx_path))

    def goto(self, slide_index: int) -> None:
        """1-based, matches PowerPoint."""
        self._submit(lambda: self._do_goto(slide_index))

    def close(self) -> None:
        self._submit(lambda: self._do_close())

    def _submit(self, fn: Callable[[], None]) -> None:
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._commands.put((fn, future))
        try:
            future.result(timeout=cfg.slides.com_timeout_s)
        except concurrent.futures.TimeoutError as e:
            raise SlideControllerError(
                f"PowerPoint command timed out after {cfg.slides.com_timeout_s}s "
                "— treat as a fault (§6.4)."
            ) from e
        except SlideControllerError:
            raise
        except Exception as e:
            # A dead PowerPoint does not always raise via a timeout — it can
            # also raise a COM error immediately (e.g. "RPC server is
            # unavailable" once the process is gone). Either way counts as
            # a fault (§6.4), so callers only need to catch one exception
            # type regardless of which failure mode actually happened.
            raise SlideControllerError(f"PowerPoint COM call failed: {e}") from e

    def _run(self) -> None:
        # Dedicated apartment-threaded COM thread. Never subscribe to COM
        # events here — event delivery needs a message pump, and this
        # thread only ever blocks on queue.get() between commands.
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            while True:
                fn, future = self._commands.get()
                try:
                    result = fn()
                except Exception as e:
                    future.set_exception(e)
                else:
                    future.set_result(result)
        finally:
            pythoncom.CoUninitialize()

    # --- everything below runs only on the COM thread ---

    def _do_open(self, pptx_path: str) -> None:
        left, top, right, bottom = self._target_monitor_rect()

        # PowerPoint's COM server has its own working directory, unrelated
        # to ours — a relative path fails with a cryptic COM error rather
        # than a clear FileNotFoundError.
        abs_path = os.path.abspath(pptx_path)

        self._app = win32com.client.Dispatch("PowerPoint.Application")
        self._presentation = self._app.Presentations.Open(
            abs_path, ReadOnly=True, Untitled=False, WithWindow=True
        )
        self._presentation.SlideShowSettings.Run()
        # The slideshow window isn't necessarily ready the instant Run()
        # returns.
        time.sleep(0.5)
        self._slideshow_window = self._presentation.SlideShowWindow

        # SlideShowWindow.Left/Top/Width/Height go through an ambiguous
        # unit (tried raw pixels: ~33% too large; tried points: broke the
        # window outright). PowerPoint's own default full-screen sizing on
        # a DPI-aware process is already correct physical pixels — verified
        # directly via GetWindowRect on the real "screenClass" HWND — so
        # positioning is done at the Win32 level instead, matching the same
        # unambiguous physical-pixel space _target_monitor_rect() uses.
        hwnd = self._find_slideshow_hwnd()
        if hwnd is None:
            raise SlideControllerError("Could not find the PowerPoint slideshow window")
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOP,
            left,
            top,
            right - left,
            bottom - top,
            win32con.SWP_SHOWWINDOW,
        )

    def _find_slideshow_hwnd(self) -> int | None:
        found: list[int] = []

        def handler(hwnd: int, _: None) -> None:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "screenClass":
                found.append(hwnd)

        deadline = time.monotonic() + 2.0
        while not found and time.monotonic() < deadline:
            win32gui.EnumWindows(handler, None)
            if not found:
                time.sleep(0.1)
        return found[0] if found else None

    def _do_goto(self, slide_index: int) -> None:
        if self._slideshow_window is None:
            raise SlideControllerError("goto() called before open()")
        self._slideshow_window.View.GotoSlide(slide_index)

    def _do_close(self) -> None:
        if self._slideshow_window is not None:
            # End the slideshow before closing — closing the presentation
            # while its show window is still active can leave PowerPoint
            # running even after Quit().
            self._slideshow_window.View.Exit()
        if self._presentation is not None:
            self._presentation.Close()
        if self._app is not None:
            # Closing the presentation alone does not quit the PowerPoint
            # process — Application.Quit() is a separate call, and without
            # it POWERPNT.EXE lingers after the script exits.
            self._app.Quit()
        self._app = None
        self._presentation = None
        self._slideshow_window = None

    def _target_monitor_rect(self) -> tuple[int, int, int, int]:
        # slides.display is a 1-based *positional* index (primary first,
        # then left-to-right) — not a literal \\.\DISPLAYn device number.
        # Those numbers are assigned by Windows per GPU port and do not
        # reliably match simple 1, 2, 3... ordering (confirmed on this dev
        # machine: the second monitor enumerates as DISPLAY6, not DISPLAY2).
        monitors = win32api.EnumDisplayMonitors()
        monitors_sorted = sorted(
            monitors,
            key=lambda m: (
                0 if win32api.GetMonitorInfo(m[0]).get("Flags") else 1,
                m[2][0],
            ),
        )
        index = cfg.slides.display - 1
        if index < 0 or index >= len(monitors_sorted):
            raise SlideControllerError(
                f"slides.display={cfg.slides.display} configured, but only "
                f"{len(monitors_sorted)} monitor(s) are currently connected."
            )
        return monitors_sorted[index][2]
