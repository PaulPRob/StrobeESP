/*
 * LED Strobe firmware for the Waveshare ESP32-C3-Zero.
 *
 * Flashes the onboard WS2812 RGB LED (GPIO10) in any RGB colour (0-255 per channel,
 * all channels flashing together) at 0.5-200 Hz with a 1-99 % duty cycle. Controlled by text
 * commands over the native USB port (USB-Serial/JTAG, /dev/ttyACM0) or over Bluetooth LE
 * (ble_link.c, PIN pairing required); settings are saved to NVS and restored at boot.
 *
 * Timing: a 1 MHz GPTimer alarm marks each on/off edge using absolute counts, so the
 * frequency doesn't drift. The ISR wakes a high-priority task that sends the WS2812 frame.
 * Every edge has the same frame latency (~30 us), so the on-time is preserved.
 *
 * WS2812 limitation: after each frame the data line must idle for the latch/reset time
 * (50 us for WS2812, up to 280 us for WS2812B V5) before the next colour is accepted.
 * On and off phases are therefore clamped to MIN_PULSE_US; the effective duty is reported.
 * Channel levels below 255 are dimmed by the WS2812's own PWM (~0.4-2 kHz, free-running),
 * which can beat against short on-times; levels of 0 or 255 are unaffected.
 *
 * Protocol (one command per line, case-insensitive, replies end with \n). The same commands
 * work over USB and Bluetooth, except PIN and UNPAIR which are accepted over USB only:
 *   ID                      -> ID LEDSTROBE <version>
 *   ? | GET                 -> status line
 *   F <hz>                  set frequency, 0.5 .. 200
 *   D <percent>             set duty cycle, 1 .. 99
 *   RGB <r> <g> <b>         set colour, each channel 0 .. 255
 *   C <R|G|B|W>             shortcut: full red, green, blue or white
 *   RUN <0|1>               stop / start flashing
 *   SET <hz> <pct> <r> <g> <b> <0|1>   set everything at once
 *   STATS                   LED update latency since the last STATS (timing diagnostics)
 *   BLE                     Bluetooth name, state and number of paired devices
 *   PIN                     (USB only) show the 6-digit Bluetooth pairing PIN
 *   PIN <6 digits>          (USB only) set a new PIN; removes all existing pairings
 *   UNPAIR                  (USB only) remove all pairings
 * Status/OK reply: OK F=<hz> D=<pct> RGB=<r>,<g>,<b> RUN=<0|1> ON_US=<us> OFF_US=<us> DEFF=<pct>
 * Errors:          ERR <message>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <ctype.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/gptimer.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "led_strip.h"
#include "strobe.h"

#define FW_VERSION      "2.1"
#define LED_GPIO        10
#define LED_COLOR_FMT   LED_STRIP_COLOR_COMPONENT_FMT_RGB   // C3-Zero LED is RGB order (verified)
#define MIN_PULSE_US    300     // WS2812B reset/latch time + frame time, rounded up
#define FREQ_MIN        0.5f
#define FREQ_MAX        200.0f
#define DUTY_MIN        1.0f
#define DUTY_MAX        99.0f
#define SAVE_DELAY_US   (2 * 1000 * 1000)   // debounce NVS writes while sliders move
#define EDGE_FROM_TIMER 0x2                 // LED task notification flag: edge came from the timer

typedef struct {
    float freq;     // Hz
    float duty;     // percent
    uint8_t rgb[3]; // channel levels 0-255
    uint8_t run;
} settings_t;

static settings_t s_set = { .freq = 2.0f, .duty = 50.0f, .rgb = {255, 0, 0}, .run = 1 };

static gptimer_handle_t s_timer;
static led_strip_handle_t s_strip;
static TaskHandle_t s_led_task;
static esp_timer_handle_t s_save_timer;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

// Shared between the ISR and set_timing(); guarded by s_lock
static volatile uint32_t s_on_us, s_off_us;
static volatile bool s_phase_on;
static volatile uint64_t s_phase_start, s_next_alarm;
static gptimer_alarm_config_t s_alarm;   // static (not const) so the ISR can use it
static SemaphoreHandle_t s_cmd_mutex;      // serialises commands from USB and Bluetooth

// Latency from timer edge to the end of the LED frame, in us (guarded by s_lock)
static uint32_t s_lat_min = UINT32_MAX, s_lat_max, s_lat_n;
static uint64_t s_lat_sum;

static bool IRAM_ATTR on_alarm(gptimer_handle_t timer, const gptimer_alarm_event_data_t *edata, void *ctx)
{
    portENTER_CRITICAL_ISR(&s_lock);
    s_phase_on = !s_phase_on;
    s_phase_start = edata->alarm_value;
    s_next_alarm = edata->alarm_value + (s_phase_on ? s_on_us : s_off_us);
    s_alarm.alarm_count = s_next_alarm;
    gptimer_set_alarm_action(timer, &s_alarm);
    bool on = s_phase_on;
    portEXIT_CRITICAL_ISR(&s_lock);

    BaseType_t woken = pdFALSE;
    xTaskNotifyFromISR(s_led_task, on | EDGE_FROM_TIMER, eSetValueWithOverwrite, &woken);
    return woken == pdTRUE;
}

static void led_show(bool on)
{
    uint8_t rgb[3] = {0, 0, 0};
    if (on) {
        portENTER_CRITICAL(&s_lock);
        memcpy(rgb, s_set.rgb, sizeof(rgb));
        portEXIT_CRITICAL(&s_lock);
    }
    led_strip_set_pixel(s_strip, 0, rgb[0], rgb[1], rgb[2]);
    led_strip_refresh(s_strip);
}

static void led_task(void *arg)
{
    uint32_t v;
    for (;;) {
        xTaskNotifyWait(0, 0, &v, portMAX_DELAY);
        led_show(v & 1);
        if (v & EDGE_FROM_TIMER) {
            uint64_t now;
            gptimer_get_raw_count(s_timer, &now);
            portENTER_CRITICAL(&s_lock);
            uint32_t lat = (uint32_t)(now - s_phase_start);
            if (lat < s_lat_min) s_lat_min = lat;
            if (lat > s_lat_max) s_lat_max = lat;
            s_lat_sum += lat;
            s_lat_n++;
            portEXIT_CRITICAL(&s_lock);
        }
    }
}

// Convert frequency/duty into clamped on/off times
static void compute_timing(const settings_t *s, uint32_t *on_us, uint32_t *off_us)
{
    uint32_t period = (uint32_t)(1e6f / s->freq + 0.5f);
    uint32_t on = (uint32_t)(period * s->duty / 100.0f + 0.5f);
    if (on < MIN_PULSE_US) on = MIN_PULSE_US;
    if (on > period - MIN_PULSE_US) on = period - MIN_PULSE_US;
    *on_us = on;
    *off_us = period - on;
}

// Apply new on/off times. If the phase in progress would now run too long
// (e.g. dropping from 0.5 Hz to 200 Hz), cut it short so the change is immediate.
static void set_timing(uint32_t on_us, uint32_t off_us)
{
    portENTER_CRITICAL(&s_lock);
    s_on_us = on_us;
    s_off_us = off_us;
    if (s_set.run) {
        uint64_t now;
        gptimer_get_raw_count(s_timer, &now);
        uint64_t want = s_phase_start + (s_phase_on ? on_us : off_us);
        if (want < s_next_alarm) {
            if (want < now + 20) want = now + 20;
            s_next_alarm = want;
            s_alarm.alarm_count = want;
            gptimer_set_alarm_action(s_timer, &s_alarm);
        }
    }
    portEXIT_CRITICAL(&s_lock);
}

static void set_running(bool run)
{
    if (run == s_set.run) return;
    s_set.run = run;
    if (run) {
        gptimer_set_raw_count(s_timer, 0);
        portENTER_CRITICAL(&s_lock);
        s_phase_on = true;
        s_phase_start = 0;
        s_next_alarm = s_on_us;
        s_alarm.alarm_count = s_on_us;
        gptimer_set_alarm_action(s_timer, &s_alarm);
        portEXIT_CRITICAL(&s_lock);
        xTaskNotify(s_led_task, 1, eSetValueWithOverwrite);
        gptimer_start(s_timer);
    } else {
        gptimer_stop(s_timer);
        xTaskNotify(s_led_task, 0, eSetValueWithOverwrite);
    }
}

static void apply(const settings_t *s)
{
    uint32_t on, off;
    portENTER_CRITICAL(&s_lock);
    memcpy(s_set.rgb, s->rgb, sizeof(s_set.rgb));
    portEXIT_CRITICAL(&s_lock);
    s_set.freq = s->freq;
    s_set.duty = s->duty;
    compute_timing(&s_set, &on, &off);
    set_timing(on, off);
    set_running(s->run);
}

static void save_cb(void *arg)
{
    nvs_handle_t h;
    if (nvs_open("strobe", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, "set2", &s_set, sizeof(s_set));
        nvs_commit(h);
        nvs_close(h);
    }
}

static void load_settings(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    nvs_handle_t h;
    settings_t s;
    size_t len = sizeof(s);
    if (nvs_open("strobe", NVS_READONLY, &h) == ESP_OK) {
        if (nvs_get_blob(h, "set2", &s, &len) == ESP_OK && len == sizeof(s) &&
            s.freq >= FREQ_MIN && s.freq <= FREQ_MAX && s.duty >= DUTY_MIN && s.duty <= DUTY_MAX) {
            s_set = s;
        }
        nvs_close(h);
    }
}

static void format_status(char *out, size_t outlen)
{
    uint32_t on = s_on_us, off = s_off_us;
    snprintf(out, outlen, "OK F=%.3f D=%.2f RGB=%u,%u,%u RUN=%d ON_US=%lu OFF_US=%lu DEFF=%.2f\n",
           s_set.freq, s_set.duty, s_set.rgb[0], s_set.rgb[1], s_set.rgb[2], s_set.run,
           (unsigned long)on, (unsigned long)off, 100.0 * on / (on + off));
}

static bool parse_level(const char *t, uint8_t *out)
{
    if (!t) return false;
    char *end;
    long v = strtol(t, &end, 10);
    if (end == t || *end || v < 0 || v > 255) return false;
    *out = (uint8_t)v;
    return true;
}

static bool parse_rgb(char *r, char *g, char *b, uint8_t rgb[3])
{
    return parse_level(r, &rgb[0]) && parse_level(g, &rgb[1]) && parse_level(b, &rgb[2]);
}

static bool parse_float(const char *t, float lo, float hi, float *out)
{
    if (!t) return false;
    char *end;
    float v = strtof(t, &end);
    if (end == t || *end) return false;
    if (v < lo || v > hi) return false;
    *out = v;
    return true;
}

static void format_stats(char *out, size_t outlen)
{
    portENTER_CRITICAL(&s_lock);
    uint32_t n = s_lat_n, mn = s_lat_min, mx = s_lat_max;
    uint64_t sum = s_lat_sum;
    s_lat_n = 0, s_lat_sum = 0, s_lat_min = UINT32_MAX, s_lat_max = 0;
    portEXIT_CRITICAL(&s_lock);
    if (n == 0) {
        snprintf(out, outlen, "STATS N=0\n");
    } else {
        snprintf(out, outlen, "STATS N=%lu LAT_MIN_US=%lu LAT_AVG_US=%lu LAT_MAX_US=%lu\n",
                 (unsigned long)n, (unsigned long)mn, (unsigned long)(sum / n), (unsigned long)mx);
    }
}

// Writes the reply to out and returns NULL, or returns an error message
static const char *handle_command(char *line, bool from_usb, char *out, size_t outlen)
{
    char *save;
    char *cmd = strtok_r(line, " \t", &save);
    if (!cmd) return "empty command";
    char *a[6];
    for (int i = 0; i < 6; i++) a[i] = strtok_r(NULL, " \t", &save);
    char *a1 = a[0], *a2 = a[1], *a3 = a[2], *a4 = a[3], *a5 = a[4], *a6 = a[5];
    settings_t s = s_set;

    if (!strcasecmp(cmd, "ID")) {
        snprintf(out, outlen, "ID LEDSTROBE %s\n", FW_VERSION);
        return NULL;
    } else if (!strcmp(cmd, "?") || !strcasecmp(cmd, "GET")) {
        format_status(out, outlen);
        return NULL;
    } else if (!strcasecmp(cmd, "STATS")) {
        format_stats(out, outlen);
        return NULL;
    } else if (!strcasecmp(cmd, "BLE")) {
        ble_link_status(out, outlen);
        return NULL;
    } else if (!strcasecmp(cmd, "PIN")) {
        if (!from_usb) return "PIN is only available over USB";
        if (a1) {
            char *end;
            unsigned long pin = strtoul(a1, &end, 10);
            if (strlen(a1) != 6 || *end || !ble_link_set_pin(pin)) return "PIN must be 6 digits";
        }
        snprintf(out, outlen, "PIN %06lu\n", (unsigned long)ble_link_get_pin());
        return NULL;
    } else if (!strcasecmp(cmd, "UNPAIR")) {
        if (!from_usb) return "UNPAIR is only available over USB";
        ble_link_unpair_all();
        ble_link_status(out, outlen);
        return NULL;
    } else if (!strcasecmp(cmd, "F")) {
        if (!parse_float(a1, FREQ_MIN, FREQ_MAX, &s.freq)) return "frequency must be 0.5-200";
    } else if (!strcasecmp(cmd, "D")) {
        if (!parse_float(a1, DUTY_MIN, DUTY_MAX, &s.duty)) return "duty must be 1-99";
    } else if (!strcasecmp(cmd, "RGB")) {
        if (!parse_rgb(a1, a2, a3, s.rgb)) return "RGB takes three levels 0-255";
    } else if (!strcasecmp(cmd, "C")) {
        static const uint8_t presets[][3] = { {255, 0, 0}, {0, 255, 0}, {0, 0, 255}, {255, 255, 255} };
        const char *p = a1 && a1[0] && !a1[1] ? strchr("RGBW", toupper((unsigned char)a1[0])) : NULL;
        if (!p) return "colour must be R, G, B or W";
        memcpy(s.rgb, presets[p - "RGBW"], 3);
    } else if (!strcasecmp(cmd, "RUN")) {
        if (!a1 || (strcmp(a1, "0") && strcmp(a1, "1"))) return "RUN takes 0 or 1";
        s.run = a1[0] == '1';
    } else if (!strcasecmp(cmd, "SET")) {
        if (!parse_float(a1, FREQ_MIN, FREQ_MAX, &s.freq)) return "frequency must be 0.5-200";
        if (!parse_float(a2, DUTY_MIN, DUTY_MAX, &s.duty)) return "duty must be 1-99";
        if (!parse_rgb(a3, a4, a5, s.rgb)) return "colour levels must be 0-255";
        if (!a6 || (strcmp(a6, "0") && strcmp(a6, "1"))) return "RUN takes 0 or 1";
        s.run = a6[0] == '1';
    } else {
        return "unknown command (ID, ?, F, D, RGB, C, RUN, SET, STATS, BLE, PIN, UNPAIR)";
    }

    apply(&s);
    esp_timer_stop(s_save_timer);
    esp_timer_start_once(s_save_timer, SAVE_DELAY_US);
    format_status(out, outlen);
    return NULL;
}

void strobe_command(const char *line, bool from_usb, char *out, size_t outlen)
{
    char buf[STROBE_LINE_MAX];
    strlcpy(buf, line, sizeof(buf));
    xSemaphoreTake(s_cmd_mutex, portMAX_DELAY);
    const char *err = handle_command(buf, from_usb, out, outlen);
    xSemaphoreGive(s_cmd_mutex);
    if (err) snprintf(out, outlen, "ERR %s\n", err);
}

void app_main(void)
{
    s_cmd_mutex = xSemaphoreCreateMutex();
    load_settings();

    led_strip_config_t strip_cfg = {
        .strip_gpio_num = LED_GPIO,
        .max_leds = 1,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_COLOR_FMT,
    };
    led_strip_rmt_config_t rmt_cfg = { .resolution_hz = 10 * 1000 * 1000 };
    ESP_ERROR_CHECK(led_strip_new_rmt_device(&strip_cfg, &rmt_cfg, &s_strip));
    led_strip_clear(s_strip);

    xTaskCreate(led_task, "led", 3072, NULL, configMAX_PRIORITIES - 1, &s_led_task);

    gptimer_config_t tcfg = {
        .clk_src = GPTIMER_CLK_SRC_DEFAULT,
        .direction = GPTIMER_COUNT_UP,
        .resolution_hz = 1000 * 1000,
    };
    ESP_ERROR_CHECK(gptimer_new_timer(&tcfg, &s_timer));
    gptimer_event_callbacks_t cbs = { .on_alarm = on_alarm };
    ESP_ERROR_CHECK(gptimer_register_event_callbacks(s_timer, &cbs, NULL));
    ESP_ERROR_CHECK(gptimer_enable(s_timer));

    esp_timer_create_args_t save_args = { .callback = save_cb, .name = "save" };
    ESP_ERROR_CHECK(esp_timer_create(&save_args, &s_save_timer));

    // Start from the saved settings
    settings_t boot = s_set;
    s_set.run = 0;
    apply(&boot);

    ble_link_init();

    // Blocking line input over USB-Serial/JTAG
    usb_serial_jtag_driver_config_t ucfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&ucfg));
    usb_serial_jtag_vfs_use_driver();
    setvbuf(stdin, NULL, _IONBF, 0);
    setvbuf(stdout, NULL, _IONBF, 0);

    char line[STROBE_LINE_MAX], reply[STROBE_REPLY_MAX];
    size_t n = 0;
    for (;;) {
        int ch = fgetc(stdin);
        if (ch == EOF) {
            vTaskDelay(1);
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            if (n == 0) continue;
            line[n] = '\0';
            n = 0;
            strobe_command(line, true, reply, sizeof(reply));
            fputs(reply, stdout);
        } else if (n < sizeof(line) - 1) {
            line[n++] = (char)ch;
        }
    }
}
