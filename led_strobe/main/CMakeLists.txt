idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_gptimer esp_driver_usb_serial_jtag nvs_flash esp_timer)
