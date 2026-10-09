// Shared between the strobe core (main.c) and the Bluetooth link (ble_link.c)
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define STROBE_LINE_MAX  96     // longest accepted command line
#define STROBE_REPLY_MAX 192    // longest reply, including the trailing newline

// Run one command line and write the newline-terminated reply into out.
// Thread-safe: called from the USB task and the NimBLE host task.
// Commands that manage Bluetooth security are refused unless from_usb is true.
void strobe_command(const char *line, bool from_usb, char *out, size_t outlen);

// Bluetooth LE link (Nordic UART Service carrying the same text protocol)
void ble_link_init(void);
uint32_t ble_link_get_pin(void);
bool ble_link_set_pin(uint32_t pin);     // saves the PIN and removes all existing bonds
void ble_link_unpair_all(void);          // removes bonds and drops any current connection
void ble_link_status(char *out, size_t outlen);
