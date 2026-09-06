#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "esp_netif.h"
#include "esp_websocket_client.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "esp_cpu.h"
#include "esp_rom_sys.h"
#include "driver/rmt_tx.h"

static const char *TAG = "AUDIO_STREAMER";

#define RMT_LED_RESOLUTION_HZ 10000000 // 10MHz, 1 tick = 0.1us

static const rmt_symbol_word_t ws2812_zero = {
    .level0 = 1,
    .duration0 = 3, // T0H = 0.3us
    .level1 = 0,
    .duration1 = 9, // T0L = 0.9us
};

static const rmt_symbol_word_t ws2812_reset = {
    .level0 = 0,
    .duration0 = 3000, // 300us reset
    .level1 = 0,
    .duration1 = 3000,
};

static size_t ws2812_encoder_cb(const void *data, size_t data_size,
                                size_t symbols_written, size_t symbols_free,
                                rmt_symbol_word_t *symbols, bool *done, void *arg)
{
    if (symbols_free < 8) return 0;
    size_t data_pos = symbols_written / 8;
    if (data_pos < data_size) {
        for (int i = 0; i < 8; i++) {
            symbols[i] = ws2812_zero; // Send all zeros to turn off
        }
        return 8;
    } else {
        symbols[0] = ws2812_reset;
        *done = true;
        return 1;
    }
}

/* Official Espressif RMT hardware peripheral driver to shut off WS2812 RGB LED */
static void turn_off_board_led(int gpio)
{
    rmt_channel_handle_t led_chan = NULL;
    rmt_tx_channel_config_t tx_chan_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .gpio_num = gpio,
        .mem_block_symbols = 64,
        .resolution_hz = RMT_LED_RESOLUTION_HZ,
        .trans_queue_depth = 4,
    };
    if (rmt_new_tx_channel(&tx_chan_config, &led_chan) != ESP_OK) return;

    rmt_encoder_handle_t encoder = NULL;
    const rmt_simple_encoder_config_t enc_cfg = {
        .callback = ws2812_encoder_cb,
    };
    if (rmt_new_simple_encoder(&enc_cfg, &encoder) != ESP_OK) {
        rmt_del_channel(led_chan);
        return;
    }

    rmt_enable(led_chan);

    uint8_t zero_pixels[3] = {0, 0, 0}; // G=0, R=0, B=0
    rmt_transmit_config_t tx_config = {.loop_count = 0};
    rmt_transmit(led_chan, encoder, zero_pixels, sizeof(zero_pixels), &tx_config);
    rmt_tx_wait_all_done(led_chan, portMAX_DELAY);

    rmt_disable(led_chan);
    rmt_del_encoder(encoder);
    rmt_del_channel(led_chan);

    gpio_reset_pin((gpio_num_t)gpio);
    gpio_set_direction((gpio_num_t)gpio, GPIO_MODE_OUTPUT);
    gpio_set_level((gpio_num_t)gpio, 0);

    ESP_LOGI(TAG, "Turned off onboard WS2812 LED on GPIO %d via official RMT driver", gpio);
}

static void turn_off_all_leds(void)
{
    const int rgb_pins[] = {48, 38, 21, 8};
    for (size_t i = 0; i < sizeof(rgb_pins)/sizeof(rgb_pins[0]); i++) {
        turn_off_board_led(rgb_pins[i]);
    }
}

// Auto-configured parameters from config.env (if present) or Kconfig sdkconfig
#if __has_include("app_config_auto.h")
#include "app_config_auto.h"
#endif

#ifdef APP_CONFIG_WIFI_SSID
#define TARGET_WIFI_SSID APP_CONFIG_WIFI_SSID
#elif defined(CONFIG_ESP_WIFI_SSID)
#define TARGET_WIFI_SSID CONFIG_ESP_WIFI_SSID
#else
#define TARGET_WIFI_SSID "myssid"
#endif

#ifdef APP_CONFIG_WIFI_PASSWORD
#define TARGET_WIFI_PASS APP_CONFIG_WIFI_PASSWORD
#elif defined(CONFIG_ESP_WIFI_PASSWORD)
#define TARGET_WIFI_PASS CONFIG_ESP_WIFI_PASSWORD
#else
#define TARGET_WIFI_PASS "mypassword"
#endif

#ifdef APP_CONFIG_SERVER_HOST
#define TARGET_SERVER_HOST APP_CONFIG_SERVER_HOST
#elif defined(CONFIG_SERVER_HOST)
#define TARGET_SERVER_HOST CONFIG_SERVER_HOST
#else
#define TARGET_SERVER_HOST "192.168.1.100"
#endif

#ifdef APP_CONFIG_SERVER_PORT
#define TARGET_SERVER_PORT APP_CONFIG_SERVER_PORT
#elif defined(CONFIG_SERVER_PORT)
#define TARGET_SERVER_PORT CONFIG_SERVER_PORT
#else
#define TARGET_SERVER_PORT 8000
#endif

#ifdef APP_CONFIG_I2S_MIC_WS_GPIO
#define TARGET_I2S_MIC_WS_GPIO APP_CONFIG_I2S_MIC_WS_GPIO
#elif defined(CONFIG_I2S_MIC_WS_GPIO)
#define TARGET_I2S_MIC_WS_GPIO CONFIG_I2S_MIC_WS_GPIO
#else
#define TARGET_I2S_MIC_WS_GPIO 4
#endif

#ifdef APP_CONFIG_I2S_MIC_SCK_GPIO
#define TARGET_I2S_MIC_SCK_GPIO APP_CONFIG_I2S_MIC_SCK_GPIO
#elif defined(CONFIG_I2S_MIC_SCK_GPIO)
#define TARGET_I2S_MIC_SCK_GPIO CONFIG_I2S_MIC_SCK_GPIO
#else
#define TARGET_I2S_MIC_SCK_GPIO 5
#endif

#ifdef APP_CONFIG_I2S_MIC_SD_GPIO
#define TARGET_I2S_MIC_SD_GPIO APP_CONFIG_I2S_MIC_SD_GPIO
#elif defined(CONFIG_I2S_MIC_SD_GPIO)
#define TARGET_I2S_MIC_SD_GPIO CONFIG_I2S_MIC_SD_GPIO
#else
#define TARGET_I2S_MIC_SD_GPIO 6
#endif

#ifdef APP_CONFIG_AUDIO_GAIN_BOOST
#define TARGET_AUDIO_GAIN_BOOST APP_CONFIG_AUDIO_GAIN_BOOST
#elif defined(CONFIG_AUDIO_GAIN_BOOST)
#define TARGET_AUDIO_GAIN_BOOST CONFIG_AUDIO_GAIN_BOOST
#else
#define TARGET_AUDIO_GAIN_BOOST 2
#endif

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
static EventGroupHandle_t s_wifi_event_group;
static int s_retry_num = 0;
#define MAXIMUM_RETRY      10

static esp_websocket_client_handle_t s_ws_client = NULL;
static volatile bool s_ws_connected = false;
static i2s_chan_handle_t s_rx_chan = NULL;

// 1024 audio samples per chunk = 64ms at 16kHz (reduces network framing overhead)
#define FRAMES_PER_CHUNK 1024

/* WiFi Event Handler */
static void wifi_event_handler(void* arg, esp_event_base_t event_base,
                               int32_t event_id, void* event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        s_ws_connected = false;
        if (s_retry_num < MAXIMUM_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGI(TAG, "Reconnecting to WiFi (attempt %d)...", s_retry_num);
        } else {
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t* event = (ip_event_got_ip_t*) event_data;
        ESP_LOGI(TAG, "WiFi Connected! IP Address: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT,
                                                        ESP_EVENT_ANY_ID,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        &instance_any_id));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT,
                                                        IP_EVENT_STA_GOT_IP,
                                                        &wifi_event_handler,
                                                        NULL,
                                                        &instance_got_ip));

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = TARGET_WIFI_SSID,
            .password = TARGET_WIFI_PASS,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    ESP_LOGI(TAG, "Connecting to WiFi SSID: %s ...", TARGET_WIFI_SSID);
    EventBits_t bits = xEventGroupWaitBits(s_wifi_event_group,
            WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
            pdFALSE,
            pdFALSE,
            portMAX_DELAY);

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Successfully connected to AP: %s", TARGET_WIFI_SSID);
    } else {
        ESP_LOGE(TAG, "Failed to connect to AP: %s", TARGET_WIFI_SSID);
    }
}

/* WebSocket Event Handler */
static void websocket_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    switch (event_id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "WebSocket connected to Mac server!");
        s_ws_connected = true;
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "WebSocket disconnected from server");
        s_ws_connected = false;
        break;
    case WEBSOCKET_EVENT_CLOSED:
        ESP_LOGW(TAG, "WebSocket connection closed by server (clean close)");
        s_ws_connected = false;
        break;
    case WEBSOCKET_EVENT_ERROR:
        ESP_LOGE(TAG, "WebSocket error occurred");
        s_ws_connected = false;
        break;
    default:
        break;
    }
}

static void websocket_app_start(void)
{
    char ws_uri[128];
    snprintf(ws_uri, sizeof(ws_uri), "ws://%s:%d/ws/audio", TARGET_SERVER_HOST, TARGET_SERVER_PORT);
    ESP_LOGI(TAG, "Initializing WebSocket connection to: %s", ws_uri);

    esp_websocket_client_config_t ws_cfg = {
        .uri = ws_uri,
        .buffer_size = 16384,
        .reconnect_timeout_ms = 1000,          // Retry every 1000ms on disconnect
        .network_timeout_ms = 5000,
        .ping_interval_sec = 3,               // Active heartbeat Ping every 3s
        .pingpong_timeout_sec = 4,            // Detect dead server within 4s
        .disable_auto_reconnect = false,      // Auto-reconnect enabled
        .enable_close_reconnect = true,       // CRUCIAL: Automatically reconnect after server restart/close!
        .keep_alive_enable = true,            // TCP layer keepalive
        .keep_alive_idle = 3,
        .keep_alive_interval = 2,
        .keep_alive_count = 3,
    };

    s_ws_client = esp_websocket_client_init(&ws_cfg);
    esp_websocket_register_events(s_ws_client, WEBSOCKET_EVENT_ANY, websocket_event_handler, (void *)s_ws_client);
    esp_websocket_client_start(s_ws_client);
}

/* I2S INMP441 Microphone Driver Init */
static void i2s_mic_init(void)
{
    ESP_LOGI(TAG, "Initializing I2S RX channel for INMP441 (WS:%d, SCK:%d, SD:%d)...",
             TARGET_I2S_MIC_WS_GPIO, TARGET_I2S_MIC_SCK_GPIO, TARGET_I2S_MIC_SD_GPIO);

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 12;
    chan_cfg.dma_frame_num = 256;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &s_rx_chan));

    // INMP441 standard Mono Left-slot configuration (tied to GND)
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = TARGET_I2S_MIC_SCK_GPIO,
            .ws   = TARGET_I2S_MIC_WS_GPIO,
            .dout = I2S_GPIO_UNUSED,
            .din  = TARGET_I2S_MIC_SD_GPIO,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv   = false,
            },
        },
    };
    std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx_chan, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_rx_chan));
    ESP_LOGI(TAG, "I2S initialized successfully. Mono Left-slot mode enabled.");
}

/* Audio Recording and WebSocket Streaming Task */
static void audio_stream_task(void *pvParameters)
{
    int32_t *raw_samples = (int32_t *)malloc(FRAMES_PER_CHUNK * sizeof(int32_t));
    int16_t *pcm16_samples = (int16_t *)malloc(FRAMES_PER_CHUNK * sizeof(int16_t));
    assert(raw_samples != NULL && pcm16_samples != NULL);

    size_t bytes_read = 0;
    int gain = TARGET_AUDIO_GAIN_BOOST;
    if (gain <= 0) gain = 2;

    ESP_LOGI(TAG, "Audio streaming task running (Gain factor: %dx)...", gain);

    uint32_t loop_count = 0;

    while (1) {
        esp_err_t ret = i2s_channel_read(s_rx_chan, raw_samples, FRAMES_PER_CHUNK * sizeof(int32_t), &bytes_read, portMAX_DELAY);
        if (ret == ESP_OK && bytes_read > 0) {
            size_t frame_count = bytes_read / sizeof(int32_t);
            int16_t peak_val = 0;

            for (size_t i = 0; i < frame_count; i++) {
                // INMP441 24-bit MSB-aligned in 32-bit word -> shift down 16 bits to 16-bit signed PCM
                int32_t sample = (raw_samples[i] >> 16) * gain;
                if (sample > 32767) sample = 32767;
                else if (sample < -32768) sample = -32768;

                pcm16_samples[i] = (int16_t)sample;

                int abs_s = abs((int)pcm16_samples[i]);
                if (abs_s > peak_val) {
                    peak_val = abs_s;
                }
            }

            // Diagnostic log every ~3 seconds (100 loops of 32ms)
            if (++loop_count % 100 == 0) {
                ESP_LOGI(TAG, "[MIC Monitor] Sample 0:%ld | Peak: %d / 32767 | WS: %s",
                         (long)(raw_samples[0] >> 16),
                         peak_val, s_ws_connected ? "CONNECTED" : "DISCONNECTED");
            }

            // Stream PCM over WebSocket if connected
            if (s_ws_client != NULL) {
                if (s_ws_connected && esp_websocket_client_is_connected(s_ws_client)) {
                    int sent = esp_websocket_client_send_bin(s_ws_client, (char *)pcm16_samples, frame_count * sizeof(int16_t), 50 / portTICK_PERIOD_MS);
                    if (sent < 0) {
                        s_ws_connected = false;
                        ESP_LOGW(TAG, "WebSocket send failed (code %d). Connection lost! Marking DISCONNECTED.", sent);
                    }
                } else {
                    // Periodic reconnect check: if client stopped or socket broke, ensure client is running
                    if (loop_count % 50 == 0) {
                        if (!esp_websocket_client_is_connected(s_ws_client)) {
                            ESP_LOGI(TAG, "Attempting to reconnect WebSocket to Mac server...");
                            esp_websocket_client_start(s_ws_client);
                        }
                    }
                }
            }
        }
    }

    free(raw_samples);
    free(pcm16_samples);
    vTaskDelete(NULL);
}

void app_main(void)
{
    ESP_LOGI(TAG, "=== Starting ESP32-S3 Audio Streamer ===");

    // Turn off onboard RGB LED on common ESP32-S3 pins (48, 38, 21, 8) using official RMT driver
    turn_off_all_leds();

    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Initialize I2S hardware
    i2s_mic_init();

    // Connect to WiFi
    wifi_init_sta();

    // Start WebSocket Client
    websocket_app_start();

    // Spawn Audio Stream Task pinned to Core 1
    xTaskCreatePinnedToCore(audio_stream_task, "audio_stream_task", 4096, NULL, 5, NULL, 1);
}
