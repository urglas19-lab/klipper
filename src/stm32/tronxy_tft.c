// Stock touch screen of Tronxy printers with the TRONXY_F446 board
// (e.g. X5SA Pro): ILI9488 480x320 on FMC bank 4 (NE4=PG12, RS=A12=PG2),
// XPT2046 resistive touch controller on bit-banged SPI.
//
// The MCU initializes the panel on its own (boot screen "Waiting for
// Klipper..."), provides simple drawing commands and reports touches as raw
// values. The user interface is drawn by klippy/extras/tronxy_display.py.
// Panel init runs as a non-blocking state machine in a task, every step
// takes only a few milliseconds.
//
// Panel access based on hbtft.c from
//   github.com/adrianeb2015/klipper.on.hellbot.magna.2.300 (GPLv3)
// Pins and FMC timing taken from Marlin (pins_TRONXY_F446.h, tft_fsmc.cpp).
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <stdint.h>
#include "autoconf.h"            // CONFIG_TRONXY_TFT_SPLASH_DE
#include "command.h"             // DECL_COMMAND_FLAGS
#include "generic/armcm_timer.h" // udelay
#include "generic/misc.h"        // timer_read_time
#include "gpio.h"                // gpio_out_setup
#include "internal.h"            // GPIO, gpio_peripheral, FMC_Bank1
#include "sched.h"               // DECL_TASK
#include "tronxy_font.h"         // tronxy_font

/****************************************************************
 * FMC -> ILI9488
 ****************************************************************/

// Bank 4 = 0x6C000000; RS on A12 -> address bit 13 (0x2000) in 16 bit mode.
// REG sits just below the RS boundary, RAM right on it (as in Marlin).
#define LCD_REG (*(volatile uint16_t *)0x6C001FFEU)
#define LCD_RAM (*(volatile uint16_t *)0x6C002000U)

#define TFT_W 480
#define TFT_H 320

static inline void dsb(void) { __asm__ volatile("dsb" ::: "memory"); }
static inline void tft_cmd(uint8_t c) { LCD_REG = c; }
static inline void tft_dat(uint16_t d) { LCD_RAM = d; }

static const uint8_t fmc_pins[] = {
    GPIO('D', 14), GPIO('D', 15), GPIO('D', 0), GPIO('D', 1),   // D0-D3
    GPIO('E', 7), GPIO('E', 8), GPIO('E', 9), GPIO('E', 10),    // D4-D7
    GPIO('E', 11), GPIO('E', 12), GPIO('E', 13), GPIO('E', 14), // D8-D11
    GPIO('E', 15), GPIO('D', 8), GPIO('D', 9), GPIO('D', 10),   // D12-D15
    GPIO('D', 4), GPIO('D', 5),                                 // NOE, NWE
    GPIO('G', 12), GPIO('G', 2),                                // NE4, A12
};

static void
fmc_init(void)
{
    RCC->AHB3ENR |= RCC_AHB3ENR_FMCEN;
    (void)RCC->AHB3ENR;
    for (uint32_t i = 0; i < sizeof(fmc_pins); i++)
        gpio_peripheral(fmc_pins[i], GPIO_FUNCTION(12) | GPIO_HIGH_SPEED, 0);
    // Read timing (BTR4) as in Marlin: ADDSET 15, ADDHLD 15, DATAST 24, mode A
    FMC_Bank1->BTCR[7] = 15 | (15 << 4) | (24 << 8) | (15 << 20) | (15 << 24);
    // Write timing (BWTR4) as in Marlin: ADDSET 8, ADDHLD 15, DATAST 8
    FMC_Bank1E->BWTR[6] = 8 | (15 << 4) | (8 << 8);
    // BCR4: SRAM, 16 bit, write enable, extended mode, bank enable.
    // Bit 7 is reserved and keeps its reset value.
    uint32_t bcr = FMC_Bank1->BTCR[6] & (1 << 7);
    bcr |= (1 << 0) | (1 << 4) | (1 << 12) | (1 << 14);
    FMC_Bank1->BTCR[6] = bcr;
    dsb();
}

static uint32_t
tft_read_id(void)
{
    tft_cmd(0xD3);               // RDID4: dummy, 0x00, 0x94, 0x88
    dsb();
    (void)LCD_RAM;
    uint32_t a = LCD_RAM & 0xFF;
    uint32_t b = LCD_RAM & 0xFF;
    uint32_t c = LCD_RAM & 0xFF;
    return (a << 16) | (b << 8) | c;
}

static void
tft_window(uint16_t x0, uint16_t y0, uint16_t x1, uint16_t y1)
{
    tft_cmd(0x2A);
    tft_dat(x0 >> 8); tft_dat(x0 & 0xFF); tft_dat(x1 >> 8); tft_dat(x1 & 0xFF);
    tft_cmd(0x2B);
    tft_dat(y0 >> 8); tft_dat(y0 & 0xFF); tft_dat(y1 >> 8); tft_dat(y1 & 0xFF);
    tft_cmd(0x2C);
}

static void
tft_fill(int x, int y, int w, int h, uint16_t color)
{
    // clip to the screen
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > TFT_W) w = TFT_W - x;
    if (y + h > TFT_H) h = TFT_H - y;
    if (w <= 0 || h <= 0)
        return;
    tft_window(x, y, x + w - 1, y + h - 1);
    uint32_t n = (uint32_t)w * h;
    while (n--)
        LCD_RAM = color;
}

// Text in the 8x16 font (code page 437), background is drawn as well.
// Characters that do not fit on the screen completely are skipped.
static void
tft_text(int x, int y, const uint8_t *s, int len, uint16_t fg, uint16_t bg
         , int sc)
{
    if (sc < 1)
        sc = 1;
    if (sc > 4)
        sc = 4;
    int cw = 8 * sc, ch = 16 * sc;
    if (y < 0 || y + ch > TFT_H)
        return;
    for (int i = 0; i < len; i++, x += cw) {
        if (x < 0 || x + cw > TFT_W)
            continue;
        const uint8_t *g = tronxy_font[s[i]];
        tft_window(x, y, x + cw - 1, y + ch - 1);
        for (int r = 0; r < ch; r++) {
            uint8_t bits = g[r / sc];
            for (int c = 0; c < 8; c++) {
                uint16_t col = (bits & (0x80 >> c)) ? fg : bg;
                for (int k = 0; k < sc; k++)
                    LCD_RAM = col;
            }
        }
    }
}

static void
tft_str(int x, int y, const char *s, uint16_t fg, uint16_t bg, int sc)
{
    int n = 0;
    while (s[n])
        n++;
    tft_text(x, y, (const uint8_t *)s, n, fg, bg, sc);
}

// Init sequence as in Marlin/hbtft (ILI9488), MADCTL 0xE8 as in the stock
// Tronxy firmware
static void
ili9488_config(void)
{
    static const uint8_t pgam[15] = {
        0x00, 0x03, 0x09, 0x08, 0x16, 0x0A, 0x3F, 0x78,
        0x4C, 0x09, 0x0A, 0x08, 0x16, 0x1A, 0x0F };
    static const uint8_t ngam[15] = {
        0x00, 0x16, 0x19, 0x03, 0x0F, 0x05, 0x32, 0x45,
        0x46, 0x04, 0x0E, 0x0D, 0x35, 0x37, 0x0F };
    tft_cmd(0xB1); tft_dat(0xA0);
    tft_cmd(0xB4); tft_dat(0x02);
    tft_cmd(0xB6); tft_dat(0x02); tft_dat(0x02);
    tft_cmd(0xC0); tft_dat(0x15); tft_dat(0x17);
    tft_cmd(0xC1); tft_dat(0x41);
    tft_cmd(0xC5); tft_dat(0x00); tft_dat(0x12); tft_dat(0x80);
    tft_cmd(0xE9); tft_dat(0x00);
    tft_cmd(0xF7); tft_dat(0xA9); tft_dat(0x51); tft_dat(0x2C); tft_dat(0x82);
    tft_cmd(0x36); tft_dat(0xE8);   // landscape 480x320
    tft_cmd(0x3A); tft_dat(0x55);   // RGB565
    tft_cmd(0xE0);
    for (int i = 0; i < 15; i++) tft_dat(pgam[i]);
    tft_cmd(0xE1);
    for (int i = 0; i < 15; i++) tft_dat(ngam[i]);
    tft_cmd(0x20);                  // INVOFF
    tft_cmd(0x13);                  // NORON
    tft_cmd(0x29);                  // DISPON
}

/****************************************************************
 * XPT2046 touch (software SPI): CS PD11, SCK PB13, MISO PB14, MOSI PB15
 ****************************************************************/

static struct gpio_out t_cs, t_sck, t_mosi;
static struct gpio_in t_miso;

#define XPT_X  0x90
#define XPT_Y  0xD0
#define XPT_Z1 0xB0
// Pressure threshold (12 bit). Marlin uses 130; light taps were seen just
// above that, so slightly lower here.
#define XPT_Z1_MIN 100

static void
xpt_init(void)
{
    t_cs = gpio_out_setup(GPIO('D', 11), 1);
    t_sck = gpio_out_setup(GPIO('B', 13), 0);
    t_mosi = gpio_out_setup(GPIO('B', 15), 0);
    t_miso = gpio_in_setup(GPIO('B', 14), 0);
}

// One conversion: 8 bit command, then 16 clocks (busy, D11..D0, 3x 0)
static uint16_t
xpt_xfer(uint8_t cmd)
{
    for (int i = 7; i >= 0; i--) {
        gpio_out_write(t_mosi, (cmd >> i) & 1);
        udelay(1);
        gpio_out_write(t_sck, 1);
        udelay(1);
        gpio_out_write(t_sck, 0);
    }
    gpio_out_write(t_mosi, 0);
    uint32_t rx = 0;
    for (int i = 0; i < 16; i++) {
        udelay(1);
        gpio_out_write(t_sck, 1);
        udelay(1);
        rx = (rx << 1) | (gpio_in_read(t_miso) ? 1 : 0);
        gpio_out_write(t_sck, 0);
    }
    return (rx >> 3) & 0xFFF;
}

static uint16_t
absdiff(uint16_t a, uint16_t b)
{
    return a > b ? a - b : b - a;
}

// 3 samples, average the two closest ones (as in Marlin)
static uint16_t
xpt_sample(uint8_t cmd)
{
    uint16_t d[3];
    gpio_out_write(t_cs, 0);
    for (int i = 0; i < 3; i++)
        d[i] = xpt_xfer(cmd);
    gpio_out_write(t_cs, 1);
    uint16_t d01 = absdiff(d[0], d[1]), d02 = absdiff(d[0], d[2]);
    uint16_t d12 = absdiff(d[1], d[2]);
    if (d01 > d02 || d01 > d12)
        d[d02 > d12 ? 0 : 1] = d[2];
    return (d[0] + d[1]) / 2;
}

/****************************************************************
 * State machine: panel init, boot screen, touch polling
 ****************************************************************/

enum {
    ST_START, ST_RESET_LOW, ST_RESET_WAIT, ST_SLEEPOUT, ST_DISPON,
    ST_DRAW, ST_DONE
};

#define DRAW_BANDS (TFT_H / 32)

#if CONFIG_TRONXY_TFT_SPLASH_DE
#define SPLASH_WAIT "Warte auf Klipper..."
#define SPLASH_BADID "Display-ID unbekannt"
#else
#define SPLASH_WAIT "Waiting for Klipper..."
#define SPLASH_BADID "Unknown display ID"
#endif

static uint8_t state = ST_START;
static uint32_t deadline;
static uint16_t draw_step;
static struct gpio_out tft_rst, tft_bl;
static uint32_t tft_id;

static uint8_t touch_down;
static uint32_t touch_deadline;

// Boot screen in small steps (at most ~2 ms each)
static int
draw_splash(int n)
{
    if (n < DRAW_BANDS) {
        tft_fill(0, n * 32, TFT_W, 32, 0x0000);
        return 0;
    }
    switch (n - DRAW_BANDS) {
    case 0:
        tft_str((TFT_W - 7 * 32) / 2, 90, "KLIPPER", 0xFFFF, 0x0000, 4);
        return 0;
    case 1:
        tft_str((TFT_W - (int)(sizeof(SPLASH_WAIT) - 1) * 16) / 2, 180
                , SPLASH_WAIT, 0xBDF7, 0x0000, 2);
        return 0;
    default:
        if ((tft_id & 0xFFFF) != 0x9488)
            tft_str(8, TFT_H - 24, SPLASH_BADID, 0xF800, 0x0000, 1);
        return 1;
    }
}

static void
touch_poll(uint32_t now)
{
    if (timer_is_before(now, touch_deadline))
        return;
    touch_deadline = now + timer_from_us(25000);
    uint16_t z = xpt_sample(XPT_Z1);
    if (z < XPT_Z1_MIN) {
        touch_down = 0;
        return;
    }
    if (touch_down)
        return;   // report only once per touch
    uint16_t rx = xpt_sample(XPT_X), ry = xpt_sample(XPT_Y);
    if (xpt_sample(XPT_Z1) < XPT_Z1_MIN)
        return;   // released during the measurement -> discard
    touch_down = 1;
    sendf("tronxy_tft_touch x=%hu y=%hu z=%hu", rx, ry, z);
}

void
tronxy_tft_task(void)
{
    uint32_t now = timer_read_time();

    switch (state) {
    case ST_START:
        tft_bl = gpio_out_setup(GPIO('G', 8), 1);
        tft_rst = gpio_out_setup(GPIO('B', 12), 0);
        fmc_init();
        xpt_init();
        deadline = now + timer_from_us(20000);
        state = ST_RESET_LOW;
        return;
    case ST_RESET_LOW:
        if (timer_is_before(now, deadline))
            return;
        gpio_out_write(tft_rst, 1);
        deadline = now + timer_from_us(150000);
        state = ST_RESET_WAIT;
        return;
    case ST_RESET_WAIT:
        if (timer_is_before(now, deadline))
            return;
        tft_cmd(0x11);                  // SLPOUT
        deadline = now + timer_from_us(150000);
        state = ST_SLEEPOUT;
        return;
    case ST_SLEEPOUT:
        if (timer_is_before(now, deadline))
            return;
        tft_id = tft_read_id();
        ili9488_config();
        deadline = now + timer_from_us(50000);
        state = ST_DISPON;
        return;
    case ST_DISPON:
        if (timer_is_before(now, deadline))
            return;
        draw_step = 0;
        state = ST_DRAW;
        return;
    case ST_DRAW:
        if (draw_splash(draw_step++))
            state = ST_DONE;
        return;
    default:
        touch_poll(now);
        return;
    }
}
DECL_TASK(tronxy_tft_task);

/****************************************************************
 * Host commands (also allowed in shutdown, so the screen can still
 * show what happened)
 ****************************************************************/

void
command_tronxy_tft_fill(uint32_t *args)
{
    if (state != ST_DONE)
        return;
    tft_fill(args[0], args[1], args[2], args[3], args[4]);
}
DECL_COMMAND_FLAGS(command_tronxy_tft_fill, HF_IN_SHUTDOWN,
                   "tronxy_tft_fill x=%hu y=%hu w=%hu h=%hu color=%hu");

void
command_tronxy_tft_text(uint32_t *args)
{
    if (state != ST_DONE)
        return;
    uint8_t len = args[5];
    const uint8_t *s = command_decode_ptr(args[6]);
    tft_text(args[0], args[1], s, len, args[3], args[4], args[2]);
}
DECL_COMMAND_FLAGS(command_tronxy_tft_text, HF_IN_SHUTDOWN,
                   "tronxy_tft_text x=%hu y=%hu scale=%c fg=%hu bg=%hu"
                   " text=%*s");

void
command_tronxy_tft_backlight(uint32_t *args)
{
    if (state == ST_START)
        return;
    gpio_out_write(tft_bl, args[0] ? 1 : 0);
}
DECL_COMMAND_FLAGS(command_tronxy_tft_backlight, HF_IN_SHUTDOWN,
                   "tronxy_tft_backlight on=%c");

void
command_tronxy_tft_query(uint32_t *args)
{
    sendf("tronxy_tft_info id=%u ready=%c", tft_id, state == ST_DONE);
}
DECL_COMMAND_FLAGS(command_tronxy_tft_query, HF_IN_SHUTDOWN,
                   "tronxy_tft_query");
