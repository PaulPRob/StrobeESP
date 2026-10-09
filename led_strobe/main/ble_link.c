/*
 * Bluetooth LE link for the LED strobe.
 *
 * Exposes the Nordic UART Service (NUS), a de-facto standard "serial over BLE" layout:
 *   RX 6E400002-...  client writes command lines (same text protocol as USB)
 *   TX 6E400003-...  board sends replies as notifications (split to fit the ATT MTU)
 *
 * Security: writing commands requires an encrypted, authenticated (MITM-protected) link.
 * The board has no display, so it uses passkey pairing with a fixed 6-digit PIN that the
 * user types on the computer. The PIN is random on first boot, stored in NVS, and can only
 * be read or changed over USB (PIN command). Bonds are kept in NVS so paired computers
 * reconnect without the PIN. One connection at a time.
 */
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "nvs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "strobe.h"

void ble_store_config_init(void);

static const char *TAG = "ble_link";

// 6E400001-B5A3-F393-E0A9-E50E24DCCA9E and friends (stored little-endian)
#define NUS_UUID(x) BLE_UUID128_INIT(0x9e, 0xca, 0xdc, 0x24, 0x0e, 0xe5, 0xa9, 0xe0, \
                                     0x93, 0xf3, 0xa3, 0xb5, x, 0x00, 0x40, 0x6e)
static const ble_uuid128_t s_nus_svc = NUS_UUID(0x01);
static const ble_uuid128_t s_nus_rx = NUS_UUID(0x02);
static const ble_uuid128_t s_nus_tx = NUS_UUID(0x03);

static uint16_t s_tx_handle;
static uint16_t s_conn = BLE_HS_CONN_HANDLE_NONE;
static uint8_t s_own_addr_type;
static uint32_t s_pin;
static char s_name[20];
static char s_rx_line[STROBE_LINE_MAX];
static size_t s_rx_len;

static void advertise(void);

static void send_reply(uint16_t conn, const char *reply)
{
    size_t len = strlen(reply);
    size_t chunk = ble_att_mtu(conn) - 3;
    for (size_t off = 0; off < len; off += chunk) {
        size_t n = len - off < chunk ? len - off : chunk;
        struct os_mbuf *om = ble_hs_mbuf_from_flat(reply + off, n);
        if (!om || ble_gatts_notify_custom(conn, s_tx_handle, om) != 0) break;
    }
}

static int rx_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    char buf[256];      // >= ATT MTU (247) - 3
    uint16_t len;
    if (ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof(buf), &len) != 0) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;

    // Accumulate into lines; a write may hold part of a line or several lines
    for (uint16_t i = 0; i < len; i++) {
        char c = buf[i];
        if (c == '\n' || c == '\r') {
            if (s_rx_len == 0) continue;
            s_rx_line[s_rx_len] = '\0';
            s_rx_len = 0;
            char reply[STROBE_REPLY_MAX];
            strobe_command(s_rx_line, false, reply, sizeof(reply));
            send_reply(conn, reply);
        } else if (s_rx_len < sizeof(s_rx_line) - 1) {
            s_rx_line[s_rx_len++] = c;
        }
    }
    return 0;
}

static int tx_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    return 0;   // TX is notify-only in practice; reads return an empty value
}

static const struct ble_gatt_svc_def s_services[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_nus_svc.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                .uuid = &s_nus_rx.u,
                .access_cb = rx_access,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP |
                         BLE_GATT_CHR_F_WRITE_ENC | BLE_GATT_CHR_F_WRITE_AUTHEN,
            },
            {
                .uuid = &s_nus_tx.u,
                .access_cb = tx_access,
                .val_handle = &s_tx_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY | BLE_GATT_CHR_F_READ |
                         BLE_GATT_CHR_F_READ_ENC | BLE_GATT_CHR_F_READ_AUTHEN,
            },
            { 0 },
        },
    },
    { 0 },
};

static int gap_event(struct ble_gap_event *event, void *arg)
{
    struct ble_gap_conn_desc desc;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            s_conn = event->connect.conn_handle;
            s_rx_len = 0;
        } else {
            advertise();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        s_conn = BLE_HS_CONN_HANDLE_NONE;
        advertise();
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        advertise();
        return 0;

    case BLE_GAP_EVENT_PASSKEY_ACTION:
        if (event->passkey.params.action == BLE_SM_IOACT_DISP) {
            // "Display" our fixed PIN: the user types it on the computer
            struct ble_sm_io io = { .action = BLE_SM_IOACT_DISP, .passkey = s_pin };
            ble_sm_inject_io(event->passkey.conn_handle, &io);
        }
        return 0;

    case BLE_GAP_EVENT_REPEAT_PAIRING:
        // A computer that lost its keys is pairing again: drop the old bond and allow it
        // (it still needs the PIN).
        if (ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc) == 0) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        return BLE_GAP_REPEAT_PAIRING_RETRY;

    case BLE_GAP_EVENT_ENC_CHANGE:
        if (event->enc_change.status != 0) {
            ESP_LOGW(TAG, "pairing/encryption failed: %d", event->enc_change.status);
        }
        return 0;
    }
    return 0;
}

static void advertise(void)
{
    // Advertising packet: flags + NUS service UUID. Scan response: device name.
    struct ble_hs_adv_fields fields = { 0 };
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.uuids128 = &s_nus_svc;
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;
    int rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGE(TAG, "adv fields: %d", rc);
        return;
    }
    struct ble_hs_adv_fields rsp = { 0 };
    rsp.name = (uint8_t *)s_name;
    rsp.name_len = strlen(s_name);
    rsp.name_is_complete = 1;
    ble_gap_adv_rsp_set_fields(&rsp);

    struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_UND,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
    };
    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER, &params, gap_event, NULL);
    if (rc != 0 && rc != BLE_HS_EALREADY) {
        ESP_LOGE(TAG, "adv start: %d", rc);
    }
}

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &s_own_addr_type);
    advertise();
}

static void on_reset(int reason)
{
    ESP_LOGW(TAG, "BLE host reset: %d", reason);
}

static void host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static void load_pin(void)
{
    nvs_handle_t h;
    if (nvs_open("strobe", NVS_READWRITE, &h) != ESP_OK) {
        s_pin = esp_random() % 1000000;
        return;
    }
    if (nvs_get_u32(h, "pin", &s_pin) != ESP_OK || s_pin > 999999) {
        s_pin = 100000 + esp_random() % 900000;     // first boot: random 6-digit PIN
        nvs_set_u32(h, "pin", s_pin);
        nvs_commit(h);
    }
    nvs_close(h);
}

uint32_t ble_link_get_pin(void)
{
    return s_pin;
}

bool ble_link_set_pin(uint32_t pin)
{
    if (pin > 999999) return false;
    nvs_handle_t h;
    if (nvs_open("strobe", NVS_READWRITE, &h) != ESP_OK) return false;
    bool ok = nvs_set_u32(h, "pin", pin) == ESP_OK && nvs_commit(h) == ESP_OK;
    nvs_close(h);
    if (!ok) return false;
    s_pin = pin;
    ble_link_unpair_all();      // existing pairings were made with the old PIN
    return true;
}

void ble_link_unpair_all(void)
{
    if (s_conn != BLE_HS_CONN_HANDLE_NONE) {
        ble_gap_terminate(s_conn, BLE_ERR_REM_USER_CONN_TERM);
    }
    ble_store_clear();
}

void ble_link_status(char *out, size_t outlen)
{
    const char *state = "advertising";
    struct ble_gap_conn_desc desc;
    uint16_t conn = s_conn;
    if (conn != BLE_HS_CONN_HANDLE_NONE && ble_gap_conn_find(conn, &desc) == 0) {
        state = desc.sec_state.authenticated ? "connected-paired" :
                desc.sec_state.encrypted ? "connected-encrypted" : "connected";
    }
    int bonds = 0;
    ble_store_util_count(BLE_STORE_OBJ_TYPE_OUR_SEC, &bonds);
    snprintf(out, outlen, "BLE NAME=%s STATE=%s BONDS=%d\n", s_name, state, bonds);
}

void ble_link_init(void)
{
    load_pin();

    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_BT);
    snprintf(s_name, sizeof(s_name), "LED-Strobe-%02X%02X", mac[4], mac[5]);

    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.reset_cb = on_reset;
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_DISP_ONLY;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 1;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;

    ble_svc_gap_init();
    ble_svc_gatt_init();
    ESP_ERROR_CHECK(ble_gatts_count_cfg(s_services));
    ESP_ERROR_CHECK(ble_gatts_add_svcs(s_services));
    ble_svc_gap_device_name_set(s_name);
    ble_store_config_init();

    nimble_port_freertos_init(host_task);
}
