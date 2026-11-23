# Stock touch screen of Tronxy printers with the TRONXY_F446 board
# (e.g. X5SA Pro): ILI9488 480x320 + XPT2046
#
# Counterpart of src/stm32/tronxy_tft.c (MCU firmware built with
# CONFIG_TRONXY_TFT). Draws a status page with touch buttons using simple
# fill and text commands. All display commands are sent with background
# priority, so they never delay motion data.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging
import textwrap
import mcu

BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000
W, H = 480, 320
FILL_MAX_PX = 9600      # ~1 ms of MCU time per command
TEXT_MAX_CHARS = 32     # always fits into one Klipper message
UPDATE_TIME = 1.

FILL_FMT = "tronxy_tft_fill x=%hu y=%hu w=%hu h=%hu color=%hu"
TEXT_FMT = "tronxy_tft_text x=%hu y=%hu scale=%c fg=%hu bg=%hu text=%*s"
LIGHT_FMT = "tronxy_tft_backlight on=%c"


def rgb(r, g, b):
    return ((r & 0xf8) << 8) | ((g & 0xfc) << 3) | (b >> 3)


BG = rgb(14, 14, 18)
WHITE = 0xffff
BLACK = 0x0000
DIM = rgb(150, 150, 155)
DIS_BG = rgb(48, 48, 54)
DIS_FG = rgb(110, 110, 118)
OK_COL = rgb(90, 220, 110)
HEAT_COL = rgb(255, 160, 40)
ERR_COL = rgb(255, 70, 60)
BAR_BG = rgb(48, 48, 54)
BAR_FG = rgb(40, 170, 80)

STATE_COLORS = {
    'standby': rgb(0, 70, 140),
    'printing': rgb(0, 115, 45),
    'paused': rgb(170, 110, 0),
    'complete': rgb(0, 100, 110),
    'cancelled': rgb(120, 60, 0),
    'error': rgb(170, 0, 0),
}

# All user visible strings. Big button labels: at most 6 characters,
# small labels: at most 13 characters. Encoded as code page 437.
TEXTS = {
    'en': {
        'standby': "READY", 'printing': "PRINTING", 'paused': "PAUSED",
        'complete': "COMPLETE", 'cancelled': "CANCELLED", 'error': "ERROR",
        'nozzle': "NOZZLE", 'bed': "BED", 'off': "off",
        'idle': "No active print", 'file': "File: ",
        'time': "Time %s", 'left': "Left ~%s", 'zoff': "Z offset %+.3f mm",
        'heat': ("HEAT", "PLA %.0f/%.0f\xb0"), 'cool': ("COOL", "heaters off"),
        'home': ("HOME", "all axes"), 'motors': ("MOTORS", "off"),
        'pause': ("PAUSE", "print"), 'resume': ("RESUME", "print"),
        'stop': ("STOP", "cancel"), 'zdown': "nozzle down",
        'zup': "nozzle up",
        'confirm': "Cancel print?", 'yes': "YES", 'no': "NO",
        'shutdown': "SHUTDOWN", 'restart': "RESTART",
        'cal_title': "Touch calibration",
        'cal_hint': "Tap the yellow cross exactly (%d/4)",
        'cal_start': "Tap the 4 crosses on the display.",
        'cal_done': "Touch calibrated: %s\n"
                    "Run SAVE_CONFIG to store the values.",
        'cal_fail': "Touch calibration failed, implausible values: %s",
        'inactive': "Tronxy display is not active",
    },
    'de': {
        'standby': "BEREIT", 'printing': "DRUCKT", 'paused': "PAUSIERT",
        'complete': "FERTIG", 'cancelled': "ABGEBROCHEN", 'error': "FEHLER",
        'nozzle': "D\xdcSE", 'bed': "BETT", 'off': "aus",
        'idle': "Kein Druck aktiv", 'file': "Datei: ",
        'time': "Zeit %s", 'left': "Rest ~%s", 'zoff': "Z-Versatz %+.3f mm",
        'heat': ("HEIZEN", "PLA %.0f/%.0f\xb0"),
        'cool': ("K\xdcHLEN", "alles aus"),
        'home': ("HOME", "alle Achsen"), 'motors': ("MOTOR", "stromlos"),
        'pause': ("PAUSE", "Druck"), 'resume': ("WEITER", "Druck"),
        'stop': ("STOPP", "abbrechen"), 'zdown': "D\xfcse runter",
        'zup': "D\xfcse hoch",
        'confirm': "Druck abbrechen?", 'yes': "JA", 'no': "NEIN",
        'shutdown': "SHUTDOWN", 'restart': "NEUSTART",
        'cal_title': "Touch kalibrieren",
        'cal_hint': "Gelbes Kreuz genau antippen (%d/4)",
        'cal_start': "Bitte die 4 Kreuze auf dem Display antippen.",
        'cal_done': "Touch kalibriert: %s\n"
                    "Mit SAVE_CONFIG dauerhaft speichern.",
        'cal_fail': "Touch-Kalibrierung fehlgeschlagen, Werte unplausibel:"
                    " %s",
        'inactive': "Tronxy-Display nicht aktiv",
    },
}

# Button grid: 4 columns x 2 rows at the bottom
BTN_W, BTN_H = 110, 70
BTN_X = (8, 126, 244, 362)
BTN_Y = (168, 246)
BUTTONS = (
    # key, column, row, color
    ('heat', 0, 0, rgb(175, 85, 0)),
    ('cool', 1, 0, rgb(0, 90, 170)),
    ('home', 2, 0, rgb(70, 82, 105)),
    ('motors', 3, 0, rgb(70, 82, 105)),
    ('pause', 0, 1, rgb(165, 120, 0)),
    ('stop', 1, 1, rgb(175, 25, 25)),
    ('zdown', 2, 1, rgb(0, 115, 120)),
    ('zup', 3, 1, rgb(0, 115, 120)),
)

# Calibration points well inside the screen: the bezel gets in the way near
# the edges, the edge values are extrapolated anyway.
CAL_POINTS = ((60, 60), (W - 61, 60), (60, H - 61), (W - 61, H - 61))


def fmt_time(sec):
    sec = int(max(0., sec))
    return "%d:%02d h" % (sec // 3600, (sec // 60) % 60)


class TronxyDisplay:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.name = config.get_name()
        self.mcu = mcu.get_printer_mcu(printer, config.get('mcu', 'mcu'))
        lang = config.getchoice('language', {'en': 'en', 'de': 'de'}, 'en')
        self.t = TEXTS[lang]
        self.heat_e = config.getfloat('preheat_extruder_temp', 200.,
                                      minval=0., maxval=300.)
        self.heat_b = config.getfloat('preheat_bed_temp', 60.,
                                      minval=0., maxval=120.)
        self.z_step = config.getfloat('z_step', 0.02, above=0., maxval=0.5)
        # raw touch values (12 bit) at the left/right/top/bottom screen edge
        self.cal = [config.getfloat('touch_x_left', 3756.),
                    config.getfloat('touch_x_right', 200.),
                    config.getfloat('touch_y_top', 134.),
                    config.getfloat('touch_y_bottom', 3878.)]
        self.cmd_queue = self.mcu.alloc_command_queue()
        self.fill_cmd = self.text_cmd = self.light_cmd = None
        self.mcu.register_config_callback(self._build_config)
        self.mcu.register_response(self._handle_touch_bg, "tronxy_tft_touch")
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:shutdown",
                                       self._handle_shutdown)
        self.gcode = printer.lookup_object('gcode')
        self.gcode.register_command(
            'TRONXY_TOUCH_CALIBRATE', self.cmd_TRONXY_TOUCH_CALIBRATE,
            desc="Calibrate the Tronxy touch screen (tap 4 crosses)")
        self.gcode.register_command(
            'TRONXY_DISPLAY_REDRAW', self.cmd_TRONXY_DISPLAY_REDRAW,
            desc="Redraw the Tronxy display")
        # state
        self.mode = 'off'       # off, main, dialog, calibrate, shutdown
        self.cache = {}
        self.btn_state = {}
        self.touch_block = 0.
        self.flash_key = None
        self.dialog_until = 0.
        self.error_msg = None
        self.error_until = 0.
        self.cal_raw = []
        self.update_timer = None
        self.objs = {}

    # ------------------------------------------------------------------
    # MCU connection
    def _build_config(self):
        if self.mcu.try_lookup_command(FILL_FMT) is None:
            logging.warning("tronxy_display: MCU firmware has no TFT"
                            " commands - display disabled")
            return
        cq = self.cmd_queue
        self.fill_cmd = self.mcu.lookup_command(FILL_FMT, cq=cq)
        self.text_cmd = self.mcu.lookup_command(TEXT_FMT, cq=cq)
        self.light_cmd = self.mcu.lookup_command(LIGHT_FMT, cq=cq)

    def _active(self):
        return self.fill_cmd is not None

    # ------------------------------------------------------------------
    # Drawing primitives
    def fill(self, x, y, w, h, color):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        w = x1 - x0
        rows = max(1, FILL_MAX_PX // w)
        for yy in range(y0, y1, rows):
            self.fill_cmd.send([x0, yy, w, min(rows, y1 - yy), color],
                               reqclock=BACKGROUND_PRIORITY_CLOCK)

    def text(self, x, y, s, fg, bg, scale=1):
        data = s.encode('cp437', 'replace')
        cw = 8 * scale
        for i in range(0, len(data), TEXT_MAX_CHARS):
            self.text_cmd.send([x + i * cw, y, scale, fg, bg,
                                data[i:i + TEXT_MAX_CHARS]],
                               reqclock=BACKGROUND_PRIORITY_CLOCK)

    def text_center(self, x, y, w, s, fg, bg, scale=1):
        self.text(x + (w - len(s) * 8 * scale) // 2, y, s, fg, bg, scale)

    def field(self, key, x, y, s, width, fg, bg, scale=1):
        # fixed width text field, only resent when it changed
        s = s[:width].ljust(width)
        val = (s, fg, bg)
        if self.cache.get(key) == val:
            return
        self.cache[key] = val
        self.text(x, y, s, fg, bg, scale)

    def clear(self, color=BG):
        self.cache = {}
        self.btn_state = {}
        self.fill(0, 0, W, H, color)

    # ------------------------------------------------------------------
    # Klipper events
    def _handle_ready(self):
        if not self._active():
            return
        lookup = self.printer.lookup_object
        for name in ('extruder', 'heater_bed', 'print_stats',
                     'display_status', 'gcode_move', 'toolhead'):
            self.objs[name] = lookup(name, None)
        self.light_cmd.send([1], reqclock=BACKGROUND_PRIORITY_CLOCK)
        self.mode = 'main'
        self._draw_main_full(self.reactor.monotonic())
        self.update_timer = self.reactor.register_timer(
            self._update, self.reactor.NOW)

    def _handle_shutdown(self):
        if not self._active():
            return
        self.reactor.register_callback(self._draw_shutdown)

    def _update(self, eventtime):
        try:
            if self.mode == 'main':
                self._draw_main(eventtime)
            elif self.mode == 'dialog' and eventtime > self.dialog_until:
                self._close_dialog(eventtime)
        except Exception:
            logging.exception("tronxy_display: drawing failed")
        return eventtime + UPDATE_TIME

    # ------------------------------------------------------------------
    # Printer state
    def _status(self, eventtime):
        o = self.objs
        st = {}
        ps = o['print_stats'].get_status(eventtime) if o['print_stats'] \
            else {}
        st['state'] = ps.get('state', 'standby')
        st['filename'] = ps.get('filename', '') or ''
        st['duration'] = ps.get('print_duration', 0.)
        ds = o['display_status'].get_status(eventtime) \
            if o['display_status'] else {}
        st['progress'] = ds.get('progress', 0.) or 0.
        st['message'] = ds.get('message') or ''
        for name in ('extruder', 'heater_bed'):
            hs = o[name].get_status(eventtime) if o[name] else {}
            st[name] = (hs.get('temperature', 0.), hs.get('target', 0.))
        gm = o['gcode_move'].get_status(eventtime) if o['gcode_move'] \
            else {}
        st['zoff'] = gm['homing_origin'][2] if 'homing_origin' in gm else 0.
        th = o['toolhead'].get_status(eventtime) if o['toolhead'] else {}
        st['homed'] = th.get('homed_axes', '')
        st['busy'] = st['state'] in ('printing', 'paused')
        return st

    def _button_view(self, key, st):
        # -> (big label, small label, enabled)
        t = self.t
        busy = st['busy']
        step = ("%.3f" % self.z_step).rstrip('0').rstrip('.')
        if key == 'heat':
            big, small = t['heat']
            return (big, small % (self.heat_e, self.heat_b), not busy)
        if key in ('cool', 'home', 'motors'):
            return t[key] + (not busy,)
        if key == 'pause':
            if st['state'] == 'paused':
                return t['resume'] + (True,)
            return t['pause'] + (st['state'] == 'printing',)
        if key == 'stop':
            return t['stop'] + (busy,)
        if key == 'zdown':
            return ("Z-" + step, t['zdown'], True)
        return ("Z+" + step, t['zup'], True)

    # ------------------------------------------------------------------
    # Main page
    def _draw_main_full(self, eventtime):
        self.clear()
        self.text(12, 42, self.t['nozzle'], DIM, BG)
        self.text(252, 42, self.t['bed'], DIM, BG)
        self._draw_main(eventtime)

    def _draw_main(self, eventtime):
        t = self.t
        st = self._status(eventtime)
        # status bar
        label = t.get(st['state'], st['state'].upper())
        col = STATE_COLORS.get(st['state'], DIS_BG)
        if self.cache.get('bar_col') != col:
            self.cache['bar_col'] = col
            self.cache.pop('state', None)
            self.cache.pop('pct', None)
            self.fill(0, 0, W, 36, col)
        self.field('state', 10, 2, label, 18, WHITE, col, 2)
        pct = "%3d%%" % (st['progress'] * 100.) if st['busy'] else ""
        self.field('pct', W - 10 - 4 * 16, 2, pct, 4, WHITE, col, 2)
        # temperatures
        for key, x in (('extruder', 12), ('heater_bed', 252)):
            cur, tgt = st[key]
            if tgt > 0.:
                s = "%.0f/%.0f\xb0" % (cur, tgt)
                fg = OK_COL if abs(cur - tgt) < 3. else HEAT_COL
            else:
                s = "%.0f\xb0 %s" % (cur, t['off'])
                fg = WHITE
            self.field(key, x, 60, s, 9, fg, BG, 3)
        # file / message / error
        if self.error_msg and eventtime < self.error_until:
            self.field('line_a', 12, 114, self.error_msg, 57, ERR_COL, BG)
        else:
            self.error_msg = None
            if st['message']:
                line = st['message']
            elif st['filename']:
                line = t['file'] + st['filename']
            else:
                line = t['idle']
            self.field('line_a', 12, 114, line, 57, WHITE, BG)
        # progress bar
        width = int(456 * min(1., max(0., st['progress']))) \
            if st['busy'] or st['state'] == 'complete' else 0
        if self.cache.get('bar') != width:
            self.cache['bar'] = width
            if width:
                self.fill(12, 134, width, 10, BAR_FG)
            self.fill(12 + width, 134, 456 - width, 10, BAR_BG)
        # print time, time left, Z offset
        parts = []
        if st['busy'] or st['state'] == 'complete':
            parts.append(t['time'] % fmt_time(st['duration']))
            p = st['progress']
            if st['busy'] and p > 0.02 and st['duration'] > 60.:
                parts.append(t['left'] % fmt_time(
                    st['duration'] / p - st['duration']))
        parts.append(t['zoff'] % st['zoff'])
        self.field('line_b', 12, 148, "   ".join(parts), 57, DIM, BG)
        # buttons
        for key, cx, cy, col in BUTTONS:
            big, small, enabled = self._button_view(key, st)
            pressed = self.flash_key == key
            view = (big, small, enabled, pressed)
            if self.btn_state.get(key) == view:
                continue
            self.btn_state[key] = view
            if pressed:
                bg, fg = WHITE, BLACK
            elif enabled:
                bg, fg = col, WHITE
            else:
                bg, fg = DIS_BG, DIS_FG
            x, y = BTN_X[cx], BTN_Y[cy]
            self.fill(x, y, BTN_W, BTN_H, bg)
            self.text_center(x, y + 8, BTN_W, big, fg, bg, 2)
            self.text_center(x, y + 46, BTN_W, small, fg, bg, 1)

    def _button_at(self, px, py):
        for key, cx, cy, col in BUTTONS:
            x, y = BTN_X[cx], BTN_Y[cy]
            if x - 3 <= px < x + BTN_W + 3 and y - 3 <= py < y + BTN_H + 3:
                return key
        return None

    def _show_error(self, msg):
        self.error_msg = msg.replace('\n', ' ')
        self.error_until = self.reactor.monotonic() + 8.
        if self.mode == 'main':
            self._draw_main(self.reactor.monotonic())

    # ------------------------------------------------------------------
    # "Cancel print?" confirmation
    def _open_dialog(self, eventtime):
        t = self.t
        self.mode = 'dialog'
        self.dialog_until = eventtime + 15.
        self.cache = {}
        self.btn_state = {}
        bg = rgb(55, 12, 12)
        self.fill(50, 60, 380, 200, WHITE)
        self.fill(53, 63, 374, 194, bg)
        self.text_center(50, 84, 380, t['confirm'], WHITE, bg, 2)
        self.fill(80, 166, 140, 70, rgb(190, 25, 25))
        self.text_center(80, 185, 140, t['yes'], WHITE, rgb(190, 25, 25), 2)
        self.fill(260, 166, 140, 70, rgb(90, 90, 98))
        self.text_center(260, 185, 140, t['no'], WHITE, rgb(90, 90, 98), 2)

    def _close_dialog(self, eventtime):
        self.mode = 'main'
        self._draw_main_full(eventtime)

    # ------------------------------------------------------------------
    # Shutdown page with restart button
    def _draw_shutdown(self, eventtime):
        t = self.t
        self.mode = 'shutdown'
        self.clear()
        red, blue = rgb(170, 0, 0), rgb(0, 90, 170)
        self.fill(0, 0, W, 36, red)
        self.text(10, 2, t['shutdown'], WHITE, red, 2)
        msg = self.printer.get_state_message()[0]
        lines = []
        for raw in msg.split('\n'):
            lines.extend(textwrap.wrap(raw.strip(), 57) or [''])
        for i, line in enumerate(lines[:9]):
            self.text(12, 48 + i * 18, line, WHITE, BG)
        self.fill(120, 236, 240, 70, blue)
        self.text_center(120, 247, 240, t['restart'], WHITE, blue, 2)
        self.text_center(120, 283, 240, "FIRMWARE_RESTART", WHITE, blue, 1)

    # ------------------------------------------------------------------
    # Touch
    def _handle_touch_bg(self, params):
        # runs in the serial thread -> hand over to the reactor
        rx, ry, rz = params['x'], params['y'], params['z']
        self.reactor.register_async_callback(
            (lambda et: self._handle_touch(et, rx, ry, rz)))

    def _map(self, rx, ry):
        xl, xr, yt, yb = self.cal
        px = (rx - xl) * (W - 1) / (xr - xl)
        py = (ry - yt) * (H - 1) / (yb - yt)
        return (min(W - 1, max(0, int(round(px)))),
                min(H - 1, max(0, int(round(py)))))

    def _handle_touch(self, eventtime, rx, ry, rz):
        if not self._active() or self.mode == 'off':
            return
        logging.debug("tronxy_display: touch raw %d,%d z=%d", rx, ry, rz)
        if self.mode == 'calibrate':
            self._calibrate_touch(eventtime, rx, ry)
            return
        if eventtime < self.touch_block:
            return
        px, py = self._map(rx, ry)
        if self.mode == 'shutdown':
            if 110 <= px < 370 and 226 <= py < 316:
                self.touch_block = eventtime + 2.
                self._run("FIRMWARE_RESTART")
            return
        if self.mode == 'dialog':
            if 166 - 6 <= py < 236 + 6:
                if 74 <= px < 226:
                    self.touch_block = eventtime + 1.
                    self._close_dialog(eventtime)
                    self._run("CANCEL_PRINT")
                elif 254 <= px < 406:
                    self.touch_block = eventtime + 0.3
                    self._close_dialog(eventtime)
            return
        key = self._button_at(px, py)
        if key is None:
            return
        st = self._status(eventtime)
        big, small, enabled = self._button_view(key, st)
        if not enabled:
            return
        self.touch_block = eventtime + 0.4
        if key == 'stop':
            self._open_dialog(eventtime)
            return
        self._flash(eventtime, key)
        if key == 'heat':
            self._run("M104 S%.0f\nM140 S%.0f" % (self.heat_e, self.heat_b))
        elif key == 'cool':
            self._run("TURN_OFF_HEATERS")
        elif key == 'home':
            self._run("G28")
        elif key == 'motors':
            self._run("M84")
        elif key == 'pause':
            self._run("RESUME" if st['state'] == 'paused' else "PAUSE")
        elif key in ('zdown', 'zup'):
            adj = self.z_step if key == 'zup' else -self.z_step
            move = " MOVE=1" if 'z' in st['homed'] else ""
            self._run("SET_GCODE_OFFSET Z_ADJUST=%.3f%s" % (adj, move))

    def _flash(self, eventtime, key):
        self.flash_key = key
        self._draw_main(eventtime)

        def unflash(et):
            if self.flash_key == key:
                self.flash_key = None
                if self.mode == 'main':
                    self._draw_main(et)
        self.reactor.register_callback(unflash, eventtime + 0.2)

    def _run(self, script):
        def cb(eventtime):
            try:
                self.gcode.run_script(script)
            except self.printer.command_error as e:
                self._show_error(str(e))
            except Exception:
                logging.exception("tronxy_display: %s failed", script)
            if self.mode == 'main':
                self._draw_main(self.reactor.monotonic())
        self.reactor.register_callback(cb)

    # ------------------------------------------------------------------
    # Touch calibration
    def _draw_cal_point(self):
        n = len(self.cal_raw)
        self.clear()
        self.text_center(0, 110, W, self.t['cal_title'], WHITE, BG, 2)
        self.text_center(0, 160, W, self.t['cal_hint'] % (n + 1,), DIM, BG)
        x, y = CAL_POINTS[n]
        self.fill(x - 18, y - 1, 37, 3, rgb(255, 230, 0))
        self.fill(x - 1, y - 18, 3, 37, rgb(255, 230, 0))

    def _calibrate_touch(self, eventtime, rx, ry):
        if eventtime < self.touch_block:
            return
        self.touch_block = eventtime + 0.6
        self.cal_raw.append((rx, ry))
        if len(self.cal_raw) < 4:
            self._draw_cal_point()
            return
        r = self.cal_raw
        (x0, y0), (x1, _), (_, y1), _ = CAL_POINTS
        left = (r[0][0] + r[2][0]) / 2.
        right = (r[1][0] + r[3][0]) / 2.
        top = (r[0][1] + r[1][1]) / 2.
        bottom = (r[2][1] + r[3][1]) / 2.
        if abs(right - left) < 1500 or abs(bottom - top) < 1000:
            self.gcode.respond_info(self.t['cal_fail'] % (r,))
            self.mode = 'main'
            self._draw_main_full(eventtime)
            return
        sx = (right - left) / (x1 - x0)
        sy = (bottom - top) / (y1 - y0)
        self.cal = [left - x0 * sx, left + (W - 1 - x0) * sx,
                    top - y0 * sy, top + (H - 1 - y0) * sy]
        configfile = self.printer.lookup_object('configfile')
        names = ('touch_x_left', 'touch_x_right', 'touch_y_top',
                 'touch_y_bottom')
        for name, val in zip(names, self.cal):
            configfile.set(self.name, name, "%.0f" % (val,))
        self.gcode.respond_info(self.t['cal_done'] % (
            ", ".join("%s=%.0f" % nv for nv in zip(names, self.cal)),))
        self.mode = 'main'
        self._draw_main_full(eventtime)

    # ------------------------------------------------------------------
    # G-code commands
    def cmd_TRONXY_TOUCH_CALIBRATE(self, gcmd):
        if not self._active() or self.mode in ('off', 'shutdown'):
            raise gcmd.error(self.t['inactive'])
        self.mode = 'calibrate'
        self.cal_raw = []
        self.touch_block = self.reactor.monotonic() + 0.5
        self._draw_cal_point()
        gcmd.respond_info(self.t['cal_start'])

    def cmd_TRONXY_DISPLAY_REDRAW(self, gcmd):
        if not self._active() or self.mode == 'off':
            raise gcmd.error(self.t['inactive'])
        if self.mode == 'shutdown':
            self._draw_shutdown(self.reactor.monotonic())
        else:
            self.mode = 'main'
            self._draw_main_full(self.reactor.monotonic())


def load_config(config):
    return TronxyDisplay(config)
